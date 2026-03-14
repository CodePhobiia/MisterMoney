"""Rebuild derived spine fact tables from historical event_log rows."""

from __future__ import annotations

import argparse
import asyncio
import json

from pmm1.materializers import (
    BookSnapshotFactMaterializer,
    CanaryCycleFactMaterializer,
    FillFactMaterializer,
    OrderFactMaterializer,
    QuoteFactMaterializer,
    ShadowCycleFactMaterializer,
)
from pmm1.settings import load_settings
from pmm1.storage.postgres import PostgresStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rebuild derived spine facts from event_log")
    parser.add_argument(
        "--config",
        default="config/default.yaml",
        help="Path to YAML config for default Postgres DSN",
    )
    parser.add_argument(
        "--postgres-dsn",
        help="Override Postgres DSN instead of reading from config",
    )
    parser.add_argument(
        "--fact",
        default="all",
        choices=[
            "order_fact",
            "fill_fact",
            "book_snapshot_fact",
            "quote_fact",
            "shadow_cycle_fact",
            "canary_cycle_fact",
            "all",
        ],
        help="Which derived fact table to rebuild",
    )
    return parser


async def amain() -> None:
    args = _build_parser().parse_args()
    settings = load_settings(args.config, enforce_runtime_guards=False)
    dsn = args.postgres_dsn or settings.storage.postgres_dsn

    store = PostgresStore(dsn)
    await store.connect()
    try:
        summary: dict[str, int] = {}
        if args.fact in {"order_fact", "all"}:
            order_materializer = OrderFactMaterializer(store, None)
            summary["order_fact"] = await order_materializer.rebuild_from_event_log(reset=True)
        if args.fact in {"fill_fact", "all"}:
            fill_materializer = FillFactMaterializer(store, None)
            summary["fill_fact"] = await fill_materializer.rebuild_from_event_log(reset=True)
        if args.fact in {"book_snapshot_fact", "all"}:
            book_materializer = BookSnapshotFactMaterializer(store, None)
            summary["book_snapshot_fact"] = (
                await book_materializer.rebuild_from_event_log(reset=True)
            )
        if args.fact in {"quote_fact", "all"}:
            quote_materializer = QuoteFactMaterializer(store, None)
            summary["quote_fact"] = await quote_materializer.rebuild_from_event_log(reset=True)
        if args.fact in {"shadow_cycle_fact", "all"}:
            shadow_materializer = ShadowCycleFactMaterializer(store, None)
            summary["shadow_cycle_fact"] = (
                await shadow_materializer.rebuild_from_event_log(reset=True)
            )
        if args.fact in {"canary_cycle_fact", "all"}:
            canary_materializer = CanaryCycleFactMaterializer(store, None)
            summary["canary_cycle_fact"] = (
                await canary_materializer.rebuild_from_event_log(reset=True)
            )
        print(json.dumps(summary, indent=2, sort_keys=True))
    finally:
        await store.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
