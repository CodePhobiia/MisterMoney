"""PMM-1 Settings — Pydantic settings model that loads from YAML + env vars."""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any, Callable

import structlog
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings

_settings_logger = structlog.get_logger(__name__)


class BotConfig(BaseModel):
    """Top-level bot configuration."""

    name: str = "PMM-1"
    env: str = "prod"
    max_markets: int = 20
    quote_cycle_ms: int = 250
    reconcile_orders_s: int = 30
    reconcile_positions_s: int = 60
    paper_mode: bool = True
    paper_nav: float = 100.0


class WalletConfig(BaseModel):
    """Wallet configuration."""

    type: str = "EOA"
    chain_id: int = 137
    private_key: str = ""
    address: str = ""


class MarketFiltersConfig(BaseModel):
    """Universe selection filters."""

    min_time_to_end_hours: int = 24
    min_volume_24h_usd: float = 1_000  # Lowered from 50k; Gamma volume_24h often sparse
    max_top_spread_cents: float = 10.0  # Widened from 4c; let scoring handle preference
    min_depth_within_2c_shares: float = 0  # Gamma doesn't provide depth; skip by default
    min_live_depth_within_2c_shares: float = 25.0
    max_markets_per_event: int = 2
    max_markets_per_theme: int = 4
    allow_sports: bool = False
    allow_crypto_intraday: bool = False
    require_clear_rules: bool = True


class StrategyConfig(BaseModel):
    """Which strategy modules are enabled."""

    enable_binary_parity: bool = True
    enable_neg_risk_arb: bool = True
    enable_market_making: bool = True
    enable_directional_overlay: bool = False


class PricingConfig(BaseModel):
    """Pricing / quoting parameters from §7."""

    base_half_spread_cents: float = 1.0
    inventory_skew_gamma: float = 0.015
    gamma_max: float = 0.05
    age_halflife_hours: float = 4.0
    cluster_skew_eta: float = 0.02
    take_threshold_cents: float = 0.8
    reward_capture_weight: float = 0.7
    # Dollar-based sizing
    target_dollar_size: float = 8.0   # Target $ per market per side
    max_dollar_size: float = 15.0     # Max $ per market per side
    # Fair-value model coefficients (logit-space)
    beta_0: float = 0.0
    beta_1: float = 1.0  # logit(midpoint) weight
    beta_2: float = 0.0  # logit(microprice) weight
    beta_3: float = 0.0  # imbalance weight
    beta_4: float = 0.0  # trade flow weight
    beta_5: float = 0.0  # related-market residual weight
    beta_6: float = 0.0  # external signal weight
    # Model haircut coefficients
    h_0: float = 0.005
    k_1: float = 0.5  # volatility
    k_2: float = 0.3  # staleness
    k_3: float = 0.2  # resolution risk
    k_4: float = 0.1  # model error
    # Fill model coefficients
    theta_0: float = -1.0
    theta_1: float = 5.0
    theta_2: float = 0.5
    theta_3: float = 1.0


class RiskConfig(BaseModel):
    """Risk limits from §14."""

    per_market_gross_nav: float = 0.02
    per_event_cluster_nav: float = 0.05
    total_directional_nav: float = 0.10
    total_arb_gross_nav: float = 0.25
    max_orders_per_market_side: int = 3
    max_quoted_markets: int = 20
    daily_pause_drawdown_nav: float = 0.015
    daily_wider_drawdown_nav: float = 0.025
    daily_flatten_drawdown_nav: float = 0.04


class ExecutionConfig(BaseModel):
    """Execution parameters from §12."""

    post_only: bool = True
    order_ttl_effective_s: int = 30
    heartbeat_s: int = 5
    ws_stale_kill_s: int = 10
    max_batch_orders: int = 15
    retry_backoff_initial_ms: int = 1000
    retry_backoff_max_ms: int = 30000

    @field_validator(
        "order_ttl_effective_s",
        "heartbeat_s",
        "ws_stale_kill_s",
        "max_batch_orders",
        "retry_backoff_initial_ms",
        "retry_backoff_max_ms",
    )
    @classmethod
    def _must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("execution values must be positive")
        return value


class ApiConfig(BaseModel):
    """API endpoints and credentials."""

    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    data_url: str = "https://data-api.polymarket.com"
    ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    geoblock_url: str = "https://polymarket.com"
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    funder: str = ""


