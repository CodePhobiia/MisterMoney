"""Tests for position state machine — MarketPosition and PositionTracker."""

import pytest

from pmm1.state.positions import MarketPosition, PositionTracker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COND = "cond_abc"
YES_TOK = "tok_yes_123"
NO_TOK = "tok_no_456"
EVENT = "event_1"


def _fresh_position(**overrides) -> MarketPosition:
    defaults = dict(
        condition_id=COND,
        token_id_yes=YES_TOK,
        token_id_no=NO_TOK,
    )
    defaults.update(overrides)
    return MarketPosition(**defaults)


def _fresh_tracker() -> PositionTracker:
    tracker = PositionTracker()
    tracker.register_market(
        COND, YES_TOK, NO_TOK, event_id=EVENT,
    )
    return tracker


# ---------------------------------------------------------------------------
# 1. New position from a BUY fill (YES side)
# ---------------------------------------------------------------------------


class TestBuyFillCreatesPosition:
    def test_buy_yes_creates_long(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.60)
        assert pos.yes_size == 100
        assert pos.yes_avg_price == pytest.approx(0.60)
        assert pos.yes_cost_basis == pytest.approx(60.0)
        assert pos.no_size == 0

    def test_buy_yes_with_fee(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=50, price=0.40, fee=1.0)
        assert pos.yes_size == 50
        # cost_basis = 50*0.40 + 1.0 = 21.0
        assert pos.yes_cost_basis == pytest.approx(21.0)
        # avg_price = 21 / 50 = 0.42
        assert pos.yes_avg_price == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# 2. New position from a SELL fill (short / reducing from zero)
# ---------------------------------------------------------------------------


class TestSellFillFromFlat:
    def test_sell_yes_from_zero_does_nothing(self):
        """Selling YES when size is 0 should not change position."""
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "SELL", size=10, price=0.70)
        assert pos.yes_size == 0
        assert pos.realized_pnl == 0.0

    def test_sell_no_from_zero_does_nothing(self):
        pos = _fresh_position()
        pos.apply_fill(NO_TOK, "SELL", size=10, price=0.30)
        assert pos.no_size == 0


# ---------------------------------------------------------------------------
# 3. Position increase (same-side fill)
# ---------------------------------------------------------------------------


class TestPositionIncrease:
    def test_two_buys_average_price(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.50)
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.70)
        assert pos.yes_size == 200
        # cost = 50 + 70 = 120, avg = 0.60
        assert pos.yes_avg_price == pytest.approx(0.60)
        assert pos.yes_cost_basis == pytest.approx(120.0)

    def test_no_side_increase(self):
        pos = _fresh_position()
        pos.apply_fill(NO_TOK, "BUY", size=40, price=0.30)
        pos.apply_fill(NO_TOK, "BUY", size=60, price=0.50)
        assert pos.no_size == 100
        # cost = 12 + 30 = 42, avg = 0.42
        assert pos.no_avg_price == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# 4. Position decrease (opposite-side fill)
# ---------------------------------------------------------------------------


class TestPositionDecrease:
    def test_partial_sell_reduces_yes(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.50)
        pos.apply_fill(YES_TOK, "SELL", size=40, price=0.60)
        assert pos.yes_size == 60
        # realized pnl = 40 * (0.60 - 0.50) = 4.0
        assert pos.realized_pnl == pytest.approx(4.0)

    def test_partial_sell_reduces_no(self):
        pos = _fresh_position()
        pos.apply_fill(NO_TOK, "BUY", size=80, price=0.25)
        pos.apply_fill(NO_TOK, "SELL", size=30, price=0.35)
        assert pos.no_size == 50
        # realized pnl = 30 * (0.35 - 0.25) = 3.0
        assert pos.realized_pnl == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# 5. Position flatten (closing fill)
# ---------------------------------------------------------------------------


class TestPositionFlatten:
    def test_full_sell_flattens_yes(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.50)
        pos.apply_fill(YES_TOK, "SELL", size=100, price=0.55)
        assert pos.yes_size == 0
        assert pos.is_flat
        assert pos.realized_pnl == pytest.approx(5.0)

    def test_oversell_clamps_to_zero(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=50, price=0.50)
        pos.apply_fill(YES_TOK, "SELL", size=80, price=0.60)
        assert pos.yes_size == 0  # clamped via max(0, ...)


# ---------------------------------------------------------------------------
# 6. Mark-to-market
# ---------------------------------------------------------------------------


