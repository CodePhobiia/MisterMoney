"""
Economic Data Checker
Fetches economic indicators from BLS, FRED, etc.
"""

import re
import aiohttp
import structlog
from datetime import datetime, timezone

from .base import SourceChecker, SourceCheckResult
from v3.evidence.entities import RuleGraph

log = structlog.get_logger()


class EconomicChecker(SourceChecker):
    """
    Fetches economic indicators and computes threshold probabilities
    
    Sources:
    - BLS (Bureau of Labor Statistics): CPI, unemployment
    - FRED (Federal Reserve Economic Data): various indicators
    """
    
    # FRED API - requires API key for production
    FRED_BASE_URL = "https://api.stlouisfed.org/fred"
    TIMEOUT_SECONDS = 10
    
    # For demo, we'll use public endpoints where possible
    
    def _identify_indicator(self, source: str) -> dict:
        """
        Identify economic indicator from source
        
        Returns dict with:
        - type: 'cpi', 'unemployment', 'gdp', 'interest_rate', etc.
        - series_id: official series identifier (if known)
        """
        source_lower = source.lower()
        
        indicators = {
            'cpi': {'pattern': r'cpi|consumer price index|inflation', 'series': 'CPIAUCSL'},
            'unemployment': {'pattern': r'unemployment|jobless', 'series': 'UNRATE'},
            'gdp': {'pattern': r'gdp|gross domestic product', 'series': 'GDP'},
            'interest': {'pattern': r'interest rate|fed funds|federal funds', 'series': 'FEDFUNDS'},
            'sp500': {'pattern': r's&p 500|s&p500|sp500', 'series': 'SP500'},
        }
        
        for indicator_type, info in indicators.items():
            if re.search(info['pattern'], source_lower):
                return {
                    'type': indicator_type,
                    'series_id': info['series']
                }
        
        return {'type': 'unknown', 'series_id': None}
    
    def _estimate_volatility(self, indicator_type: str) -> float:
        """
        Estimate annualized volatility for an economic indicator
        
        Based on historical volatility of common indicators
        """
        volatility_map = {
            'cpi': 0.03,  # CPI is relatively stable, ~3% annual changes
            'unemployment': 0.15,  # More volatile
            'gdp': 0.05,  # Moderate volatility
            'interest': 0.25,  # Can be quite volatile
            'sp500': 0.20,  # Stock market volatility
        }
        
        return volatility_map.get(indicator_type, 0.10)  # Default 10%
    
    async def check(self, condition_id: str, rule: RuleGraph) -> SourceCheckResult:
        """
        Check economic indicator against threshold
        
        For "will CPI exceed X by date Y", computes probability using
        historical volatility and simple diffusion model.
        """
        indicator_info = self._identify_indicator(rule.source_name)
        indicator_type = indicator_info['type']
        series_id = indicator_info['series_id']
        
        if indicator_type == 'unknown':
            log.warning("economic_unknown_indicator",
                       source=rule.source_name,
                       condition_id=condition_id)
            return SourceCheckResult(
                condition_id=condition_id,
                source=rule.source_name,
                current_value=0.0,
                threshold=rule.threshold_num or 0.0,
                probability=0.5,
                confidence=0.0,
                raw_data={'error': 'Unknown economic indicator'},
                ttl_seconds=3600
            )
        
        # For demo purposes, use mock current values
        # In production, would fetch from FRED API
        mock_values = {
            'cpi': 310.5,  # CPI-U index value
            'unemployment': 3.7,  # Unemployment rate %
            'gdp': 27000,  # GDP in billions
            'interest': 5.33,  # Fed funds rate %
            'sp500': 5000,  # S&P 500 index
        }
        
        current_value = mock_values.get(indicator_type, 0.0)
        threshold = rule.threshold_num if rule.threshold_num is not None else 0.0
        
        if current_value == 0.0:
            log.warning("economic_no_current_value",
                       indicator=indicator_type,
                       condition_id=condition_id)
            return SourceCheckResult(
                condition_id=condition_id,
                source=rule.source_name,
                current_value=0.0,
                threshold=threshold,
                probability=0.5,
                confidence=0.0,
                raw_data={'error': 'No current value available'},
                ttl_seconds=3600
            )
        
        # Compute probability based on current value vs threshold
        if rule.operator in ('>', '>='):
            if current_value >= threshold:
                probability = 0.85  # Already at or above threshold
            else:
                # Use distance and volatility to estimate probability
                distance_pct = (threshold - current_value) / current_value
                vol = self._estimate_volatility(indicator_type)
                
                # Simple heuristic: smaller distance = higher probability
                # Adjust by volatility: higher vol = easier to reach
                probability = max(0.05, min(0.95, 0.5 - distance_pct + vol))
        
        elif rule.operator in ('<', '<='):
            if current_value <= threshold:
                probability = 0.85
            else:
                distance_pct = (current_value - threshold) / current_value
                vol = self._estimate_volatility(indicator_type)
                probability = max(0.05, min(0.95, 0.5 - distance_pct + vol))
        
        else:
            # Unknown operator
            distance_pct = abs(current_value - threshold) / max(current_value, threshold)
            probability = max(0.05, min(0.95, 1.0 - distance_pct))
        
        log.info("economic_check_success",
                indicator=indicator_type,
                current_value=current_value,
                threshold=threshold,
                probability=probability)
        
        return SourceCheckResult(
            condition_id=condition_id,
            source=rule.source_name,
            current_value=current_value,
            threshold=threshold,
            probability=probability,
            confidence=0.6,  # Moderate confidence (using mock data)
            raw_data={
                'indicator_type': indicator_type,
                'series_id': series_id,
                'note': 'Using mock data for demo'
            },
            ttl_seconds=3600  # Economic data updates slowly, cache for 1 hour
        )
