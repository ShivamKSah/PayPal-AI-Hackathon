"""Webhook events model definition."""

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


class WebhookEvent(SQLModel, table=True):
    __tablename__ = "webhook_events"

    event_id: str = Field(primary_key=True)
    event_type: str
    verified: bool
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    processed_at: Optional[datetime] = None
