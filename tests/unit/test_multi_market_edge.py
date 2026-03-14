"""Tests for MultiMarketEdgeController -- Benjamini-Hochberg FDR correction (ST-02).

Validates that:
1. With all-null markets, FDR controls false positives
2. With real-edge markets, BH confirms them
3. Confidence is capped when individual SPRT passes but FDR fails
"""



from pmm1.analytics.edge_tracker import EdgeTracker, MultiMarketEdgeController


def _record_interleaved_trades(
    tracker: EdgeTracker,
    n_trades: int,
    wins: int,
    predicted_p: float = 0.50,
    market_p: float = 0.50,
    pnl_win: float = 0.01,
    pnl_loss: float = -0.01,
) -> None:
    """Record trades with interleaved wins/losses to avoid sequential bias.

    Distributes wins evenly across the trade sequence rather than putting
    all wins first (which would cause spurious early SPRT confirmation).
    """
    losses = n_trades - wins
    # Build interleaved sequence: distribute wins as evenly as possible
    outcomes: list[float] = []
    w_remaining = wins
    l_remaining = losses
    for i in range(n_trades):
        remaining = n_trades - i
        # Probability of next being a win, proportional to remaining wins
        if remaining > 0 and w_remaining / remaining > l_remaining / remaining:
            outcomes.append(1.0)
            w_remaining -= 1
        else:
            outcomes.append(0.0)
            l_remaining -= 1
    for o in outcomes:
        tracker.record_trade(
            predicted_p=predicted_p,
            market_p=market_p,
            outcome=o,
            pnl=pnl_win if o > 0.5 else pnl_loss,
        )


class TestBHFDRNoFalsePositives:
    """ST-02: 20 null markets -> BH should confirm very few (<=2)."""

    def test_bh_fdr_no_false_positives(self):
        """20 markets, all null (no edge) -> BH should confirm <= 2."""
        controller = MultiMarketEdgeController(fdr_level=0.10)

        # Create 20 null markets with slight random variation in win rate
        # All have true win rate ~50% (no edge)
        for i in range(20):
            cid = f"market-{i}"
            tracker = controller.get_or_create_tracker(cid, min_trades=50)
            # 100 trades at ~50% win rate
            n_trades = 100
            wins = 50 + (i % 3 - 1)  # 49, 50, or 51 wins -- all near null
            _record_interleaved_trades(tracker, n_trades, wins)

        confirmed = controller.apply_fdr_correction()
        # With all-null data, BH at FDR=0.10 should confirm at most 2
        # (10% of 20 = 2 expected false discoveries at most)
        assert len(confirmed) <= 2, (
            f"BH confirmed {len(confirmed)} markets from 20 nulls, expected <= 2"
        )


class TestBHFDRConfirmsRealEdge:
    """ST-02: 20 markets, 5 with real edge -> BH confirms those 5."""

    def test_bh_fdr_confirms_real_edge(self):
        """20 markets, 5 have real edge -> BH confirms those 5."""
        controller = MultiMarketEdgeController(fdr_level=0.10)

        real_edge_ids = set()
        # 5 markets with strong real edge
        for i in range(5):
            cid = f"real-{i}"
            real_edge_ids.add(cid)
            tracker = controller.get_or_create_tracker(cid, min_trades=50)
            # 200 trades at 70% win rate -- very strong signal
            _record_interleaved_trades(
                tracker, n_trades=200, wins=140,
                predicted_p=0.70, pnl_win=0.10, pnl_loss=-0.10,
            )

        # 15 null markets
        for i in range(15):
            cid = f"null-{i}"
            tracker = controller.get_or_create_tracker(cid, min_trades=50)
            _record_interleaved_trades(tracker, n_trades=100, wins=50)

        confirmed = controller.apply_fdr_correction()

        # All 5 real-edge markets should be confirmed
        assert real_edge_ids.issubset(confirmed), (
            f"Expected all real edges {real_edge_ids} in confirmed {confirmed}"
        )
        # At most a couple false positives from null markets
        false_positives = confirmed - real_edge_ids
        assert len(false_positives) <= 2, (
            f"Too many false positives: {false_positives}"
        )


