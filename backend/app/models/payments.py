"""Payment model definition."""

import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


class PaymentBase(SQLModel):
    proposal_id: str = Field(index=True)
    paypal_order_id: Optional[str] = None
    capture_id: Optional[str] = Field(default=None, unique=True, index=True)
    request_id: str = Field(index=True)
    amount_cents: int
    state: str = Field(default="RESERVED")  # RESERVED, ORDER_CREATED, CAPTURED, CONFIRMED, FAILED, RELEASED
    captured_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None


class Payment(PaymentBase, table=True):
    __tablename__ = "payments"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PaymentRead(PaymentBase):
    id: str
    created_at: datetime