class TestMarkToMarket:
    def test_unrealized_pnl_positive(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.50)
        mtm = pos.mark_to_market(yes_price=0.70, no_price=0.30)
        # 100 * (0.70 - 0.50) = 20.0
        assert mtm == pytest.approx(20.0)

    def test_unrealized_pnl_negative(self):
        pos = _fresh_position()
        pos.apply_fill(NO_TOK, "BUY", size=50, price=0.40)
        mtm = pos.mark_to_market(yes_price=0.60, no_price=0.30)
        # 50 * (0.30 - 0.40) = -5.0
        assert mtm == pytest.approx(-5.0)

    def test_flat_position_zero_mtm(self):
        pos = _fresh_position()
        assert pos.mark_to_market(0.60, 0.40) == 0.0

    def test_both_sides(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.50)
        pos.apply_fill(NO_TOK, "BUY", size=100, price=0.40)
        mtm = pos.mark_to_market(yes_price=0.55, no_price=0.45)
        # yes: 100*(0.55-0.50)=5, no: 100*(0.45-0.40)=5 => 10
        assert mtm == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 7. Cost basis tracking
# ---------------------------------------------------------------------------


class TestCostBasis:
    def test_cost_basis_after_buy(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=200, price=0.45)
        assert pos.yes_cost_basis == pytest.approx(90.0)
        assert pos.total_cost_basis == pytest.approx(90.0)

    def test_cost_basis_both_sides(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.60)
        pos.apply_fill(NO_TOK, "BUY", size=100, price=0.35)
        assert pos.yes_cost_basis == pytest.approx(60.0)
        assert pos.no_cost_basis == pytest.approx(35.0)
        assert pos.total_cost_basis == pytest.approx(95.0)

    def test_cost_basis_after_partial_sell(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.50)
        pos.apply_fill(YES_TOK, "SELL", size=60, price=0.55)
        # remaining: 40 shares, avg 0.50 => cost_basis = 20
        assert pos.yes_cost_basis == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# 8. Realized PnL on partial close
# ---------------------------------------------------------------------------


class TestRealizedPnl:
    def test_pnl_winning_trade(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.40)
        pos.apply_fill(YES_TOK, "SELL", size=50, price=0.60)
        # 50 * (0.60 - 0.40) = 10.0
        assert pos.realized_pnl == pytest.approx(10.0)

    def test_pnl_losing_trade(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.60)
        pos.apply_fill(YES_TOK, "SELL", size=100, price=0.45)
        # 100 * (0.45 - 0.60) = -15.0
        assert pos.realized_pnl == pytest.approx(-15.0)

    def test_pnl_with_fee(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.50, fee=2.0)
        pos.apply_fill(YES_TOK, "SELL", size=100, price=0.60, fee=1.0)
        # avg_price = (50+2)/100 = 0.52
        # pnl = 100 * (0.60 - 0.52) - 1.0 = 8 - 1 = 7.0
        assert pos.realized_pnl == pytest.approx(7.0)

    def test_pnl_accumulates_across_sells(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.50)
        pos.apply_fill(YES_TOK, "SELL", size=30, price=0.60)
        pos.apply_fill(YES_TOK, "SELL", size=30, price=0.55)
        # first: 30*(0.60-0.50)=3, second: 30*(0.55-0.50)=1.5
        assert pos.realized_pnl == pytest.approx(4.5)


# ---------------------------------------------------------------------------
# 9. Net exposure calculation
# ---------------------------------------------------------------------------


class TestNetExposure:
    def test_yes_only(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.50)
        assert pos.net_exposure == pytest.approx(100.0)

    def test_no_only(self):
        pos = _fresh_position()
        pos.apply_fill(NO_TOK, "BUY", size=80, price=0.40)
        assert pos.net_exposure == pytest.approx(-80.0)

    def test_both_sides_net(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.50)
        pos.apply_fill(NO_TOK, "BUY", size=60, price=0.40)
        assert pos.net_exposure == pytest.approx(40.0)

    def test_gross_exposure(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=100, price=0.50)
        pos.apply_fill(NO_TOK, "BUY", size=60, price=0.40)
        assert pos.gross_exposure == pytest.approx(160.0)


# ---------------------------------------------------------------------------
# 10. get_active_positions returns non-flat positions
# ---------------------------------------------------------------------------


class TestGetActivePositions:
    def test_empty_tracker(self):
        tracker = PositionTracker()
        assert tracker.get_active_positions() == []

    def test_only_active_returned(self):
        tracker = PositionTracker()
        tracker.register_market("c1", "y1", "n1")
        tracker.register_market("c2", "y2", "n2")
        tracker.register_market("c3", "y3", "n3")
        tracker.apply_fill("y1", "BUY", 50, 0.5)
        # c2 stays flat
        tracker.apply_fill("y3", "BUY", 30, 0.6)

        active = tracker.get_active_positions()
        active_ids = {p.condition_id for p in active}
        assert active_ids == {"c1", "c3"}
        assert tracker.active_count == 2

    def test_flattened_position_excluded(self):
        tracker = _fresh_tracker()
        tracker.apply_fill(YES_TOK, "BUY", 100, 0.50)
        assert tracker.active_count == 1
        tracker.apply_fill(YES_TOK, "SELL", 100, 0.55)
        assert tracker.active_count == 0


