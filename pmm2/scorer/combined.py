"""Combined EV scorer — V_{m,j} for each market-bundle pair.

V = E^spread + E^arb + E^liq + E^reb - C^tox - C^res - C^carry

Integrates all EV components and costs to compute marginal return per bundle.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from pmm1.storage.database import Database
from pmm2.queue.estimator import QueueEstimator
from pmm2.queue.hazard import FillHazard
from pmm2.scorer.arb_ev import compute_arb_ev
from pmm2.scorer.bundles import QuoteBundle, generate_bundles
from pmm2.scorer.rebate_ev import compute_rebate_ev
from pmm2.scorer.resolution import compute_resolution_cost
from pmm2.scorer.reward_ev import compute_reward_ev
from pmm2.scorer.spread_ev import compute_spread_ev
from pmm2.scorer.toxicity import compute_toxicity
from pmm2.universe.metadata import EnrichedMarket

logger = structlog.get_logger(__name__)


class MarketEVScorer:
    """Combined EV scorer — V_{m,j} for each market-bundle pair."""

    def __init__(
        self,
        db: Database,
        fill_hazard: FillHazard,
        queue_estimator: QueueEstimator,
    ) -> None:
        """Initialize scorer.

        Args:
            db: database connection
            fill_hazard: fill hazard calculator
            queue_estimator: queue position estimator
        """
        self.db = db
        self.fill_hazard = fill_hazard
        self.queue_estimator = queue_estimator

        # Cache toxicity per market (expires after 1 hour)
        self._toxicity_cache: dict[str, tuple[float, float]] = {}  # condition_id -> (tox, ts)

    async def score_bundle(
        self,
        market: EnrichedMarket,
        bundle: QuoteBundle,
        reservation_price: float,
        nav: float,
    ) -> QuoteBundle:
        """Score a single bundle, filling all EV fields and computing total_value.

        Args:
            market: enriched market metadata
            bundle: quote bundle to score
            reservation_price: our fair value / reservation price
            nav: current bot NAV in USDC

        Returns:
            Updated bundle with all EV fields filled
        """
        # --- 1. Get fill probabilities from hazard ---
        # Use 30-second horizon for order scoring
        horizon_sec = 30.0

        # Get depletion rates for tokens
        depletion_bid = self.fill_hazard.get_depletion_rate(market.token_id_yes)
        depletion_ask = self.fill_hazard.get_depletion_rate(market.token_id_no)

        # Estimate queue ahead (rough proxy: visible depth at price level)
        # For now, assume we're at the front (queue_ahead = 0)
        # In practice, this would come from QueueEstimator
        queue_ahead_bid = 0.0
        queue_ahead_ask = 0.0

        fill_prob_bid = self.fill_hazard.fill_probability(
            queue_ahead=queue_ahead_bid,
            order_size=bundle.bid_size,
            horizon_sec=horizon_sec,
            depletion_rate=depletion_bid,
        )

        fill_prob_ask = self.fill_hazard.fill_probability(
            queue_ahead=queue_ahead_ask,
            order_size=bundle.ask_size,
            horizon_sec=horizon_sec,
            depletion_rate=depletion_ask,
        )

        # --- 2. Compute each EV component ---
        # Spread EV
        bundle.spread_ev = compute_spread_ev(
            bundle=bundle,
            fill_prob_bid=fill_prob_bid,
            fill_prob_ask=fill_prob_ask,
            reservation_price=reservation_price,
        )

        # Arbitrage EV (per-market, not per-bundle)
        bundle.arb_ev = compute_arb_ev(market=market)

        # Liquidity reward EV
        # Estimate competitor mass from liquidity (rough proxy)
        q_others = max(market.liquidity * 0.1, 50.0)  # 10% of liquidity or min 50
        bundle.liq_ev = compute_reward_ev(
            market=market,
            bundle=bundle,
            q_others=q_others,
            p_scoring=0.85,
            h_allocator=1.0,
            t_epoch=24.0,
        )

        # Rebate EV
        # Estimate fills per hour from volume (rough proxy)
        fills_per_hour = max(market.volume_24h / 24.0 / 100.0, 0.1)  # conservative
        total_depth = max(market.liquidity, 100.0)
        bundle.rebate_ev = compute_rebate_ev(
            market=market,
            bundle=bundle,
            expected_fills_per_hour=fills_per_hour,
            total_market_depth=total_depth,
        )

        # --- 3. Compute costs ---
        # Toxicity cost
        bundle.tox_cost = await self._get_toxicity(market.condition_id)

        # Resolution cost
        bundle.res_cost = compute_resolution_cost(market=market)

        # Carry cost (capital * carry_rate * time_fraction)
        # Assuming 0.5% daily carry rate (conservative for crypto)
        carry_rate_daily = 0.005
        time_fraction = 1.0 / 24.0  # 1 hour horizon
        bundle.carry_cost = bundle.capital_usdc * carry_rate_daily * time_fraction

        # --- 4. Total value ---
        bundle.total_value = (
            bundle.spread_ev
            + bundle.arb_ev
            + bundle.liq_ev
            + bundle.rebate_ev
            - bundle.tox_cost
            - bundle.res_cost
            - bundle.carry_cost
        )

        # --- 5. Marginal return ---
        if bundle.capital_usdc > 0:
            bundle.marginal_return = bundle.total_value / bundle.capital_usdc
        else:
            bundle.marginal_return = 0.0

        logger.info(
            "bundle_scored",
            condition_id=bundle.market_condition_id,
            bundle=bundle.bundle_type,
            spread_ev=bundle.spread_ev,
            liq_ev=bundle.liq_ev,
            rebate_ev=bundle.rebate_ev,
            arb_ev=bundle.arb_ev,
            tox_cost=bundle.tox_cost,
            res_cost=bundle.res_cost,
            carry_cost=bundle.carry_cost,
            total_value=bundle.total_value,
            marginal_return=bundle.marginal_return,
        )

        return bundle

    async def score_market(
        self,
        market: EnrichedMarket,
        nav: float,
        reservation_price: float | None = None,
        min_order_size: float = 5.0,
    ) -> list[QuoteBundle]:
        """Generate and score all bundles for a market.

        Args:
            market: enriched market metadata
            nav: current bot NAV in USDC
            reservation_price: our fair value (default: market mid)

        Returns:
            Scored bundles sorted by marginal_return desc
        """
        # Default reservation price = mid
        if reservation_price is None:
            reservation_price = market.mid

        # --- 1. Generate bundles ---
        bundles = generate_bundles(market=market, nav=nav, min_order_size=min_order_size)

        if not bundles:
            logger.debug(
                "no_bundles_generated",
                condition_id=market.condition_id,
            )
            return []

        # --- 2. Score each bundle ---
        scored_bundles = []
        for bundle in bundles:
            scored = await self.score_bundle(
                market=market,
                bundle=bundle,
                reservation_price=reservation_price,
                nav=nav,
            )
            scored_bundles.append(scored)

        # --- 3. Sort by marginal return desc ---
        scored_bundles.sort(key=lambda b: b.marginal_return, reverse=True)

        logger.info(
            "market_scored",
            condition_id=market.condition_id,
            bundle_count=len(scored_bundles),
            best_return=scored_bundles[0].marginal_return if scored_bundles else 0.0,
        )

        return scored_bundles

    async def persist_scores(self, scored_bundles: list[QuoteBundle]) -> None:
        """Write scores to market_score table.

        Args:
            scored_bundles: list of scored bundles
        """
        if not scored_bundles:
            return

        ts_iso = datetime.now(timezone.utc).isoformat()

        rows = []
        for bundle in scored_bundles:
            # Convert to basis points for storage (easier to read)
            def to_bps(val: float) -> float:
                return val * 10000

            rows.append(
                (
                    ts_iso,
                    bundle.market_condition_id,
                    bundle.bundle_type,
                    to_bps(bundle.spread_ev),
                    to_bps(bundle.arb_ev),
                    to_bps(bundle.liq_ev),
                    to_bps(bundle.rebate_ev),
                    to_bps(bundle.tox_cost),
                    to_bps(bundle.res_cost),
                    to_bps(bundle.carry_cost),
                    to_bps(bundle.marginal_return),
                    bundle.capital_usdc,
                    0,  # allocator_rank (filled by allocator later)
                )
            )

        await self.db.execute_many(
            """
            INSERT INTO market_score (
                ts, condition_id, bundle,
                spread_ev_bps, arb_ev_bps, liq_ev_bps, rebate_ev_bps,
                tox_cost_bps, res_cost_bps, carry_cost_bps,
                marginal_return_bps, target_capital_usdc, allocator_rank
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

        logger.info("scores_persisted", count=len(rows))

    async def _get_toxicity(self, condition_id: str) -> float:
        """Get toxicity from cache or compute fresh.

        Cache expires after 1 hour.

        Args:
            condition_id: market condition ID

        Returns:
            Toxicity cost
        """
        import time

        now = time.time()
        cache_ttl = 3600  # 1 hour

        if condition_id in self._toxicity_cache:
            tox, ts = self._toxicity_cache[condition_id]
            if now - ts < cache_ttl:
                return tox

        # Compute fresh
        tox = await compute_toxicity(
            db=self.db,
            condition_id=condition_id,
            lookback_hours=24,
        )

        self._toxicity_cache[condition_id] = (tox, now)
        return tox
