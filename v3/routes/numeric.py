"""
Numeric Route
Computes fair value for numeric/threshold markets using barrier and hazard models
"""

import math
from datetime import datetime, timezone
from typing import Literal
import structlog

from v3.evidence.entities import RuleGraph, EvidenceItem, FairValueSignal
from v3.evidence.graph import EvidenceGraph
from v3.providers.registry import ProviderRegistry
from v3.intake.source_checkers.base import SourceCheckResult

log = structlog.get_logger()


class NumericRoute:
    """
    Computes fair value for numeric/threshold markets
    
    Two solver modes:
    1. Barrier probability: P(price crosses threshold before expiry)
    2. Hazard rate: P(event occurs before expiry)
    """
    
    def __init__(
        self,
        registry: ProviderRegistry,
        evidence_graph: EvidenceGraph,
        source_registry
    ):
        """
        Initialize numeric route
        
        Args:
            registry: Provider registry for AI models
            evidence_graph: Evidence graph for storing signals
            source_registry: Source registry for checking sources
        """
        self.registry = registry
        self.evidence_graph = evidence_graph
        self.source_registry = source_registry
        self.anomaly_detector = AnomalyDetector(registry)
    
    def _barrier_probability(
        self,
        current: float,
        threshold: float,
        vol: float,
        time_to_expiry_hours: float,
        operator: str = '>'
    ) -> float:
        """
        Compute barrier crossing probability using simplified barrier option pricing
        
        Based on geometric Brownian motion (GBM) assumption:
        P(S(T) crosses K) ≈ Φ((ln(S/K) + μT) / (σ√T))
        
        Where:
        - S = current value
        - K = threshold/barrier
        - T = time to expiry (in years)
        - σ = annualized volatility
        - μ = drift (assumed 0 for simplicity, neutral market)
        - Φ = cumulative normal distribution
        
        Args:
            current: Current value
            threshold: Barrier/threshold level
            vol: Annualized volatility (e.g., 0.8 = 80%)
            time_to_expiry_hours: Time until expiry in hours
            operator: Comparison operator ('>', '>=', '<', '<=')
            
        Returns:
            Probability between 0 and 1
        """
        if current <= 0 or threshold <= 0:
            log.warning("barrier_invalid_values",
                       current=current,
                       threshold=threshold)
            return 0.5
        
        if time_to_expiry_hours <= 0:
            # Already expired, check current state
            if operator in ('>', '>='):
                return 1.0 if current >= threshold else 0.0
            else:
                return 1.0 if current <= threshold else 0.0
        
        # Special case: already past the barrier
        # For "will X reach Y by date Z", if already at/past Y, very high probability
        if operator in ('>', '>=') and current >= threshold:
            # Already above threshold - market essentially resolved
            # Small chance it never technically "reached" it in observed history
            return 0.95
        elif operator in ('<', '<=') and current <= threshold:
            # Already below threshold
            return 0.95
        
        # Convert time to years
        T = time_to_expiry_hours / (365 * 24)
        
        # Log-moneyness
        ln_SK = math.log(current / threshold)
        
        # Assume zero drift (neutral market) for simplicity
        mu = 0.0
        
        # Standard deviation of log returns
        sigma_sqrt_T = vol * math.sqrt(T)
        
        if sigma_sqrt_T < 1e-6:
            # Near-zero volatility, use current state
            if operator in ('>', '>='):
                return 1.0 if current >= threshold else 0.0
            else:
                return 1.0 if current <= threshold else 0.0
        
        # Z-score
        z = (ln_SK + mu * T) / sigma_sqrt_T
        
        # Approximate cumulative normal distribution
        # Using error function approximation: Φ(z) ≈ 0.5 * (1 + erf(z/√2))
        def phi(x):
            """Cumulative normal distribution (approximation)"""
            # Using abramowitz & stegun approximation
            a1 = 0.254829592
            a2 = -0.284496736
            a3 = 1.421413741
            a4 = -1.453152027
            a5 = 1.061405429
            p = 0.3275911
            
            sign = 1 if x >= 0 else -1
            x = abs(x) / math.sqrt(2)
            
            t = 1.0 / (1.0 + p * x)
            y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x)
            
            return 0.5 * (1.0 + sign * y)
        
        # Probability of crossing barrier
        if operator in ('>', '>='):
            # Probability price goes above threshold
            prob = phi(z)
        else:
            # Probability price goes below threshold
            prob = 1.0 - phi(z)
        
        # Clamp to reasonable range
        return max(0.01, min(0.99, prob))
    
    def _hazard_probability(
        self,
        rate: float,
        time_to_expiry_hours: float
    ) -> float:
        """
        Compute event probability using exponential distribution (hazard rate model)
        
        P(event by time T) = 1 - exp(-λT)
        
        Where:
        - λ = hazard rate (events per hour)
        - T = time to expiry (hours)
        
        Args:
            rate: Hazard rate (events per hour, e.g., 0.01 = 1% chance per hour)
            time_to_expiry_hours: Time until expiry in hours
            
        Returns:
            Probability between 0 and 1
        """
        if time_to_expiry_hours <= 0:
            return 0.0  # Already expired, no chance
        
        if rate <= 0:
            return 0.0  # No events expected
        
        # Exponential CDF
        prob = 1.0 - math.exp(-rate * time_to_expiry_hours)
        
        return max(0.0, min(1.0, prob))
    
    def _time_to_expiry_hours(self, window_end: datetime | None) -> float:
        """
        Compute time to expiry in hours
        
        Args:
            window_end: End of resolution window
            
        Returns:
            Hours until expiry (0 if already passed)
        """
        if not window_end:
            # No expiry specified, assume distant future (1 year)
            return 365 * 24
        
        now = datetime.now(timezone.utc)
        
        # Ensure window_end is timezone-aware
        if window_end.tzinfo is None:
            window_end = window_end.replace(tzinfo=timezone.utc)
        
        delta = window_end - now
        hours = delta.total_seconds() / 3600
        
        return max(0, hours)
    
    async def solve(
        self,
        condition_id: str,
        rule: RuleGraph,
        source_check: SourceCheckResult,
        evidence: list[EvidenceItem]
    ) -> FairValueSignal:
        """
        Compute fair value for numeric/threshold markets
        
        Two solver modes:
        1. Barrier probability: P(price crosses threshold before expiry)
           - Uses GBM assumption with historical vol from source_check
           
        2. Hazard rate: P(event occurs before expiry)
           - Uses exponential distribution with historical rate
        
        If source_check.confidence >= 0.8: publish directly
        If source_check.confidence < 0.8: trigger anomaly detector
        
        Args:
            condition_id: Unique identifier for condition
            rule: Structured rule graph
            source_check: Result from source checker
            evidence: List of evidence items (currently unused, for future enhancement)
            
        Returns:
            FairValueSignal with calibrated probability
        """
        log.info("numeric_solve_start",
                condition_id=condition_id,
                source_confidence=source_check.confidence,
                source_probability=source_check.probability)
        
        # Check if we should trigger anomaly detection
        if source_check.confidence < 0.8:
            log.info("low_confidence_triggering_anomaly_detector",
                    condition_id=condition_id,
                    confidence=source_check.confidence)
            
            anomaly_type = await self.anomaly_detector.classify(
                condition_id,
                source_check,
                evidence
            )
            
            log.info("anomaly_classification",
                    condition_id=condition_id,
                    anomaly_type=anomaly_type)
            
            # For now, still compute probability but flag with lower confidence
            # In production, would escalate regime_shift to GPT-5.4
        
        # Determine solver mode based on source check
        time_to_expiry = self._time_to_expiry_hours(rule.window_end)
        
        # Try to extract numeric values
        try:
            current_value = float(source_check.current_value)
            threshold = float(source_check.threshold)
            is_numeric = True
        except (ValueError, TypeError):
            is_numeric = False
            current_value = 0.0
            threshold = 0.0
        
        if is_numeric and current_value > 0 and threshold > 0:
            # Use barrier probability model
            # Estimate volatility from source check or use default
            vol = source_check.raw_data.get('volatility', 0.5)  # Default 50% annual vol
            
            operator = rule.operator or '>'
            
            p_calibrated = self._barrier_probability(
                current=current_value,
                threshold=threshold,
                vol=vol,
                time_to_expiry_hours=time_to_expiry,
                operator=operator
            )
            
            log.info("barrier_probability_computed",
                    current=current_value,
                    threshold=threshold,
                    vol=vol,
                    time_to_expiry_hours=time_to_expiry,
                    probability=p_calibrated)
        
        else:
            # Use hazard rate model or fall back to source probability
            # For now, use source_check.probability as baseline
            p_calibrated = source_check.probability
            
            log.info("using_source_probability",
                    probability=p_calibrated)
        
        # Compute uncertainty based on confidence
        # Lower confidence → higher uncertainty
        uncertainty = 1.0 - source_check.confidence
        
        # Compute bounds
        p_low = max(0.0, p_calibrated - uncertainty * 0.1)
        p_high = min(1.0, p_calibrated + uncertainty * 0.1)
        
        # Create fair value signal
        signal = FairValueSignal(
            condition_id=condition_id,
            generated_at=datetime.now(timezone.utc),
            p_calibrated=p_calibrated,
            p_low=p_low,
            p_high=p_high,
            uncertainty=uncertainty,
            route='numeric',
            evidence_ids=[],  # Would populate with evidence item IDs
            models_used=['numeric_route_barrier'],
            expires_at=rule.window_end,
            created_at=datetime.now(timezone.utc)
        )
        
        log.info("numeric_solve_complete",
                condition_id=condition_id,
                p_calibrated=p_calibrated,
                uncertainty=uncertainty)
        
        return signal


