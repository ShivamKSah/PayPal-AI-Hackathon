"""Mandate model definition."""

import uuid
from datetime import datetime, timezone
from typing import Optional, List
from sqlmodel import SQLModel, Field, JSON


class MandateBase(SQLModel):
    session_id: str = Field(index=True)
    budget_cents: int
    reserved_cents: int = Field(default=0)
    spent_cents: int = Field(default=0)
    per_txn_cap_cents: int
    categories: List[str] = Field(default_factory=list, sa_type=JSON)
    merchant_allowlist: List[str] = Field(default_factory=list, sa_type=JSON)
    allow_new_merchants: bool = Field(default=False)
    allow_recurring: bool = Field(default=False)
    velocity_per_hour: int = Field(default=5)
    near_limit_pct: int = Field(default=80)
    escalate_first_merchant: bool = Field(default=False)
    status: str = Field(default="ACTIVE")
    expires_at: datetime


class Mandate(MandateBase, table=True):
    __tablename__ = "mandates"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    # Encrypted vault token reference: never exposed in external responses
    vault_token_ref: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MandateCreate(SQLModel):
    session_id: str
    budget_cents: int
    per_txn_cap_cents: int
    categories: List[str]
    merchant_allowlist: List[str]
    allow_new_merchants: bool = False
    allow_recurring: bool = False
    velocity_per_hour: int = 5
    near_limit_pct: int = 80
    escalate_first_merchant: bool = False
    expires_at: datetime
    vault_token_ref: Optional[str] = None


class MandateRead(MandateBase):
    id: str
    created_at: datetime


class MandateResponse(MandateRead):
    session_token: Optional[str] = None
