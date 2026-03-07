"""
V3 Gamma Sync
Syncs market metadata, rules, and clarifications from Polymarket Gamma API
"""

import httpx
from datetime import datetime
from typing import List, Dict
import structlog

from .schemas import MarketMeta

log = structlog.get_logger()


class GammaSync:
    """Syncs market metadata, rules, and clarifications from Polymarket Gamma API"""
    
    GAMMA_BASE = "https://gamma-api.polymarket.com"
    
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
    
    async def close(self):
        """Close HTTP client"""
        await self.client.aclose()
    
    async def sync_markets(self, limit: int = 200) -> List[MarketMeta]:
        """
        Fetch active markets from Gamma API
        
        Args:
            limit: Maximum number of markets to fetch
            
        Returns:
            List of MarketMeta objects
        """
        log.info("gamma_sync_markets_start", limit=limit)
        
        url = f"{self.GAMMA_BASE}/markets"
        params = {
            "closed": "false",
            "limit": limit,
            "_sort": "volume24hr",
            "_order": "desc"
        }
        
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            markets = []
            for market_data in data:
                try:
                    # Polymarket returns a list of markets, each with nested condition data
                    # Extract relevant fields
                    condition_id = market_data.get("condition_id") or market_data.get("id")
                    
                    market = MarketMeta(
                        condition_id=condition_id,
                        question=market_data.get("question", ""),
                        description=market_data.get("description", ""),
                        resolution_source=market_data.get("outcome_prices", {}).get("source", "Polymarket"),
                        end_date=datetime.fromisoformat(market_data.get("end_date_iso", "2026-12-31T00:00:00")),
                        rules=market_data.get("rules", market_data.get("description", "")),
                        clarifications=[],  # Will be fetched separately
                        volume_24h=float(market_data.get("volume_24hr", 0.0)),
                        current_mid=self._extract_mid_price(market_data),
                    )
                    markets.append(market)
                except Exception as e:
                    log.warning("gamma_sync_market_parse_failed", 
                              condition_id=market_data.get("condition_id"), 
                              error=str(e))
                    continue
            
            log.info("gamma_sync_markets_complete", count=len(markets))
            return markets
            
        except Exception as e:
            log.error("gamma_sync_markets_failed", error=str(e))
            raise
    
    def _extract_mid_price(self, market_data: dict) -> float:
        """Extract current mid price from market data"""
        try:
            # Check for outcome prices
            outcome_prices = market_data.get("outcome_prices")
            if outcome_prices and isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
                # YES/NO market: mid is average of YES and NO
                yes_price = float(outcome_prices[0])
                return yes_price
            
            # Fallback: check for last_price
            last_price = market_data.get("last_price")
            if last_price:
                return float(last_price)
            
            # Default to 0.5 if no price data
            return 0.5
        except Exception:
            return 0.5
    
    async def sync_rules(self, condition_ids: List[str]) -> Dict[str, str]:
        """
        Fetch resolution rules for specific conditions
        
        Args:
            condition_ids: List of condition IDs to fetch rules for
            
        Returns:
            Dict mapping condition_id to rules text
        """
        log.info("gamma_sync_rules_start", count=len(condition_ids))
        
        rules_map = {}
        
        for condition_id in condition_ids:
            try:
                url = f"{self.GAMMA_BASE}/markets/{condition_id}"
                response = await self.client.get(url)
                response.raise_for_status()
                data = response.json()
                
                rules = data.get("rules") or data.get("description", "")
                rules_map[condition_id] = rules
                
            except Exception as e:
                log.warning("gamma_sync_rule_failed", 
                          condition_id=condition_id, 
                          error=str(e))
                rules_map[condition_id] = ""
        
        log.info("gamma_sync_rules_complete", count=len(rules_map))
        return rules_map
    
    async def sync_clarifications(self, condition_ids: List[str]) -> Dict[str, List[str]]:
        """
        Fetch clarifications for specific conditions
        
        Args:
            condition_ids: List of condition IDs to fetch clarifications for
            
        Returns:
            Dict mapping condition_id to list of clarification texts
        """
        log.info("gamma_sync_clarifications_start", count=len(condition_ids))
        
        clarifications_map = {}
        
        for condition_id in condition_ids:
            try:
                # Polymarket doesn't have a dedicated clarifications endpoint
                # Check for comments/updates endpoint
                url = f"{self.GAMMA_BASE}/markets/{condition_id}/comments"
                response = await self.client.get(url)
                
                if response.status_code == 200:
                    data = response.json()
                    # Filter for official clarifications (from market creator)
                    clarifications = [
                        comment.get("text", "")
                        for comment in data
                        if comment.get("is_official", False)
                    ]
                    clarifications_map[condition_id] = clarifications
                else:
                    clarifications_map[condition_id] = []
                    
            except Exception as e:
                log.warning("gamma_sync_clarification_failed", 
                          condition_id=condition_id, 
                          error=str(e))
                clarifications_map[condition_id] = []
        
        log.info("gamma_sync_clarifications_complete", count=len(clarifications_map))
        return clarifications_map
