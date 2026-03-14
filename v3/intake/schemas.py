"""
V3 Intake Schemas
Market metadata from external sources
"""

from datetime import datetime

from pydantic import BaseModel, Field


class MarketMeta(BaseModel):
    """Market metadata from Polymarket Gamma API"""

    condition_id: str
    question: str
    description: str
    resolution_source: str
    end_date: datetime
    rules: str  # raw rules text from Polymarket
    clarifications: list[str] = Field(default_factory=list)
    volume_24h: float
    current_mid: float

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }
