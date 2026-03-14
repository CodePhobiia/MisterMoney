"""Feature engine — compute microprice, imbalance, trade flow, volatility, time-to-resolution.

Produces a FeatureVector for each market, used by fair_value and quote_engine.
"""

from __future__ import annotations

import math
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel

from pmm1.state.books import OrderBook

logger = structlog.get_logger(__name__)


class FeatureVector(BaseModel):
    """Complete feature set for a single market at a point in time."""

    # Core book features
    midpoint: float = 0.5
    microprice: float = 0.5
    imbalance: float = 0.0  # [-1, 1]
    spread: float = 0.0
    spread_cents: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 1.0
    bid_depth_2c: float = 0.0
    ask_depth_2c: float = 0.0

    # Flow features
    signed_trade_flow: float = 0.0  # F_t: positive = buy pressure
    trade_intensity: float = 0.0  # trades per second
    sweep_intensity: float = 0.0  # large trade intensity

    # MM-04+PM-04: VPIN toxicity
    vpin: float = 0.0
    toxicity_level: str = "normal"  # normal, elevated, toxic

    # Volatility
    realized_vol: float = 0.0  # V_t: short-horizon realized volatility
    sigma_eff: float = 0.25  # MM-02: binary-appropriate volatility
    kappa_estimate: float = 0.1  # MM-03 placeholder: adverse-selection intensity
    vol_regime: str = "normal"  # low, normal, high, extreme

    # PM-11: Multi-timeframe volatility
    vol_5m: float = 0.0
    vol_1h: float = 0.0
    vol_24h: float = 0.0
    vol_ratio_short_long: float = 1.0  # short/long vol ratio for regime detection

    # Time features
    time_to_resolution_hours: float = float("inf")
    time_to_resolution_fraction: float = 0.0  # 0 = just started, 1 = about to resolve

    # PM-03: Time-of-day spread regime
    hour_of_day: int = 12
    tod_spread_mult: float = 1.0
    tod_size_mult: float = 1.0

    # PM-07: Maker rebate optimization
    fee_market: bool = False
    rebate_spread_discount: float = 0.0

    # Related market features
    related_market_residual: float = 0.0  # R_t

    # External signal (placeholder for v1)
    external_signal: float = 0.0  # E_t

    # Meta
    token_id: str = ""
    condition_id: str = ""
    timestamp: float = 0.0
    is_stale: bool = False

    @property
    def logit_midpoint(self) -> float:
        """logit(m_t) — used in fair value model."""
        clamped = max(0.001, min(0.999, self.midpoint))
        return math.log(clamped / (1 - clamped))

    @property
    def logit_microprice(self) -> float:
        """logit(μ_t)."""
        clamped = max(0.001, min(0.999, self.microprice))
        return math.log(clamped / (1 - clamped))


