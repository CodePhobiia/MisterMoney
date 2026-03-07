"""
V3 Evidence Layer
Complete evidence management for MisterMoney Resolution Intelligence
"""

from .db import Database
from .entities import (
    SourceDocument,
    EvidenceItem,
    RuleGraph,
    BlindEstimate,
    MarketAwareDecision,
    FairValueSignal,
    ChangeEvent,
    RoutePlan,
)
from .graph import EvidenceGraph
from .retrieval import EvidenceRetrieval
from .normalizer import EvidenceNormalizer
from .storage import ObjectStore

__all__ = [
    # Core
    'Database',
    'EvidenceGraph',
    'EvidenceRetrieval',
    'EvidenceNormalizer',
    'ObjectStore',
    
    # Entities
    'SourceDocument',
    'EvidenceItem',
    'RuleGraph',
    'BlindEstimate',
    'MarketAwareDecision',
    'FairValueSignal',
    'ChangeEvent',
    'RoutePlan',
]
