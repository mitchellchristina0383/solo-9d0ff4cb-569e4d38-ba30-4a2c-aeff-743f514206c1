"""Restart-persistence check for entry availability accounting.

Runs the app under uvicorn in a subprocess against a temporary SQLite file,
builds an active availability timeline, kills the server, starts it again on
the same database and verifies the timeline is identical and still grows.

    PYTHONPATH=. python tests/test_restart.py

Exits non-zero on failure.  Standard library only (plus the app's own deps).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(db_path: str, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["ALERT_DB_PATH"] = db_path
    env["PYTHONPATH"] = os.path.join(ROOT, ".pylibs") + os.pathsep + ROOT
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            urllib.request.urlopen(base + "/health", timeout=1).read()
            return proc
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("server did not start")


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def call(base: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def totals(body: dict) -> tuple:
    return (body["available_ms"], body["interrupted_ms"], body["unknown_ms"],
            body["total_ms"])


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    workdir = tempfile.mkdtemp(prefix="alert-restart-")
    db_path = os.path.join(workdir, "alerts.db")

    print("[R1] timeline survives a server restart")
    server = start_server(db_path, port)
    try:
        call(base, "POST", "/graph", {"version": "v1", "services": [
            {"name": "A", "dependencies": ["B"]},
            {"name": "B", "dependencies": []},
        ]})
        status, body = call(base, "POST", "/availability", {"entry": "A"})
        check("monitor started", status == 201, str(body))
        call(base, "POST", "/reports", {"service": "A", "seq": 1, "status": "recover"})
        call(base, "POST", "/reports", {"service": "B", "seq": 1, "status": "recover"})
        time.sleep(0.05)
        call(base, "POST", "/reports", {"service": "B", "seq": 2, "status": "fault"})
        status, before = call(base, "GET", "/availability")
        check("pre-restart query ok", status == 200, str(before))
        check("pre-restart timeline has an interrupted period",
              len(before["interrupted_periods"]) == 1, str(before))
    finally:
        stop_server(server)

    server = start_server(db_path, port)
    try:
        status, after = call(base, "GET", "/availability")
        check("monitor still active after restart", status == 200, str(after))
        check("entry preserved", after["entry"] == "A", str(after))
        check("start instant preserved",
              after["started_at"] == before["started_at"], str(after))
        # The open (last) segment is bounded by the query cutoff, so its end
        # legitimately advances; every earlier segment must be byte-identical.
        check("closed segments preserved exactly",
              [(s["state"], s["start"], s["end"], s["duration_ms"])
               for s in after["segments"][:-1]]
              == [(s["state"], s["start"], s["end"], s["duration_ms"])
                  for s in before["segments"][:-1]],
              f"{before['segments']} vs {after['segments']}")
        check("open segment keeps state and start and extends to the new cutoff",
              after["segments"][-1]["state"] == before["segments"][-1]["state"]
              and after["segments"][-1]["start"] == before["segments"][-1]["start"]
              and after["segments"][-1]["end"] == after["until"],
              str(after))
        check("interrupted periods preserved, last one extended to the cutoff",
              [(p["state"], p["start"]) for p in after["interrupted_periods"]]
              == [(p["state"], p["start"]) for p in before["interrupted_periods"]]
              and after["interrupted_periods"][-1]["end"] == after["until"],
              str(after))
        check("three durations still cover the interval",
              sum(totals(after)[:3]) == after["total_ms"], str(after))
        check("total advanced by the elapsed time, not reset",
              0 <= after["total_ms"] - before["total_ms"] <= 60_000
              and after["available_ms"] == before["available_ms"],
              f"{before['total_ms']} -> {after['total_ms']}")
        check("reports after restart are accepted and extend the timeline",
              call(base, "POST", "/reports",
                   {"service": "B", "seq": 3, "status": "recover"})[0] == 200,
              "")
        _, grown = call(base, "GET", "/availability")
        check("timeline ends available after recovery",
              grown["segments"][-1]["state"] == "available", str(grown))
        check("growth is internally consistent",
              sum(totals(grown)[:3]) == grown["total_ms"]
              and grown["total_ms"] >= after["total_ms"], str(grown))
    finally:
        stop_server(server)

    print(f"\n{'-' * 60}\nPASS={PASS} FAIL={FAIL}")
    if FAIL:
        return 1
    print("ALL RESTART CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
