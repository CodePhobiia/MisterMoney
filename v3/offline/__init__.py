"""
V3 Offline Worker — Async adjudication tier for high-stakes markets

Provides:
- EscalationQueue: Priority queue for markets needing async review
- OfflineWorker: GPT-5.4-pro deep review processor
- WeeklyEvaluator: Performance analysis and calibration label generation
"""

from .queue import EscalationQueue
from .worker import OfflineWorker
from .weekly_eval import WeeklyEvaluator

__all__ = [
    "EscalationQueue",
    "OfflineWorker",
    "WeeklyEvaluator",
]
