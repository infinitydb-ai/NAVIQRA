from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def new_id() -> str:
    return str(uuid4())


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TripRequest(StrictModel):
    traveler_id: str = Field(min_length=1, max_length=128)
    origin: str = Field(min_length=2, max_length=160)
    destination: str = Field(min_length=2, max_length=160)
    start_date: date
    end_date: date
    budget: float = Field(gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    travelers: int = Field(default=1, ge=1, le=30)
    interests: list[str] = Field(default_factory=list, max_length=30)
    constraints: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("end_date")
    @classmethod
    def validate_dates(cls, value: date, info: Any) -> date:
        start = info.data.get("start_date")
        if start and value <= start:
            raise ValueError("end_date must be after start_date")
        return value


class Activity(StrictModel):
    name: str
    description: str
    day: int = Field(ge=1)
    estimated_cost: float = Field(ge=0)
    currency: str = "USD"
    category: str = "general"


class Itinerary(StrictModel):
    trip_id: str
    destination: str
    start_date: date
    end_date: date
    activities: list[Activity]
    estimated_total: float
    currency: str
    rationale: list[str]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryRecord(StrictModel):
    id: str = Field(default_factory=new_id)
    traveler_id: str
    kind: str = "preference"
    key: str
    value: Any
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BookingIntent(StrictModel):
    trip_id: str
    provider: str = "demo"
    item_type: str = Field(pattern="^(stay|flight|activity)$")
    item_id: str
    amount: float = Field(gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    idempotency_key: str = Field(min_length=8, max_length=128)


class ApprovalRequest(StrictModel):
    approved: bool
    approver: str = Field(min_length=1, max_length=128)


class BookingStatus(str, Enum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


class Booking(StrictModel):
    id: str = Field(default_factory=new_id)
    trip_id: str
    provider: str
    item_type: str
    item_id: str
    amount: float
    currency: str
    status: BookingStatus = BookingStatus.PENDING_APPROVAL
    idempotency_key: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    approved_by: str | None = None


class AgentPlan(StrictModel):
    trip_id: str
    stages: list[str]
    itinerary: Itinerary
    memory_used: list[MemoryRecord]
    warnings: list[str] = Field(default_factory=list)


class RecoveryStatus(str, Enum):
    AWAITING_SELECTION = "awaiting_selection"
    APPROVED = "approved"
    EXECUTING = "executing"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RecoveryCreateRequest(StrictModel):
    trip_id: str = Field(min_length=1, max_length=128)
    disruption: str = Field(min_length=1, max_length=500)
    options: list[dict[str, Any]] = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)


class RecoverySelectRequest(StrictModel):
    selected_option_id: str = Field(min_length=1, max_length=128)
    notes: str = Field(default="", max_length=500)


class RecoveryDecisionRequest(StrictModel):
    approved: bool
    approver: str = Field(min_length=1, max_length=128)
    notes: str = Field(default="", max_length=500)


class RecoveryExecuteRequest(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    provider: str = Field(default="sandbox", min_length=1, max_length=64)
    simulate_failure: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class RecoveryCancelRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=500)
    cancelled_by: str = Field(min_length=1, max_length=128)

