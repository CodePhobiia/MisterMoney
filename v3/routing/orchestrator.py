"""
V3 Route Orchestrator
Dispatches markets to appropriate routes based on classification
"""

import asyncio
from datetime import datetime, timedelta

import structlog

from v3.evidence.db import Database
from v3.evidence.entities import (
    EvidenceItem,
    FairValueSignal,
    RoutePlan,
)
from v3.evidence.graph import EvidenceGraph
from v3.intake.schemas import MarketMeta
from v3.providers.registry import ProviderRegistry
from v3.routes.dossier import DossierRoute
from v3.routes.rule_heavy import RuleHeavyRoute
from v3.routes.simple import SimpleRoute

log = structlog.get_logger()


# Route-specific SLA timeouts (in seconds)
ROUTE_TIMEOUTS = {
    "numeric": 10.0,
    "simple": 20.0,
    "rule": 30.0,
    "dossier": 60.0,
}


class RouteOrchestrator:
    """Dispatches markets to appropriate routes based on classification"""

    def __init__(self,
                 registry: ProviderRegistry,
                 evidence_graph: EvidenceGraph,
                 db: Database):
        """
        Initialize route orchestrator

        Args:
            registry: Provider registry instance
            evidence_graph: Evidence graph instance
            db: Database instance
        """
        self.registry = registry
        self.evidence_graph = evidence_graph
        self.db = db

        # Initialize routes
        self.simple_route = SimpleRoute(registry, evidence_graph)
        self.rule_route = RuleHeavyRoute(registry, evidence_graph)
        self.dossier_route = DossierRoute(registry, evidence_graph)
        # TODO: Initialize numeric route when ready

    async def execute(self,
                     plan: RoutePlan,
                     market: MarketMeta,
                     evidence: list[EvidenceItem],
                     rule_text: str) -> FairValueSignal:
        """
        Routes to numeric/simple/rule/dossier based on plan.route.
        Enforces SLA timeouts per route.
        On timeout: return last cached signal or neutral.

        Args:
            plan: Route plan with route selection
            market: Market metadata
            evidence: List of evidence items
            rule_text: Resolution rules text

        Returns:
            FairValueSignal from the appropriate route
        """
        log.info("route_orchestrator_execute",
                condition_id=plan.condition_id,
                route=plan.route,
                priority=plan.priority)

        # Get timeout for this route
        timeout_seconds = ROUTE_TIMEOUTS.get(plan.route, 30.0)

        # Dispatch to appropriate route with timeout
        try:
            signal = await self._execute_with_timeout(
                self._dispatch_route(plan, market, evidence, rule_text),
                timeout_seconds=timeout_seconds,
                condition_id=plan.condition_id
            )
            return signal

        except TimeoutError:
            log.warning("route_timeout",
                       condition_id=plan.condition_id,
                       route=plan.route,
                       timeout_seconds=timeout_seconds)

            # Try to get last cached signal
            cached = await self._get_cached_signal(plan.condition_id)
            if cached:
                log.info("using_cached_signal", condition_id=plan.condition_id)
                return cached

            # Return neutral signal as fallback
            return self._neutral_signal(plan.condition_id, plan.route)

    async def _dispatch_route(self,
                             plan: RoutePlan,
                             market: MarketMeta,
                             evidence: list[EvidenceItem],
                             rule_text: str) -> FairValueSignal:
        """
        Internal dispatcher to route-specific handlers

        Args:
            plan: Route plan
            market: Market metadata
            evidence: Evidence items
            rule_text: Resolution rules

        Returns:
            FairValueSignal from the selected route
        """
        if plan.route == "simple":
            return await self.simple_route.execute(
                condition_id=plan.condition_id,
                market=market,
                evidence_bundle=evidence,
                rule_text=rule_text,
                clarifications=[],
            )

        elif plan.route == "numeric":
            # TODO: Implement numeric route
            log.warning("numeric_route_not_implemented", condition_id=plan.condition_id)
            return self._neutral_signal(plan.condition_id, "numeric",
                                       reason="Numeric route not yet implemented")

        elif plan.route == "rule":
            return await self.rule_route.execute(
                condition_id=plan.condition_id,
                market=market,
                evidence_bundle=evidence,
                rule_text=rule_text,
                clarifications=getattr(market, 'clarifications', []),
            )

        elif plan.route == "dossier":
            # Fetch documents from evidence items
            doc_ids = set(item.doc_id for item in evidence if item.doc_id)
            documents = []
            for doc_id in doc_ids:
                doc = await self.evidence_graph.get_document(doc_id)
                if doc:
                    documents.append(doc)

            return await self.dossier_route.execute(
                condition_id=plan.condition_id,
                market=market,
                documents=documents,
                evidence=evidence,
                rule_text=rule_text,
                clarifications=getattr(market, 'clarifications', []),
            )

        else:
            log.error("unknown_route", route=plan.route, condition_id=plan.condition_id)
            return self._neutral_signal(plan.condition_id, plan.route,
                                       reason=f"Unknown route: {plan.route}")

    async def _execute_with_timeout(self,
                                    coro,
                                    timeout_seconds: float,
                                    condition_id: str) -> FairValueSignal:
        """
        Execute coroutine with timeout

        Args:
            coro: Coroutine to execute
            timeout_seconds: Timeout in seconds
            condition_id: Market condition ID (for logging)

        Returns:
            FairValueSignal from the coroutine

        Raises:
            asyncio.TimeoutError: If execution exceeds timeout
        """
        return await asyncio.wait_for(coro, timeout=timeout_seconds)

    async def _get_cached_signal(self, condition_id: str) -> FairValueSignal | None:
        """
        Retrieve last cached signal for a condition

        Args:
            condition_id: Market condition ID

        Returns:
            Last FairValueSignal or None if not found
        """
        try:
            return await self.evidence_graph.get_latest_signal(condition_id)
        except Exception as e:
            log.error("get_cached_signal_failed",
                     condition_id=condition_id,
                     error=str(e))
            return None

    def _neutral_signal(self,
                       condition_id: str,
                       route: str,
                       reason: str = "Fallback neutral signal") -> FairValueSignal:
        """
        Create a neutral (50/50) signal as fallback

        Args:
            condition_id: Market condition ID
            route: Route type
            reason: Reason for neutral signal

        Returns:
            Neutral FairValueSignal
        """
        log.info("creating_neutral_signal",
                condition_id=condition_id,
                reason=reason)

        return FairValueSignal(
            condition_id=condition_id,
            generated_at=datetime.utcnow(),
            p_calibrated=0.5,
            p_low=0.3,
            p_high=0.7,
            uncertainty=0.2,
            skew_cents=None,
            hurdle_cents=10.0,
            hurdle_met=False,
            route=route,
            evidence_ids=[],
            counterevidence_ids=[],
            models_used=["neutral_fallback"],
            expires_at=datetime.utcnow() + timedelta(minutes=5),
        )
