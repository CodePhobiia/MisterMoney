"""
V3 Evidence Layer
Complete evidence management for MisterMoney Resolution Intelligence
"""

from .db import Database
from .entities import (
    BlindEstimate,
    ChangeEvent,
    EvidenceItem,
    FairValueSignal,
    MarketAwareDecision,
    RoutePlan,
    RuleGraph,
    SourceDocument,
)
from .graph import EvidenceGraph
from .normalizer import EvidenceNormalizer
from .retrieval import EvidenceRetrieval
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
