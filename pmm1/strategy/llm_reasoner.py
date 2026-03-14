"""Embedded Opus reasoning for live fair value estimation.

Implements the full Paper 1 + Paper 2 forecasting pipeline:

1. BLIND PASS (no market price) — prevents 0.994 correlation
   with market prices that Paper 2 documents. The LLM must
   form its own view before seeing what the market thinks.

2. MARKET-AWARE CHALLENGE — then show the market price and ask
   Opus to reconcile. This is Paper 1's dossier architecture
   (synthesis → challenge → adjudication) compressed into two
   passes within a single model.

3. CALIBRATION — extremize to correct RLHF hedging (Paper 2 §3),
   then blend ~33% AI / ~67% market (Paper 1 optimal weight).

4. RANGE-BEFORE-POINT — Paper 1 says asking for a range first
   improves calibration vs asking for a point estimate directly.

5. BRIER MINIMIZATION — explicit instruction to minimize Brier
   score, which Paper 1 identifies as more effective than generic
   "be calibrated" prompts.

6. BASE RATE ANCHORING — Paper 2 documents recency bias as a key
   LLM failure mode. The prompt forces base rate consideration
   before analyzing recent events.

Architecture:
    - Background task cycles through active markets
    - Quote loop reads from in-memory cache (never blocks)
    - Stale signals decay toward midpoint exponentially
"""

from __future__ import annotations

import asyncio
import json
import math
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
    p_blind: float  # Blind pass (no market price seen)
    p_challenged: float  # After market-aware challenge
    p_calibrated: float  # After extremization
    uncertainty: float
    reasoning: str
    contra_points: str  # Arguments against own estimate
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
        """Exponential decay toward market midpoint."""
        age = self.age_seconds
        lam = math.exp(-age / half_life_s)
        return lam * self.p_calibrated + (1.0 - lam) * market_mid


@dataclass
class ReasonerConfig:
    """Configuration for the embedded LLM reasoner."""

    enabled: bool = False
    auth_token: str = ""  # sk-ant-oat01-... OAuth token
    model: str = "claude-opus-4-6-20250610"
    thinking_budget: int = 10000  # Extended thinking tokens
    max_tokens: int = 12000
    cycle_interval_s: float = 120.0
    per_market_timeout_s: float = 90.0
    max_markets_per_cycle: int = 10
    min_confidence: float = 0.70
    extremization_alpha: float = 1.73  # Paper 2 §3: √3
    signal_max_age_s: float = 600.0
    decay_half_life_s: float = 900.0

    @classmethod
    def from_env(cls) -> ReasonerConfig:
        """Load from environment variables."""
        return cls(
            enabled=os.getenv(
                "PMM1_LLM_ENABLED", "",
            ).lower() in ("1", "true", "yes"),
            auth_token=os.getenv("ANTHROPIC_OAUTH_TOKEN", ""),
            model=os.getenv(
                "PMM1_LLM_MODEL",
                "claude-opus-4-6-20250610",
            ),
            thinking_budget=int(
                os.getenv("PMM1_LLM_THINKING_BUDGET", "10000"),
            ),
            cycle_interval_s=float(
                os.getenv("PMM1_LLM_CYCLE_INTERVAL", "120"),
            ),
        )


# ── Paper 1 + Paper 2 Prompts ──

_SYSTEM_PROMPT = """\
You are a superforecaster-calibrated probability estimator for \
prediction markets. Your goal is to MINIMIZE your Brier score \
— that is, (your_probability - actual_outcome)^2 averaged over \
many predictions.

Calibration rules (from forecasting research):
1. RANGE FIRST: Before giving a point estimate, state your \
credible interval [low, high] for the true probability.
2. BASE RATES: Start with the base rate for this category of \
event before incorporating specific evidence. Recency bias \
is your biggest enemy — recent headlines feel important but \
base rates are more predictive.
3. EXTREMIZE: RLHF training makes you hedge toward 50%. Fight \
this. If your analysis says 75%, say 75%, not 65%.
4. CONTRA-REASONING: After forming your estimate, generate the \
strongest argument AGAINST your own position. If the contra \
argument is compelling, adjust.
5. UNCERTAINTY: State your genuine uncertainty. High uncertainty \
means we should trust the market more than you.

Output ONLY valid JSON."""


