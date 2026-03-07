"""PMM-2 shadow mode — run full pipeline without execution, log counterfactuals.

Shadow mode enables PMM-2 to run alongside V1 (PMM-1) and compare:
- Market selection (which markets to quote)
- Order pricing and sizing
- Entry/exit decisions
- Expected value estimates

This provides data-driven validation before live capital deployment.

Components:
- ShadowLogger: logs every allocation cycle's decisions
- CounterfactualEngine: compares V1 actual vs PMM-2 counterfactual
- ShadowDashboard: generates status reports for Telegram
- V1StateSnapshot: captures V1's current state for comparison
"""

from pmm2.shadow.counterfactual import CounterfactualEngine
from pmm2.shadow.dashboard import ShadowDashboard
from pmm2.shadow.logger import ShadowLogger
from pmm2.shadow.v1_snapshot import V1StateSnapshot

__all__ = [
    "ShadowLogger",
    "CounterfactualEngine",
    "ShadowDashboard",
    "V1StateSnapshot",
]