class StorageConfig(BaseModel):
    """Storage backend configuration."""

    redis_url: str = "redis://localhost:6379/0"
    postgres_dsn: str = "postgresql://pmm1:pmm1@localhost:5432/pmm1"
    parquet_dir: str = "./data/parquet"


class TakeProfitConfig(BaseModel):
    """Take-profit exit settings."""

    enabled: bool = True
    threshold_pct: float = 0.15
    partial_exit_pct: float = 0.50
    full_exit_pct: float = 0.30
    min_hold_minutes: int = 30
    cooldown_minutes: int = 10


class StopLossConfig(BaseModel):
    """Stop-loss exit settings."""

    enabled: bool = True
    threshold_pct: float = 0.20
    hard_stop_pct: float = 0.40
    max_loss_per_trade_usd: float = 5.0


class ResolutionExitConfig(BaseModel):
    """Resolution time-based exit settings."""

    enabled: bool = True
    exit_start_hours: float = 6.0
    exit_complete_hours: float = 2.0
    aggressive_after_hours: float = 1.0
    block_new_buys_hours: float = 8.0


class FlattenConfig(BaseModel):
    """Emergency flatten settings."""

    config_flag_path: str = "/tmp/pmm1_flatten"
    price_tolerance_pct: float = 0.05

    @field_validator("config_flag_path")
    @classmethod
    def _validate_flag_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("flatten.config_flag_path must be an absolute path")
        if path == Path("/"):
            raise ValueError("flatten.config_flag_path cannot be '/'")
        return value

    @field_validator("price_tolerance_pct")
    @classmethod
    def _validate_price_tolerance(cls, value: float) -> float:
        if value <= 0 or value > 0.25:
            raise ValueError("flatten.price_tolerance_pct must be in (0, 0.25]")
        return value


class OrphanConfig(BaseModel):
    """Orphan position handling settings."""

    check_interval_s: int = 60
    min_size_to_unwind: float = 5.0


class InventorySkewConfig(BaseModel):
    """Dynamic inventory skew settings."""

    gamma_max: float = 0.05
    age_halflife_hours: float = 4.0


class FillEscalationConfig(BaseModel):
    """Fill escalation ladder settings for Fill-Speed Mode."""

    enabled: bool = True
    level_1_secs: int = 15 * 60  # 15 min
    level_1_ticks: int = 1
    level_2_secs: int = 30 * 60  # 30 min
    level_2_ticks: int = 2
    level_3_secs: int = 45 * 60  # 45 min
    level_3_ticks: int = 3
    taker_enabled: bool = True
    taker_trigger_secs: int = 20 * 60  # 20 min
    taker_min_shares: float = 5.0

    @model_validator(mode="after")
    def _validate_escalation(self) -> FillEscalationConfig:
        if self.level_1_secs <= 0 or self.level_2_secs <= 0 or self.level_3_secs <= 0:
            raise ValueError("fill escalation timers must be positive")
        if self.taker_trigger_secs <= 0:
            raise ValueError("fill escalation taker trigger must be positive")
        if self.taker_min_shares <= 0:
            raise ValueError("fill escalation taker_min_shares must be positive")
        if self.taker_enabled and not self.enabled:
            raise ValueError("taker bootstrap requires fill escalation to be enabled")
        if self.taker_enabled and self.taker_trigger_secs < 300:
            raise ValueError("taker bootstrap trigger must be at least 300 seconds")
        return self


class ExitConfig(BaseModel):
    """Root exit / sell-logic configuration."""

    take_profit: TakeProfitConfig = Field(default_factory=TakeProfitConfig)
    stop_loss: StopLossConfig = Field(default_factory=StopLossConfig)
    resolution: ResolutionExitConfig = Field(default_factory=ResolutionExitConfig)
    flatten: FlattenConfig = Field(default_factory=FlattenConfig)
    orphan: OrphanConfig = Field(default_factory=OrphanConfig)
    inventory_skew: InventorySkewConfig = Field(default_factory=InventorySkewConfig)
    fill_escalation: FillEscalationConfig = Field(default_factory=FillEscalationConfig)


class V3Config(BaseModel):
    """V3 canary configuration — gradual ramp of V3 fair value signals."""

    enabled: bool = False  # Master kill switch
    max_skew_cents: float = 1.0  # Max V3 can move fair value (in cents)
    redis_url: str = "redis://localhost:6379"
    min_confidence: float = 0.70  # Minimum confidence to use V3 signal
    signal_max_age_seconds: float = 300.0  # Max age before falling back to midpoint


