"""Audit log model with hash-chaining."""

import uuid
import hashlib
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from sqlmodel import SQLModel, Field, JSON, Index


class AuditLogBase(SQLModel):
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    actor: str  # human, agent, engine, paypal, recovery
    event: str
    ref_type: Optional[str] = None
    ref_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = Field(default=None, sa_type=JSON)
    prev_hash: str
    row_hash: str


class AuditLog(AuditLogBase, table=True):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_log_ref", "ref_type", "ref_id"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)


def compute_row_hash(prev_hash: str, ts: datetime, actor: str, event: str, ref_type: Optional[str], ref_id: Optional[str], payload: Optional[Dict[str, Any]]) -> str:
    """Compute SHA-256 hash chaining this row to the previous row."""
    canonical_payload = json.dumps(payload or {}, sort_keys=True)
    data = f"{prev_hash}|{ts.isoformat()}|{actor}|{event}|{ref_type or ''}|{ref_id or ''}|{canonical_payload}"
    return hashlib.sha256(data.encode("utf-8")).hexdigest()
