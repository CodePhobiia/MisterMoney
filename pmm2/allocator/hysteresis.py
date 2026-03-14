"""Reallocation hysteresis — prevent allocator thrashing.

Rules:
1. Target changes must clear: |ΔCap| > max(hysteresis_frac * Cap, hysteresis_min_usdc)
2. Rank changes must persist for min_persistence_cycles before capital moves
3. Override exceptions bypass hysteresis:
   - Inventory breach
   - Reward eligibility changed
   - Market resolved/halted
   - Structural arb appeared

Scale-aware: at $104 NAV, hysteresis_min_usdc = $5 (not $500).
"""

from __future__ import annotations

import time

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


class MarketAllocationState(BaseModel):
    """Track allocation history for hysteresis."""

    condition_id: str
    current_capital: float = 0.0
    target_capital: float = 0.0
    rank_history: list[int] = []  # last N allocator ranks
    last_change_time: float = 0.0
    consecutive_rank_cycles: int = 0  # how many cycles at current rank


class ReallocationHysteresis:
    """Prevent allocator thrashing.

    Target changes must clear:
    |ΔCap| > max(hysteresis_frac * Cap, hysteresis_min_usdc)

    Rank changes must persist for min_persistence_cycles before capital moves.

    Override exceptions (bypass hysteresis):
    - Inventory breach
    - Reward eligibility changed
    - Market resolved/halted
    - Structural arb appeared
    """

    def __init__(
        self,
        hysteresis_frac: float = 0.10,
        hysteresis_min_usdc: float = 5.0,  # $5 not $500 (scaled for $104 NAV)
        min_persistence_cycles: int = 3,
        max_rank_history: int = 10,
    ):
        """Initialize reallocation hysteresis.

        Args:
            hysteresis_frac: fractional threshold (0.10 = 10% of current capital)
            hysteresis_min_usdc: minimum dollar threshold ($5 scale-aware)
            min_persistence_cycles: how many cycles a rank must persist before reallocation
            max_rank_history: max length of rank_history (for memory)
        """
        self.hysteresis_frac = hysteresis_frac
        self.hysteresis_min_usdc = hysteresis_min_usdc
        self.min_persistence_cycles = min_persistence_cycles
        self.max_rank_history = max_rank_history
        self.states: dict[str, MarketAllocationState] = {}

        logger.info(
            "hysteresis_initialized",
            hysteresis_frac=hysteresis_frac,
            hysteresis_min_usdc=hysteresis_min_usdc,
            min_persistence_cycles=min_persistence_cycles,
        )

    def should_reallocate(
        self,
        condition_id: str,
        current_cap: float,
        target_cap: float,
        rank: int,
        override: bool = False,
    ) -> tuple[bool, str]:
        """Check if reallocation should proceed.

        Returns (should_move, reason).

        Rules:
        1. If override=True, always allow (emergency)
        2. |delta| must exceed hysteresis threshold
        3. Rank must have been stable for min_persistence_cycles

        Args:
            condition_id: market condition ID
            current_cap: current allocated capital
            target_cap: proposed new capital
            rank: current allocator rank (lower = better)
            override: bypass hysteresis (emergency)

        Returns:
            (should_move, reason) tuple
        """
        # --- 1. Override check ---
        if override:
            logger.info(
                "hysteresis_override",
                condition_id=condition_id,
                current_cap=current_cap,
                target_cap=target_cap,
            )
            return (True, "override")

        # --- 2. Delta threshold check ---
        delta = abs(target_cap - current_cap)
        threshold = max(
            self.hysteresis_frac * max(current_cap, target_cap),
            self.hysteresis_min_usdc,
        )

        if delta < threshold:
            logger.debug(
                "hysteresis_blocked_delta",
                condition_id=condition_id,
                delta=delta,
                threshold=threshold,
            )
            return (False, f"delta_too_small: {delta:.2f} < {threshold:.2f}")

        # --- 3. Rank persistence check ---
        state = self.states.get(condition_id)
        if state is None:
            # First time seeing this market, allow reallocation
            logger.debug(
                "hysteresis_new_market",
                condition_id=condition_id,
            )
            return (True, "new_market")

        # If we have no current capital, allow entry without history requirement
        if current_cap == 0.0 and target_cap > 0.0:
            logger.debug(
                "hysteresis_new_entry",
                condition_id=condition_id,
                target_cap=target_cap,
            )
            return (True, "new_entry")

        # Check if rank has been stable
        if len(state.rank_history) >= self.min_persistence_cycles:
            # Check if all recent ranks are the same
            recent_ranks = state.rank_history[-self.min_persistence_cycles :]
            if all(r == rank for r in recent_ranks):
                # Rank is stable, allow reallocation
                logger.debug(
                    "hysteresis_rank_stable",
                    condition_id=condition_id,
                    rank=rank,
                    cycles=self.min_persistence_cycles,
                )
                return (True, "rank_stable")
            else:
                # Rank is changing, block reallocation
                logger.debug(
                    "hysteresis_blocked_rank_unstable",
                    condition_id=condition_id,
                    recent_ranks=recent_ranks,
                )
                return (False, "rank_unstable")
        else:
            # Not enough history yet, require more cycles
            logger.debug(
                "hysteresis_insufficient_history",
                condition_id=condition_id,
                history_len=len(state.rank_history),
                required=self.min_persistence_cycles,
            )
            return (
                False,
                f"insufficient_history: "
                f"{len(state.rank_history)} < "
                f"{self.min_persistence_cycles}",
            )

    def update_cycle(self, condition_id: str, target_cap: float, rank: int) -> None:
        """Record this cycle's target and rank for hysteresis tracking.

        Args:
            condition_id: market condition ID
            target_cap: target capital for this cycle
            rank: allocator rank for this cycle
        """
        if condition_id not in self.states:
            self.states[condition_id] = MarketAllocationState(
                condition_id=condition_id,
                current_capital=0.0,
                target_capital=target_cap,
                rank_history=[rank],
                last_change_time=time.time(),
                consecutive_rank_cycles=1,
            )
        else:
            state = self.states[condition_id]
            state.target_capital = target_cap
            state.rank_history.append(rank)

            # Trim history if too long
            if len(state.rank_history) > self.max_rank_history:
                state.rank_history = state.rank_history[-self.max_rank_history :]

            # Update consecutive rank cycles
            if len(state.rank_history) >= 2 and state.rank_history[-1] == state.rank_history[-2]:
                state.consecutive_rank_cycles += 1
            else:
                state.consecutive_rank_cycles = 1

        logger.debug(
            "hysteresis_cycle_updated",
            condition_id=condition_id,
            target_cap=target_cap,
            rank=rank,
            history_len=len(self.states[condition_id].rank_history),
        )

    def override_reasons(
        self,
        condition_id: str,
        inventory_breach: bool = False,
        reward_changed: bool = False,
        resolved: bool = False,
        arb_appeared: bool = False,
    ) -> bool:
        """Check if any override condition is met.

        Args:
            condition_id: market condition ID
            inventory_breach: inventory risk breach detected
            reward_changed: reward eligibility changed
            resolved: market resolved or halted
            arb_appeared: structural arbitrage opportunity appeared

        Returns:
            True if should override hysteresis
        """
        if inventory_breach:
            logger.info("hysteresis_override_inventory_breach", condition_id=condition_id)
            return True

        if reward_changed:
            logger.info("hysteresis_override_reward_changed", condition_id=condition_id)
            return True

        if resolved:
            logger.info("hysteresis_override_resolved", condition_id=condition_id)
            return True

        if arb_appeared:
            logger.info("hysteresis_override_arb_appeared", condition_id=condition_id)
            return True

        return False

    def record_reallocation(self, condition_id: str, new_capital: float) -> None:
        """Record that reallocation occurred.

        Args:
            condition_id: market condition ID
            new_capital: new capital allocation
        """
        if condition_id in self.states:
            self.states[condition_id].current_capital = new_capital
            self.states[condition_id].last_change_time = time.time()

        logger.info(
            "hysteresis_reallocation_recorded",
            condition_id=condition_id,
            new_capital=new_capital,
        )
