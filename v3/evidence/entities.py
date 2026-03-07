"""
V3 Evidence Layer Entities
Pydantic models for all evidence layer data structures
"""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field
import json


class SourceDocument(BaseModel):
    """Raw source document (article, API response, filing, etc.)"""
    
    doc_id: str
    url: str | None = None
    source_type: str  # 'article', 'api', 'filing', 'social'
    publisher: str | None = None
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    content_hash: str
    title: str | None = None
    text_path: str | None = None  # path to stored full text
    metadata: dict = Field(default_factory=dict)
    embedding: list[float] | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


class EvidenceItem(BaseModel):
    """Extracted claim or data point supporting/contradicting a condition"""
    
    evidence_id: str
    condition_id: str
    doc_id: str | None = None
    ts_event: datetime | None = None  # when the event occurred
    ts_observed: datetime = Field(default_factory=datetime.utcnow)  # when we saw it
    polarity: Literal['YES', 'NO', 'MIXED', 'NEUTRAL']
    claim: str
    reliability: float = Field(ge=0.0, le=1.0, default=0.5)
    freshness_hours: float | None = None
    extracted_values: dict = Field(default_factory=dict)
    embedding: list[float] | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


class RuleGraph(BaseModel):
    """Structured representation of a market condition"""
    
    condition_id: str
    source_name: str  # human-readable condition name
    operator: str | None = None  # '>', '<', '>=', '<=', '==', 'contains'
    threshold_num: float | None = None
    threshold_text: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    edge_cases: list[dict] = Field(default_factory=list)
    clarification_ids: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


class BlindEstimate(BaseModel):
    """Pure probability estimate without market info"""
    
    p_hat: float = Field(ge=0.0, le=1.0)  # point estimate
    uncertainty: float = Field(ge=0.0)  # epistemic uncertainty
    evidence_ids: list[str] = Field(default_factory=list)
    model: str  # which model generated this
    reasoning_summary: str | None = None
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


class MarketAwareDecision(BaseModel):
    """Trading decision combining blind estimate with market state"""
    
    blind_estimate: BlindEstimate
    current_mid: float  # current market midpoint
    edge_cents: float  # our edge in cents
    hurdle_cents: float  # minimum edge to trade
    action: Literal['TRADE', 'NO_EDGE', 'WAIT']
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


class FairValueSignal(BaseModel):
    """Calibrated probability signal with uncertainty bounds"""
    
    condition_id: str
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    p_calibrated: float = Field(ge=0.0, le=1.0)
    p_low: float | None = Field(default=None, ge=0.0, le=1.0)
    p_high: float | None = Field(default=None, ge=0.0, le=1.0)
    uncertainty: float = Field(ge=0.0)
    skew_cents: float | None = None
    hurdle_cents: float | None = None
    hurdle_met: bool | None = None
    route: Literal['numeric', 'simple', 'rule', 'dossier']
    evidence_ids: list[str] = Field(default_factory=list)
    counterevidence_ids: list[str] = Field(default_factory=list)
    models_used: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


class ChangeEvent(BaseModel):
    """Event representing a change in evidence or conditions"""
    
    event_type: str  # 'new_evidence', 'rule_updated', 'signal_generated', etc.
    condition_id: str
    payload: dict = Field(default_factory=dict)
    ts: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


class RoutePlan(BaseModel):
    """Routing decision for how to resolve a condition"""
    
    condition_id: str
    route: Literal['numeric', 'simple', 'rule', 'dossier']
    priority: int = 0  # higher = more urgent
    reason: str  # why this route was chosen
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }
