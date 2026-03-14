"""Exit manager — centralised sell/exit logic for all exit layers.

Layers (priority order):
1. FLATTEN — emergency operator command or kill switch
2. STOP-LOSS — drawdown-triggered mandatory exit
3. RESOLUTION — time-based ramp before market end_date
4. TAKE-PROFIT — threshold-triggered partial/full exit
5. ORPHAN — positions not in active universe
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel

from pmm1.settings import ExitConfig
from pmm1.state.books import BookManager
from pmm1.state.positions import MarketPosition, PositionTracker
from pmm1.strategy.universe import MarketMetadata

logger = structlog.get_logger(__name__)


class SellSignal(BaseModel):
    """A signal to exit (sell) part or all of a position."""

    token_id: str
    condition_id: str
    size: float
    price: float | None = None  # None → use best_bid at execution time
    urgency: str = "low"  # low | medium | high | critical
    reason: str = ""  # flatten | stop_loss | hard_stop | resolution | take_profit | orphan


class ResolutionAction(BaseModel):
    """Describes what resolution-exit phase a market is in."""

    action: str = ""  # FORCE_EXIT | AGGRESSIVE_EXIT | URGENT_EXIT | GRADUAL_EXIT | NO_NEW_BUYS
    fraction: float = 0.0  # for GRADUAL_EXIT: 0→1 ramp
    hours_left: float = 0.0
    block_new_buys: bool = False


class ExitManager:
    """Evaluates all exit conditions and produces prioritised SellSignals."""

    def __init__(
        self,
        config: ExitConfig,
        position_tracker: PositionTracker,
        book_manager: BookManager,
        kill_switch: Any = None,
        clob_public: Any = None,
    ) -> None:
        self.config = config
        self.positions = position_tracker
        self.books = book_manager
        self.kill_switch = kill_switch
        self.clob_public = clob_public  # REST fallback for orphan books

        # Cooldown tracking for take-profit (condition_id → last TP time)
        self._tp_cooldowns: dict[str, float] = {}

        # Last orphan check time
        self._last_orphan_check: float = 0.0

        # Resolution action cache (condition_id → ResolutionAction)
        self._resolution_cache: dict[str, ResolutionAction] = {}

    # ── Public API ──

    async def evaluate_all(
        self,
        active_markets: dict[str, MarketMetadata],
    ) -> list[SellSignal]:
        """Run all exit checks across all positions.

        Returns a list of SellSignals sorted by priority (highest first).
        """
        signals: list[SellSignal] = []
        now = time.time()

        # Check flatten first (applies to ALL positions)
        flatten_active = self._is_flatten_active()

        for pos in self.positions.get_active_positions():
            # Process both YES and NO sides independently
            sides_to_check = []
            if pos.yes_size >= 5.0:
                sides_to_check.append((pos.token_id_yes, pos.yes_size, pos.yes_avg_price))
            if pos.no_size >= 5.0:
                sides_to_check.append((pos.token_id_no, pos.no_size, pos.no_avg_price))

            if not sides_to_check:
                continue

            for token_id, inv_size, avg_price in sides_to_check:
                current_price = self._get_best_bid(token_id)
                md = active_markets.get(pos.condition_id)
                is_orphan = pos.condition_id not in active_markets

                # R-H2: Escalate when book is empty for aged positions
                if current_price is None and not is_orphan:
                    hold_hours = (now - pos.last_update) / 3600.0
                    if hold_hours > 8.0 and avg_price > 0:
                        # Emergency exit at 10% haircut after 8 hours with no book
                        logger.warning(
                            "empty_book_emergency_exit",
                            condition_id=pos.condition_id[:16],
                            hold_hours=f"{hold_hours:.1f}",
                        )
                        signals.append(SellSignal(
                            token_id=token_id,
                            condition_id=pos.condition_id,
                            size=inv_size,
                            price=round(avg_price * 0.90, 4),
                            urgency="high",
                            reason="empty_book_aged",
                        ))
                        continue
                    elif hold_hours > 4.0:
                        logger.warning(
                            "empty_book_aged_position",
                            condition_id=pos.condition_id[:16],
                            hold_hours=f"{hold_hours:.1f}",
                        )

                # 1. FLATTEN (highest priority)
                if flatten_active:
                    sig = self._build_flatten_signal(pos, token_id, inv_size, current_price)
                    if sig:
                        signals.append(sig)
                        continue  # Don't stack signals for this side

                # 2. STOP-LOSS
                if self.config.stop_loss.enabled and avg_price > 0 and current_price is not None:
                    sig = self._check_stop_loss(pos, token_id, inv_size, avg_price, current_price)
                    if sig:
                        signals.append(sig)
                        continue

                # 3. RESOLUTION
                if self.config.resolution.enabled and md and md.end_date:
                    sig = self._check_resolution(pos, token_id, inv_size, md, current_price)
                    if sig:
                        signals.append(sig)
                        continue

                # 4. TAKE-PROFIT
                if self.config.take_profit.enabled and avg_price > 0 and current_price is not None:
                    sig = self._check_take_profit(
                        pos, token_id, inv_size,
                        avg_price, current_price, now,
                    )
                    if sig:
                        signals.append(sig)
                        continue

                # 5. ORPHAN (only check periodically)
                if (
                    is_orphan and
                    now - self._last_orphan_check >= self.config.orphan.check_interval_s
                ):
                    sig = await self._check_orphan(pos, token_id, inv_size, current_price)
                    if sig:
                        signals.append(sig)

        # Update orphan check timestamp
        if now - self._last_orphan_check >= self.config.orphan.check_interval_s:
            self._last_orphan_check = now

        # Sort by priority: critical > high > medium > low
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        signals.sort(key=lambda s: priority_order.get(s.urgency, 4))

        return signals

    def get_resolution_action(
        self,
        condition_id: str,
        active_markets: dict[str, MarketMetadata],
    ) -> ResolutionAction | None:
        """Get current resolution exit phase for a market.

        Used by main loop to decide block_new_buys and gamma multiplier.
        """
        md = active_markets.get(condition_id)
        if not md or not md.end_date or not self.config.resolution.enabled:
            return None

        now = datetime.now(UTC)
        end = md.end_date
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)

        hours_left = (end - now).total_seconds() / 3600.0
        cfg = self.config.resolution

        action = ResolutionAction(hours_left=hours_left)

        if hours_left <= 0:
            action.action = "FORCE_EXIT"
            action.fraction = 1.0
            action.block_new_buys = True
        elif hours_left <= cfg.aggressive_after_hours:
            action.action = "AGGRESSIVE_EXIT"
            action.fraction = 1.0
            action.block_new_buys = True
        elif hours_left <= cfg.exit_complete_hours:
            action.action = "URGENT_EXIT"
            action.fraction = 1.0
            action.block_new_buys = True
        elif hours_left <= cfg.exit_start_hours:
            action.action = "GRADUAL_EXIT"
            action.fraction = 1.0 - (hours_left - cfg.exit_complete_hours) / (
                cfg.exit_start_hours - cfg.exit_complete_hours
            )
            action.block_new_buys = True
        elif hours_left <= cfg.block_new_buys_hours:
            action.action = "NO_NEW_BUYS"
            action.fraction = 0.0
            action.block_new_buys = True
        else:
            return None

        self._resolution_cache[condition_id] = action
        return action

    # ── Private checks ──

    def _is_flatten_active(self) -> bool:
        """Check if emergency flatten is triggered."""
        # File flag
        if os.path.exists(self.config.flatten.config_flag_path):
            return True
        # Kill switch
        if (
            self.kill_switch
            and hasattr(self.kill_switch, "is_triggered")
            and self.kill_switch.is_triggered
        ):
            return True
        return False

    def _build_flatten_signal(
        self,
        pos: MarketPosition,
        token_id: str,
        size: float,
        current_price: float | None,
    ) -> SellSignal | None:
        if size < 5.0:
            return None
        price = current_price
        if price and price > 0:
            # Accept up to tolerance% worse than current bid
            price = round(price * (1.0 - self.config.flatten.price_tolerance_pct), 4)
        return SellSignal(
            token_id=token_id,
            condition_id=pos.condition_id,
            size=size,
            price=price,
            urgency="critical",
            reason="flatten",
        )

    def _check_stop_loss(
        self,
        pos: MarketPosition,
        token_id: str,
        size: float,
        avg_price: float,
        current_price: float,
    ) -> SellSignal | None:
        cfg = self.config.stop_loss

        if avg_price <= 0:
            return None

        unrealized_pct = (current_price - avg_price) / avg_price
        unrealized_usd = size * (current_price - avg_price)

        # Hard stop: immediate full exit
        if unrealized_pct <= -cfg.hard_stop_pct or unrealized_usd <= -cfg.max_loss_per_trade_usd:
            logger.warning(
                "stop_loss_hard_triggered",
                condition_id=pos.condition_id[:16],
                unrealized_pct=f"{unrealized_pct:.2%}",
                unrealized_usd=f"${unrealized_usd:.2f}",
            )
            return SellSignal(
                token_id=token_id,
                condition_id=pos.condition_id,
                size=size,
                price=current_price,
                urgency="critical",
                reason="hard_stop",
            )

        # Soft stop: full exit at best bid
        if unrealized_pct <= -cfg.threshold_pct:
            logger.warning(
                "stop_loss_triggered",
                condition_id=pos.condition_id[:16],
                unrealized_pct=f"{unrealized_pct:.2%}",
            )
            return SellSignal(
                token_id=token_id,
                condition_id=pos.condition_id,
                size=size,
                price=current_price,
                urgency="high",
                reason="stop_loss",
            )

        return None

    def _check_resolution(
        self,
        pos: MarketPosition,
        token_id: str,
        size: float,
        md: MarketMetadata,
        current_price: float | None,
    ) -> SellSignal | None:
        if not md.end_date:
            return None

        now = datetime.now(UTC)
        end = md.end_date
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)

        hours_left = (end - now).total_seconds() / 3600.0
        cfg = self.config.resolution

        if hours_left <= 0:
            # Market ended — force exit at any price
            logger.warning("resolution_force_exit", condition_id=pos.condition_id[:16])
            price = current_price if current_price and current_price > 0 else None
            return SellSignal(
                token_id=token_id,
                condition_id=pos.condition_id,
                size=size,
                price=price,
                urgency="critical",
                reason="resolution",
            )

        if hours_left <= cfg.aggressive_after_hours:
            # Very close to resolution — cross the spread
            price = current_price
            if price and price > 0:
                price = round(price * 0.98, 4)  # Accept 2% slippage
            return SellSignal(
                token_id=token_id,
                condition_id=pos.condition_id,
                size=size,
                price=price,
                urgency="critical",
                reason="resolution",
            )

        if hours_left <= cfg.exit_complete_hours:
            # Must be flat — sell everything at best bid
            return SellSignal(
                token_id=token_id,
                condition_id=pos.condition_id,
                size=size,
                price=current_price,
                urgency="high",
                reason="resolution",
            )

        if hours_left <= cfg.exit_start_hours:
            # Gradual exit ramp
            denom = cfg.exit_start_hours - cfg.exit_complete_hours
            if denom <= 0:
                fraction = 1.0
            else:
                fraction = 1.0 - (
                    hours_left - cfg.exit_complete_hours
                ) / denom
            fraction = max(0.0, min(1.0, fraction))
            sell_size = max(5.0, round(size * fraction, 2))
            if sell_size > size:
                sell_size = size
            return SellSignal(
                token_id=token_id,
                condition_id=pos.condition_id,
                size=sell_size,
                price=current_price,
                urgency="medium",
                reason="resolution",
            )

        return None

    def _check_take_profit(
        self,
        pos: MarketPosition,
        token_id: str,
        size: float,
        avg_price: float,
        current_price: float,
        now: float,
    ) -> SellSignal | None:
        cfg = self.config.take_profit

        if avg_price <= 0:
            return None

        # Minimum hold time check
        hold_seconds = now - getattr(pos, 'created_at', pos.last_update)
        if hold_seconds < cfg.min_hold_minutes * 60:
            return None

        # Cooldown check
        last_tp = self._tp_cooldowns.get(pos.condition_id, 0.0)
        if now - last_tp < cfg.cooldown_minutes * 60:
            return None

        unrealized_pct = (current_price - avg_price) / avg_price

        # Full exit at higher threshold
        if unrealized_pct >= cfg.full_exit_pct:
            logger.info(
                "take_profit_full",
                condition_id=pos.condition_id[:16],
                unrealized_pct=f"{unrealized_pct:.2%}",
            )
            self._tp_cooldowns[pos.condition_id] = now
            return SellSignal(
                token_id=token_id,
                condition_id=pos.condition_id,
                size=size,
                price=current_price,
                urgency="high",
                reason="take_profit",
            )

        # Partial exit at lower threshold
        if unrealized_pct >= cfg.threshold_pct:
            partial_size = max(5.0, round(size * cfg.partial_exit_pct, 2))
            if partial_size > size:
                partial_size = size
            logger.info(
                "take_profit_partial",
                condition_id=pos.condition_id[:16],
                unrealized_pct=f"{unrealized_pct:.2%}",
                partial_size=partial_size,
            )
            self._tp_cooldowns[pos.condition_id] = now
            return SellSignal(
                token_id=token_id,
                condition_id=pos.condition_id,
                size=partial_size,
                price=current_price,
                urgency="medium",
                reason="take_profit",
            )

        return None

    async def _check_orphan(
        self,
        pos: MarketPosition,
        token_id: str,
        size: float,
        current_price: float | None,
    ) -> SellSignal | None:
        if size < self.config.orphan.min_size_to_unwind:
            return None

        # If we don't have a WS book price, try REST fallback
        price = current_price
        if price is None and self.clob_public is not None:
            try:
                rest_book = await self.clob_public.get_order_book(token_id)
                if rest_book and rest_book.bids:
                    price = float(rest_book.bids[0].price)
            except Exception as e:
                logger.debug("orphan_rest_book_failed", token_id=token_id[:16], error=str(e))

        if price is None or price <= 0:
            return None

        logger.info(
            "orphan_exit_signal",
            condition_id=pos.condition_id[:16],
            token_id=token_id[:16],
            size=size,
            price=price,
        )
        return SellSignal(
            token_id=token_id,
            condition_id=pos.condition_id,
            size=size,
            price=price,
            urgency="low",
            reason="orphan",
        )

    # ── TWAP Exit (PM-06) ──

    def compute_twap_exit(
        self,
        condition_id: str,
        total_size: float,
        urgency: str = "medium",
        n_slices: int = 5,
        interval_minutes: float = 2.0,
    ) -> list[dict]:
        """TWAP-style exit: slice large exits over time (PM-06).

        For non-critical exits with size > $10, distribute the exit
        into N child orders spaced over time to reduce market impact.

        Returns list of {size, delay_s, urgency} for each slice.
        """
        if urgency == "critical" or total_size < 10.0:
            return [{"size": total_size, "delay_s": 0, "urgency": urgency}]

        slices = []
        remaining = total_size
        for i in range(n_slices):
            slice_size = total_size / n_slices
            if i == n_slices - 1:
                slice_size = remaining  # Last slice gets remainder
            slices.append({
                "size": round(slice_size, 2),
                "delay_s": int(i * interval_minutes * 60),
                "urgency": urgency,
            })
            remaining -= slice_size

        return slices

    # ── Kelly-rational exit (KP-07) ──

    def get_kelly_exit_signal(
        self, condition_id: str, p_true: float, p_market: float,
        current_pnl_pct: float = 0.0,
    ) -> str | None:
        """Kelly-rational exit signal (KP-07)."""
        from pmm1.math.kelly import kelly_fraction_auto, kelly_growth_rate
        _, fraction = kelly_fraction_auto(p_true, p_market)
        growth = kelly_growth_rate(p_true, p_market)
        if growth < 0:
            return "kelly_sl"
        if fraction < 0.02:
            return "kelly_tp"
        return None

    # ── Helpers ──

    def _get_best_bid(self, token_id: str) -> float | None:
        """Get best bid price from WS book."""
        book = self.books.get(token_id)
        if book is None:
            return None
        level = book.get_best_bid()
        if level is None:
            return None
        return level.price_float if level.price_float > 0 else None
