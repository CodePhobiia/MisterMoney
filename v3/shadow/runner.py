"""
V3 Shadow Runner
Runs V3 signal pipeline on live markets without affecting V1 decisions
"""

import asyncio
from datetime import datetime

import structlog

from v3.evidence.db import Database
from v3.evidence.entities import FairValueSignal, RoutePlan
from v3.evidence.graph import EvidenceGraph
from v3.evidence.normalizer import EvidenceNormalizer
from v3.intake.evidence_collector import EvidenceCollector
from v3.intake.gamma_sync import GammaSync
from v3.intake.schemas import MarketMeta
from v3.intake.source_registry import SourceRegistry
from v3.providers.registry import ProviderRegistry
from v3.routing.change_detector import ChangeDetector
from v3.routing.orchestrator import RouteOrchestrator

from .logger import ShadowLogger
from .metrics import BrierScoreTracker, LatencyTracker

log = structlog.get_logger()


class ShadowRunner:
    """
    Runs V3 signal pipeline on live markets without affecting V2 decisions.

    Every cycle:
    1. Fetch top markets from Gamma API
    2. Classify each market (numeric/simple/rule/dossier)
    3. Check change detector — skip if no refresh needed
    4. Run appropriate route → get FairValueSignal
    5. Log signal + counterfactual to JSONL + DB
    6. Never touches V1 execution
    """

    def __init__(
        self,
        db: Database,
        registry: ProviderRegistry,
        evidence_graph: EvidenceGraph,
        config: dict
    ):
        """
        Initialize shadow runner

        Args:
            db: Database instance
            registry: Provider registry
            evidence_graph: Evidence graph instance
            config: Configuration dict
        """
        self.db = db
        self.registry = registry
        self.evidence_graph = evidence_graph
        self.config = config

        # Initialize components
        self.gamma_sync = GammaSync()
        self.source_registry = SourceRegistry()
        self.orchestrator = RouteOrchestrator(registry, evidence_graph, db)
        self.change_detector = ChangeDetector(db)

        # Initialize evidence collection
        self.evidence_collector = EvidenceCollector(
            evidence_graph=evidence_graph,
            normalizer=EvidenceNormalizer()
        )

        # Initialize logging and metrics
        self.logger = ShadowLogger(config.get("log_dir", "data/v3/shadow"))
        self.brier_tracker = BrierScoreTracker(db)
        self.latency_tracker = LatencyTracker()

        log.info("shadow_runner_initialized")

    async def run_cycle(self) -> dict:
        """
        One shadow evaluation cycle.

        Returns:
            Summary: {markets_evaluated, signals_generated, errors, latency_ms}
        """
        cycle_start = datetime.utcnow()

        log.info("shadow_cycle_start")

        markets_evaluated = 0
        signals_generated = 0
        errors = 0

        try:
            # 1. Fetch top markets from Gamma API
            limit = self.config.get("market_limit", 50)
            markets = await self.gamma_sync.sync_markets(limit=limit)

            log.info("gamma_markets_fetched", count=len(markets))

            # 2. Evaluate each market
            for market in markets:
                try:
                    await self._evaluate_market(market)
                    markets_evaluated += 1
                    signals_generated += 1

                except Exception as e:
                    log.error("market_evaluation_failed",
                             condition_id=market.condition_id,
                             error=str(e))

                    # Log error
                    await self.logger.log_error(
                        condition_id=market.condition_id,
                        route="unknown",
                        error=str(e),
                        market_meta={
                            "question": market.question,
                            "volume_24h": market.volume_24h,
                        }
                    )

                    errors += 1

        except Exception as e:
            log.error("shadow_cycle_failed", error=str(e))
            errors += 1

        # Calculate cycle duration
        cycle_end = datetime.utcnow()
        cycle_duration_ms = (cycle_end - cycle_start).total_seconds() * 1000

        summary = {
            "markets_evaluated": markets_evaluated,
            "signals_generated": signals_generated,
            "errors": errors,
            "latency_ms": cycle_duration_ms,
            "timestamp": cycle_end.isoformat(),
        }

        log.info("shadow_cycle_complete", **summary)

        return summary

    async def _evaluate_market(self, market: MarketMeta) -> None:
        """
        Evaluate a single market through the V3 pipeline

        Args:
            market: Market metadata from Gamma
        """
        eval_start = datetime.utcnow()

        # 1. Classify market
        route = self.source_registry.classify_market(
            question=market.question,
            rules=market.rules,
            source=market.resolution_source
        )

        log.debug("market_classified",
                 condition_id=market.condition_id,
                 route=route)

        # 2. Check if refresh needed
        change_event = await self.change_detector.needs_refresh(
            condition_id=market.condition_id,
            market=market
        )

        if change_event is None:
            log.debug("market_refresh_skipped",
                     condition_id=market.condition_id,
                     reason="no_change_detected")
            return

        log.info("market_refresh_needed",
                condition_id=market.condition_id,
                event_type=change_event.event_type)

        # 3. Collect fresh evidence if needed
        existing_evidence = await self.evidence_graph.get_evidence_bundle(
            condition_id=market.condition_id,
            max_items=5
        )

        if len(existing_evidence) == 0:
            # No evidence yet — collect some
            log.info("collecting_evidence", condition_id=market.condition_id)
            try:
                collected = await self.evidence_collector.collect_for_market(market)
                log.info("evidence_collected",
                        condition_id=market.condition_id,
                        count=len(collected))
            except Exception as e:
                log.warning("evidence_collection_failed",
                          condition_id=market.condition_id,
                          error=str(e))

        # 4. Fetch full evidence bundle (now includes freshly collected items)
        evidence = await self.evidence_graph.get_evidence_bundle(
            condition_id=market.condition_id,
            max_items=50
        )

        # 5. Build route plan
        plan = RoutePlan(
            condition_id=market.condition_id,
            route=route,
            priority=0,
            reason=f"Classified as {route} via source registry"
        )

        # 6. Execute route
        try:
            signal = await self.orchestrator.execute(
                plan=plan,
                market=market,
                evidence=evidence,
                rule_text=market.rules
            )

            eval_end = datetime.utcnow()
            eval_duration_ms = (eval_end - eval_start).total_seconds() * 1000

            log.info("market_evaluated",
                    condition_id=market.condition_id,
                    route=route,
                    p_calibrated=signal.p_calibrated,
                    uncertainty=signal.uncertainty,
                    latency_ms=eval_duration_ms)

            # 7. Record latency metrics
            for model in signal.models_used:
                self.latency_tracker.record(
                    route=route,
                    provider=model,
                    latency_ms=eval_duration_ms
                )

            # 8. Log signal
            await self._log_signal(signal, market, eval_duration_ms)

            # 9. Record prediction for Brier scoring
            await self.brier_tracker.record_prediction(
                condition_id=market.condition_id,
                route=route,
                p_predicted=signal.p_calibrated,
                timestamp=eval_end
            )

        except Exception as e:
            log.error("route_execution_failed",
                     condition_id=market.condition_id,
                     route=route,
                     error=str(e))

            await self.logger.log_error(
                condition_id=market.condition_id,
                route=route,
                error=str(e),
                market_meta={
                    "question": market.question,
                    "volume_24h": market.volume_24h,
                }
            )

            raise

    async def _log_signal(
        self,
        signal: FairValueSignal,
        market: MarketMeta,
        latency_ms: float
    ) -> None:
        """
        Log V3 signal with V1 counterfactual

        Args:
            signal: V3 signal generated
            market: Market metadata
            latency_ms: Evaluation latency
        """
        # V1 fair value = book midpoint (simple)
        v1_fair_value = market.current_mid

        # Token usage (approximate from models_used)
        token_usage = {
            model: 1000  # Placeholder — actual tracking would need provider stats
            for model in signal.models_used
        }

        # Log to JSONL
        await self.logger.log_signal(
            signal=signal,
            market_meta={
                "question": market.question,
                "volume_24h": market.volume_24h,
                "current_mid": market.current_mid,
                "spread_cents": None,  # Not available in MarketMeta
            },
            v1_fair_value=v1_fair_value,
            latency_ms=latency_ms,
            token_usage=token_usage
        )

    async def run_loop(self, interval_seconds: int = 300) -> None:
        """
        Main shadow loop — runs every 5 minutes.
        Catches all exceptions (never crashes).
        Logs cycle summary.

        Args:
            interval_seconds: Time between cycles (default 300 = 5 min)
        """
        log.info("shadow_loop_starting", interval_seconds=interval_seconds)

        cycle_count = 0

        while True:
            try:
                cycle_count += 1

                log.info("shadow_loop_cycle", cycle=cycle_count)

                # Run cycle
                summary = await self.run_cycle()

                log.info("shadow_loop_cycle_complete",
                        cycle=cycle_count,
                        **summary)

                # Wait for next cycle
                await asyncio.sleep(interval_seconds)

            except asyncio.CancelledError:
                log.warning("shadow_loop_cancelled")
                break

            except Exception as e:
                log.error("shadow_loop_error",
                         cycle=cycle_count,
                         error=str(e))

                # Don't crash — wait and retry
                await asyncio.sleep(interval_seconds)

    async def close(self) -> None:
        """Close all connections and cleanup"""
        log.info("shadow_runner_closing")

        await self.gamma_sync.close()

        log.info("shadow_runner_closed")
