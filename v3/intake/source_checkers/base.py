"""
Base classes for source checkers
"""

from abc import ABC, abstractmethod
from datetime import datetime
from pydantic import BaseModel, Field

from v3.evidence.entities import RuleGraph


class SourceCheckResult(BaseModel):
    """Result from checking a resolution source"""
    
    condition_id: str
    source: str
    current_value: float | str
    threshold: float | str
    probability: float = Field(ge=0.0, le=1.0)  # deterministic computation
    confidence: float = Field(ge=0.0, le=1.0)  # how confident we are in the data
    checked_at: datetime = Field(default_factory=datetime.utcnow)
    raw_data: dict = Field(default_factory=dict)
    ttl_seconds: int = 60  # numeric markets refresh fast
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


class SourceChecker(ABC):
    """Base class for all source checkers"""
    
    @abstractmethod
    async def check(self, condition_id: str, rule: RuleGraph) -> SourceCheckResult:
        """
        Check current state of a condition against its resolution source
        
        Args:
            condition_id: Unique identifier for the condition
            rule: Structured rule with source, threshold, window, etc.
            
        Returns:
            SourceCheckResult with current value, probability, and confidence
        """
        pass
