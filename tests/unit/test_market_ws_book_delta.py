from __future__ import annotations

from pmm1.state.books import BookManager
from pmm1.ws.market_ws import MarketWebSocket


async def test_market_ws_emits_book_delta_callback():
    deltas: list[tuple[str, float, float, float]] = []

    async def on_book_delta(token_id: str, price: float, old_size: float, new_size: float) -> None:
        deltas.append((token_id, price, old_size, new_size))

    manager = BookManager()
    ws = MarketWebSocket(book_manager=manager, on_book_delta=on_book_delta)
    book = manager.get_or_create("token-1")
    book.apply_snapshot(
        [{"price": "0.48", "size": "10"}],
        [{"price": "0.52", "size": "8"}],
    )

    await ws._handle_price_change(
        "token-1",
        {"changes": [{"side": "bids", "price": "0.48", "size": "7"}]},
    )

    assert deltas == [("token-1", 0.48, 10.0, 7.0)]
