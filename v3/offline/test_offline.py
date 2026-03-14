"""
V3 Offline Worker Integration Tests

Tests:
1. EscalationQueue enqueue/dequeue/peek/size (Redis)
2. Priority ordering (highest priority dequeued first)
3. Deduplication (re-enqueue updates priority)
4. OfflineWorker.process_one with a mock market
5. WeeklyEvaluator.generate_calibration_labels with mock resolved markets
6. Weekly report formatting
"""

import asyncio

import pytest

from v3.evidence.db import Database
from v3.offline.queue import EscalationQueue
from v3.offline.weekly_eval import WeeklyEvaluator
from v3.offline.worker import OfflineWorker
from v3.providers.registry import ProviderRegistry
from v3.serving.publisher import SignalPublisher

# Test configuration
DB_DSN = "postgresql://mmbot:mmbot_v3_2026@localhost/mistermoney_v3"
REDIS_URL = "redis://localhost:6379"


@pytest.fixture
async def queue():
    """Create and cleanup escalation queue"""
    q = EscalationQueue(REDIS_URL)
    await q.connect()

    # Clear queue before test
    if q.client:
        await q.client.delete(q.QUEUE_KEY)
        # Clear any metadata keys
        keys = await q.client.keys(f"{q.METADATA_PREFIX}*")
        if keys:
            await q.client.delete(*keys)

    yield q

    # Cleanup after test
    if q.client:
        await q.client.delete(q.QUEUE_KEY)
        keys = await q.client.keys(f"{q.METADATA_PREFIX}*")
        if keys:
            await q.client.delete(*keys)

    await q.close()


@pytest.fixture
async def db():
    """Create database connection"""
    database = Database(DB_DSN)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def registry():
    """Create provider registry"""
    reg = ProviderRegistry()
    await reg.initialize()
    yield reg
    await reg.close_all()


@pytest.fixture
async def publisher(db):
    """Create signal publisher"""
    pub = SignalPublisher(db, REDIS_URL)
    await pub.connect()
    yield pub
    await pub.close()


@pytest.mark.asyncio
async def test_queue_enqueue_dequeue(queue):
    """Test basic enqueue/dequeue"""
    print("\n=== Test: Queue Enqueue/Dequeue ===")

    # Enqueue a market
    await queue.enqueue(
        condition_id="test_market_1",
        reason="high_uncertainty",
        priority=0.8,
        metadata={"notional": 10000}
    )

    # Check size
    size = await queue.size()
    assert size == 1, f"Expected size 1, got {size}"
    print(f"✓ Queue size: {size}")

    # Dequeue
    item = await queue.dequeue()
    assert item is not None, "Expected item, got None"

    condition_id, metadata = item
    assert condition_id == "test_market_1"
    assert metadata["reason"] == "high_uncertainty"
    assert metadata["priority"] == 0.8
    print(f"✓ Dequeued: {condition_id} with priority {metadata['priority']}")

    # Queue should be empty
    size = await queue.size()
    assert size == 0, f"Expected size 0, got {size}"
    print("✓ Queue empty after dequeue")


@pytest.mark.asyncio
async def test_queue_priority_ordering(queue):
    """Test that highest priority is dequeued first"""
    print("\n=== Test: Priority Ordering ===")

    # Enqueue multiple markets with different priorities
    markets = [
        ("market_low", 0.3),
        ("market_high", 0.9),
        ("market_mid", 0.6),
    ]

    for condition_id, priority in markets:
        await queue.enqueue(
            condition_id=condition_id,
            reason="test",
            priority=priority,
            metadata={}
        )

    print(f"✓ Enqueued {len(markets)} markets")

    # Dequeue — should get highest priority first
    item1 = await queue.dequeue()
    assert item1[0] == "market_high", f"Expected market_high first, got {item1[0]}"
    print(f"✓ First dequeue: {item1[0]} (priority 0.9)")

    item2 = await queue.dequeue()
    assert item2[0] == "market_mid", f"Expected market_mid second, got {item2[0]}"
    print(f"✓ Second dequeue: {item2[0]} (priority 0.6)")

    item3 = await queue.dequeue()
    assert item3[0] == "market_low", f"Expected market_low third, got {item3[0]}"
    print(f"✓ Third dequeue: {item3[0]} (priority 0.3)")


