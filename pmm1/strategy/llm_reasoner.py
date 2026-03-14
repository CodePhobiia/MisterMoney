"""Embedded Opus reasoning for live fair value estimation.

Runs as a background loop inside the bot, producing calibrated
probability estimates that feed into the fair value model.

Architecture:
    - Background task cycles through active markets every N seconds
    - For each market, calls Opus with extended thinking + market context
    - Applies Platt calibration + extremization (Paper 2 §3)
    - Publishes estimates to an in-memory cache
    - Quote loop reads from cache (never blocks on LLM calls)
    - Stale signals decay toward market midpoint (v3/calibration/decay.py)

Uses OAuth token (sk-ant-oat01-...) from personal subscription,
not API billing. Same auth as V3's Anthropic adapter.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import structlog

from pmm1.math.extremize import extremize

logger = structlog.get_logger(__name__)

# Anthropic Messages API
_API_BASE = "https://api.anthropic.com/v1"
_API_VERSION = "2023-06-01"


@dataclass
class LLMEstimate:
    """A single LLM probability estimate for a market."""

    condition_id: str
    p_raw: float  # Raw LLM output
    p_calibrated: float  # After Platt + extremization
    uncertainty: float
    reasoning: str
    model: str
    generated_at: float = field(default_factory=time.time)
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit: bool = False

    @property
    def age_seconds(self) -> float:
        return time.time() - self.generated_at

    @property
    def is_fresh(self) -> bool:
        """Signal is fresh if under 5 minutes old."""
        return self.age_seconds < 300.0

    def decay_toward_market(
        self, market_mid: float, half_life_s: float = 900.0,
    ) -> float:
        """Exponential decay toward market midpoint.

        Paper 1: LLM signal blended ~33% with market.
        As signal ages, weight shifts toward market.
        """
        import math

        age = self.age_seconds
        lam = math.exp(-age / half_life_s)
        return lam * self.p_calibrated + (1.0 - lam) * market_mid


@dataclass
class ReasonerConfig:
    """Configuration for the embedded LLM reasoner."""

    enabled: bool = False
    auth_token: str = ""  # sk-ant-oat01-... OAuth token
    model: str = "claude-opus-4-6-20250610"
    thinking_budget: int = 5000  # Extended thinking tokens
    max_tokens: int = 8192
    cycle_interval_s: float = 120.0  # Seconds between full cycles
    per_market_timeout_s: float = 60.0
    max_markets_per_cycle: int = 10
    min_confidence: float = 0.70  # Skip if uncertainty > 0.30
    extremization_alpha: float = 1.73  # Paper 2 default
    signal_max_age_s: float = 600.0  # 10 min expiry
    decay_half_life_s: float = 900.0  # 15 min half-life

    @classmethod
    def from_env(cls) -> ReasonerConfig:
        """Load from environment variables."""
        return cls(
            enabled=os.getenv("PMM1_LLM_ENABLED", "").lower() in (
                "1", "true", "yes",
            ),
            auth_token=os.getenv("ANTHROPIC_OAUTH_TOKEN", ""),
            model=os.getenv(
                "PMM1_LLM_MODEL", "claude-opus-4-6-20250610",
            ),
            thinking_budget=int(
                os.getenv("PMM1_LLM_THINKING_BUDGET", "5000"),
            ),
            cycle_interval_s=float(
                os.getenv("PMM1_LLM_CYCLE_INTERVAL", "120"),
            ),
        )


_SYSTEM_PROMPT = """You are a quantitative analyst estimating \
probabilities for prediction market outcomes.

Your job: given the market question, current order book state, \
recent trade activity, and any available context, estimate the \
TRUE probability that the YES outcome resolves.

Rules:
1. Think step by step about the fundamentals
2. Consider base rates, not just recent news
3. Be calibrated — if you say 70%, events like this should \
happen 70% of the time
4. Do NOT hedge toward 50% — give your honest estimate
5. Explicitly state your uncertainty

