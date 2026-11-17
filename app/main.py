"""FastAPI application: dependency-alert API."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .availability import get_availability, start_monitor, stop_monitor
from .database import close_connection, get_connection, init_db, write_tx
from .errors import (
    AlreadyMonitoring,
    GraphError,
    InvalidUntil,
    MonitorClosed,
    NoGraph,
    NotMonitoring,
    ReportConflict,
    UnknownEntry,
    UnknownService,
)
from .graph_service import classify_alerts, get_graph, replace_graph
from .report_service import submit_report
from .schemas import (
    AlertsResponse,
    AvailabilityResponse,
    GraphPayload,
    GraphResponse,
    MonitorResponse,
    MonitorStartPayload,
    ReplaceResponse,
    ReportPayload,
    ReportResponse,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Dependency Alert API",
    version="1.0.0",
    description=(
        "Service dependency graph + fault/recovery reports with monotonic "
        "sequence numbers and upstream-root-cause alert suppression."
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def _db_session(request: Request, call_next):
    # One SQLite connection per request (thread-local); always released.
    get_connection()
    try:
        return await call_next(request)
    finally:
        close_connection()


@app.exception_handler(GraphError)
async def _graph_error(_: Request, exc: GraphError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(ReportConflict)
async def _conflict(_: Request, exc: ReportConflict):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(UnknownService)
async def _unknown(_: Request, exc: UnknownService):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(NoGraph)
async def _no_graph(_: Request, exc: NoGraph):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(UnknownEntry)
async def _unknown_entry(_: Request, exc: UnknownEntry):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(AlreadyMonitoring)
async def _already_monitoring(_: Request, exc: AlreadyMonitoring):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(NotMonitoring)
async def _not_monitoring(_: Request, exc: NotMonitoring):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(InvalidUntil)
async def _invalid_until(_: Request, exc: InvalidUntil):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(MonitorClosed)
async def _monitor_closed(_: Request, exc: MonitorClosed):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.post("/graph", response_model=ReplaceResponse, status_code=200)
def put_graph(payload: GraphPayload) -> ReplaceResponse:
    """Replace the **whole** dependency graph.

    Invalid payloads (cycles, unknown references, duplicate names/edges) are
    rejected and the previously committed graph is retained.  States of
    same-named services survive the replacement.
    """
    return ReplaceResponse(**replace_graph(payload))


@app.get("/graph", response_model=GraphResponse)
def read_graph() -> GraphResponse:
    """Return the currently committed graph."""
    return get_graph()


@app.post("/reports", response_model=ReportResponse, status_code=200)
def post_report(payload: ReportPayload) -> ReportResponse:
    """Report a service fault or recovery with a per-service increasing seq."""
    return ReportResponse(**submit_report(payload))


@app.get("/alerts", response_model=AlertsResponse)
def read_alerts() -> AlertsResponse:
    """Return actionable faults, suppressed faults and their upstream root causes."""
    return AlertsResponse(**classify_alerts())


@app.post("/availability", response_model=MonitorResponse, status_code=201)
def begin_availability_monitor(payload: MonitorStartPayload) -> MonitorResponse:
    """Start monitoring availability of an entry in the current graph.

    The entry must be a node of the currently committed graph.  Retrying with
    the same entry is idempotent and creates no new segment.  Use
    ``POST /admin/reset`` (or ``DELETE /availability``) to stop a monitor.
    """
    return MonitorResponse(**start_monitor(payload.entry))


@app.get("/availability", response_model=AvailabilityResponse)
def read_availability(
    until: str | None = Query(
        default=None,
        description="Optional UTC ISO-8601 cutoff (e.g. 2026-11-17T12:00:00Z); "
        "defaults to the query's commit time",
    )
) -> AvailabilityResponse:
    """Return available / interrupted / unknown durations and interrupted periods."""
    return AvailabilityResponse(**get_availability(until))


@app.delete("/availability", response_model=AvailabilityResponse)
def delete_availability_monitor() -> AvailabilityResponse:
    """Stop monitoring and return the final summary of the monitored interval."""
    return AvailabilityResponse(**stop_monitor())


@app.post("/admin/reset", status_code=200)
def admin_reset() -> dict:
    """Clear all data (test/operations helper)."""
    with write_tx() as conn:
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM service_state")
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM nodes")
        conn.execute("DELETE FROM graph_meta")
        conn.execute("DELETE FROM availability_segment")
        conn.execute("DELETE FROM availability_monitor")
    return {"reset": True}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