class TradeAccumulator:
    """Accumulates recent trades for flow and volatility computation."""

    def __init__(self, window_s: float = 60.0, max_trades: int = 1000) -> None:
        self._window_s = window_s
        self._trades: deque[dict[str, Any]] = deque(maxlen=max_trades)
        self._prices: deque[tuple[float, float]] = deque(maxlen=max_trades)  # (timestamp, price)

        # MM-04+PM-04: VPIN volume bars
        self._volume_bars: list[tuple[float, float]] = []  # (buy_vol, sell_vol)
        self._current_bar_buy: float = 0.0
        self._current_bar_sell: float = 0.0
        self._current_bar_total: float = 0.0
        self._volume_bar_size: float = 100.0  # $100 per bar
        self._vpin_window: int = 20  # bars

    def add_trade(
        self,
        price: float,
        size: float,
        side: str,
        timestamp: float | None = None,
    ) -> None:
        """Record a trade."""
        ts = timestamp or time.time()
        self._trades.append({
            "price": price,
            "size": size,
            "side": side,
            "timestamp": ts,
        })
        self._prices.append((ts, price))

        # MM-04+PM-04: VPIN — accumulate volume into bars
        dollar_vol = price * size
        if side == "BUY":
            self._current_bar_buy += dollar_vol
        else:
            self._current_bar_sell += dollar_vol
        self._current_bar_total += dollar_vol

        # Complete bar when threshold reached
        if self._current_bar_total >= self._volume_bar_size:
            self._volume_bars.append((self._current_bar_buy, self._current_bar_sell))
            self._current_bar_buy = 0.0
            self._current_bar_sell = 0.0
            self._current_bar_total = 0.0
            # Keep last N bars
            if len(self._volume_bars) > self._vpin_window * 2:
                self._volume_bars = self._volume_bars[-self._vpin_window:]

    def _prune(self, now: float | None = None) -> None:
        """Remove trades outside the window."""
        cutoff = (now or time.time()) - self._window_s
        while self._trades and self._trades[0]["timestamp"] < cutoff:
            self._trades.popleft()
        while self._prices and self._prices[0][0] < cutoff:
            self._prices.popleft()

    def get_signed_flow(self) -> float:
        """Compute signed trade flow F_t.

        F_t = Σ(size_i · sign_i) where sign = +1 for BUY, -1 for SELL
        Normalized by total volume.
        """
        self._prune()
        if not self._trades:
            return 0.0

        signed_volume = 0.0
        total_volume = 0.0
        for t in self._trades:
            sign = 1.0 if t["side"] == "BUY" else -1.0
            signed_volume += t["size"] * sign
            total_volume += t["size"]

        if total_volume == 0:
            return 0.0
        return signed_volume / total_volume  # Normalized to [-1, 1]

    def get_trade_intensity(self) -> float:
        """Trades per second in the window."""
        self._prune()
        if len(self._trades) < 2:
            return 0.0
        duration = float(self._trades[-1]["timestamp"] - self._trades[0]["timestamp"])
        if duration <= 0:
            return 0.0
        return float(len(self._trades) / duration)

    def get_sweep_intensity(self, size_threshold: float = 500.0) -> float:
        """Large trade (sweep) intensity — fraction of volume from large trades."""
        self._prune()
        if not self._trades:
            return 0.0

        total_vol = float(sum(t["size"] for t in self._trades))
        sweep_vol = float(sum(t["size"] for t in self._trades if t["size"] >= size_threshold))

        if total_vol == 0:
            return 0.0
        return float(sweep_vol / total_vol)

    def get_realized_volatility(self) -> float:
        """Short-horizon realized volatility from trade prices.

        Uses log returns of consecutive prices.
        """
        self._prune()
        if len(self._prices) < 3:
            return 0.0

        log_returns = []
        prices = list(self._prices)
        for i in range(1, len(prices)):
            p0 = prices[i - 1][1]
            p1 = prices[i][1]
            if p0 > 0 and p1 > 0:
                log_returns.append(math.log(p1 / p0))

        if len(log_returns) < 2:
            return 0.0

        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        return math.sqrt(variance)

    def get_realized_volatility_windowed(self, window_seconds: float = 300.0) -> float:
        """Realized vol over a specific time window.

        PM-11: Multi-timeframe volatility using log-return std over a
        caller-specified window (e.g. 300s for 5m, 3600s for 1h).
        """
        now = time.time()
        cutoff = now - window_seconds
        recent = [(ts, p) for ts, p in self._prices if ts >= cutoff]

        if len(recent) < 3:
            return 0.0

        log_returns = []
        for i in range(1, len(recent)):
            p0 = recent[i - 1][1]
            p1 = recent[i][1]
            if p0 > 0 and p1 > 0:
                log_returns.append(math.log(p1 / p0))

        if len(log_returns) < 2:
            return 0.0

        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        return math.sqrt(variance)

    def get_vpin(self) -> float:
        """Volume-Synchronized Probability of Informed Trading.

        VPIN = avg(|V_buy - V_sell| / V_total) over rolling volume bars.
        Range: [0, 1]. Higher = more toxic flow.
        """
        bars = self._volume_bars[-self._vpin_window:]
        if len(bars) < 3:
            return 0.0

        vpins = []
        for buy_vol, sell_vol in bars:
            total = buy_vol + sell_vol
            if total > 0:
                vpins.append(abs(buy_vol - sell_vol) / total)

        return sum(vpins) / len(vpins) if vpins else 0.0