class AnomalyDetector:
    """
    Detects anomalies in source check results
    
    Uses Sonnet 4.6 (low reasoning) to classify low-confidence checks
    """
    
    def __init__(self, registry: ProviderRegistry):
        """
        Initialize anomaly detector
        
        Args:
            registry: Provider registry for AI models
        """
        self.registry = registry
    
    async def classify(
        self,
        condition_id: str,
        source_check: SourceCheckResult,
        evidence: list[EvidenceItem]
    ) -> Literal["normal", "regime_shift", "data_quality_issue"]:
        """
        Classify anomaly type for low-confidence source checks
        
        Only called when source_check.confidence < 0.8
        Uses Sonnet 4.6 (low reasoning) to classify
        
        Args:
            condition_id: Condition identifier
            source_check: Low-confidence source check result
            evidence: Supporting evidence items
            
        Returns:
            Anomaly classification
        """
        # Build context for AI classification
        context = f"""
        Condition: {condition_id}
        Source: {source_check.source}
        Current Value: {source_check.current_value}
        Threshold: {source_check.threshold}
        Confidence: {source_check.confidence}
        Raw Data: {source_check.raw_data}
        
        Evidence Items: {len(evidence)}
        
        Classify this low-confidence check into one of:
        1. normal - Data is valid but noisy/uncertain
        2. regime_shift - Market conditions have fundamentally changed (e.g., volatility spike, structural break)
        3. data_quality_issue - Source data is missing, corrupt, or unreliable
        
        Respond with only the classification: normal, regime_shift, or data_quality_issue
        """
        
        try:
            # Use Sonnet 4.6 from registry
            provider = self.registry.providers.get('sonnet')
            
            if not provider:
                log.warning("anomaly_detector_no_provider",
                           condition_id=condition_id)
                # Default to data_quality_issue if no provider available
                return "data_quality_issue"
            
            # Simple zero-shot classification
            response = await provider.generate(
                prompt=context,
                max_tokens=50,
                temperature=0.0,
                thinking_level='none'
            )
            
            response_lower = response.lower().strip()
            
            if 'regime' in response_lower or 'shift' in response_lower:
                return "regime_shift"
            elif 'data' in response_lower or 'quality' in response_lower:
                return "data_quality_issue"
            else:
                return "normal"
        
        except Exception as e:
            log.error("anomaly_detector_error",
                     condition_id=condition_id,
                     error=str(e))
            # Default to data_quality_issue on error
            return "data_quality_issue"