class OpsConfig(BaseModel):
    """Operational monitoring and health-check settings."""

    runtime_status_path: str = "./data/runtime_status.json"
    alert_cooldown_s: int = 300
    status_stale_after_s: int = 45
    no_active_quotes_after_s: int = 180
    no_active_quotes_min_markets: int = 1
    reconnect_storm_window_s: int = 300
    reconnect_storm_threshold: int = 5
    excessive_churn_window_s: int = 600
    excessive_churn_ratio: float = 2.0
    excessive_churn_min_cancels: int = 25
    recent_failure_window_s: int = 900

    @field_validator("runtime_status_path")
    @classmethod
    def _validate_status_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ops.runtime_status_path cannot be blank")
        return value

    @field_validator(
        "alert_cooldown_s",
        "status_stale_after_s",
        "no_active_quotes_after_s",
        "no_active_quotes_min_markets",
        "reconnect_storm_window_s",
        "reconnect_storm_threshold",
        "excessive_churn_window_s",
        "excessive_churn_min_cancels",
        "recent_failure_window_s",
    )
    @classmethod
    def _validate_positive_ints(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("ops thresholds must be positive")
        return value

    @field_validator("excessive_churn_ratio")
    @classmethod
    def _validate_churn_ratio(cls, value: float) -> float:
        if value < 1.0:
            raise ValueError("ops.excessive_churn_ratio must be at least 1.0")
        return value


class UniverseWeights(BaseModel):
    """Weights for universe scoring formula from §5."""

    w1_volume: float = 1.0
    w2_depth: float = 0.8
    w3_spread: float = 2.0
    w4_toxicity: float = 1.5
    w5_resolution_risk: float = 1.0
    w6_reward_ev: float = 1.2
    w7_arb_ev: float = 1.5


class Settings(BaseSettings):
    """Root settings loaded from YAML + environment variables.

    Env vars override YAML values. Env var prefix: PMM1_.
    Nested: PMM1_BOT__NAME, PMM1_RISK__PER_MARKET_GROSS_NAV, etc.
    """

    bot: BotConfig = Field(default_factory=BotConfig)
    wallet: WalletConfig = Field(default_factory=WalletConfig)
    market_filters: MarketFiltersConfig = Field(default_factory=MarketFiltersConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    universe_weights: UniverseWeights = Field(default_factory=UniverseWeights)
    exit: ExitConfig = Field(default_factory=ExitConfig)
    v3: V3Config = Field(default_factory=V3Config)
    ops: OpsConfig = Field(default_factory=OpsConfig)
    raw_config: dict[str, Any] = Field(default_factory=dict, exclude=True)

    model_config = {"env_prefix": "PMM1_", "env_nested_delimiter": "__"}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override dict into base dict."""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(
    config_path: str | Path | None = None,
    override_path: str | Path | None = None,
    *,
    enforce_runtime_guards: bool = True,
) -> Settings:
    """Load settings from YAML file(s) + environment variables.

    Args:
        config_path: Path to base YAML config (default: config/default.yaml)
        override_path: Optional overlay config (e.g. config/prod.yaml)

    Returns:
        Fully resolved Settings instance.
    """
    project_root = Path(__file__).parent.parent

    if config_path is None:
        config_path = project_root / "config" / "default.yaml"
    config_path = Path(config_path)

    yaml_data: dict[str, Any] = {}

    if config_path.exists():
        with open(config_path) as f:
            yaml_data = yaml.safe_load(f) or {}

    if override_path is not None:
        override_path = Path(override_path)
        if override_path.exists():
            with open(override_path) as f:
                override_data = yaml.safe_load(f) or {}
            yaml_data = _deep_merge(yaml_data, override_data)

    # Load .env file if present (dotenv-style)
    env_file = project_root / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                # Don't override existing env vars
                if k not in os.environ:
                    os.environ[k] = v

    # Inject sensitive fields from environment into yaml_data before Settings parse
    # Support both PMM1_* prefixed names and the direct names from .env
    env_mappings = {
        "PMM1_WALLET_PRIVATE_KEY": ("wallet", "private_key"),
        "PMM1_WALLET_ADDRESS": ("wallet", "address"),
        "PMM1_API_KEY": ("api", "api_key"),
        "PMM1_API_SECRET": ("api", "api_secret"),
        "PMM1_API_PASSPHRASE": ("api", "api_passphrase"),
        "PMM1_API_FUNDER": ("api", "funder"),
        "PMM1_REDIS_URL": ("storage", "redis_url"),
        "PMM1_POSTGRES_DSN": ("storage", "postgres_dsn"),
        # Direct .env names (without PMM1_ prefix)
        "PRIVATE_KEY": ("wallet", "private_key"),
        "WALLET_ADDRESS": ("wallet", "address"),
        "CHAIN_ID": ("wallet", "chain_id"),
        "CLOB_HOST": ("api", "clob_url"),
        "POLY_API_KEY": ("api", "api_key"),
        "POLY_API_SECRET": ("api", "api_secret"),
        "POLY_PASSPHRASE": ("api", "api_passphrase"),
    }

    for env_var, (section, key) in env_mappings.items():
        val = os.environ.get(env_var)
        if val:
            yaml_data.setdefault(section, {})[key] = val

    # Preserve raw YAML for PMM-2 config loading (before stripping unknown keys)
    raw_config = dict(yaml_data)
    
    # Strip keys not in Settings model (e.g. pmm2) to avoid pydantic extra_forbidden
    known_sections = set(Settings.model_fields.keys())
    for k in list(yaml_data.keys()):
        if k not in known_sections and k != "raw_config":
            del yaml_data[k]
    
    yaml_data["raw_config"] = raw_config
    settings = Settings(**yaml_data)
    if enforce_runtime_guards:
        _validate_runtime_guards(settings)
    return settings


def _validate_runtime_guards(settings: Settings) -> None:
    """Fail closed on dangerous live-mode combinations."""
    if settings.bot.paper_mode:
        return

    missing_fields: list[str] = []
    if not settings.wallet.private_key:
        missing_fields.append("wallet.private_key")
    if not settings.wallet.address:
        missing_fields.append("wallet.address")
    if not settings.api.api_key:
        missing_fields.append("api.api_key")
    if not settings.api.api_secret:
        missing_fields.append("api.api_secret")
    if not settings.api.api_passphrase:
        missing_fields.append("api.api_passphrase")

    if missing_fields:
        raise ValueError(
            "live trading requires credentials for "
            + ", ".join(missing_fields)
        )

    if settings.exit.fill_escalation.taker_enabled:
        taker_ack = os.getenv("PMM1_ACK_TAKER_BOOTSTRAP", "").strip().upper()
        if taker_ack != "YES":
            raise ValueError(
                "live taker bootstrap requires PMM1_ACK_TAKER_BOOTSTRAP=YES"
            )


class SettingsManager:
    """Manages settings with hot-reload support via SIGHUP."""

    _instance: Settings | None = None
    _config_path: str = ""
    _on_reload_callbacks: list[Callable] = []

    @classmethod
    def load(cls, config_path: str) -> Settings:
        """Load settings from path and register SIGHUP handler."""
        cls._config_path = config_path
        cls._instance = load_settings(config_path)
        cls._setup_signal_handler()
        return cls._instance

    @classmethod
    def _setup_signal_handler(cls) -> None:
        """Register SIGHUP handler for config hot-reload."""
        try:
            def handle_sighup(signum, frame):
                cls.reload()
            signal.signal(signal.SIGHUP, handle_sighup)
        except (OSError, ValueError):
            # SIGHUP not available on this platform (e.g. Windows)
            pass

    @classmethod
    def reload(cls) -> Settings | None:
        """Reload settings from disk. Called on SIGHUP or manually."""
        if not cls._config_path:
            return None
        try:
            new_settings = load_settings(cls._config_path)
            old = cls._instance
            cls._instance = new_settings
            _settings_logger.info("config_reloaded", path=cls._config_path)
            for cb in cls._on_reload_callbacks:
                try:
                    cb(old, new_settings)
                except Exception as e:
                    _settings_logger.error("config_reload_callback_error", error=str(e))
            return new_settings
        except Exception as e:
            _settings_logger.error("config_reload_failed", error=str(e))
            return None

    @classmethod
    def on_reload(cls, callback: Callable) -> None:
        """Register a callback for config reloads. Signature: (old, new) -> None."""
        cls._on_reload_callbacks.append(callback)

    @classmethod
    def current(cls) -> Settings | None:
        """Get current settings instance."""
        return cls._instance
