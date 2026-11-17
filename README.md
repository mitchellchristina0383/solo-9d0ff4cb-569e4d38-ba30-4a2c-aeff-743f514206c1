# Dependency Alert API

值班工程师面对一串服务故障告警时，需要先处理真正的**上游根因故障**，同时保留尚未恢复的
下游告警但予以抑制。本服务用 **FastAPI + SQLite（WAL）** 实现：

- 整版提交服务依赖图（`A` 依赖 `B`，则 `B` 是 `A` 的上游）；
- 带**每服务递增正整数序号**上报故障 / 恢复，支持幂等重试、冲突拒绝、迟到不回退；
- 查询时动态计算**需处理故障**与**被抑制故障**及其上游根因；
- 替换依赖图时按服务名保留状态，恢复后下游自动重新归类，无需重报；
- **入口可用性计时**：选定入口后按“可用 / 中断 / 未知”累计时长，仅在已提交状态真正改变
  结论时切分段，返回三类时长与中断时段；
- 所有写操作走序列化事务，并发上报 / 换版 / 查询不会混合不同已提交状态。

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# API:   http://localhost:8000
# 文档:  http://localhost:8000/docs   （Swagger UI，可直接操作）
# 健康:  http://localhost:8000/health
```

SQLite 数据文件保存在命名卷 `alert-data`（容器内 `/data/alerts.db`）。

### 本地直接运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ALERT_DB_PATH=./data/alerts.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/graph` | 整版替换依赖图。校验失败（环路 / 未知引用 / 重名 / 重边 / 自依赖）返回 `422` 并**保留旧版** |
| `GET` | `/graph` | 读取当前已提交图（含内部修订号 `revision`） |
| `POST` | `/reports` | 上报故障 / 恢复 |
| `GET` | `/alerts` | 查询告警分类（需处理 / 被抑制 / 根因 / 路径） |
| `POST` | `/availability` | 选定当前图中的一个入口开始可用性计时 |
| `GET` | `/availability` | 查询已监测区间的可用 / 中断 / 未知时长与中断时段 |
| `DELETE` | `/availability` | 结束计时，冻结并返回整段核算结果 |
| `POST` | `/admin/reset` | 清空全部数据（测试 / 运维辅助） |
| `GET` | `/health` | 健康检查 |

### 提交依赖图

```json
POST /graph
{
  "version": "v1",
  "services": [
    {"name": "A", "dependencies": ["B", "C"]},
    {"name": "B", "dependencies": ["D"]},
    {"name": "C", "dependencies": ["D"]},
    {"name": "D", "dependencies": []}
  ]
}
```

`dependencies` 中列出的是**上游**服务；所有被引用服务必须在同一次提交中出现，
整图必须是 DAG（Kahn 算法检环）。每次成功提交 `revision` 递增。

### 上报故障 / 恢复

```json
POST /reports
{"service": "B", "seq": 7, "status": "fault"}
```

序号 `seq` 为**每个服务各自维护的严格递增正整数**，规则：

| 情况 | 结果 |
| --- | --- |
| `seq > last_seq` | 应用新状态 |
| `seq == last_seq` 且内容相同 | 幂等重试，`applied=false, duplicate=true`，状态不变 |
| `seq == last_seq` 但内容不同（fault/recover 互斥） | `409 conflict`，状态不变 |
| `seq < last_seq`（迟到上报） | `409 conflict`，**绝不回退状态** |
| 服务不在当前图中 | `404 unknown_service` |
| `seq <= 0` | `422`（请求体校验） |

序号允许有空洞（如 1 之后直接 3）。每次上报（含被拒绝的）都写入 `events` 审计表。

### 查询告警

```json
GET /alerts
{
  "graph_revision": 1,
  "graph_version": "v1",
  "services": ["A", "B", "C", "D"],
  "actionable_faults": [{"service": "D", "last_seq": 1}],
  "suppressed_faults": [
    {
      "service": "B",
      "last_seq": 1,
      "root_causes": ["D"],
      "paths": [["B", "D"]]
    }
  ],
  "down_services": ["B", "D"],
  "unreported_services": ["C"],
  "healthy_services": []
}
```

## 抑制规则（核心语义）

