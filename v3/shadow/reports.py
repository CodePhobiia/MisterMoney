"""
V3 Shadow Daily Reports
Generates and sends daily shadow mode summaries to Telegram
"""

import os
import json
from datetime import datetime, timedelta
from typing import Dict, Any
import aiohttp
import structlog

from v3.evidence.db import Database
from .metrics import BrierScoreTracker

log = structlog.get_logger()


async def send_telegram(
    chat_id: str, 
    text: str, 
    bot_token: str | None = None
) -> bool:
    """
    Send message via Telegram Bot API
    
    Args:
        chat_id: Telegram chat ID
        text: Message text (supports Telegram markdown)
        bot_token: Bot token (reads from env TELEGRAM_BOT_TOKEN if not provided)
        
    Returns:
        True if sent successfully, False otherwise
    """
    if bot_token is None:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not bot_token:
        log.error("telegram_bot_token_not_found")
        return False
    
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    log.info("telegram_message_sent", chat_id=chat_id)
                    return True
                else:
                    error_text = await response.text()
                    log.error("telegram_send_failed", 
                             status=response.status,
                             error=error_text)
                    return False
    except Exception as e:
        log.error("telegram_request_failed", error=str(e))
        return False


class DailyReporter:
    """Sends daily shadow mode summary to Telegram"""
    
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    
    def __init__(
        self, 
        db: Database, 
        metrics: BrierScoreTracker,
        logger_dir: str = "data/v3/shadow"
    ):
        """
        Initialize daily reporter
        
        Args:
            db: Database instance
            metrics: BrierScoreTracker instance
            logger_dir: Shadow logger directory
        """
        self.db = db
        self.metrics = metrics
        self.logger_dir = logger_dir
    
    async def generate_daily_report(self, date: str | None = None) -> str:
        """
        Generate daily report text
        
        Args:
            date: Date string (YYYY-MM-DD), defaults to yesterday
            
        Returns:
            Formatted report text for Telegram
        """
        if date is None:
            # Default to yesterday (since we run this at midnight)
            yesterday = datetime.utcnow() - timedelta(days=1)
            date = yesterday.strftime("%Y-%m-%d")
        
        log.info("generating_daily_report", date=date)
        
        # Get summary from shadow logs
        summary = await self._get_log_summary(date)
        
        # Get route performance
        route_summary = await self.metrics.get_route_summary()
        
        # Get provider usage
        provider_usage = await self._get_provider_usage(date)
        
        # Get counterfactual analysis
        counterfactual = await self._get_counterfactual_analysis(date)
        
        # Build report
        report = f"📊 **V3 Shadow Report — {date}**\n\n"
        
        # Overall stats
        report += f"Markets evaluated: {summary['markets_evaluated']}\n"
        report += f"Signals generated: {summary['signals_generated']}\n"
        report += f"Errors: {summary['errors']}\n\n"
        
        # Route breakdown
        report += "**Route breakdown:**\n"
        
        route_breakdown = summary.get('route_breakdown', {})
        route_latencies = await self._get_route_latencies(date)
        
        for route in ['numeric', 'simple', 'rule', 'dossier']:
            count = route_breakdown.get(route, 0)
            avg_latency = route_latencies.get(route, 0)
            
            if count > 0:
                latency_str = f"{avg_latency / 1000:.1f}s" if avg_latency >= 1000 else f"{avg_latency:.0f}ms"
                report += f"• {route.capitalize()}: {count} markets, avg latency {latency_str}\n"
        
        report += "\n"
        
        # Brier scores (if we have resolved markets)
        if route_summary:
            report += "**Prediction quality (Brier scores):**\n"
            for route, stats in route_summary.items():
                brier = stats.get('brier')
                n = stats.get('n', 0)
                if brier is not None and n > 0:
                    report += f"• {route.capitalize()}: {brier:.3f} ({n} resolved)\n"
            report += "\n"
        
        # Counterfactual comparison
        if counterfactual:
            report += "**Counterfactual:**\n"
            report += f"• V3 would have improved {counterfactual['improved']} markets\n"
            report += f"• V3 would have hurt {counterfactual['hurt']} markets\n"
            report += f"• Net edge: {counterfactual['net_edge_cents']:+.1f}¢ avg\n\n"
        
        # Provider usage
        if provider_usage:
            report += "**Provider usage:**\n"
            for provider, stats in provider_usage.items():
                calls = stats.get('calls', 0)
                tokens = stats.get('tokens', 0)
                
                if calls > 0:
                    tokens_str = f"{tokens / 1000:.0f}k" if tokens >= 1000 else str(tokens)
                    report += f"• {provider.capitalize()}: {calls} calls, {tokens_str} tokens\n"
        
        log.info("daily_report_generated", date=date, length=len(report))
        
        return report
    
    async def send_report(self, date: str | None = None) -> bool:
        """
        Generate and send daily report via Telegram
        
        Args:
            date: Date string (YYYY-MM-DD), defaults to yesterday
            
        Returns:
            True if sent successfully
        """
        try:
            report = await self.generate_daily_report(date)
            success = await send_telegram(self.TELEGRAM_CHAT_ID, report)
            
            if success:
                log.info("daily_report_sent", date=date or "yesterday")
            else:
                log.error("daily_report_send_failed", date=date or "yesterday")
            
            return success
        
        except Exception as e:
            log.error("daily_report_generation_failed", error=str(e))
            return False
    
    async def _get_log_summary(self, date: str) -> Dict[str, Any]:
        """Read summary stats from shadow logs"""
        from pathlib import Path
        
        log_path = Path(self.logger_dir) / f"shadow_{date}.jsonl"
        error_path = Path(self.logger_dir) / f"errors_{date}.jsonl"
        
        route_counts = {}
        signal_count = 0
        
        if log_path.exists():
            try:
                with open(log_path, 'r') as f:
                    for line in f:
                        entry = json.loads(line)
                        route = entry.get("v3_signal", {}).get("route")
                        
                        if route:
                            route_counts[route] = route_counts.get(route, 0) + 1
                            signal_count += 1
            except Exception as e:
                log.error("failed_to_read_log_summary", date=date, error=str(e))
        
        error_count = 0
        
        if error_path.exists():
            try:
                with open(error_path, 'r') as f:
                    error_count = sum(1 for _ in f)
            except Exception as e:
                log.error("failed_to_count_errors", date=date, error=str(e))
        
        return {
            "date": date,
            "markets_evaluated": signal_count,
            "signals_generated": signal_count,
            "errors": error_count,
            "route_breakdown": route_counts,
        }
    
    async def _get_route_latencies(self, date: str) -> Dict[str, float]:
        """Get average latency per route from logs"""
        from pathlib import Path
        
        log_path = Path(self.logger_dir) / f"shadow_{date}.jsonl"
        
        route_latencies = {}
        route_counts = {}
        
        if not log_path.exists():
            return {}
        
        try:
            with open(log_path, 'r') as f:
                for line in f:
                    entry = json.loads(line)
                    route = entry.get("v3_signal", {}).get("route")
                    latency = entry.get("latency_ms", 0)
                    
                    if route:
                        route_latencies[route] = route_latencies.get(route, 0) + latency
                        route_counts[route] = route_counts.get(route, 0) + 1
        except Exception as e:
            log.error("failed_to_read_latencies", date=date, error=str(e))
            return {}
        
        # Calculate averages
        return {
            route: route_latencies[route] / route_counts[route]
            for route in route_latencies
        }
    
    async def _get_provider_usage(self, date: str) -> Dict[str, Dict[str, int]]:
        """Get provider call counts and token usage from logs"""
        from pathlib import Path
        
        log_path = Path(self.logger_dir) / f"shadow_{date}.jsonl"
        
        provider_stats = {}
        
        if not log_path.exists():
            return {}
        
        try:
            with open(log_path, 'r') as f:
                for line in f:
                    entry = json.loads(line)
                    token_usage = entry.get("token_usage", {})
                    
                    for provider, tokens in token_usage.items():
                        if provider not in provider_stats:
                            provider_stats[provider] = {"calls": 0, "tokens": 0}
                        
                        provider_stats[provider]["calls"] += 1
                        provider_stats[provider]["tokens"] += tokens
        except Exception as e:
            log.error("failed_to_read_provider_usage", date=date, error=str(e))
            return {}
        
        return provider_stats
    
    async def _get_counterfactual_analysis(self, date: str) -> Dict[str, Any] | None:
        """
        Compare V3 signals to V1 fair values
        
        Returns:
            Dict with improved/hurt counts and net edge
        """
        from pathlib import Path
        
        log_path = Path(self.logger_dir) / f"shadow_{date}.jsonl"
        
        if not log_path.exists():
            return None
        
        improved = 0
        hurt = 0
        total_edge_diff = 0.0
        count = 0
        
        try:
            with open(log_path, 'r') as f:
                for line in f:
                    entry = json.loads(line)
                    
                    v3_p = entry.get("v3_signal", {}).get("p_calibrated")
                    v1_p = entry.get("v1_fair_value")
                    market_mid = entry.get("market", {}).get("current_mid")
                    
                    if v3_p is None or v1_p is None or market_mid is None:
                        continue
                    
                    # Edge in cents
                    v3_edge = abs(v3_p - market_mid) * 100
                    v1_edge = abs(v1_p - market_mid) * 100
                    
                    if v3_edge > v1_edge:
                        improved += 1
                    elif v3_edge < v1_edge:
                        hurt += 1
                    
                    total_edge_diff += (v3_edge - v1_edge)
                    count += 1
        except Exception as e:
            log.error("failed_to_analyze_counterfactual", date=date, error=str(e))
            return None
        
        if count == 0:
            return None
        
        return {
            "improved": improved,
            "hurt": hurt,
            "net_edge_cents": total_edge_diff / count,
        }
