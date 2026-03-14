"""Tests for NewsFetcher."""

import pytest

from pmm1.strategy.news_fetcher import NewsFetcher


@pytest.mark.asyncio
async def test_disabled_returns_empty():
    fetcher = NewsFetcher(backend="none")
    result = await fetcher.fetch_context("test question")
    assert result == ""


@pytest.mark.asyncio
async def test_no_api_key_returns_empty():
    fetcher = NewsFetcher(backend="perplexity", api_key="")
    result = await fetcher.fetch_context("test question")
    assert result == ""


def test_status():
    fetcher = NewsFetcher(backend="none")
    status = fetcher.get_status()
    assert status["backend"] == "none"
    assert not status["enabled"]
    assert status["total_calls"] == 0


def test_from_env():
    import os
    os.environ["PMM1_NEWS_BACKEND"] = "perplexity"
    os.environ["PMM1_NEWS_API_KEY"] = "test-key"
    try:
        fetcher = NewsFetcher.from_env()
        assert fetcher.backend == "perplexity"
        assert fetcher.api_key == "test-key"
    finally:
        os.environ.pop("PMM1_NEWS_BACKEND", None)
        os.environ.pop("PMM1_NEWS_API_KEY", None)
