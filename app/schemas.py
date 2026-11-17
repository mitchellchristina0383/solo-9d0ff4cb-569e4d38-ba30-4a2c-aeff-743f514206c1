"""Pydantic request/response models."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["fault", "recover"]


class ServiceIn(BaseModel):
    name: str = Field(..., min_length=1, description="Unique service name")
    dependencies: list[str] = Field(
        default_factory=list,
        description="Names of upstream services this service depends on",
    )


class GraphPayload(BaseModel):
    version: Optional[str] = Field(
        default=None, description="Optional caller-supplied graph version label"
    )
    services: list[ServiceIn] = Field(
        ..., min_length=1, description="Full dependency graph (whole-version replace)"
    )


class ReportPayload(BaseModel):
    service: str = Field(..., min_length=1)
    seq: int = Field(..., gt=0, description="Strictly positive, increasing per service")
    status: Status


class RootCause(BaseModel):
    service: str
    paths: list[list[str]]


class ActionableFault(BaseModel):
    service: str
    last_seq: int


class SuppressedFault(BaseModel):
    service: str
    last_seq: int
    root_causes: list[str]
    paths: list[list[str]]


class AlertsResponse(BaseModel):
    graph_revision: int
    graph_version: Optional[str]
    services: list[str]
    actionable_faults: list[ActionableFault]
    suppressed_faults: list[SuppressedFault]
    down_services: list[str]
    unreported_services: list[str]
    healthy_services: list[str]


class GraphResponse(BaseModel):
    revision: int
    version: Optional[str]
    committed_at: Optional[str]
    services: list[ServiceIn]


class ReportResponse(BaseModel):
    applied: bool
    duplicate: bool
    service: str
    seq: int
    status: Status
    current_status: Literal["up", "down"]
    last_seq: int


class ReplaceResponse(BaseModel):
    accepted: bool
    revision: int
    version: Optional[str]
    service_count: int
    edge_count: int
    preserved_state: list[str]


# ---------------------------------------------------------------------------
# Availability monitoring
# ---------------------------------------------------------------------------


class MonitorStartPayload(BaseModel):
    entry: str = Field(
        ..., min_length=1, description="Entry service to monitor (must be in the graph)"
    )


class MonitorResponse(BaseModel):
    entry: str
    started_at: str
    start_revision: int
    current_state: Literal["available", "interrupted", "unknown"]
    already_monitoring: bool


class AvailabilityPeriod(BaseModel):
    state: Literal["available", "interrupted", "unknown"]
    start: str
    end: str
    duration_ms: int
    graph_revision: Optional[int]


class AvailabilityResponse(BaseModel):
    entry: str
    started_at: str
    until: str
    graph_revision: Optional[int]
    start_revision: int
    available_ms: int
    interrupted_ms: int
    unknown_ms: int
    total_ms: int
    available_seconds: float
    interrupted_seconds: float
    unknown_seconds: float
    total_seconds: float
    segments: list[AvailabilityPeriod]
    interrupted_periods: list[AvailabilityPeriod]
