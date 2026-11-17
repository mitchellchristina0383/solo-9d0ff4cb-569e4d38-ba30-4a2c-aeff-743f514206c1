"""Offline unit tests for availability-conclusion and timeline rules.

These exercise the pure pieces (conclusion derivation, timestamp helpers,
segment splitting/summarization) against an in-memory SQLite database, so
they run without a server:

    PYTHONPATH=. python tests/test_availability_unit.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


def _database():
    from app import database

    database.DB_PATH = tempfile.mktemp(suffix=".db")
    database._local.conn = None
    database.init_db()
    return database


def _commit_graph(conn, revision, nodes):
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM nodes")
    conn.executemany(
        "INSERT INTO nodes (service, revision) VALUES (?, ?)",
        [(n, revision) for n in nodes],
    )
    for downstream, deps in nodes.items():
        for up in deps:
            conn.execute(
                "INSERT INTO edges (revision, upstream, downstream) VALUES (?, ?, ?)",
                (revision, up, downstream),
            )
    conn.execute(
        "INSERT INTO graph_meta (id, revision, version) VALUES (1, ?, 't') "
        "ON CONFLICT(id) DO UPDATE SET revision = excluded.revision",
        (revision,),
    )


def test_timestamps() -> None:
    from app.availability import format_timestamp, parse_timestamp

    print("[U1] timestamp helpers")
    check("Z suffix parses to epoch", parse_timestamp("1970-01-01T00:00:00.000Z") == 0)
    check("millisecond precision round-trips",
          parse_timestamp(format_timestamp(1_700_000_000_123)) == 1_700_000_000_123)
    check("naive timestamps are treated as UTC",
          parse_timestamp("1970-01-01T00:00:01") == 1000)
    check("explicit offsets convert to UTC",
          parse_timestamp("1970-01-01T01:00:00+01:00") == 0)


def test_conclusion() -> None:
    from app import availability

    print("[U2] conclusion derivation")
    db = _database()
    with db.write_tx() as conn:
        # A -> B -> D, A -> C
        _commit_graph(conn, 1, {"A": ["B", "C"], "B": ["D"], "C": [], "D": []})

        check("nothing reported -> unknown", availability.conclusion(conn, "A") == "unknown")
        check("entry not in graph -> unknown", availability.conclusion(conn, "ZZ") == "unknown")

        conn.execute(
            "INSERT INTO service_state VALUES ('A', 'down', 1), ('B', 'up', 1), "
            "('C', 'up', 1), ('D', 'up', 1)"
        )
        check("entry fault -> interrupted even with all upstream up",
              availability.conclusion(conn, "A") == "interrupted")

        conn.execute("UPDATE service_state SET status='up' WHERE service='A'")
        check("entry + full closure up -> available",
              availability.conclusion(conn, "A") == "available")

        conn.execute("UPDATE service_state SET status='down' WHERE service='D'")
        check("deeply nested upstream fault -> interrupted",
              availability.conclusion(conn, "A") == "interrupted")

        conn.execute("DELETE FROM service_state WHERE service='D'")
        conn.execute("UPDATE service_state SET status='up' WHERE service='A'")
        check("unreported deep upstream with no fault -> unknown",
              availability.conclusion(conn, "A") == "unknown")

        # A fault somewhere upstream wins over an unreported sibling.
        conn.execute("DELETE FROM service_state WHERE service='C'")
        conn.execute("UPDATE service_state SET status='up' WHERE service='B'")
        conn.execute("INSERT INTO service_state VALUES ('D', 'down', 1)")
        check("fault + unrelated unreported sibling -> interrupted, not unknown",
              availability.conclusion(conn, "A") == "interrupted")

        # Graph change removes the faulty node from A's closure.
        _commit_graph(conn, 2, {"A": ["B"], "B": [], "D": []})
        conn.execute("UPDATE service_state SET status='up' WHERE service='D'")
        conn.execute("INSERT INTO service_state VALUES ('C', 'down', 1)")
        check("graph change removing a node drops it from the closure -> available",
              availability.conclusion(conn, "A") == "available")
    db.close_connection()


def test_splitting_and_sums() -> None:
    from app import availability

    print("[U3] segment splitting, no-op suppression and duration arithmetic")
    db = _database()
    from app.availability import _summarize, _current_monitor

    base = 1_700_000_000_000
    with db.write_tx() as conn:
        _commit_graph(conn, 1, {"A": ["B"], "B": []})
        conn.execute(
            "INSERT INTO availability_monitor (id, entry, start_revision, started_at) "
            "VALUES (1, 'A', 1, ?)",
            (availability.format_timestamp(base),),
        )
        conn.execute(
            "INSERT INTO availability_segment (start_at, end_at, state, graph_revision) "
            "VALUES (?, NULL, 'unknown', 1)",
            (availability.format_timestamp(base),),
        )

        # No change -> no split.
        availability.record_conclusion(conn, now_ms=base + 100)
        check("unchanged conclusion adds no segment",
              len(list(conn.execute("SELECT 1 FROM availability_segment"))) == 1)

        conn.execute("INSERT INTO service_state VALUES ('A','up',1),('B','up',1)")
        availability.record_conclusion(conn, now_ms=base + 200)
        segments = list(conn.execute(
            "SELECT start_at, end_at, state FROM availability_segment ORDER BY id"))
        check("change splits at the commit instant",
              [(s["state"], s["start_at"], s["end_at"]) for s in segments]
              == [("unknown", availability.format_timestamp(base),
                   availability.format_timestamp(base + 200)),
                  ("available", availability.format_timestamp(base + 200), None)],
              str([tuple(s) for s in segments]))

        # Backwards clock: a split may never make a segment end before it starts.
        availability.record_conclusion(conn, now_ms=base + 150)
        segments = list(conn.execute(
            "SELECT start_at, end_at, state FROM availability_segment ORDER BY id"))
        check("backwards clock clamps the split to the open segment start",
              segments[-1]["state"] == "available"
              and segments[-1]["start_at"] == availability.format_timestamp(base + 200),
              str([tuple(s) for s in segments]))

        monitor = _current_monitor(conn)
        summary = _summarize(conn, monitor, base + 1000)
        check("durations cover the whole interval",
              summary["available_ms"] + summary["interrupted_ms"]
              + summary["unknown_ms"] == summary["total_ms"] == 1000, str(summary))
        check("open segment counted up to the cutoff",
              summary["segments"][-1]["end"] == availability.format_timestamp(base + 1000),
              str(summary["segments"]))

        # A trailing hole (only possible with corrupted data) is reported unknown.
        conn.execute("UPDATE availability_segment SET end_at = ? "
                     "WHERE end_at IS NULL",
                     (availability.format_timestamp(base + 400),))
        summary = _summarize(conn, monitor, base + 1000)
        check("defensive gap is counted as unknown and totals still cover",
              summary["unknown_ms"] >= 600
              and summary["available_ms"] + summary["interrupted_ms"]
              + summary["unknown_ms"] == summary["total_ms"], str(summary))

    db.close_connection()


def main() -> int:
    test_timestamps()
    test_conclusion()
    test_splitting_and_sums()
    print(f"\n{'-' * 60}\nPASS={PASS} FAIL={FAIL}")
    if FAIL:
        print("Failures:")
        for f in FAILURES:
            print(" -", f)
        return 1
    print("ALL UNIT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
