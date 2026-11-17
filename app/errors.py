"""Domain errors mapped to HTTP responses in main.py."""
from __future__ import annotations


class GraphError(Exception):
    """Graph payload is structurally invalid (unknown reference, cycle, ...)."""

    status_code = 422
    code = "invalid_graph"


class ReportConflict(Exception):
    """Same seq with different content, or stale (lower) seq."""

    status_code = 409
    code = "conflict"


class UnknownService(Exception):
    """Report references a service that is not in the current graph."""

    status_code = 404
    code = "unknown_service"


class NoGraph(Exception):
    """No dependency graph has been committed yet."""

    status_code = 409
    code = "no_graph"


class UnknownEntry(Exception):
    """Entry service requested for monitoring is not part of the current graph."""

    status_code = 404
    code = "unknown_entry"


class AlreadyMonitoring(Exception):
    """An availability monitor is already running."""

    status_code = 409
    code = "already_monitoring"


class MonitorClosed(Exception):
    """The monitoring period was already stopped (or is otherwise unavailable)."""

    status_code = 409
    code = "monitor_closed"


class NotMonitoring(Exception):
    """No availability monitor is running."""

    status_code = 409
    code = "not_monitoring"


class InvalidUntil(Exception):
    """The requested ``until`` cutoff is malformed or inconsistent."""

    status_code = 422
    code = "invalid_until"
