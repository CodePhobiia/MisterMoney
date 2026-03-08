"""Tests for kill switch — trigger, auto-clear, multi-reason, clear."""

import time

import pytest

from pmm1.risk.kill_switch import KillSwitch, KillSwitchReason


class TestKillSwitchBasic:
    def test_initially_not_triggered(self):
        ks = KillSwitch()
        assert ks.is_triggered is False

    def test_trigger_manual(self):
        ks = KillSwitch()
        ks.trigger_manual("test")
        assert ks.is_triggered is True
        assert KillSwitchReason.MANUAL in ks.active_reasons

    def test_clear_specific_reason(self):
        ks = KillSwitch()
        ks.trigger_manual("test")
        ks.clear(KillSwitchReason.MANUAL)
        assert ks.is_triggered is False

    def test_clear_all(self):
        ks = KillSwitch()
        ks.trigger_manual("test1")
        ks.trigger_drawdown("test2")
        ks.clear()
        assert ks.is_triggered is False
        assert len(ks.active_reasons) == 0


class TestKillSwitchAutoClear:
    def test_auto_clear_after_timeout(self):
        ks = KillSwitch()
        # Trigger with very short auto-clear
        ks._trigger(
            KillSwitchReason.STALE_MARKET_FEED,
            "test",
            auto_clear_s=0.01,
        )
        assert ks.is_triggered is True
        # Wait for auto-clear
        time.sleep(0.02)
        # is_triggered calls _check_auto_clear
        assert ks.is_triggered is False

    def test_auto_clear_120s_default_for_stale_feed(self):
        ks = KillSwitch()
        ks.check_stale_feed(5.0)  # 5s > 2s default
        assert ks.is_triggered is True
        # Check the event has 120s auto-clear
        event = ks._active_reasons[KillSwitchReason.STALE_MARKET_FEED]
        assert event.auto_clear_after_s == 120.0

    def test_auto_clear_120s_for_exchange_paused(self):
        ks = KillSwitch()
        ks.report_exchange_paused()
        event = ks._active_reasons[KillSwitchReason.EXCHANGE_PAUSED]
        assert event.auto_clear_after_s == 120.0


class TestKillSwitchMultiReason:
    def test_multiple_reasons_all_required(self):
        ks = KillSwitch()
        ks.trigger_manual("manual")
        ks.trigger_drawdown("drawdown")
        assert ks.is_triggered is True
        # Clear one — still triggered
        ks.clear(KillSwitchReason.MANUAL)
        assert ks.is_triggered is True
        # Clear second — now cleared
        ks.clear(KillSwitchReason.DRAWDOWN)
        assert ks.is_triggered is False


class TestKillSwitchStaleFeed:
    def test_stale_feed_triggers(self):
        ks = KillSwitch(ws_stale_kill_s=2.0)
        result = ks.check_stale_feed(3.0)
        assert result is True
        assert ks.is_triggered is True

    def test_fresh_feed_clears(self):
        ks = KillSwitch(ws_stale_kill_s=2.0)
        ks.check_stale_feed(3.0)
        assert ks.is_triggered is True
        # Feed comes back
        ks.check_stale_feed(0.5)
        assert ks.is_triggered is False


class TestKillSwitchAuth:
    def test_auth_failure_accumulates(self):
        ks = KillSwitch(max_auth_failures=3)
        assert ks.report_auth_failure() is False
        assert ks.report_auth_failure() is False
        assert ks.report_auth_failure() is True
        assert ks.is_triggered is True

    def test_auth_success_resets(self):
        ks = KillSwitch(max_auth_failures=3)
        ks.report_auth_failure()
        ks.report_auth_failure()
        ks.report_auth_success()
        assert ks._auth_failure_count == 0


class TestKillSwitchReconciliation:
    def test_recon_mismatch_accumulates(self):
        ks = KillSwitch(max_reconciliation_mismatches=2)
        assert ks.report_reconciliation_mismatch("test") is False
        assert ks.report_reconciliation_mismatch("test") is True
        assert ks.is_triggered is True

    def test_recon_clean_resets(self):
        ks = KillSwitch(max_reconciliation_mismatches=3)
        ks.report_reconciliation_mismatch("test")
        ks.report_reconciliation_clean()
        assert ks._reconciliation_mismatch_count == 0


class TestKillSwitchCallbacks:
    def test_set_on_trigger(self):
        ks = KillSwitch()
        called = []

        async def cb(reason, msg):
            called.append((reason, msg))

        ks.set_on_trigger(cb)
        assert ks._on_trigger is cb


class TestKillSwitchStatus:
    def test_get_status(self):
        ks = KillSwitch()
        status = ks.get_status()
        assert "is_triggered" in status
        assert "active_reasons" in status
        assert status["is_triggered"] is False