class FeatureEngine:
    """Computes feature vectors for all tracked markets."""

    def __init__(self, trade_window_s: float = 60.0) -> None:
        self._trade_accumulators: dict[str, TradeAccumulator] = {}
        self._trade_window_s = trade_window_s
        self._kappa_ema: dict[str, float] = {}  # MM-03: per-token kappa EMA

    def get_accumulator(self, token_id: str) -> TradeAccumulator:
        """Get or create a trade accumulator for a token."""
        if token_id not in self._trade_accumulators:
            self._trade_accumulators[token_id] = TradeAccumulator(self._trade_window_s)
        return self._trade_accumulators[token_id]

    def record_trade(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        timestamp: float | None = None,
    ) -> None:
        """Record a trade for feature computation."""
        acc = self.get_accumulator(token_id)
        acc.add_trade(price, size, side, timestamp)

    def compute(
        self,
        token_id: str,
        book: OrderBook | None,
        condition_id: str = "",
        end_date: datetime | None = None,
        related_residual: float = 0.0,
        external_signal: float = 0.0,
        fee_market: bool = False,
    ) -> FeatureVector:
        """Compute full feature vector for a market.

        Args:
            token_id: The asset token ID.
            book: Current order book state.
            condition_id: Market condition ID.
            end_date: Market end date for time features.
            related_residual: R_t from related markets.
            external_signal: E_t from external sources.
            fee_market: PM-07 — whether this market charges maker fees (enables rebate).
        """
        now = time.time()
        now_dt = datetime.now(UTC)
        acc = self.get_accumulator(token_id)

        # Book features
        midpoint = 0.5
        microprice = 0.5
        imbalance = 0.0
        spread = 0.0
        best_bid = 0.0
        best_ask = 1.0
        bid_depth_2c = 0.0
        ask_depth_2c = 0.0
        is_stale = True

        if book is not None:
            mid = book.get_midpoint()
            if mid is not None:
                midpoint = mid

            micro = book.get_microprice()
            if micro is not None:
                microprice = micro

            imbalance = book.get_imbalance()

            sp = book.get_spread()
            if sp is not None:
                spread = sp

            bb = book.get_best_bid()
            if bb is not None:
                best_bid = bb.price_float

            ba = book.get_best_ask()
            if ba is not None:
                best_ask = ba.price_float

            bid_depth_2c = book.get_depth_within(2.0, "bid")
            ask_depth_2c = book.get_depth_within(2.0, "ask")
            is_stale = book.is_stale

        # Flow features
        signed_trade_flow = acc.get_signed_flow()
        trade_intensity = acc.get_trade_intensity()
        sweep_intensity = acc.get_sweep_intensity()

        # MM-04+PM-04: VPIN toxicity
        vpin = acc.get_vpin()
        if vpin > 0.6:
            toxicity_level = "toxic"
        elif vpin > 0.3:
            toxicity_level = "elevated"
        else:
            toxicity_level = "normal"

        # Volatility
        realized_vol = acc.get_realized_volatility()

        # PM-11: Multi-timeframe volatility
        vol_5m = acc.get_realized_volatility_windowed(300)
        vol_1h = acc.get_realized_volatility_windowed(3600)
        vol_24h = realized_vol  # existing (uses all trades in window)
        vol_ratio_short_long = vol_5m / max(0.0001, vol_1h) if vol_1h > 0 else 1.0

        # MM-02: Binary-appropriate volatility
        # Bernoulli variance p*(1-p) is the correct variance for binary outcomes
        # Blend with realized vol for robustness against non-binary price dynamics
        bernoulli_var = midpoint * (1.0 - midpoint)
        sigma_eff = (0.7 * bernoulli_var + 0.3 * min(realized_vol ** 2, 0.25)) ** 0.5

        # Derive vol_regime from sigma_eff instead of raw log-return vol
        # sigma_eff ranges: 0 (at p=0 or p=1) to 0.5 (at p=0.5)
        if sigma_eff < 0.15:
            vol_regime = "low"
        elif sigma_eff < 0.30:
            vol_regime = "normal"
        elif sigma_eff < 0.42:
            vol_regime = "high"
        else:
            vol_regime = "extreme"

        # MM-03: Dynamic kappa (order arrival rate) estimation
        # kappa = alpha * trade_intensity + beta * avg_book_depth
        alpha_kappa = 0.5
        beta_kappa = 0.01
        raw_kappa = alpha_kappa * trade_intensity + beta_kappa * (bid_depth_2c + ask_depth_2c) / 2.0
        # EMA with 5-minute halflife (~300 observations at 1/sec)
        ema_decay = 0.997  # exp(-1/300)
        prev_kappa = self._kappa_ema.get(token_id, raw_kappa)
        kappa_ema = ema_decay * prev_kappa + (1 - ema_decay) * raw_kappa
        self._kappa_ema[token_id] = kappa_ema
        kappa_estimate = max(0.01, kappa_ema)

        # Time features
        time_to_resolution_hours = float("inf")
        time_to_resolution_fraction = 0.0
        if end_date is not None:
            # Gamma may provide naive datetimes; normalize to UTC-aware for safe subtraction.
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=UTC)
            delta = (end_date - now_dt).total_seconds()
            time_to_resolution_hours = max(0, delta / 3600)

            # Assume market existed for at least 7 days for fraction calc
            total_life = 7 * 24  # hours
            elapsed = total_life - time_to_resolution_hours
            time_to_resolution_fraction = max(0.0, min(1.0, elapsed / total_life))

        # PM-03: Time-of-day regime
        hour = now_dt.hour
        if 21 <= hour or hour < 4:
            # Low liquidity: 21:00-04:00 UTC — widen spread, reduce size
            tod_spread_mult = 1.5
            tod_size_mult = 0.6
        elif 14 <= hour < 21:
            # Peak hours: 14:00-20:00 UTC — normal
            tod_spread_mult = 1.0
            tod_size_mult = 1.0
        else:
            # Off-peak: 04:00-14:00 UTC — slight adjustment
            tod_spread_mult = 1.2
            tod_size_mult = 0.8

        # PM-07: Maker rebate — tighten spread by rebate amount on fee markets
        rebate_spread_discount = 0.0
        if fee_market:
            rebate_spread_discount = 0.001  # 10 bps = 0.1 cents

        return FeatureVector(
            midpoint=midpoint,
            microprice=microprice,
            imbalance=imbalance,
            spread=spread,
            spread_cents=spread * 100,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_depth_2c=bid_depth_2c,
            ask_depth_2c=ask_depth_2c,
            signed_trade_flow=signed_trade_flow,
            trade_intensity=trade_intensity,
            sweep_intensity=sweep_intensity,
            vpin=vpin,
            toxicity_level=toxicity_level,
            realized_vol=realized_vol,
            sigma_eff=sigma_eff,
            kappa_estimate=kappa_estimate,  # MM-03: dynamic kappa
            vol_regime=vol_regime,
            vol_5m=vol_5m,
            vol_1h=vol_1h,
            vol_24h=vol_24h,
            vol_ratio_short_long=vol_ratio_short_long,
            time_to_resolution_hours=time_to_resolution_hours,
            time_to_resolution_fraction=time_to_resolution_fraction,
            hour_of_day=hour,
            tod_spread_mult=tod_spread_mult,
            tod_size_mult=tod_size_mult,
            fee_market=fee_market,
            rebate_spread_discount=rebate_spread_discount,
            related_market_residual=related_residual,
            external_signal=external_signal,
            token_id=token_id,
            condition_id=condition_id,
            timestamp=now,
            is_stale=is_stale,
        )