@pytest.mark.asyncio
async def test_queue_deduplication(queue):
    """Test that re-enqueue updates priority if higher"""
    print("\n=== Test: Deduplication ===")

    # Enqueue with priority 0.5
    await queue.enqueue(
        condition_id="market_dup",
        reason="initial",
        priority=0.5,
        metadata={"version": 1}
    )

    size = await queue.size()
    assert size == 1
    print("✓ Initial enqueue: priority 0.5")

    # Re-enqueue with higher priority
    await queue.enqueue(
        condition_id="market_dup",
        reason="updated",
        priority=0.8,
        metadata={"version": 2}
    )

    size = await queue.size()
    assert size == 1, f"Expected size 1 (deduplicated), got {size}"
    print("✓ Re-enqueue with higher priority: size still 1")

    # Dequeue — should have updated priority
    item = await queue.dequeue()
    condition_id, metadata = item
    assert metadata["priority"] == 0.8, f"Expected priority 0.8, got {metadata['priority']}"
    print(f"✓ Priority updated to {metadata['priority']}")

    # Re-enqueue with lower priority (should not update)
    await queue.enqueue(
        condition_id="market_dup2",
        reason="initial",
        priority=0.8,
        metadata={}
    )

    await queue.enqueue(
        condition_id="market_dup2",
        reason="lower",
        priority=0.3,
        metadata={}
    )

    item = await queue.dequeue()
    _, metadata = item
    assert metadata["priority"] == 0.8, "Priority should not decrease"
    print("✓ Lower priority ignored (no downgrade)")


@pytest.mark.asyncio
async def test_queue_peek(queue):
    """Test peek without removing"""
    print("\n=== Test: Peek ===")

    # Enqueue markets
    await queue.enqueue("market_1", "test", 0.9, {})
    await queue.enqueue("market_2", "test", 0.7, {})
    await queue.enqueue("market_3", "test", 0.5, {})

    # Peek top 2
    items = await queue.peek(n=2)
    assert len(items) == 2
    assert items[0]["condition_id"] == "market_1"
    assert items[1]["condition_id"] == "market_2"
    print(f"✓ Peeked top 2: {items[0]['condition_id']}, {items[1]['condition_id']}")

    # Queue size should be unchanged
    size = await queue.size()
    assert size == 3
    print(f"✓ Queue size unchanged: {size}")


@pytest.mark.asyncio
async def test_worker_process_one(db, registry, queue, publisher):
    """Test OfflineWorker.process_one with mock market"""
    print("\n=== Test: Worker Process One ===")

    # Create worker
    worker = OfflineWorker(db, registry, queue, publisher)

    # Mock market
    condition_id = "test_market_worker"
    metadata = {
        "reason": "model_disagreement",
        "priority": 0.85,
        "notional": 50000
    }

    # Process
    result = await worker.process_one(condition_id, metadata)

    print("✓ Processing completed")
    print(f"  Model used: {result.get('model_used')}")
    print(f"  Action: {result.get('action')}")
    print(f"  Processing time: {result.get('processing_time_s', 0):.2f}s")

    # Check result structure
    assert "condition_id" in result
    assert "model_used" in result
    assert "action" in result

    if result.get("action") != "error":
        assert "p_hat" in result
        assert "uncertainty" in result
        print(f"  P(YES): {result['p_hat']:.3f} ± {result['uncertainty']:.3f}")


