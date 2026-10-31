"""Case model definition."""

import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from sqlmodel import SQLModel, Field, JSON


class CaseBase(SQLModel):
    payment_id: str = Field(index=True)
    human_claim: str
    evidence: Optional[Dict[str, Any]] = Field(default=None, sa_type=JSON)
    root_cause: Optional[str] = None
    allowed_min_pct: Optional[int] = None
    allowed_max_pct: Optional[int] = None
    economic_inputs: Optional[Dict[str, Any]] = Field(default=None, sa_type=JSON)
    suggested_pct: Optional[int] = None
    proposed_refund_cents: Optional[int] = None
    explanation: Optional[str] = None
    status: str = Field(default="OPEN")  # OPEN, DIAGNOSED, PROPOSED, ACCEPTED, REJECTED, REFUNDED, ESCALATED_TO_DISPUTE


class Case(CaseBase, table=True):
    __tablename__ = "cases"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CaseRead(CaseBase):
    id: str
    created_at: datetime
