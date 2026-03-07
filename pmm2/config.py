"""PMM-2 configuration — loaded from config/default.yaml under pmm2: key."""

from __future__ import annotations

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


class PMM2Config(BaseModel):
    """PMM-2 configuration — loaded from config/default.yaml under pmm2: key."""

    # Mode
    enabled: bool = False
    shadow_mode: bool = True  # True = log decisions, don't execute
    live_capital_pct: float = 0.0  # 0 = shadow, 0.1 = 10%, 1.0 = full

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


def load_pmm2_config(yaml_dict: dict) -> PMM2Config:
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

    try:
        config = PMM2Config(**pmm2_section)
        logger.info(
            "pmm2_config_loaded",
            enabled=config.enabled,
            shadow_mode=config.shadow_mode,
            live_pct=config.live_capital_pct,
        )
        return config
    except Exception as e:
        logger.error("pmm2_config_load_failed", error=str(e))
        return PMM2Config()