1. 只有当前仍处于故障（down）状态的服务才会出现在 `actionable_faults` 或
   `suppressed_faults` 中，两类互斥且并集恰为全部 down 服务。
2. **根因**：故障且没有任何故障上游的服务。根因永远是需处理故障。
3. 一个非根因故障被抑制，当且仅当存在一条到达某根因的依赖路径，且**路径上每个节点都
   处于故障中**。
4. 健康节点或**从未上报**的节点会截断路径：其下游即使故障也不能经由该路径被抑制；
   若所有路径都被截断，该故障仍归入需处理。
5. 分类是查询时根据当前图 + 当前状态**动态计算**的。服务恢复、依赖图换版后立即重新
   归类，不需要下游重新上报。

## 换版与状态保留

- 服务状态（up/down、最后序号）独立于图存储，按服务名为主键。换版后同名服务的状态与
  序号历史原样保留；被移除服务的状态行不删除但不会出现在查询中，将来重新加入时恢复可见。
- 新图中首次出现的服务归入 `unreported_services`。

## 并发模型

- SQLite 开启 WAL；每个写事务以 `BEGIN IMMEDIATE` 开始，写者全局串行化，遇
  `SQLITE_BUSY` 短时重试。
- 读事务持有单一已提交快照，因此换版与查询并发时，每次 `/alerts` 看到的节点集、边与
  修订号必然属于同一个已提交版本，不会出现混合状态。
- 每个请求使用独立的线程级连接，请求结束即关闭。

## 入口可用性计时（中断预算核算）

可靠性工程师需要核算**关键入口**在值班期间实际中断了多久。计时以“入口”为单位：一次只
监测一个入口，选定后开始累计，直到 `DELETE /availability` 结束（或 `POST /admin/reset`
清空数据）。选定前不存在任何计时；计时段落与起止时刻均持久化在 SQLite 中，进程重启后
继续累计。

### 开始与结束

```json
POST /availability
{"entry": "A"}
```

```json
{
  "entry": "A",
  "started_at": "2026-11-17T06:35:43.091Z",
  "start_revision": 1,
  "current_state": "unknown",
  "already_monitoring": false
}
```

- 入口必须是**当前已提交图**中的节点，否则 `404 unknown_entry`；尚未提交任何图时
  `409 no_graph`。
- 以相同入口重复调用是幂等重试：不新建时段、`already_monitoring=true`。
- 已在计时时换另一个入口 → `409 already_monitoring`。

`DELETE /availability` 结束计时：把最后一个时段闭合在结束时刻、返回整段核算结果，此后
上报不再延长该段；重复结束返回 `409 monitor_closed`。同一份数据只能有一个核算周期，
重置数据后可重新开始。

### 三种结论

入口在任一时刻恰处于以下一种状态：

| 结论 | 条件 |
| --- | --- |
| `interrupted`（中断） | 入口本身，或**任一递归上游**，已提交上报为故障 |
| `available`（可用） | 入口与**全部递归上游**都已提交上报为健康 |
| `unknown`（未知） | 其余情况：入口不在当前图中，或至少一个相关服务尚无上报 |

要点：

- 上游取**传递闭包**，不只是直接上游。
- 任一相关服务故障即记中断，即使别的相关服务尚未上报（故障证据优先，避免漏计中断）。
- “可用”必须所有相关服务都有健康证据；缺报记未知，不视为可用。
- 与入口无关的服务的状态变化不会改变结论。
- 换图会改变入口的上游闭包（新增 / 移除服务或边），因此换图后立即重算结论。

### 查询

```json
GET /availability            # 默认截止到本次查询时刻
GET /availability?until=2026-11-17T12:00:00Z
```

```json
{
  "entry": "A",
  "started_at": "2026-11-17T06:35:43.091Z",
  "until": "2026-11-17T06:37:36.749Z",
  "graph_revision": 5,
  "start_revision": 1,
  "available_ms": 94968,
  "interrupted_ms": 18423,
  "unknown_ms": 267,
  "total_ms": 113658,
  "available_seconds": 94.968,
  "interrupted_seconds": 18.423,
  "unknown_seconds": 0.267,
  "total_seconds": 113.658,
  "segments": [
    {"state": "unknown", "start": "…43.091Z", "end": "…43.308Z",
     "duration_ms": 217, "graph_revision": 1},
    {"state": "available", "start": "…43.308Z", "end": "…51.744Z",
     "duration_ms": 8436, "graph_revision": 1},
    {"state": "interrupted", "start": "…51.744Z", "end": "…10.167Z",
     "duration_ms": 18423, "graph_revision": 1}
  ],
  "interrupted_periods": [
    {"state": "interrupted", "start": "…51.744Z", "end": "…10.167Z",
     "duration_ms": 18423, "graph_revision": 1}
  ]
}
```

