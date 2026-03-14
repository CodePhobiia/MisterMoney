"""
V3 Offline Worker Entry Point

Usage:
    python -m v3.offline.main

Runs the async escalation worker + weekly evaluator.
"""

import asyncio
from datetime import datetime, timedelta

import structlog

from v3.evidence.db import Database
from v3.offline.queue import EscalationQueue
from v3.offline.weekly_eval import WeeklyEvaluator
from v3.offline.worker import OfflineWorker
from v3.providers.registry import ProviderRegistry
from v3.serving.publisher import SignalPublisher

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

log = structlog.get_logger()


async def run_weekly_eval(db: Database, registry: ProviderRegistry) -> None:
    """
    Run weekly evaluation and send report

    Args:
        db: Database instance
        registry: Provider registry
    """
    log.info("running_weekly_evaluation")

    evaluator = WeeklyEvaluator(db, registry)

    try:
        # Run evaluation
        stats = await evaluator.evaluate_resolved_markets()

        # Generate calibration labels
        resolved = []
        for route_stats in stats["by_route"].values():
            resolved.extend(route_stats["markets"])

        labels = await evaluator.generate_calibration_labels(resolved)

        # Format and send report
        report = evaluator.format_weekly_report(stats)
        await evaluator.send_report(report)

        log.info(
            "weekly_evaluation_completed",
            total_markets=stats["total_markets"],
            labels_generated=len(labels)
        )

    except Exception as e:
        log.error("weekly_evaluation_failed", error=str(e), exc_info=True)


async def schedule_weekly_eval(db: Database, registry: ProviderRegistry) -> None:
    """
    Schedule weekly evaluation to run every Sunday at midnight UTC

    Args:
        db: Database instance
        registry: Provider registry
    """
    log.info("scheduling_weekly_evaluator")

    while True:
        try:
            # Calculate next Sunday midnight
            now = datetime.utcnow()
            days_until_sunday = (6 - now.weekday()) % 7
            if days_until_sunday == 0 and now.hour >= 1:
                # Already passed this week's run, schedule next week
                days_until_sunday = 7

            next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + \
                       timedelta(days=days_until_sunday)

            wait_seconds = (next_run - now).total_seconds()

            log.info(
                "weekly_eval_scheduled",
                next_run=next_run.isoformat(),
                wait_hours=round(wait_seconds / 3600, 1)
            )

            # Wait until next run
            await asyncio.sleep(wait_seconds)

            # Run evaluation
            await run_weekly_eval(db, registry)

            # Wait a bit to avoid double-run
            await asyncio.sleep(3600)

        except Exception as e:
            log.error("weekly_eval_scheduler_error", error=str(e), exc_info=True)
            await asyncio.sleep(3600)  # Wait an hour before retry


async def main():
    """Main entry point"""
    log.info("v3_offline_worker_starting")

    # 1. Connect to Postgres
    db_dsn = "postgresql://mmbot:mmbot_v3_2026@localhost/mistermoney_v3"
    db = Database(db_dsn)
    await db.connect()
    log.info("database_connected")

    # 2. Initialize provider registry
    registry = ProviderRegistry()
    await registry.initialize()
    log.info("provider_registry_initialized")

    # 3. Create Redis-backed components
    redis_url = "redis://localhost:6379"

    queue = EscalationQueue(redis_url)
    await queue.connect()
    log.info("escalation_queue_connected")

    publisher = SignalPublisher(db, redis_url)
    await publisher.connect()
    log.info("signal_publisher_connected")

    # 4. Create offline worker
    worker = OfflineWorker(db, registry, queue, publisher)
    log.info("offline_worker_created")

    # 5. Create weekly evaluator
    _evaluator = WeeklyEvaluator(db, registry)
    log.info("weekly_evaluator_created")

    # 6. Start worker loop in background
    worker_task = asyncio.create_task(
        worker.run_loop(poll_interval=60, max_per_hour=20)
    )
    log.info("worker_loop_started")

    # 7. Schedule weekly eval
    eval_task = asyncio.create_task(
        schedule_weekly_eval(db, registry)
    )
    log.info("weekly_eval_scheduled")

    # Wait for tasks (run forever)
    try:
        await asyncio.gather(worker_task, eval_task)
    except KeyboardInterrupt:
        log.info("shutdown_signal_received")
    finally:
        # Cleanup
        log.info("cleaning_up")
        await queue.close()
        await publisher.close()
        await registry.close_all()
        await db.close()
        log.info("v3_offline_worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
