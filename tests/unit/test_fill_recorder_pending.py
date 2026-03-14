from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from pmm1.recorder.fill_recorder import FillRecorder
from pmm1.state.books import BookManager
from pmm1.storage.database import Database


async def _exercise_pending_fill(db_path: str) -> dict[str, object]:
    db = Database(db_path)
    await db.init()
    recorder = FillRecorder(db, BookManager())

    fill_id = await recorder.record_fill(
        ts=datetime.now(UTC),
        condition_id="cond-1",
        token_id="token-1",
        order_id="ord-1",
        side="BUY",
        price=0.5,
        size=2.0,
        fee=None,
        mid_at_fill=None,
        is_scoring=False,
        reward_eligible=False,
        exchange_trade_id="trade-1",
        fill_identity="fill-1",
        fee_known=False,
        fee_source="unknown",
        ingest_state="pending_unknown_order",
        raw_event_json={"id": "trade-1"},
    )

    pending = await recorder.get_pending_fills("ord-1")
    await recorder.resolve_pending_fill(
        fill_id,
        condition_id="cond-1",
        token_id="token-1",
        order_id="ord-1",
        side="BUY",
        fee=0.0,
        fee_known=True,
        fee_source="zero_fee_market",
        mid_at_fill=0.5,
        is_scoring=False,
        reward_eligible=False,
        raw_event_json={"id": "trade-1"},
    )
    row = await db.fetch_one(
        """
        SELECT ingest_state, fee_known, fee_source, resolved_at, fill_identity
        FROM fill_record
        WHERE id = ?
        """,
        (fill_id,),
    )
    await db.close()
    return {
        "fill_id": fill_id,
        "pending_count": len(pending),
        "row": row or {},
    }


def test_fill_recorder_persists_and_resolves_pending_unknown_fill(tmp_path) -> None:
    result = asyncio.run(_exercise_pending_fill(str(tmp_path / "pmm1.db")))

    assert result["fill_id"] > 0
    assert result["pending_count"] == 1
    row = result["row"]
    assert row["ingest_state"] == "applied"
    assert row["fee_known"] == 1
    assert row["fee_source"] == "zero_fee_market"
    assert row["resolved_at"]
    assert row["fill_identity"] == "fill-1"