_BLIND_PROMPT_TEMPLATE = """\
MARKET QUESTION: {question}

RESOLUTION CRITERIA: {resolution_criteria}

{context_block}

IMPORTANT: You have NOT been shown the current market price. \
Form your estimate from fundamentals only.

Step 1: What is the base rate for this type of event?
Step 2: What specific evidence shifts the probability?
Step 3: State your credible interval [low, high].
Step 4: Give your point estimate.
Step 5: What is the strongest argument against your estimate?

Output JSON:
{{
    "base_rate": 0.50,
    "credible_interval": [0.40, 0.70],
    "p_hat": 0.55,
    "uncertainty": 0.15,
    "reasoning_summary": "...",
    "contra_argument": "The strongest case against my estimate is...",
    "key_evidence": ["evidence1", "evidence2"],
    "risk_flags": []
}}"""


_CHALLENGE_PROMPT_TEMPLATE = """\
You previously estimated this market BLIND (without seeing \
the market price):

YOUR BLIND ESTIMATE: {blind_p:.3f} ± {blind_uncertainty:.3f}
YOUR REASONING: {blind_reasoning}
YOUR CONTRA: {contra_argument}

NOW: The market price is {market_price:.3f}.

{book_block}

The market disagrees with you by {disagreement:.1f} percentage points.

TASK: Reconcile your estimate with the market price.
- If the market knows something you don't, adjust toward it.
- If your analysis is stronger, hold your ground.
- Markets are efficient on average but can be wrong on specifics.
- Paper 1 research: optimal blend is ~67% market / ~33% your \
estimate. But if you have specific information the market lacks, \
deviate more.

Output JSON:
{{
    "p_hat_final": 0.55,
    "uncertainty": 0.12,
    "market_weight": 0.67,
    "model_weight": 0.33,
    "reasoning_summary": "After seeing market at X, I adjust because...",
    "adjustment_reason": "why I moved toward/away from market",
    "risk_flags": []
}}"""