- 区间为 `[started_at, until)`；**三段时长之和恒等于 `total_ms`**，完整覆盖整个区间。
- 未结束的时段按查询截止时刻计算（`until` 即实际生效的截止时刻，已钳制到查询时刻）。
- `until` 早于开始时刻或格式非法 → `422 invalid_until`。
- `segments` 是按时间连续、互不重叠的完整结论序列；`interrupted_periods` 是其中
  `interrupted` 的子集，即**中断时段**。
- 时间戳一律 UTC、毫秒精度、`…Z` 结尾；时长为整数毫秒，另附秒数便于阅读。

### 何时切分时段

只有当**已提交**的上报或换版真正改变结论时才新增时段：

| 操作 | 是否切分 |
| --- | --- |
| 新的上报（`seq > last_seq`）且改变结论 | 切分 |
| 新的上报但不改变结论（如已在中断中再报故障） | **不切分** |
| 幂等重试（`seq == last_seq` 且内容相同） | **不切分** |
| 冲突 / 迟到被拒（`409`） | **不切分** |
| 换版但结论不变（如仅改版本名、拓扑不影响入口） | **不切分** |
| 换版且结论改变 | 切分，新时段记录新 `graph_revision` |

相邻时段状态必然不同，不会出现同状态连续两段。

### 一致性与写入原子性

- 时段的写入与上报 / 换版**在同一个 `BEGIN IMMEDIATE` 事务中**完成。若计时写入失败，
  整个事务回滚：不会留下已生效的上报，也不会留下新图（实测注入触发器使计时段插入
  失败时，上报返回 500 且 `service_state`/`events` 无变化；换图同样保持旧版本）。
- 计时段落存于 SQLite（`availability_monitor`、`availability_segment`），重启后读取同一
  数据即可继续核算。
- 查询在单个读事务中完成，监测入口、时段序列与图修订号来自**同一个已提交快照**；
  并发上报 / 换版时每次查询都不会混合两个状态。

## 自测

`tests/selftest.py` 仅依赖标准库，对运行中的服务做端到端校验：

```bash
python tests/selftest.py                      # 默认 http://127.0.0.1:8000
BASE_URL=http://localhost:8000 python tests/selftest.py
# 容器内：docker compose exec alert-api python tests/selftest.py
```

覆盖：图校验与原子替换、序号幂等 / 冲突 / 迟到语义、全故障路径抑制与健康 / 未上报截断、
恢复即时重分类、换版状态保留，入口可用性计时（三种结论、切分规则、三类时长求和覆盖、
中断时段、`until` 截止、结束冻结、并发快照），以及同序号并发重试、同序号异内容竞争、
多序号乱序竞争、并发换版、换版与查询交错等关键并发场景。

两个不依赖运行中服务的测试：

```bash
# 纯逻辑单元测试：时间戳、结论推导、时段切分与时长求和、时钟回拨钳制（18 项）
PYTHONPATH=.:.pylibs python tests/test_availability_unit.py

# 重启持久化：子进程起服务、建时段、杀掉、同一 DB 重启后核对并继续累计（14 项）
PYTHONPATH=.:.pylibs python tests/test_restart.py
```

## 目录结构

```
app/
  main.py            FastAPI 路由与异常映射
  database.py        SQLite 连接、schema、迁移、事务管理
  graph_service.py   图校验、整版替换、告警分类
  report_service.py  上报序号语义
  availability.py    入口可用性计时：结论推导、时段切分、区间核算
  schemas.py         Pydantic 模型
  errors.py          领域错误
tests/selftest.py                端到端自测（标准库）
tests/test_availability_unit.py  计时逻辑单元测试（离线）
tests/test_restart.py            重启持久化测试（自动起停子进程）
Dockerfile, docker-compose.yml, requirements.txt
```
