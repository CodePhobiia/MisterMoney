"""
Generic API Checker
Fallback checker for structured APIs
"""

import re
import aiohttp
import structlog
from datetime import datetime, timezone

from .base import SourceChecker, SourceCheckResult
from v3.evidence.entities import RuleGraph

log = structlog.get_logger()


class GenericAPIChecker(SourceChecker):
    """
    Fallback checker for known structured APIs
    
    Attempts to fetch URL and extract numeric values from JSON response
    """
    
    TIMEOUT_SECONDS = 8
    
    def _extract_value_from_json(self, data: dict, hint: str | None = None) -> float | str | None:
        """
        Extract a numeric or string value from JSON response
        
        Strategies:
        1. If hint provided, look for that key
        2. Look for common value keys: 'value', 'price', 'result', 'data'
        3. If single key-value pair, use the value
        4. If nested, try to find first numeric value
        """
        if not isinstance(data, dict):
            return None
        
        # Strategy 1: Use hint
        if hint and hint in data:
            return data[hint]
        
        # Strategy 2: Common keys
        common_keys = ['value', 'price', 'result', 'data', 'current', 'latest']
        for key in common_keys:
            if key in data:
                val = data[key]
                if isinstance(val, (int, float, str)):
                    return val
                elif isinstance(val, dict):
                    # Recurse
                    return self._extract_value_from_json(val, hint)
        
        # Strategy 3: Single key-value
        if len(data) == 1:
            key = list(data.keys())[0]
            val = data[key]
            if isinstance(val, (int, float, str)):
                return val
            elif isinstance(val, dict):
                return self._extract_value_from_json(val, hint)
        
        # Strategy 4: First numeric value
        for key, val in data.items():
            if isinstance(val, (int, float)):
                return val
            elif isinstance(val, str) and re.match(r'^[\d.]+$', val):
                try:
                    return float(val)
                except ValueError:
                    pass
        
        return None
    
    def _is_valid_url(self, url: str) -> bool:
        """Check if string is a valid HTTP(S) URL"""
        return url.startswith(('http://', 'https://'))
    
    async def check(self, condition_id: str, rule: RuleGraph) -> SourceCheckResult:
        """
        Fetch generic API and extract value
        
        Attempts to parse JSON response and extract numeric value,
        then compare against threshold.
        """
        source = rule.source_name
        
        if not self._is_valid_url(source):
            log.warning("generic_invalid_url",
                       source=source,
                       condition_id=condition_id)
            return SourceCheckResult(
                condition_id=condition_id,
                source=source,
                current_value="invalid",
                threshold=rule.threshold_num or 0.0,
                probability=0.5,
                confidence=0.0,
                raw_data={'error': 'Invalid URL'},
                ttl_seconds=300
            )
        
        try:
            async with aiohttp.ClientSession() as session:
                timeout = aiohttp.ClientTimeout(total=self.TIMEOUT_SECONDS)
                async with session.get(source, timeout=timeout) as resp:
                    if resp.status != 200:
                        log.error("generic_api_error",
                                 status=resp.status,
                                 url=source)
                        return SourceCheckResult(
                            condition_id=condition_id,
                            source=source,
                            current_value="error",
                            threshold=rule.threshold_num or 0.0,
                            probability=0.5,
                            confidence=0.0,
                            raw_data={'error': f'HTTP {resp.status}'},
                            ttl_seconds=300
                        )
                    
                    # Try to parse as JSON
                    try:
                        data = await resp.json()
                    except Exception as e:
                        log.error("generic_json_parse_error",
                                 error=str(e),
                                 url=source)
                        return SourceCheckResult(
                            condition_id=condition_id,
                            source=source,
                            current_value="parse_error",
                            threshold=rule.threshold_num or 0.0,
                            probability=0.5,
                            confidence=0.0,
                            raw_data={'error': 'Failed to parse JSON'},
                            ttl_seconds=300
                        )
                    
                    # Extract value
                    current_value = self._extract_value_from_json(data)
                    
                    if current_value is None:
                        log.warning("generic_no_value_extracted",
                                   url=source,
                                   data_keys=list(data.keys()) if isinstance(data, dict) else None)
                        return SourceCheckResult(
                            condition_id=condition_id,
                            source=source,
                            current_value="not_found",
                            threshold=rule.threshold_num or 0.0,
                            probability=0.5,
                            confidence=0.0,
                            raw_data=data if isinstance(data, dict) else {'raw': str(data)},
                            ttl_seconds=300
                        )
                    
                    # Compute probability
                    threshold = rule.threshold_num if rule.threshold_num is not None else 0.0
                    
                    # Try to convert to float for comparison
                    try:
                        current_numeric = float(current_value)
                        threshold_numeric = float(threshold)
                        
                        if rule.operator in ('>', '>='):
                            if current_numeric >= threshold_numeric:
                                probability = 0.9
                            else:
                                # Linear interpolation
                                ratio = current_numeric / threshold_numeric if threshold_numeric != 0 else 0.5
                                probability = max(0.05, min(0.95, ratio))
                        
                        elif rule.operator in ('<', '<='):
                            if current_numeric <= threshold_numeric:
                                probability = 0.9
                            else:
                                ratio = threshold_numeric / current_numeric if current_numeric != 0 else 0.5
                                probability = max(0.05, min(0.95, ratio))
                        
                        else:
                            # Unknown operator, use distance
                            distance_pct = abs(current_numeric - threshold_numeric) / max(abs(threshold_numeric), 1.0)
                            probability = max(0.05, min(0.95, 1.0 - distance_pct))
                        
                        confidence = 0.7  # Moderate confidence for generic API
                    
                    except (ValueError, TypeError):
                        # Non-numeric value, can't compute probability
                        log.warning("generic_non_numeric_value",
                                   current_value=current_value,
                                   threshold=threshold)
                        probability = 0.5
                        confidence = 0.3
                    
                    log.info("generic_check_success",
                            url=source,
                            current_value=current_value,
                            threshold=threshold,
                            probability=probability)
                    
                    return SourceCheckResult(
                        condition_id=condition_id,
                        source=source,
                        current_value=current_value,
                        threshold=threshold,
                        probability=probability,
                        confidence=confidence,
                        raw_data=data if isinstance(data, dict) else {'value': str(data)},
                        ttl_seconds=120  # Cache for 2 minutes
                    )
        
        except aiohttp.ClientError as e:
            log.error("generic_network_error", error=str(e), url=source)
            return SourceCheckResult(
                condition_id=condition_id,
                source=source,
                current_value="network_error",
                threshold=rule.threshold_num or 0.0,
                probability=0.5,
                confidence=0.0,
                raw_data={'error': str(e)},
                ttl_seconds=300
            )
        except Exception as e:
            log.error("generic_unexpected_error", error=str(e), url=source)
            return SourceCheckResult(
                condition_id=condition_id,
                source=source,
                current_value="unexpected_error",
                threshold=rule.threshold_num or 0.0,
                probability=0.5,
                confidence=0.0,
                raw_data={'error': str(e)},
                ttl_seconds=300
            )