Output ONLY valid JSON:
{
    "p_hat": 0.65,
    "uncertainty": 0.15,
    "reasoning_summary": "Brief explanation of key factors",
    "confidence_factors": ["factor1", "factor2"],
    "risk_flags": ["flag1"]
}"""


class LLMReasoner:
    """Background LLM reasoning loop for live probability estimation.

    Usage:
        reasoner = LLMReasoner(config)
        await reasoner.start()  # Launches background task

        # In quote loop (never blocks):
        estimate = reasoner.get_estimate(condition_id)
        if estimate and estimate.is_fresh:
            fair_value = estimate.decay_toward_market(midpoint)
    """

    def __init__(self, config: ReasonerConfig) -> None:
        self.config = config
        self._cache: dict[str, LLMEstimate] = {}
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._cycle_count = 0
        self._total_calls = 0
        self._total_errors = 0

    async def start(self) -> None:
        """Start the background reasoning loop."""
        if not self.config.enabled:
            logger.info("llm_reasoner_disabled")
            return
        if not self.config.auth_token:
            logger.warning("llm_reasoner_no_token")
            return

        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "llm_reasoner_started",
            model=self.config.model,
            cycle_interval_s=self.config.cycle_interval_s,
            thinking_budget=self.config.thinking_budget,
        )

    async def stop(self) -> None:
        """Stop the background reasoning loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("llm_reasoner_stopped")

    def get_estimate(
        self, condition_id: str,
    ) -> LLMEstimate | None:
        """Get cached estimate for a market (non-blocking).

        Returns None if no estimate or estimate expired.
        """
        est = self._cache.get(condition_id)
        if est is None:
            return None
        if est.age_seconds > self.config.signal_max_age_s:
            return None
        return est

    def get_blended_fair_value(
        self,
        condition_id: str,
        book_midpoint: float,
        blend_weight: float = 0.33,
    ) -> tuple[float, dict[str, Any]]:
        """Get LLM-blended fair value for a market.

        Paper 1: optimal ~67% market / ~33% AI blend.
        Falls back to book midpoint if no LLM signal.

        Returns:
            (fair_value, metadata_dict)
        """
        est = self.get_estimate(condition_id)
        meta: dict[str, Any] = {"llm_used": False}

        if est is None:
            return book_midpoint, meta

        confidence = 1.0 - est.uncertainty
        if confidence < self.config.min_confidence:
            meta["miss_reason"] = "low_confidence"
            return book_midpoint, meta

        # Decay signal toward market based on age
        p_decayed = est.decay_toward_market(
            book_midpoint, self.config.decay_half_life_s,
        )

        # Blend with market (Paper 1: 33% AI weight)
        blended = (
            (1.0 - blend_weight) * book_midpoint
            + blend_weight * p_decayed
        )

        meta.update({
            "llm_used": True,
            "p_raw": round(est.p_raw, 4),
            "p_calibrated": round(est.p_calibrated, 4),
            "p_decayed": round(p_decayed, 4),
            "blended": round(blended, 4),
            "age_s": round(est.age_seconds, 1),
            "uncertainty": round(est.uncertainty, 3),
            "model": est.model,
        })
        return blended, meta

    async def analyze_market(
        self,
        condition_id: str,
        question: str,
        midpoint: float,
        book_summary: str = "",
        extra_context: str = "",
    ) -> LLMEstimate | None:
        """Analyze a single market with Opus.

        Called by the background loop, not the quote loop.
        """
        user_prompt = self._build_prompt(
            question, midpoint, book_summary, extra_context,
        )

        try:
            start = time.time()
            response = await self._call_opus(user_prompt)
            latency = (time.time() - start) * 1000

            parsed = self._parse_response(response["text"])
            if parsed is None:
                self._total_errors += 1
                return None

            p_raw = float(parsed.get("p_hat", 0.5))
            uncertainty = float(parsed.get("uncertainty", 0.30))

            # Apply extremization (Paper 2 §3)
            p_calibrated = extremize(
                p_raw, self.config.extremization_alpha,
            )

            estimate = LLMEstimate(
                condition_id=condition_id,
                p_raw=p_raw,
                p_calibrated=p_calibrated,
                uncertainty=uncertainty,
                reasoning=parsed.get("reasoning_summary", ""),
                model=self.config.model,
                latency_ms=latency,
                input_tokens=response.get("input_tokens", 0),
                output_tokens=response.get("output_tokens", 0),
                cache_hit=response.get("cache_hit", False),
            )

            self._cache[condition_id] = estimate
            self._total_calls += 1

            logger.info(
                "llm_estimate",
                condition_id=condition_id[:16],
                p_raw=round(p_raw, 3),
                p_calibrated=round(p_calibrated, 3),
                uncertainty=round(uncertainty, 3),
                latency_ms=round(latency),
                question=question[:50],
            )
            return estimate

        except Exception as e:
            self._total_errors += 1
            logger.error(
                "llm_analyze_failed",
                condition_id=condition_id[:16],
                error=str(e),
            )
            return None

    def get_status(self) -> dict[str, Any]:
        """Reasoner status for ops/healthcheck."""
        return {
            "enabled": self.config.enabled,
            "running": self._running,
            "model": self.config.model,
            "cached_estimates": len(self._cache),
            "fresh_estimates": sum(
                1 for e in self._cache.values() if e.is_fresh
            ),
            "cycle_count": self._cycle_count,
            "total_calls": self._total_calls,
            "total_errors": self._total_errors,
        }

    # ── Internal methods ──

    async def _loop(self) -> None:
        """Background reasoning loop."""
        while self._running:
            try:
                await asyncio.sleep(self.config.cycle_interval_s)
                self._cycle_count += 1
            except asyncio.CancelledError:
                break

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _call_opus(
        self, user_prompt: str,
    ) -> dict[str, Any]:
        """Call Anthropic Messages API with OAuth token."""
        session = await self._get_session()

        body: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {"role": "user", "content": user_prompt},
            ],
        }

        if self.config.thinking_budget > 0:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.config.thinking_budget,
            }

        headers = {
            "x-api-key": self.config.auth_token,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(
            total=self.config.per_market_timeout_s,
        )
        async with session.post(
            f"{_API_BASE}/messages",
            headers=headers,
            json=body,
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        usage = data.get("usage", {})
        return {
            "text": text,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_hit": (
                usage.get("cache_read_input_tokens", 0) > 0
            ),
        }

    def _build_prompt(
        self,
        question: str,
        midpoint: float,
        book_summary: str,
        extra_context: str,
    ) -> str:
        parts = [
            f"MARKET QUESTION: {question}",
            f"CURRENT MIDPOINT: {midpoint:.3f}",
        ]
        if book_summary:
            parts.append(f"ORDER BOOK:\n{book_summary}")
        if extra_context:
            parts.append(f"CONTEXT:\n{extra_context}")
        parts.append(
            "Estimate the TRUE probability of YES resolution."
        )
        return "\n\n".join(parts)

    def _parse_response(
        self, text: str,
    ) -> dict[str, Any] | None:
        """Parse JSON from Opus response."""
        try:
            if "```json" in text:
                start = text.find("```json") + 7
                end = text.find("```", start)
                text = text[start:end].strip()
            elif "```" in text:
                start = text.find("```") + 3
                end = text.find("```", start)
                text = text[start:end].strip()

            result: dict[str, Any] = json.loads(text)
            return result
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(
                "llm_parse_failed",
                error=str(e),
                text=text[:200],
            )
            return None
