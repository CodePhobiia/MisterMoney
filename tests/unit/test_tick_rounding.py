"""Tests for tick rounding — round_bid, round_ask, round_size, price_to_string."""

from decimal import Decimal

import pytest

from pmm1.execution.tick_rounding import (
    MIN_PRICE,
    MAX_PRICE,
    TICK_STANDARD,
    TICK_FINE,
    TICK_ULTRA_FINE,
    round_bid,
    round_ask,
    round_size,
    round_price,
    price_to_string,
    is_valid_tick,
    ensure_spread,
)


class TestRoundBid:
    """round_bid always rounds DOWN (conservative for buyer)."""

    def test_exact_tick(self):
        assert round_bid(0.50, TICK_STANDARD) == Decimal("0.50")

    def test_rounds_down(self):
        assert round_bid(0.505, TICK_STANDARD) == Decimal("0.50")

    def test_rounds_down_near_tick(self):
        assert round_bid(0.509, TICK_STANDARD) == Decimal("0.50")

    def test_fine_tick(self):
        assert round_bid(0.5005, TICK_FINE) == Decimal("0.500")

    def test_ultra_fine_tick(self):
        assert round_bid(0.50005, TICK_ULTRA_FINE) == Decimal("0.5000")

    def test_clamps_to_min(self):
        assert round_bid(0.0001, TICK_STANDARD) == MIN_PRICE

    def test_clamps_to_max(self):
        # round_bid rounds DOWN, so max valid bid at 0.01 tick = 0.99
        result = round_bid(1.5, TICK_STANDARD)
        assert result <= MAX_PRICE
        assert result == Decimal("0.99")


class TestRoundAsk:
    """round_ask always rounds UP (conservative for seller)."""

    def test_exact_tick(self):
        assert round_ask(0.50, TICK_STANDARD) == Decimal("0.50")

    def test_rounds_up(self):
        assert round_ask(0.501, TICK_STANDARD) == Decimal("0.51")

    def test_rounds_up_near_tick(self):
        assert round_ask(0.5001, TICK_STANDARD) == Decimal("0.51")

    def test_fine_tick(self):
        assert round_ask(0.5001, TICK_FINE) == Decimal("0.501")

    def test_clamps_to_max(self):
        result = round_ask(1.5, TICK_STANDARD)
        assert result == MAX_PRICE


class TestRoundSize:
    def test_exact(self):
        assert round_size(10.0) == Decimal("10.00")

    def test_rounds_down(self):
        assert round_size(10.005) == Decimal("10.00")

    def test_min_size(self):
        assert round_size(0.001) == Decimal("0.01")


class TestPriceToString:
    def test_standard_price(self):
        result = price_to_string(Decimal("0.50"))
        assert result == "0.50"

    def test_trailing_zeros_kept_to_two(self):
        result = price_to_string(Decimal("0.50"))
        assert len(result.split(".")[1]) >= 2

    def test_fine_price(self):
        result = price_to_string(Decimal("0.505"))
        assert "0.505" in result


class TestIsValidTick:
    def test_valid(self):
        assert is_valid_tick(0.50, TICK_STANDARD) is True

    def test_invalid(self):
        assert is_valid_tick(0.505, TICK_STANDARD) is False

    def test_out_of_range(self):
        assert is_valid_tick(0.0, TICK_STANDARD) is False
        assert is_valid_tick(1.0, TICK_STANDARD) is False


class TestEnsureSpread:
    def test_normal_spread(self):
        bid, ask = ensure_spread(Decimal("0.49"), Decimal("0.51"))
        assert bid < ask
        assert ask - bid >= TICK_STANDARD

    def test_crossed_spread(self):
        bid, ask = ensure_spread(Decimal("0.51"), Decimal("0.49"))
        assert bid < ask
        assert ask - bid >= TICK_STANDARD