class LLMReasoner:
    """Background LLM reasoning loop with Paper 1+2 pipeline.

    Two-pass architecture per market:
    1. Blind estimate (no market price) — prevents price copying
    2. Market-aware challenge — reconcile with market
    3. Extremize + cache

    The quote loop reads from cache, never blocks.
    """

    def __init__(
        self,
        config: ReasonerConfig,
        bot_state: Any = None,
    ) -> None:
        self.config = config
        self.bot_state = bot_state
        self._cache: dict[str, LLMEstimate] = {}
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._cycle_count = 0
        self._total_calls = 0
        self._total_errors = 0
        self._calibration_history: list[dict[str, float]] = []

    def set_bot_state(self, state: Any) -> None:
        """Inject bot state after construction."""
        self.bot_state = state

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
        """Get cached estimate (non-blocking)."""
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
        """Get LLM-blended fair value.

        Paper 1: optimal ~67% market / ~33% AI.
        """
        est = self.get_estimate(condition_id)
        meta: dict[str, Any] = {"llm_used": False}

        if est is None:
            return book_midpoint, meta

        confidence = 1.0 - est.uncertainty
        if confidence < self.config.min_confidence:
            meta["miss_reason"] = "low_confidence"
            return book_midpoint, meta

        p_decayed = est.decay_toward_market(
            book_midpoint, self.config.decay_half_life_s,
        )

        blended = (
            (1.0 - blend_weight) * book_midpoint
            + blend_weight * p_decayed
        )

        meta.update({
            "llm_used": True,
            "p_blind": round(est.p_blind, 4),
            "p_challenged": round(est.p_challenged, 4),
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
        resolution_criteria: str = "",
        extra_context: str = "",
    ) -> LLMEstimate | None:
        """Full two-pass analysis of a single market.

        Pass 1: Blind estimate (no market price shown)
        Pass 2: Market-aware challenge (reconcile with market)
        Post: Extremize for RLHF correction
        """
        start = time.time()
        total_input = 0
        total_output = 0
        any_cache_hit = False

        # ── PASS 1: Blind estimate ──
        context_block = ""
        if extra_context:
            context_block = f"AVAILABLE CONTEXT:\n{extra_context}"

        blind_prompt = _BLIND_PROMPT_TEMPLATE.format(
            question=question,
            resolution_criteria=(
                resolution_criteria or "Standard market resolution"
            ),
            context_block=context_block,
        )

        try:
            blind_resp = await self._call_opus(blind_prompt)
            total_input += blind_resp.get("input_tokens", 0)
            total_output += blind_resp.get("output_tokens", 0)
            any_cache_hit = any_cache_hit or blind_resp.get(
                "cache_hit", False,
            )

            blind_parsed = self._parse_response(blind_resp["text"])
            if blind_parsed is None:
                self._total_errors += 1
                return None

            p_blind = float(blind_parsed.get("p_hat", 0.5))
            blind_uncertainty = float(
                blind_parsed.get("uncertainty", 0.25),
            )
            blind_reasoning = blind_parsed.get(
                "reasoning_summary", "",
            )
            contra_argument = blind_parsed.get(
                "contra_argument", "",
            )

            logger.info(
                "llm_blind_pass",
                condition_id=condition_id[:16],
                p_blind=round(p_blind, 3),
                question=question[:50],
            )

        except Exception as e:
            self._total_errors += 1
            logger.error(
                "llm_blind_pass_failed",
                condition_id=condition_id[:16],
                error=str(e),
            )
            return None

        # ── PASS 2: Market-aware challenge ──
        disagreement = abs(p_blind - midpoint) * 100

        book_block = ""
        if book_summary:
            book_block = f"ORDER BOOK STATE:\n{book_summary}"

        challenge_prompt = _CHALLENGE_PROMPT_TEMPLATE.format(
            blind_p=p_blind,
            blind_uncertainty=blind_uncertainty,
            blind_reasoning=blind_reasoning,
            contra_argument=contra_argument,
            market_price=midpoint,
            disagreement=disagreement,
            book_block=book_block,
        )

        try:
            challenge_resp = await self._call_opus(challenge_prompt)
            total_input += challenge_resp.get("input_tokens", 0)
            total_output += challenge_resp.get("output_tokens", 0)
            any_cache_hit = (
                any_cache_hit
                or challenge_resp.get("cache_hit", False)
            )

            challenge_parsed = self._parse_response(
                challenge_resp["text"],
            )
            if challenge_parsed is None:
                # Fall back to blind estimate
                p_challenged = p_blind
                uncertainty = blind_uncertainty
            else:
                p_challenged = float(
                    challenge_parsed.get("p_hat_final", p_blind),
                )
                uncertainty = float(
                    challenge_parsed.get(
                        "uncertainty", blind_uncertainty,
                    ),
                )

            logger.info(
                "llm_challenge_pass",
                condition_id=condition_id[:16],
                p_blind=round(p_blind, 3),
                p_challenged=round(p_challenged, 3),
                market=round(midpoint, 3),
                disagreement=round(disagreement, 1),
            )

        except Exception as e:
            # Fallback to blind estimate on challenge failure
            logger.warning(
                "llm_challenge_failed_using_blind",
                condition_id=condition_id[:16],
                error=str(e),
            )
            p_challenged = p_blind
            uncertainty = blind_uncertainty

        # ── POST: Extremize ──
        p_calibrated = extremize(
            p_challenged, self.config.extremization_alpha,
        )

        latency = (time.time() - start) * 1000

        estimate = LLMEstimate(
            condition_id=condition_id,
            p_blind=p_blind,
            p_challenged=p_challenged,
            p_calibrated=p_calibrated,
            uncertainty=uncertainty,
            reasoning=blind_reasoning,
            contra_points=contra_argument,
            model=self.config.model,
            latency_ms=latency,
            input_tokens=total_input,
            output_tokens=total_output,
            cache_hit=any_cache_hit,
        )

        self._cache[condition_id] = estimate
        self._total_calls += 1

        logger.info(
            "llm_estimate_complete",
            condition_id=condition_id[:16],
            p_blind=round(p_blind, 3),
            p_challenged=round(p_challenged, 3),
            p_calibrated=round(p_calibrated, 3),
            uncertainty=round(uncertainty, 3),
            latency_ms=round(latency),
            input_tokens=total_input,
            output_tokens=total_output,
            question=question[:50],
        )
        return estimate

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
        """Background reasoning loop.

        Cycles through active markets, prioritizing those with
        stale or missing estimates.
        """
        # Wait for bot state to be available
        await asyncio.sleep(30)

        while self._running:
            try:
                markets = self._get_priority_markets()
                analyzed = 0

                for market in markets:
                    if not self._running:
                        break
                    if analyzed >= self.config.max_markets_per_cycle:
                        break

                    cid = market.get("condition_id", "")
                    existing = self._cache.get(cid)

                    # Skip if estimate is still fresh
                    if existing and existing.age_seconds < (
                        self.config.cycle_interval_s * 0.8
                    ):
                        continue

                    book = None
                    book_summary = ""
                    if self.bot_state:
                        tid = market.get("token_id", "")
                        book = self.bot_state.book_manager.get(tid)
                        if book:
                            mid = book.get_midpoint()
                            best_bid = book.best_bid
                            best_ask = book.best_ask
                            book_summary = (
                                f"Best bid: {best_bid:.3f}, "
                                f"Best ask: {best_ask:.3f}, "
                                f"Midpoint: {mid:.3f}, "
                                f"Spread: "
                                f"{(best_ask - best_bid):.3f}"
                            )
                        else:
                            mid = 0.5
                    else:
                        mid = market.get("midpoint", 0.5)

                    await self.analyze_market(
                        condition_id=cid,
                        question=market.get("question", ""),
                        midpoint=mid,
                        book_summary=book_summary,
                        resolution_criteria=market.get(
                            "resolution_criteria", "",
                        ),
                    )
                    analyzed += 1

                    # Brief pause between markets
                    await asyncio.sleep(2)

                self._cycle_count += 1
                logger.info(
                    "llm_cycle_complete",
                    cycle=self._cycle_count,
                    analyzed=analyzed,
                    cached=len(self._cache),
                )

                await asyncio.sleep(self.config.cycle_interval_s)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("llm_loop_error", error=str(e))
                await asyncio.sleep(30)

    def _get_priority_markets(self) -> list[dict[str, Any]]:
        """Get markets to analyze, prioritized by staleness."""
        if not self.bot_state:
            return []

        markets: list[dict[str, Any]] = []
        for cid, md in self.bot_state.active_markets.items():
            # Skip extreme-priced markets (no edge)
            tid = getattr(md, "token_id_yes", "")
            book = self.bot_state.book_manager.get(tid)
            mid = book.get_midpoint() if book else 0.5
            if mid < 0.10 or mid > 0.90:
                continue

            existing = self._cache.get(cid)
            staleness = (
                existing.age_seconds if existing else 99999.0
            )

            markets.append({
                "condition_id": cid,
                "question": getattr(md, "question", ""),
                "token_id": tid,
                "midpoint": mid,
                "resolution_criteria": getattr(
                    md, "resolution_source", "",
                ),
                "staleness": staleness,
            })

        # Most stale first
        markets.sort(key=lambda m: -m["staleness"])
        return markets

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
