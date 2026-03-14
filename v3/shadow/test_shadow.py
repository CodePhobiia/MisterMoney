"""
V3 Shadow Mode Integration Tests
Tests all shadow mode components
"""

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from v3.evidence.db import Database
from v3.evidence.entities import FairValueSignal
from v3.shadow.logger import ShadowLogger
from v3.shadow.metrics import BrierScoreTracker, LatencyTracker
from v3.shadow.reports import DailyReporter

# Test database DSN (use test DB if available)
TEST_DB_DSN = "postgresql://mmbot:mmbot_v3_2026@localhost/mistermoney_v3"


class TestShadowLogger:
    """Test shadow logger functionality"""

    @pytest.mark.asyncio
    async def test_log_signal(self):
        """Test signal logging to JSONL"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = ShadowLogger(log_dir=tmpdir)

            # Create test signal
            signal = FairValueSignal(
                condition_id="test-123",
                p_calibrated=0.65,
                p_low=0.55,
                p_high=0.75,
                uncertainty=0.10,
                route="simple",
                evidence_ids=["e1", "e2"],
                counterevidence_ids=["e3"],
                models_used=["sonnet"],
            )

            market_meta = {
                "question": "Will it rain tomorrow?",
                "volume_24h": 50000.0,
                "current_mid": 0.60,
                "spread_cents": 2.0,
            }

            # Log signal
            await logger.log_signal(
                signal=signal,
                market_meta=market_meta,
                v1_fair_value=0.60,
                latency_ms=15000,
                token_usage={"sonnet": 1250}
            )

            # Verify log file exists and contains entry
            log_path = Path(tmpdir) / f"shadow_{datetime.utcnow().strftime('%Y-%m-%d')}.jsonl"

            assert log_path.exists()

            with open(log_path) as f:
                entry = json.loads(f.readline())

            assert entry["condition_id"] == "test-123"
            assert entry["v3_signal"]["p_calibrated"] == 0.65
            assert entry["v3_signal"]["route"] == "simple"
            assert entry["v1_fair_value"] == 0.60
            assert entry["latency_ms"] == 15000
            assert entry["market"]["question"] == "Will it rain tomorrow?"

            print("✅ ShadowLogger.log_signal test passed")

    @pytest.mark.asyncio
    async def test_log_error(self):
        """Test error logging"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = ShadowLogger(log_dir=tmpdir)

            await logger.log_error(
                condition_id="test-456",
                route="rule",
                error="Provider timeout",
                market_meta={"question": "Test market"}
            )

            # Verify error log
            error_path = Path(tmpdir) / f"errors_{datetime.utcnow().strftime('%Y-%m-%d')}.jsonl"

            assert error_path.exists()

            with open(error_path) as f:
                entry = json.loads(f.readline())

            assert entry["condition_id"] == "test-456"
            assert entry["route"] == "rule"
            assert entry["error"] == "Provider timeout"

            print("✅ ShadowLogger.log_error test passed")

    @pytest.mark.asyncio
    async def test_daily_summary(self):
        """Test daily summary generation"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = ShadowLogger(log_dir=tmpdir)

            # Log multiple signals
            for i in range(3):
                signal = FairValueSignal(
                    condition_id=f"test-{i}",
                    p_calibrated=0.5 + i * 0.1,
                    uncertainty=0.1,
                    route="simple" if i < 2 else "numeric",
                    evidence_ids=[],
                    models_used=["sonnet"],
                )

                await logger.log_signal(
                    signal=signal,
                    market_meta={"question": f"Test {i}", "volume_24h": 1000, "current_mid": 0.5},
                    v1_fair_value=0.5,
                    latency_ms=1000 * (i + 1)
                )

            # Get summary
            summary = await logger.get_daily_summary()

            assert summary["markets_evaluated"] == 3
            assert summary["signals_generated"] == 3
            assert summary["route_breakdown"]["simple"] == 2
            assert summary["route_breakdown"]["numeric"] == 1

            print("✅ ShadowLogger.get_daily_summary test passed")


class TestBrierScoreTracker:
    """Test Brier score tracking"""

    @pytest.mark.asyncio
    async def test_record_and_score_prediction(self):
        """Test recording and scoring predictions"""
        db = Database(TEST_DB_DSN)
        await db.connect()

        try:
            # Create table
            await db.pool.execute("""
                CREATE TABLE IF NOT EXISTS v3_predictions (
                    id SERIAL PRIMARY KEY,
                    condition_id TEXT NOT NULL,
                    route TEXT NOT NULL,
                    p_predicted FLOAT NOT NULL,
                    predicted_at TIMESTAMPTZ NOT NULL,
                    actual_outcome FLOAT,
                    brier_score FLOAT,
                    scored_at TIMESTAMPTZ
                )
            """)

            tracker = BrierScoreTracker(db)

            # Record prediction
            await tracker.record_prediction(
                condition_id="test-brier-1",
                route="simple",
                p_predicted=0.70,
                timestamp=datetime.utcnow()
            )

            # Score resolved market (actual outcome = YES = 1.0)
            result = await tracker.score_resolved(
                condition_id="test-brier-1",
                actual_outcome=1.0
            )

            assert result["prediction_count"] == 1
            assert result["avg_brier_score"] == pytest.approx((0.70 - 1.0) ** 2)

            print("✅ BrierScoreTracker record/score test passed")

            # Cleanup
            await db.pool.execute("DELETE FROM v3_predictions WHERE condition_id = 'test-brier-1'")

        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_route_summary(self):
        """Test route summary generation"""
        db = Database(TEST_DB_DSN)
        await db.connect()

        try:
            tracker = BrierScoreTracker(db)

            # Record and score multiple predictions
            test_data = [
                ("test-route-1", "simple", 0.80, 1.0),
                ("test-route-2", "simple", 0.60, 1.0),
                ("test-route-3", "numeric", 0.50, 0.0),
            ]

            for condition_id, route, p_pred, actual in test_data:
                await tracker.record_prediction(
                    condition_id=condition_id,
                    route=route,
                    p_predicted=p_pred,
                    timestamp=datetime.utcnow()
                )

                await tracker.score_resolved(
                    condition_id=condition_id,
                    actual_outcome=actual
                )

            # Get route summary
            summary = await tracker.get_route_summary()

            assert "simple" in summary
            assert "numeric" in summary
            assert summary["simple"]["n"] == 2
            assert summary["numeric"]["n"] == 1

            print("✅ BrierScoreTracker route summary test passed")

            # Cleanup
            for condition_id, _, _, _ in test_data:
                await db.pool.execute(
                    "DELETE FROM v3_predictions"
                    " WHERE condition_id = $1",
                    condition_id,
                )

        finally:
            await db.close()


class TestLatencyTracker:
    """Test latency tracking"""

    def test_record_and_summary(self):
        """Test latency recording and summary generation"""
        tracker = LatencyTracker()

        # Record latencies
        tracker.record("simple", "sonnet", 12000)
        tracker.record("simple", "sonnet", 15000)
        tracker.record("simple", "sonnet", 18000)
        tracker.record("numeric", "sonnet", 500)
        tracker.record("numeric", "sonnet", 600)

        # Get summary
        summary = tracker.get_summary()

        assert "simple" in summary
        assert "numeric" in summary
        assert "sonnet" in summary["simple"]
        assert "sonnet" in summary["numeric"]

        # Check simple route stats
        simple_stats = summary["simple"]["sonnet"]
        assert simple_stats["n"] == 3
        assert simple_stats["p50"] == pytest.approx(15000, rel=0.01)

        # Check numeric route stats
        numeric_stats = summary["numeric"]["sonnet"]
        assert numeric_stats["n"] == 2
        assert numeric_stats["p50"] == pytest.approx(550, rel=0.01)

        print("✅ LatencyTracker record/summary test passed")


class TestDailyReporter:
    """Test daily report generation"""

    @pytest.mark.asyncio
    async def test_generate_daily_report(self):
        """Test daily report text generation"""
        db = Database(TEST_DB_DSN)
        await db.connect()

        try:
            # Create predictions table
            await db.pool.execute("""
                CREATE TABLE IF NOT EXISTS v3_predictions (
                    id SERIAL PRIMARY KEY,
                    condition_id TEXT NOT NULL,
                    route TEXT NOT NULL,
                    p_predicted FLOAT NOT NULL,
                    predicted_at TIMESTAMPTZ NOT NULL,
                    actual_outcome FLOAT,
                    brier_score FLOAT,
                    scored_at TIMESTAMPTZ
                )
            """)

            tracker = BrierScoreTracker(db)

            with tempfile.TemporaryDirectory() as tmpdir:
                # Create mock log data
                log_path = Path(tmpdir) / f"shadow_{datetime.utcnow().strftime('%Y-%m-%d')}.jsonl"

                with open(log_path, 'w') as f:
                    for i in range(5):
                        entry = {
                            "timestamp": datetime.utcnow().isoformat(),
                            "condition_id": f"test-{i}",
                            "v3_signal": {
                                "p_calibrated": 0.6,
                                "route": "simple" if i < 3 else "numeric",
                            },
                            "v1_fair_value": 0.5,
                            "market": {
                                "question": f"Test market {i}",
                                "volume_24h": 10000,
                                "current_mid": 0.5,
                            },
                            "latency_ms": 10000,
                            "token_usage": {"sonnet": 1000},
                        }
                        f.write(json.dumps(entry) + '\n')

                reporter = DailyReporter(db, tracker, logger_dir=tmpdir)

                # Generate report for today (since we created logs for today)
                today = datetime.utcnow().strftime('%Y-%m-%d')
                report = await reporter.generate_daily_report(date=today)

                assert "V3 Shadow Report" in report
                assert "Markets evaluated: 5" in report
                assert "Signals generated: 5" in report
                assert "Simple: 3 markets" in report
                assert "Numeric: 2 markets" in report

                print("✅ DailyReporter.generate_daily_report test passed")
                print("\nSample report:")
                print(report)

        finally:
            await db.close()


async def run_all_tests():
    """Run all tests"""
    print("\n" + "="*60)
    print("V3 Shadow Mode Integration Tests")
    print("="*60 + "\n")

    # Test logger
    print("Testing ShadowLogger...")
    test_logger = TestShadowLogger()
    await test_logger.test_log_signal()
    await test_logger.test_log_error()
    await test_logger.test_daily_summary()
    print()

    # Test Brier tracker
    print("Testing BrierScoreTracker...")
    test_brier = TestBrierScoreTracker()
    await test_brier.test_record_and_score_prediction()
    await test_brier.test_route_summary()
    print()

    # Test latency tracker
    print("Testing LatencyTracker...")
    test_latency = TestLatencyTracker()
    test_latency.test_record_and_summary()
    print()

    # Test daily reporter
    print("Testing DailyReporter...")
    test_reporter = TestDailyReporter()
    await test_reporter.test_generate_daily_report()
    print()

    print("="*60)
    print("All tests passed! ✅")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(run_all_tests())
