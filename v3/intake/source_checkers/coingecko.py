"""
CoinGecko Price Checker
Fetches cryptocurrency prices from CoinGecko free API
"""

import re

import aiohttp
import structlog

from v3.evidence.entities import RuleGraph

from .base import SourceChecker, SourceCheckResult

log = structlog.get_logger()


class CoinGeckoChecker(SourceChecker):
    """Fetches crypto prices from CoinGecko and computes barrier probabilities"""

    BASE_URL = "https://api.coingecko.com/api/v3"
    TIMEOUT_SECONDS = 10

    def _extract_coin_id(self, source: str) -> str | None:
        """
        Extract coin ID from source URL or text

        Examples:
            'bitcoin' -> 'bitcoin'
            'coingecko.com/en/coins/ethereum' -> 'ethereum'
            'BTC' -> 'bitcoin' (common mappings)
        """
        if not source:
            return None

        source_lower = source.lower()

        # Common symbol mappings
        symbol_map = {
            'btc': 'bitcoin',
            'eth': 'ethereum',
            'usdt': 'tether',
            'bnb': 'binancecoin',
            'sol': 'solana',
            'xrp': 'ripple',
            'ada': 'cardano',
            'doge': 'dogecoin',
        }

        # Try direct symbol lookup
        if source_lower in symbol_map:
            return symbol_map[source_lower]

        # Try extracting from URL
        coin_match = re.search(r'/coins/([a-z0-9-]+)', source_lower)
        if coin_match:
            return coin_match.group(1)

        # Try using it directly if it looks like a coin ID
        if re.match(r'^[a-z0-9-]+$', source_lower):
            return source_lower

        return None

    def _extract_threshold(self, rule: RuleGraph) -> float | None:
        """Extract price threshold from rule"""
        # Try rule.threshold_num first
        if rule.threshold_num is not None:
            return float(rule.threshold_num)

        # Try parsing from threshold_text
        if rule.threshold_text:
            # Look for dollar amounts
            dollar_match = re.search(r'\$([0-9,]+(?:\.[0-9]+)?)', rule.threshold_text)
            if dollar_match:
                return float(dollar_match.group(1).replace(',', ''))

            # Look for plain numbers
            num_match = re.search(r'([0-9,]+(?:\.[0-9]+)?)', rule.threshold_text)
            if num_match:
                return float(num_match.group(1).replace(',', ''))

        return None

    def _estimate_volatility(self, price_data: dict) -> float:
        """
        Estimate annualized volatility from price data

        For now, use a simple heuristic based on 24h change.
        In production, would fetch historical data.
        """
        # Try to get 24h change percentage
        price_change_24h = price_data.get('price_change_percentage_24h', 0)

        if price_change_24h:
            # Convert 24h change to annualized vol (very rough approximation)
            # Daily vol ≈ abs(24h change), annual vol ≈ daily vol * sqrt(365)
            daily_vol = abs(price_change_24h) / 100.0
            annualized_vol = daily_vol * (365 ** 0.5)
            return max(0.3, min(3.0, annualized_vol))  # Clamp between 30% and 300%

        # Default to moderate volatility for crypto
        return 0.8  # 80% annualized

    async def check(self, condition_id: str, rule: RuleGraph) -> SourceCheckResult:
        """
        Check current price from CoinGecko and compute barrier probability

        For "will X reach $Y by date Z" markets, computes probability
        using simplified barrier option pricing.
        """
        coin_id = self._extract_coin_id(rule.source_name)
        threshold = self._extract_threshold(rule)

        if not coin_id:
            log.warning("coingecko_no_coin_id",
                       source=rule.source_name,
                       condition_id=condition_id)
            return SourceCheckResult(
                condition_id=condition_id,
                source=rule.source_name,
                current_value=0.0,
                threshold=threshold or 0.0,
                probability=0.5,
                confidence=0.0,
                raw_data={'error': 'Could not extract coin ID'},
                ttl_seconds=60
            )

        if threshold is None:
            log.warning("coingecko_no_threshold",
                       source=rule.source_name,
                       condition_id=condition_id)
            return SourceCheckResult(
                condition_id=condition_id,
                source=rule.source_name,
                current_value=0.0,
                threshold=0.0,
                probability=0.5,
                confidence=0.0,
                raw_data={'error': 'Could not extract threshold'},
                ttl_seconds=60
            )

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.BASE_URL}/simple/price"
                params = {
                    'ids': coin_id,
                    'vs_currencies': 'usd',
                    'include_24hr_change': 'true'
                }

                timeout = aiohttp.ClientTimeout(total=self.TIMEOUT_SECONDS)
                async with session.get(url, params=params, timeout=timeout) as resp:
                    if resp.status != 200:
                        log.error("coingecko_api_error",
                                 status=resp.status,
                                 coin=coin_id)
                        return SourceCheckResult(
                            condition_id=condition_id,
                            source=rule.source_name,
                            current_value=0.0,
                            threshold=threshold,
                            probability=0.5,
                            confidence=0.0,
                            raw_data={'error': f'HTTP {resp.status}'},
                            ttl_seconds=60
                        )

                    data = await resp.json()

                    if coin_id not in data:
                        log.error("coingecko_coin_not_found", coin=coin_id)
                        return SourceCheckResult(
                            condition_id=condition_id,
                            source=rule.source_name,
                            current_value=0.0,
                            threshold=threshold,
                            probability=0.5,
                            confidence=0.0,
                            raw_data={'error': 'Coin not found in response'},
                            ttl_seconds=60
                        )

                    coin_data = data[coin_id]
                    current_price = coin_data.get('usd', 0)

                    if current_price == 0:
                        log.error("coingecko_zero_price", coin=coin_id)
                        return SourceCheckResult(
                            condition_id=condition_id,
                            source=rule.source_name,
                            current_value=0.0,
                            threshold=threshold,
                            probability=0.5,
                            confidence=0.0,
                            raw_data=coin_data,
                            ttl_seconds=60
                        )

                    # Compute simple probability based on current price vs threshold
                    # If already above threshold: high probability
                    # If below: use ratio as proxy
                    if rule.operator in ('>', '>='):
                        if current_price >= threshold:
                            probability = 0.95  # Already there or past it
                        else:
                            # Simple linear interpolation
                            ratio = current_price / threshold
                            probability = max(0.01, min(0.95, ratio))
                    elif rule.operator in ('<', '<='):
                        if current_price <= threshold:
                            probability = 0.95
                        else:
                            ratio = threshold / current_price
                            probability = max(0.01, min(0.95, ratio))
                    else:
                        # Unknown operator, use distance-based heuristic
                        distance_pct = abs(current_price - threshold) / threshold
                        probability = max(0.01, min(0.99, 1.0 - distance_pct))

                    log.info("coingecko_check_success",
                            coin=coin_id,
                            current_price=current_price,
                            threshold=threshold,
                            probability=probability)

                    return SourceCheckResult(
                        condition_id=condition_id,
                        source=rule.source_name,
                        current_value=current_price,
                        threshold=threshold,
                        probability=probability,
                        confidence=0.9,  # High confidence in CoinGecko data
                        raw_data=coin_data,
                        ttl_seconds=30  # Crypto prices change fast
                    )

        except aiohttp.ClientError as e:
            log.error("coingecko_network_error", error=str(e), coin=coin_id)
            return SourceCheckResult(
                condition_id=condition_id,
                source=rule.source_name,
                current_value=0.0,
                threshold=threshold,
                probability=0.5,
                confidence=0.0,
                raw_data={'error': str(e)},
                ttl_seconds=60
            )
        except Exception as e:
            log.error("coingecko_unexpected_error", error=str(e), coin=coin_id)
            return SourceCheckResult(
                condition_id=condition_id,
                source=rule.source_name,
                current_value=0.0,
                threshold=threshold,
                probability=0.5,
                confidence=0.0,
                raw_data={'error': str(e)},
                ttl_seconds=60
            )
