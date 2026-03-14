from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from pmm1.state.books import build_order_book_from_snapshot


def test_build_order_book_from_snapshot_populates_best_levels() -> None:
    book = build_order_book_from_snapshot(
        "token-1",
        bids=[SimpleNamespace(price="0.48", size="12")],
        asks=[SimpleNamespace(price="0.52", size="7")],
        tick_size=Decimal("0.01"),
    )

    best_bid = book.get_best_bid()
    best_ask = book.get_best_ask()

    assert best_bid is not None
    assert best_bid.price_float == 0.48
    assert best_bid.size_float == 12.0
    assert best_ask is not None
    assert best_ask.price_float == 0.52
    assert best_ask.size_float == 7.0