# ---------------------------------------------------------------------------
# 11. is_flat property
# ---------------------------------------------------------------------------


class TestIsFlat:
    def test_new_position_is_flat(self):
        pos = _fresh_position()
        assert pos.is_flat is True

    def test_position_with_yes_is_not_flat(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=1, price=0.50)
        assert pos.is_flat is False

    def test_position_with_no_is_not_flat(self):
        pos = _fresh_position()
        pos.apply_fill(NO_TOK, "BUY", size=1, price=0.30)
        assert pos.is_flat is False

    def test_position_after_flatten_is_flat(self):
        pos = _fresh_position()
        pos.apply_fill(YES_TOK, "BUY", size=50, price=0.50)
        pos.apply_fill(YES_TOK, "SELL", size=50, price=0.55)
        assert pos.is_flat is True


# ---------------------------------------------------------------------------
# PositionTracker integration helpers
# ---------------------------------------------------------------------------


class TestPositionTracker:
    def test_register_and_get(self):
        tracker = _fresh_tracker()
        pos = tracker.get(COND)
        assert pos is not None
        assert pos.condition_id == COND

    def test_get_by_token(self):
        tracker = _fresh_tracker()
        assert tracker.get_by_token(YES_TOK) is not None
        assert tracker.get_by_token(NO_TOK) is not None
        assert tracker.get_by_token("unknown") is None

    def test_apply_fill_via_tracker(self):
        tracker = _fresh_tracker()
        result = tracker.apply_fill(YES_TOK, "BUY", 100, 0.55)
        assert result is not None
        assert result.yes_size == 100

    def test_apply_fill_unknown_token(self):
        tracker = _fresh_tracker()
        result = tracker.apply_fill("unknown_tok", "BUY", 10, 0.5)
        assert result is None

    def test_event_positions(self):
        tracker = _fresh_tracker()
        positions = tracker.get_event_positions(EVENT)
        assert len(positions) == 1
        assert positions[0].condition_id == COND

    def test_total_realized_pnl(self):
        tracker = PositionTracker()
        tracker.register_market("c1", "y1", "n1")
        tracker.register_market("c2", "y2", "n2")
        tracker.apply_fill("y1", "BUY", 100, 0.50)
        tracker.apply_fill("y1", "SELL", 100, 0.60)
        tracker.apply_fill("y2", "BUY", 50, 0.40)
        tracker.apply_fill("y2", "SELL", 50, 0.30)
        # c1 pnl = 100*(0.60-0.50) = 10
        # c2 pnl = 50*(0.30-0.40) = -5
        assert tracker.get_total_realized_pnl() == pytest.approx(5.0)

    def test_market_count(self):
        tracker = _fresh_tracker()
        assert tracker.market_count == 1
        tracker.register_market("c2", "y2", "n2")
        assert tracker.market_count == 2

    def test_register_updates_existing(self):
        tracker = _fresh_tracker()
        tracker.apply_fill(YES_TOK, "BUY", 100, 0.50)
        # Re-register same condition with new tokens
        tracker.register_market(
            COND, "new_yes", "new_no", neg_risk=True,
        )
        pos = tracker.get(COND)
        assert pos is not None
        assert pos.yes_size == 100  # preserved
        assert pos.token_id_yes == "new_yes"
        assert pos.neg_risk is True

    def test_total_net_exposure(self):
        tracker = PositionTracker()
        tracker.register_market("c1", "y1", "n1")
        tracker.register_market("c2", "y2", "n2")
        tracker.apply_fill("y1", "BUY", 100, 0.50)
        tracker.apply_fill("n2", "BUY", 60, 0.40)
        # c1 net_exposure_usdc = 100*0.50 = 50 (abs)
        # c2 net_exposure_usdc = -(60*0.40) = -24 (abs=24)
        total = tracker.get_total_net_exposure()
        assert total == pytest.approx(74.0)

    def test_total_gross_exposure(self):
        tracker = PositionTracker()
        tracker.register_market("c1", "y1", "n1")
        tracker.apply_fill("y1", "BUY", 100, 0.50)
        tracker.apply_fill("n1", "BUY", 60, 0.40)
        total = tracker.get_total_gross_exposure()
        # 100*0.50 + 60*0.40 = 50 + 24 = 74
        assert total == pytest.approx(74.0)
