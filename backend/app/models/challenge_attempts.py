"""Challenge attempts model definition."""

import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from sqlmodel import SQLModel, Field, JSON


class ChallengeAttempt(SQLModel, table=True):
    __tablename__ = "challenge_attempts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    session_id: str = Field(index=True)
    attack_type: str
    verdict: str  # DENY, APPROVE, ESCALATE
    rule_hits: Optional[List[Dict[str, Any]]] = Field(default=None, sa_type=JSON)
    breached: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
