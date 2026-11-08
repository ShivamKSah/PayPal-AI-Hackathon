"""Proposal model definition."""

import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from sqlmodel import SQLModel, Field, JSON, Index


class ProposalBase(SQLModel):
    mandate_id: str = Field(index=True)
    instruction_text: str
    agent_reasoning: Optional[str] = None
    cart_json: Dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    cart_hash: str
    catalog_snapshot: Optional[Dict[str, Any]] = Field(default=None, sa_type=JSON)
    constraints: Optional[Dict[str, Any]] = Field(default=None, sa_type=JSON)
    verdict: str  # APPROVE, ESCALATE, DENY
    rule_hits: List[Dict[str, Any]] = Field(default_factory=list, sa_type=JSON)
    reviewer: Optional[str] = None
    llm_fallback: bool = Field(default=False)
    model_used: Optional[str] = None
    llm_attempts: int = Field(default=0)


class Proposal(ProposalBase, table=True):
    __tablename__ = "proposals"
    __table_args__ = (
        Index("idx_proposals_mandate_created", "mandate_id", "created_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)


class ProposalCreate(SQLModel):
    mandate_id: str
    instruction_text: str
    cart_json: Dict[str, Any]
    agent_reasoning: Optional[str] = None
    llm_fallback: bool = False
    model_used: Optional[str] = None
    llm_attempts: int = 0


class ProposalRead(ProposalBase):
    id: str
    created_at: datetime
