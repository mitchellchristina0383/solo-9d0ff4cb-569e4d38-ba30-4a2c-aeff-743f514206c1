"""Entry availability accounting.

A monitor watches one *entry* service in the currently committed dependency
graph.  At any instant the entry is in exactly one of three states:

``available``
    The entry and **every** transitive upstream service have a committed
    health report of ``up``.
``interrupted``
    The entry itself, or any transitive upstream service, has a committed
    health report of ``down``.
``unknown``
    Anything else -- the entry is not in the current graph, or at least one
    relevant service has no report (or the graph changed such that a relevant
    service is missing).

The conclusion is persisted as a contiguous sequence of segments.  A segment
is started when monitoring begins, and is split **only** when a committed
report or a committed graph replacement actually changes the conclusion.
Idempotent retries, rejected reports and changes that leave the conclusion
unchanged therefore never create a segment.

All segment mutations happen inside the caller's ``BEGIN IMMEDIATE``
transaction (reports, graph replacement, monitor start).  A failure while
writing the timeline rolls the whole transaction back, so a report or a new
graph can never be committed without its corresponding timing update.
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .database import read_tx, write_tx
from .errors import (
    AlreadyMonitoring,
    InvalidUntil,
    MonitorClosed,
    NoGraph,
    NotMonitoring,
    UnknownEntry,
)

# ---------------------------------------------------------------------------
# Time helpers.  Timestamps are stored with millisecond precision and are
# compared as integer epoch milliseconds, so segment durations add up exactly.
# ---------------------------------------------------------------------------

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def parse_timestamp(value: str) -> int:
    """Parse an ISO-8601 timestamp into UTC epoch milliseconds."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidUntil(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return calendar.timegm(parsed.utctimetuple()) * 1000 + parsed.microsecond // 1000