class TestFDREdgeConfidenceCapped:
    """ST-02: SPRT says confirmed but FDR fails -> confidence capped at 0.5."""

    def test_fdr_edge_confidence_capped(self):
        """Individual SPRT says confirmed but FDR fails -> confidence capped at 0.5."""
        controller = MultiMarketEdgeController(fdr_level=0.10)

        # Create a market whose SPRT says "edge_confirmed" but whose
        # batch GLR p-value is too high to survive FDR correction.
        # We do this by giving it near-null data but forcing the SPRT decision.
        borderline_cid = "borderline-0"
        tracker = controller.get_or_create_tracker(borderline_cid, min_trades=50)
        # 100 trades at 52% -- batch GLR p-value will be high (no real edge)
        _record_interleaved_trades(tracker, n_trades=100, wins=52)
        # Force the individual SPRT to say "edge_confirmed" (simulating a
        # spurious early confirmation that the FDR correction should catch)
        tracker.sprt_decision = "edge_confirmed"

        # Add 19 more null markets (all ~50%)
        for i in range(19):
            cid = f"null-{i}"
            t = controller.get_or_create_tracker(cid, min_trades=50)
            _record_interleaved_trades(t, n_trades=100, wins=50)
            # Force SPRT to say confirmed for these too
            t.sprt_decision = "edge_confirmed"

        controller.apply_fdr_correction()

        # The borderline market's SPRT says "edge_confirmed" but FDR should
        # reject it (batch GLR shows no real edge)
        assert not controller.is_edge_confirmed(borderline_cid), (
            "Borderline market should NOT survive FDR correction"
        )
        confidence = controller.get_edge_confidence(borderline_cid)
        assert confidence <= 0.5, (
            f"Confidence should be capped at 0.5 when FDR rejects, got {confidence}"
        )

    def test_fdr_confirmed_market_keeps_full_confidence(self):
        """A market confirmed by both SPRT and FDR keeps its full confidence."""
        controller = MultiMarketEdgeController(fdr_level=0.10)

        # One very strong edge market
        strong_cid = "strong-0"
        tracker = controller.get_or_create_tracker(strong_cid, min_trades=50)
        _record_interleaved_trades(
            tracker, n_trades=200, wins=140,
            predicted_p=0.70, pnl_win=0.10, pnl_loss=-0.10,
        )

        controller.apply_fdr_correction()

        assert controller.is_edge_confirmed(strong_cid), (
            "Strong edge market should survive FDR correction"
        )
        confidence = controller.get_edge_confidence(strong_cid)
        # SPRT should also have confirmed (70% win rate over 200 trades),
        # so confidence should be 1.0
        assert confidence == 1.0, (
            f"Expected full confidence 1.0 for FDR-confirmed market, got {confidence}"
        )


class TestMultiMarketEdgeControllerBasics:
    """Basic functionality tests for MultiMarketEdgeController."""

    def test_get_or_create_tracker_returns_same_instance(self):
        controller = MultiMarketEdgeController()
        t1 = controller.get_or_create_tracker("market-1")
        t2 = controller.get_or_create_tracker("market-1")
        assert t1 is t2

    def test_tracked_count(self):
        controller = MultiMarketEdgeController()
        controller.get_or_create_tracker("m1")
        controller.get_or_create_tracker("m2")
        controller.get_or_create_tracker("m3")
        assert controller.tracked_count == 3

    def test_remove_market(self):
        controller = MultiMarketEdgeController()
        controller.get_or_create_tracker("m1")
        controller.get_or_create_tracker("m2")
        controller.remove_market("m1")
        assert controller.tracked_count == 1

    def test_empty_controller_returns_empty_confirmed(self):
        controller = MultiMarketEdgeController()
        confirmed = controller.apply_fdr_correction()
        assert len(confirmed) == 0

    def test_unknown_market_confidence(self):
        controller = MultiMarketEdgeController()
        assert controller.get_edge_confidence("nonexistent") == 0.1

    def test_insufficient_trades_excluded_from_fdr(self):
        """Markets with fewer than min_trades should not be included in FDR."""
        controller = MultiMarketEdgeController(fdr_level=0.10)
        tracker = controller.get_or_create_tracker("m1", min_trades=50)
        # Only record 10 trades (below min_trades=50)
        _record_interleaved_trades(tracker, n_trades=10, wins=8)
        confirmed = controller.apply_fdr_correction()
        assert len(confirmed) == 0
