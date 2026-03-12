"""Phase 4 settings and dangerous-toggle guardrail tests."""

from __future__ import annotations

import textwrap

import pytest

from pmm1.settings import FillEscalationConfig, load_settings
from pmm2.config import PMM2Config


def _write_config(tmp_path, body: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


def test_load_settings_live_taker_bootstrap_requires_ack(tmp_path, monkeypatch):
    config_path = _write_config(
        tmp_path,
        """
        bot:
          paper_mode: false
        wallet:
          private_key: "pk"
          address: "0x123"
        api:
          api_key: "key"
          api_secret: "secret"
          api_passphrase: "pass"
        exit:
          fill_escalation:
            taker_enabled: true
        """,
    )
    monkeypatch.delenv("PMM1_ACK_TAKER_BOOTSTRAP", raising=False)

    with pytest.raises(ValueError, match="PMM1_ACK_TAKER_BOOTSTRAP=YES"):
        load_settings(config_path=config_path)

    settings = load_settings(config_path=config_path, enforce_runtime_guards=False)
    assert settings.exit.fill_escalation.taker_enabled is True


def test_fill_escalation_rejects_overly_aggressive_taker_trigger():
    with pytest.raises(ValueError, match="at least 300 seconds"):
        FillEscalationConfig(taker_enabled=True, taker_trigger_secs=60)


def test_pmm2_live_mode_requires_explicit_ack(monkeypatch):
    monkeypatch.delenv("PMM1_ACK_PMM2_LIVE", raising=False)

    with pytest.raises(ValueError, match="PMM1_ACK_PMM2_LIVE=YES"):
        PMM2Config(enabled=True, shadow_mode=False, live_capital_pct=0.1)


def test_pmm2_shadow_mode_requires_zero_live_capital():
    with pytest.raises(ValueError, match="shadow mode requires live_capital_pct=0.0"):
        PMM2Config(enabled=True, shadow_mode=True, live_capital_pct=0.1)
