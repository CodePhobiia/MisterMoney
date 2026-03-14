"""External news context for the LLM reasoner.

Supports multiple backends:
- perplexity: Perplexity API (real-time web search + synthesis)
- none: Disabled (no external calls)

Paper 1 insight: retrieval-augmented generation is "the single
most important component" of effective LLM forecasting.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Any

import aiohttp
import structlog

logger = structlog.get_logger(__name__)

_PRICE_LEAK_PATTERNS = [
    r'(?:betting|prediction)\s+(?:odds|market)[^.]{0,40}\d+[%\u00a2]',
    r'(?:polymarket|kalshi|metaculus)[^.]{0,40}\d+[%\u00a2]',
    r'implied\s+probability[^.]{0,30}\d+',
    r'(?:bookmakers?|oddsmakers?)\s+(?:favor|give|set|odds)[^.]{0,30}\d+',
    r'market\s+(?:is\s+)?(?:pricing|priced|trading)\s+(?:at\s+)?\$?0?\.\d+',
    r'currently\s+(?:at|trading)\s+\$?0\.\d+',
    r'\d+\s*%\s*(?:chance|probability|likelihood)',
    r'(?:forecaster|analyst|expert)s?\s+(?:predict|estimate|expect|give)',
    r'(?:consensus|aggregate)\s+(?:forecast|estimate|probability)',
    r'(?:prediction|forecast)\s+(?:market|platform)',
    r'(?:trader|bettor|punter|investor)s?\s+(?:expect|believe|see)',
    # LLM-05: Additional price-leak patterns
    # Poll numbers with vs/to format
    r'\d+\s*%?\s*(?:to|vs\.?|versus)\s*\d+\s*%',
    # Analyst/expert explicit predictions
    r'(?:analyst|expert|strategist)s?\s+(?:project|forecast|predict|expect)\s+.*\d+',
    # Sports odds terminology
    r'(?:odds-on|underdog|even money|pick\s*em|moneyline)\s+(?:favorite|contender)?',
    # "Expected to win/lose" framing
    r'(?:is\s+)?(?:expected|projected|likely|poised|set)\s+to\s+(?:win|lose|pass|fail|succeed)',
    # Projected/forecasted at X%
    r'(?:projected|forecasted|estimated)\s+(?:at|to\s+be)\s+\d+',
    # Implicit consensus/aggregate
    r'(?:most|majority\s+of)\s+(?:polls?|surveys?|models?|experts?)\s+(?:show|suggest|indicate|predict)',
    # Betting line references
    r'(?:spread|over/under|line)\s+(?:is\s+)?[+-]?\d+\.?\d*',
    # Probability language with numbers
    r'(?:there\s+is\s+a|probability\s+of|chances?\s+(?:of|are))\s+\d+',
    # Futures/prediction platform mentions with values
    r'(?:futures?|prediction|forecast)\s+(?:market|contract)s?\s+(?:at|trading|priced)',
    # Implied probability from odds
    r'(?:decimal|fractional|american)\s+odds\s+(?:of\s+)?[+-]?\d+',
]


class NewsFetcher:
    """Fetches relevant news context for market questions."""

    def __init__(
        self,
        backend: str = "none",
        api_key: str = "",
        cache_ttl_s: float = 300.0,
        timeout_s: float = 15.0,
    ) -> None:
        self.backend = backend
        self.api_key = api_key
        self.cache_ttl_s = cache_ttl_s
        self.timeout_s = timeout_s
        self._cache: dict[str, tuple[float, str]] = {}
        self._session: aiohttp.ClientSession | None = None
        self._total_calls = 0
        self._total_errors = 0

    @staticmethod
    def _sanitize_question(question: str) -> str:
        """Sanitize a market question to prevent prompt injection.

        - Strips control characters (below 0x20) except space and tab.
        - Removes lines starting with common prompt injection prefixes.
        - Truncates to 500 characters.
        """
        # Strip control characters (keep space 0x20, tab 0x09, newline 0x0A)
        question = "".join(
            ch for ch in question
            if ch >= " " or ch in ("\t", "\n")
        )
        # Remove prompt injection patterns (lines beginning with role prefixes)
        question = re.sub(
            r"(?mi)^(SYSTEM|ASSISTANT|Human|user)\s*:.*$",
            "",
            question,
        )
        # Collapse any leftover blank lines from removal
        question = re.sub(r"\n{2,}", "\n", question).strip()
        # Truncate to 500 characters
        return question[:500]

    def _evict_stale_cache(self) -> None:
        """Remove stale news cache entries to prevent memory leaks."""
        stale = [
            k for k, (ts, _) in self._cache.items()
            if time.time() - ts > self.cache_ttl_s * 2
        ]
        for k in stale:
            del self._cache[k]

    @classmethod
    def from_env(cls) -> NewsFetcher:
        """Load configuration from environment variables."""
        return cls(
            backend=os.getenv("PMM1_NEWS_BACKEND", "none"),
            api_key=os.getenv("PMM1_NEWS_API_KEY", ""),
        )

    def _filter_price_leaks(self, text: str, paranoid: bool = False) -> str:
        """Strip market price references that would contaminate blind pass.

        Paper 2: LLMs show 0.994 correlation with market prices when
        shown them. Even indirect references in news can anchor.
        """
        for pattern in _PRICE_LEAK_PATTERNS:
            text = re.sub(
                pattern, '[market reference removed]',
                text, flags=re.IGNORECASE,
            )

        # LLM-05: Paranoid mode strips ALL numeric percentages
        if paranoid:
            text = re.sub(r'\d+\.?\d*\s*%', '[number removed]', text)

        return text

    async def fetch_context(
        self, question: str, max_words: int = 200,
        category: str = "",
    ) -> str:
        """Fetch and summarize relevant news for a market question.

        Returns formatted string for prompt injection, or empty
        string if backend is disabled or fetch fails.

        LLM-12: Category-aware news policy:
        - Finance/Sports: increase max_words to 400 (news is value-additive)
        - Entertainment/Technology: reduce max_words to 100 (noise amplification risk)
        """
        if self.backend == "none" or not self.api_key:
            return ""

        # LLM-12: Adjust max_words by category
        if category:
            cat_lower = category.lower()
            if cat_lower in ("finance", "sports", "economics"):
                max_words = max(max_words, 400)
            elif cat_lower in ("entertainment", "technology"):
                max_words = min(max_words, 100)

        question = self._sanitize_question(question)

        # Check cache
        cache_key = hashlib.md5(question.encode()).hexdigest()
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < self.cache_ttl_s:
            return cached[1]

        try:
            if self.backend == "perplexity":
                result = await self._fetch_perplexity(
                    question, max_words,
                )
            else:
                return ""

            result = self._filter_price_leaks(result)

            if result:
                self._cache[cache_key] = (time.time(), result)
                self._total_calls += 1
            return result

        except Exception as e:
            self._total_errors += 1
            logger.warning(
                "news_fetch_failed",
                backend=self.backend,
                error=str(e),
                question=question[:50],
            )
            return ""

    async def _fetch_perplexity(
        self, question: str, max_words: int,
    ) -> str:
        """Fetch context via Perplexity API."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        prompt = (
            f"What are the latest FACTS relevant to this "
            f"prediction market question: \"{question}\"\n\n"
            f"Focus on factual events and data, not "
            f"predictions or opinions. Be concise \u2014 "
            f"max {max_words} words. Include dates. "
            f"Cite your sources when possible."
        )

        body = {
            "model": "sonar",
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 500,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=self.timeout_s)
        async with self._session.post(
            "https://api.perplexity.ai/chat/completions",
            headers=headers,
            json=body,
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        choices = data.get("choices", [])
        if choices:
            content: str = choices[0].get("message", {}).get("content", "")
            return content.strip()
        return ""

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    def get_status(self) -> dict[str, Any]:
        """Status for ops reporting."""
        return {
            "backend": self.backend,
            "enabled": self.backend != "none" and bool(self.api_key),
            "cached_queries": len(self._cache),
            "total_calls": self._total_calls,
            "total_errors": self._total_errors,
        }