@pytest.mark.asyncio
async def test_weekly_evaluator_labels(db, registry):
    """Test WeeklyEvaluator.generate_calibration_labels"""
    print("\n=== Test: Weekly Evaluator - Calibration Labels ===")

    evaluator = WeeklyEvaluator(db, registry)

    # Mock resolved markets
    resolved = [
        {
            "condition_id": "market_1",
            "question": "Will BTC hit $100k?",
            "route": "numeric",
            "p_hat": 0.65,
            "uncertainty": 0.15,
            "outcome": 1,
            "brier_score": 0.1225
        },
        {
            "condition_id": "market_2",
            "question": "Will Trump win?",
            "route": "simple",
            "p_hat": 0.45,
            "uncertainty": 0.20,
            "outcome": 0,
            "brier_score": 0.2025
        },
        {
            "condition_id": "market_3",
            "question": "Will Fed cut rates?",
            "route": "dossier",
            "p_hat": 0.80,
            "uncertainty": 0.10,
            "outcome": 1,
            "brier_score": 0.04
        }
    ]

    # Generate labels
    labels = await evaluator.generate_calibration_labels(resolved)

    assert len(labels) == 3, f"Expected 3 labels, got {len(labels)}"
    print(f"✓ Generated {len(labels)} calibration labels")

    # Check label structure
    for label in labels:
        assert "condition_id" in label
        assert "route" in label
        assert "features" in label
        assert "outcome" in label
        assert "brier_score" in label

    print("✓ All labels have correct structure")


@pytest.mark.asyncio
async def test_weekly_report_format(db, registry):
    """Test weekly report formatting"""
    print("\n=== Test: Weekly Report Format ===")

    evaluator = WeeklyEvaluator(db, registry)

    # Mock stats
    stats = {
        "total_markets": 23,
        "by_route": {
            "numeric": {
                "count": 12,
                "avg_brier": 0.08,
                "markets": []
            },
            "simple": {
                "count": 8,
                "avg_brier": 0.15,
                "markets": []
            },
            "rule": {
                "count": 2,
                "avg_brier": 0.22,
                "markets": []
            },
            "dossier": {
                "count": 1,
                "avg_brier": 0.19,
                "markets": []
            }
        },
        "insights": {
            "route_insights": {
                "numeric": "Excellent on crypto markets",
                "simple": "Underweights breaking news"
            },
            "systematic_biases": [
                "Overconfident on trending markets"
            ],
            "calibration_recommendations": [
                "Increase uncertainty on <24h markets"
            ]
        }
    }

    # Format report
    report = evaluator.format_weekly_report(stats)

    print("✓ Report generated:")
    print(report)

    # Check content
    assert "V3 Weekly Evaluation" in report
    assert "Markets resolved: 23" in report
    assert "Numeric" in report
    assert "0.08" in report
    print("\n✓ Report contains expected content")


async def run_all_tests():
    """Run all tests"""
    print("=" * 60)
    print("V3 OFFLINE WORKER INTEGRATION TESTS")
    print("=" * 60)

    # Create fixtures
    queue = EscalationQueue(REDIS_URL)
    await queue.connect()

    db = Database(DB_DSN)
    await db.connect()

    registry = ProviderRegistry()
    await registry.initialize()

    publisher = SignalPublisher(db, REDIS_URL)
    await publisher.connect()

    try:
        # Clear queue
        if queue.client:
            await queue.client.delete(queue.QUEUE_KEY)
            keys = await queue.client.keys(f"{queue.METADATA_PREFIX}*")
            if keys:
                await queue.client.delete(*keys)

        # Run tests
        await test_queue_enqueue_dequeue(queue)
        await test_queue_priority_ordering(queue)
        await test_queue_deduplication(queue)
        await test_queue_peek(queue)
        await test_worker_process_one(db, registry, queue, publisher)
        await test_weekly_evaluator_labels(db, registry)
        await test_weekly_report_format(db, registry)

        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED")
        print("=" * 60)

    except Exception as e:
        print("\n" + "=" * 60)
        print(f"❌ TEST FAILED: {e}")
        print("=" * 60)
        raise

    finally:
        # Cleanup
        await queue.close()
        await publisher.close()
        await registry.close_all()
        await db.close()


if __name__ == "__main__":
    asyncio.run(run_all_tests())
