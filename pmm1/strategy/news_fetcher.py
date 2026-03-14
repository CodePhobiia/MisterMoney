"""External news context for the LLM reasoner.

Supports multiple backends:
- perplexity: Perplexity API (real-time web search + synthesis)
- none: Disabled (no external calls)

Paper 1 insight: retrieval-augmented generation is "the single
most important component" of effective LLM forecasting.
"""

from __future__ import annotations

import os
import time
from typing import Any

import aiohttp
import structlog

logger = structlog.get_logger(__name__)


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

    @classmethod
    def from_env(cls) -> NewsFetcher:
        """Load configuration from environment variables."""
        return cls(
            backend=os.getenv("PMM1_NEWS_BACKEND", "none"),
            api_key=os.getenv("PMM1_NEWS_API_KEY", ""),
        )

    async def fetch_context(
        self, question: str, max_words: int = 200,
    ) -> str:
        """Fetch and summarize relevant news for a market question.

        Returns formatted string for prompt injection, or empty
        string if backend is disabled or fetch fails.
        """
        if self.backend == "none" or not self.api_key:
            return ""

        # Check cache
        cache_key = question[:100]
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
            f"max {max_words} words. Include dates."
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