def format_timestamp(ms: int) -> str:
    seconds, millis = divmod(ms, 1000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return f"{dt.strftime(_TS_FORMAT)}.{millis:03d}Z"


# ---------------------------------------------------------------------------
# Graph / state helpers (always called with an open transaction)
# ---------------------------------------------------------------------------


def _current_revision(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute("SELECT revision FROM graph_meta WHERE id = 1").fetchone()
    return row["revision"] if row else None


def _upstream_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    upstream: dict[str, list[str]] = {
        r["service"]: [] for r in conn.execute("SELECT service FROM nodes")
    }
    for row in conn.execute(
        "SELECT upstream, downstream FROM edges ORDER BY upstream, downstream"
    ):
        upstream.setdefault(row["downstream"], []).append(row["upstream"])
    return upstream


def _upstream_closure(entry: str, upstream: dict[str, list[str]]) -> set[str]:
    """All transitive upstream services of ``entry`` (the graph is a DAG)."""
    seen: set[str] = set()
    stack = list(upstream.get(entry, []))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(upstream.get(node, []))
    return seen


def conclusion(
    conn: sqlite3.Connection, entry: str, upstream: Optional[dict[str, list[str]]] = None
) -> str:
    """Return the current conclusion for ``entry``: available/interrupted/unknown."""
    if upstream is None:
        upstream = _upstream_map(conn)
    if entry not in upstream:
        return "unknown"

    relevant = {entry} | _upstream_closure(entry, upstream)
    statuses = {
        r["service"]: r["status"]
        for r in conn.execute(
            "SELECT service, status FROM service_state WHERE service IN (%s)"
            % ",".join("?" * len(relevant)),
            sorted(relevant),
        )
    }

    # A fault on the entry or anywhere upstream interrupts it immediately --
    # even if other relevant services have not reported yet.
    if any(statuses.get(svc) == "down" for svc in relevant):
        return "interrupted"
    # Availability requires positive health evidence for the whole set.
    if all(statuses.get(svc) == "up" for svc in relevant):
        return "available"
    return "unknown"


# ---------------------------------------------------------------------------
# Timeline maintenance
# ---------------------------------------------------------------------------


def _current_monitor(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """The monitor row, whether active or stopped (at most one)."""
    return conn.execute(
        "SELECT id, entry, start_revision, started_at, stopped_at "
        "FROM availability_monitor WHERE id = 1"
    ).fetchone()


def _active_monitor(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """The monitor row if it is still running."""
    monitor = _current_monitor(conn)
    return monitor if monitor is not None and monitor["stopped_at"] is None else None


def _open_segment(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, start_at, state FROM availability_segment "
        "WHERE end_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


def record_conclusion(
    conn: sqlite3.Connection, now_ms: Optional[int] = None
) -> Optional[str]:
    """Recompute the conclusion and split the timeline if it changed.

    Must be called from inside the same write transaction that committed the
    triggering report / graph replacement.  Raises nothing on "no change":
    consecutive segments always have distinct states.
    """
    monitor = _active_monitor(conn)
    if monitor is None:
        return None

    revision = _current_revision(conn)
    state = conclusion(conn, monitor["entry"])

    now = _now_ms() if now_ms is None else now_ms
    open_segment = _open_segment(conn)

    if open_segment is None:
        conn.execute(
            "INSERT INTO availability_segment "
            "(start_at, end_at, state, graph_revision) VALUES (?, NULL, ?, ?)",
            (format_timestamp(now), state, revision),
        )
        return state

    if open_segment["state"] == state:
        # No conclusion change: do not add a segment.
        return state

    # Split exactly at the committed change.  Guard against a clock that is
    # not perfectly monotonic: a segment may never end before it starts.
    split_ms = max(now, parse_timestamp(open_segment["start_at"]))
    split_at = format_timestamp(split_ms)
    conn.execute(
        "UPDATE availability_segment SET end_at = ? WHERE id = ?",
        (split_at, open_segment["id"]),
    )
    conn.execute(
        "INSERT INTO availability_segment "
        "(start_at, end_at, state, graph_revision) VALUES (?, NULL, ?, ?)",
        (split_at, state, revision),
    )
    return state


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_monitor(entry: str) -> dict:
    """Start monitoring ``entry`` (must be a node of the committed graph).

    Starting with the same entry while a monitor is already active is an
    idempotent retry: the existing timeline is returned unchanged and no new
    segment is created.  A different entry while monitoring is a conflict.
    """
    with write_tx() as conn:
        if _current_revision(conn) is None:
            raise NoGraph("no dependency graph has been committed yet")

        exists = conn.execute(
            "SELECT 1 FROM nodes WHERE service = ?", (entry,)
        ).fetchone()
        if exists is None:
            raise UnknownEntry(
                f"entry {entry!r} is not part of the current graph"
            )

        existing = _current_monitor(conn)
        if existing is not None and existing["stopped_at"] is None:
            if existing["entry"] != entry:
                raise AlreadyMonitoring(
                    f"already monitoring entry {existing['entry']!r}; "
                    f"stop it before monitoring {entry!r}"
                )
            open_segment = _open_segment(conn)
            return {
                "entry": existing["entry"],
                "started_at": existing["started_at"],
                "start_revision": existing["start_revision"],
                "current_state": open_segment["state"] if open_segment else None,
                "already_monitoring": True,
            }

        if existing is not None:
            raise MonitorClosed(
                f"entry {existing['entry']!r} was already monitored and stopped; "
                "a finished accounting period cannot be restarted"
            )

        now = _now_ms()
        started_at = format_timestamp(now)
        revision = _current_revision(conn)
        state = conclusion(conn, entry)

        conn.execute(
            "INSERT INTO availability_monitor "
            "(id, entry, start_revision, started_at, stopped_at) "
            "VALUES (1, ?, ?, ?, NULL)",
            (entry, revision, started_at),
        )
        conn.execute(
            "INSERT INTO availability_segment "
            "(start_at, end_at, state, graph_revision) VALUES (?, NULL, ?, ?)",
            (started_at, state, revision),
        )

    return {
        "entry": entry,
        "started_at": started_at,
        "start_revision": revision,
        "current_state": state,
        "already_monitoring": False,
    }


def stop_monitor() -> dict:
    """Stop monitoring, finalize the timeline and return the summary.

    The segments are retained, so the same report can be queried again with
    ``GET /availability`` after stopping; the summary is bounded by the stop
    instant and later reports can no longer extend it.
    """
    with write_tx() as conn:
        monitor = _active_monitor(conn)
        if monitor is None:
            if _current_monitor(conn) is not None:
                raise MonitorClosed("the availability monitor is already stopped")
            raise NotMonitoring("no availability monitor is running")

        now = max(_now_ms(), parse_timestamp(monitor["started_at"]))
        stopped_at = format_timestamp(now)
        open_segment = _open_segment(conn)
        if open_segment is not None:
            conn.execute(
                "UPDATE availability_segment SET end_at = ? WHERE id = ?",
                (stopped_at, open_segment["id"]),
            )
        conn.execute(
            "UPDATE availability_monitor SET stopped_at = ? WHERE id = 1",
            (stopped_at,),
        )
        return _summarize(conn, monitor, now)


def _blank_totals() -> dict[str, int]:
    return {"available": 0, "interrupted": 0, "unknown": 0}


def _summarize(
    conn: sqlite3.Connection,
    monitor: sqlite3.Row,
    until_ms: int,
) -> dict:
    """Build the availability report from the committed segment timeline.

    Segments are clipped to ``[monitor.started_at, until_ms]`` and any data
    gap (which a correct database never has) is defensively reported as
    ``unknown`` so the three durations always cover the whole interval.
    """
    started_ms = parse_timestamp(monitor["started_at"])
    # A stopped period ends exactly at the stop instant, regardless of the
    # query's own cutoff.
    end_ms = (
        min(parse_timestamp(monitor["stopped_at"]), until_ms)
        if monitor["stopped_at"] is not None
        else until_ms
    )
    if end_ms < started_ms:
        raise InvalidUntil(
            f"until {format_timestamp(end_ms)} is before monitor start "
            f"{monitor['started_at']}"
        )

    segments = list(
        conn.execute(
            "SELECT start_at, end_at, state, graph_revision "
            "FROM availability_segment ORDER BY start_at, id"
        )
    )

    totals = _blank_totals()
    periods: list[dict] = []
    cursor = started_ms

    def add(start_ms: int, end_ms: int, state: str, revision: Optional[int]) -> None:
        if end_ms <= start_ms:
            return
        duration = end_ms - start_ms
        totals[state] += duration
        item = {
            "state": state,
            "start": format_timestamp(start_ms),
            "end": format_timestamp(end_ms),
            "duration_ms": duration,
            "graph_revision": revision,
        }
        periods.append(item)

    for segment in segments:
        seg_start = max(parse_timestamp(segment["start_at"]), started_ms)
        seg_end = (
            parse_timestamp(segment["end_at"])
            if segment["end_at"] is not None
            else end_ms
        )
        seg_end = min(seg_end, end_ms)
        if seg_end <= cursor:
            continue
        # Defensive: never leave an uncovered hole.
        if seg_start > cursor:
            add(cursor, min(seg_start, end_ms), "unknown", segment["graph_revision"])
        add(max(seg_start, cursor), seg_end, segment["state"], segment["graph_revision"])
        cursor = seg_end

    if cursor < end_ms:
        add(cursor, end_ms, "unknown", _current_revision(conn))

    total_ms = end_ms - started_ms
    # If there were no segments at all, the whole interval is unknown.
    if sum(totals.values()) == 0 and total_ms > 0:
        totals["unknown"] = total_ms
        periods.append(
            {
                "state": "unknown",
                "start": format_timestamp(started_ms),
                "end": format_timestamp(end_ms),
                "duration_ms": total_ms,
                "graph_revision": _current_revision(conn),
            }
        )

    return {
        "entry": monitor["entry"],
        "started_at": monitor["started_at"],
        "until": format_timestamp(end_ms),
        "graph_revision": _current_revision(conn),
        "start_revision": monitor["start_revision"],
        "available_ms": totals["available"],
        "interrupted_ms": totals["interrupted"],
        "unknown_ms": totals["unknown"],
        "total_ms": total_ms,
        "available_seconds": totals["available"] / 1000,
        "interrupted_seconds": totals["interrupted"] / 1000,
        "unknown_seconds": totals["unknown"] / 1000,
        "total_seconds": total_ms / 1000,
        "segments": periods,
        "interrupted_periods": [p for p in periods if p["state"] == "interrupted"],
    }


def get_availability(until: Optional[str] = None) -> dict:
    """Return the availability timeline up to ``until`` (default: now).

    The cutoff is clamped to the query time: a monitor cannot have observed
    the future, so the returned three durations never include unobserved
    time.  A stopped monitor is capped at its stop instant.  The effective
    cutoff is echoed back as ``until``.

    The whole read happens inside one read transaction, so the monitor, the
    segment timeline and the graph revision all come from the same committed
    snapshot.
    """
    requested_ms: Optional[int] = parse_timestamp(until) if until else None

    with read_tx() as conn:
        monitor = _current_monitor(conn)
        if monitor is None:
            raise NotMonitoring("no availability monitor is running")
        started_ms = parse_timestamp(monitor["started_at"])
        if requested_ms is None:
            cutoff = _now_ms()
        elif requested_ms < started_ms:
            raise InvalidUntil(
                f"until {format_timestamp(requested_ms)} is before monitor start "
                f"{monitor['started_at']}"
            )
        else:
            cutoff = min(requested_ms, _now_ms())
        # ``_summarize`` additionally clamps to the stop instant when the
        # monitor has been stopped.
        return _summarize(conn, monitor, cutoff)
