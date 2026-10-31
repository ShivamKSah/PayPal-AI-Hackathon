"""Settlement model definition."""

import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


class SettlementBase(SQLModel):
    case_id: str = Field(index=True)
    human_accepted: bool
    paypal_refund_id: Optional[str] = None
    request_id: str = Field(index=True)
    final_amount_cents: int
    settled_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Settlement(SettlementBase, table=True):
    __tablename__ = "settlements"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)


class SettlementRead(SettlementBase):
    id: str
