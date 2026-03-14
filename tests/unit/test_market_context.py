"""Tests for MarketContextBuilder."""

from pmm1.strategy.market_context import MarketContextBuilder


class FakeBook:
    def get_midpoint(self): return 0.55
    def get_microprice(self): return 0.53
    def get_imbalance(self): return -0.15
    def get_bids(self, n=3): return [FakeLevel(0.54, 150), FakeLevel(0.53, 200)]
    def get_asks(self, n=3): return [FakeLevel(0.56, 80), FakeLevel(0.57, 120)]
    def get_depth_within(self, cents, side="both"):
        if side == "bid": return 350.0
        if side == "ask": return 200.0
        return 550.0


class FakeLevel:
    def __init__(self, price, size):
        self.price = price
        self.size = size


class FakeMarketMd:
    is_sports = True
    is_crypto_intraday = False
    theme = "Sports"
    end_date = None
    volume_24h = 50000.0
    liquidity = 25000.0
    toxicity_estimate = 0.015
    reward_eligible = True
    reward_daily_rate = 0.001
    neg_risk = False
    fees_enabled = False
    fee_rate = 0.0


def test_book_summary_includes_depth():
    ctx = MarketContextBuilder()
    summary = ctx.build_book_summary(FakeBook())
    assert "Bid" in summary
    assert "Ask" in summary
    assert "Imbalance" in summary
    assert "Microprice" in summary
    assert "Depth within 2" in summary
    assert "sell pressure" in summary or "buy" in summary


def test_book_summary_none():
    ctx = MarketContextBuilder()
    assert ctx.build_book_summary(None) == ""


def test_market_metadata_sports():
    ctx = MarketContextBuilder()
    meta = ctx.build_market_metadata(FakeMarketMd())
    assert "Sports" in meta
    assert "$50,000" in meta
    assert "Reward eligible" in meta
    assert "Adverse selection" in meta


def test_cross_market_empty_event():
    ctx = MarketContextBuilder()
    result = ctx.build_cross_market_context("cid1", "", {}, None)
    assert result == ""


def test_cross_market_finds_related():
    class FakeMd:
        event_id = "evt1"
        token_id_yes = "tok1"
        question = "Will it rain?"

    class FakeBookMgr:
        def get(self, tid):
            book = type('B', (), {'get_midpoint': lambda self: 0.60})()
            return book

    markets = {
        "cid1": FakeMd(),
        "cid2": FakeMd(),
    }

    ctx = MarketContextBuilder()
    result = ctx.build_cross_market_context(
        "cid1", "evt1", markets, FakeBookMgr(),
    )
    assert "RELATED MARKETS" in result
    assert "Will it rain?" in result
