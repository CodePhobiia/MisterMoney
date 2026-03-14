"""PMM-2 configuration — loaded from config/default.yaml under pmm2: key."""

from __future__ import annotations

import os
from typing import Any

import structlog
from pydantic import BaseModel, Field, field_validator, model_validator

logger = structlog.get_logger(__name__)

_LIVE_STAGE_BY_PCT = {
    0.05: "canary_5pct",
    0.10: "canary_10pct",
    0.25: "canary_25pct",
    1.00: "production",
}


class PMM2CanaryConfig(BaseModel):
    """Explicit PMM-2 live-canary controls and market restrictions."""

    enabled: bool = False
    max_markets: int = 4
    require_reward_eligible: bool = True
    exclude_neg_risk: bool = True
    require_clean_outcomes: bool = True
    max_ambiguity_score: float = 0.15
    min_volume_24h: float = 50_000.0
    min_liquidity: float = 2_500.0
    max_spread_cents: float = 5.0
    min_hours_to_resolution: float = 24.0

    @field_validator("max_markets")
    @classmethod
    def _validate_max_markets(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("pmm2.canary.max_markets must be positive")
        return value

    @field_validator(
        "max_ambiguity_score",
        "min_volume_24h",
        "min_liquidity",
        "max_spread_cents",
        "min_hours_to_resolution",
    )
    @classmethod
    def _validate_non_negative(cls, value: float, info: Any) -> float:
        if value < 0.0:
            raise ValueError(f"pmm2.canary.{info.field_name} must be non-negative")
        return value

    @field_validator("max_ambiguity_score")
    @classmethod
    def _validate_ambiguity_score(cls, value: float) -> float:
        if value > 1.0:
            raise ValueError("pmm2.canary.max_ambiguity_score must be between 0.0 and 1.0")
        return value


class PMM2Config(BaseModel):
    """PMM-2 configuration — loaded from config/default.yaml under pmm2: key."""

    # Mode
    enabled: bool = False
    shadow_mode: bool = True  # True = log decisions, don't execute
    live_enabled: bool = False  # Separate explicit gate for any live PMM-2 control
    live_capital_pct: float = 0.0  # 0 = shadow, 0.1 = 10%, 1.0 = full
    canary: PMM2CanaryConfig = Field(default_factory=PMM2CanaryConfig)

    # Cadences (seconds unless noted)
    allocator_interval_sec: float = 60.0
    scoring_check_sec: float = 30.0
    universe_refresh_sec: float = 300.0
    queue_update_ms: float = 250.0
    medium_loop_sec: float = 10.0

    # Limits
    max_markets_active: int = 12
    max_slots_total: int = 48

    # Allocator scoring parameters
    min_positive_return_bps: float = 6.0
    corr_penalty_lambda: float = 0.20
    churn_penalty_phi: float = 0.15
    queue_uncertainty_psi: float = 0.10

    # Persistence parameters
    hysteresis_base_usdc: float = 0.25
    scoring_extra_usdc: float = 0.40
    eta_extra_usdc: float = 0.30
    max_reprices_per_minute: int = 2

    # Risk limits (NAV fractions)
    per_market_cap_nav: float = 0.03
    per_event_cap_nav: float = 0.06
    total_active_cap_nav: float = 0.30
    per_market_cap_floor: float = 8.0

    model_config = {"frozen": False}

    @field_validator("live_capital_pct")
    @classmethod
    def _validate_live_capital_pct(cls, value: float) -> float:
        value = round(float(value), 4)
        if value < 0.0 or value > 1.0:
            raise ValueError("pmm2.live_capital_pct must be between 0.0 and 1.0")
        if value not in {0.0, *set(_LIVE_STAGE_BY_PCT)}:
            allowed = ", ".join(["0.0", "0.05", "0.10", "0.25", "1.0"])
            raise ValueError(
                f"pmm2.live_capital_pct must be one of the explicit rollout stages: {allowed}"
            )
        return value

    @model_validator(mode="after")
    def _validate_operational_mode(self) -> PMM2Config:
        if not self.enabled:
            return self

        if self.shadow_mode:
            if self.live_capital_pct != 0.0:
                raise ValueError("pmm2 shadow mode requires live_capital_pct=0.0")
            if self.live_enabled:
                raise ValueError("pmm2 shadow mode requires live_enabled=false")
            return self

        if self.live_capital_pct <= 0.0:
            raise ValueError("pmm2 live mode requires live_capital_pct > 0.0")
        if not self.live_enabled:
            raise ValueError("pmm2 live mode requires live_enabled=true")
        if self.is_canary_live and not self.canary.enabled:
            raise ValueError("pmm2 canary live mode requires pmm2.canary.enabled=true")
        if not self.is_canary_live and self.canary.enabled:
            raise ValueError("pmm2 full live mode requires pmm2.canary.enabled=false")

        live_ack = os.getenv("PMM1_ACK_PMM2_LIVE", "").strip().upper()
        if live_ack != "YES":
            raise ValueError("pmm2 live mode requires PMM1_ACK_PMM2_LIVE=YES")

        return self

    @property
    def is_live(self) -> bool:
        return self.enabled and not self.shadow_mode and self.live_capital_pct > 0.0

    @property
    def is_canary_live(self) -> bool:
        return self.is_live and self.live_capital_pct < 1.0

    @property
    def stage_name(self) -> str:
        if not self.enabled:
            return "disabled"
        if self.shadow_mode:
            return "shadow"
        return _LIVE_STAGE_BY_PCT.get(round(self.live_capital_pct, 4), "custom")

    @property
    def controller_label(self) -> str:
        if not self.enabled:
            return "pmm2_disabled"
        if self.shadow_mode:
            return "pmm2_shadow"
        if self.is_canary_live:
            return "pmm2_canary"
        return "pmm2_production"

    @property
    def controller_strategy(self) -> str:
        if self.shadow_mode:
            return "pmm2_shadow"
        if self.is_canary_live:
            return "pmm2_canary"
        return "pmm2_production"

    @property
    def effective_active_cap_pct(self) -> float:
        if self.shadow_mode:
            return self.total_active_cap_nav
        if not self.is_live:
            return 0.0
        return min(self.total_active_cap_nav, self.live_capital_pct)


def load_pmm2_config(yaml_dict: dict[str, Any]) -> PMM2Config:
    """Load PMM2Config from the 'pmm2' key of config YAML.

    If 'pmm2' key doesn't exist, returns default config (disabled).

    Args:
        yaml_dict: Full config dictionary loaded from YAML

    Returns:
        PMM2Config instance
    """
    pmm2_section = yaml_dict.get("pmm2", {})

    if not pmm2_section:
        logger.info("pmm2_config_not_found_using_defaults")
        return PMM2Config()

    config = PMM2Config(**pmm2_section)
    logger.info(
        "pmm2_config_loaded",
        enabled=config.enabled,
        shadow_mode=config.shadow_mode,
        live_enabled=config.live_enabled,
        live_pct=config.live_capital_pct,
        stage=config.stage_name,
        controller=config.controller_label,
        canary_enabled=config.canary.enabled,
        effective_active_cap_pct=config.effective_active_cap_pct,
    )
    return config
