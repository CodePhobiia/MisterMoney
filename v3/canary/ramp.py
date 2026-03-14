"""
Canary Ramp Manager
Manages gradual ramp of V3 influence through stages
"""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml

log = structlog.get_logger()


class CanaryRamp:
    """
    Manages gradual ramp of V3 influence.

    Ramp stages:
    1. Shadow only (v3_enabled=False) — V3 runs, logs, no influence
    2. Canary 1¢ (v3_max_skew_cents=1) — tiny influence
    3. Canary 2¢ (v3_max_skew_cents=2) — small influence
    4. Canary 5¢ (v3_max_skew_cents=5) — moderate influence
    5. Full production (v3_max_skew_cents=100) — uncapped

    Kill switch: set v3_enabled=False in config → immediate fallback to midpoint
    """

    STAGES = [
        {"name": "shadow", "max_skew": 0, "enabled": False},
        {"name": "canary_1c", "max_skew": 1.0, "enabled": True},
        {"name": "canary_2c", "max_skew": 2.0, "enabled": True},
        {"name": "canary_5c", "max_skew": 5.0, "enabled": True},
        {"name": "production", "max_skew": 100.0, "enabled": True},
    ]

    def __init__(self, config_path: str = "config/default.yaml"):
        """
        Initialize canary ramp manager.

        Args:
            config_path: Path to YAML config file
        """
        self.config_path = Path(config_path)
        if not self.config_path.is_absolute():
            # Resolve relative to project root
            project_root = Path(__file__).parent.parent.parent
            self.config_path = project_root / config_path

    def _load_config(self) -> dict:
        """Load YAML config."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config not found: {self.config_path}")

        with open(self.config_path) as f:
            return yaml.safe_load(f) or {}

    def _save_config(self, config: dict) -> None:
        """Save YAML config."""
        with open(self.config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    def get_current_stage(self) -> dict:
        """
        Get current canary stage based on config.

        Returns:
            Stage dict with name, max_skew, enabled, and is_current flag
        """
        config = self._load_config()
        v3_config = config.get("v3", {})

        enabled = v3_config.get("enabled", False)
        max_skew = v3_config.get("max_skew_cents", 0.0)

        # Find matching stage
        for stage in self.STAGES:
            if stage["enabled"] == enabled and stage["max_skew"] == max_skew:
                return {**stage, "is_current": True}

        # No exact match — return custom stage
        return {
            "name": "custom",
            "max_skew": max_skew,
            "enabled": enabled,
            "is_current": True,
        }

    def advance_stage(self) -> dict:
        """
        Advance to the next canary stage.

        Returns:
            New stage dict

        Raises:
            ValueError: If already at the last stage
        """
        current = self.get_current_stage()

        # Find current stage index
        current_idx = None
        for i, stage in enumerate(self.STAGES):
            if (stage["enabled"] == current["enabled"] and
                stage["max_skew"] == current["max_skew"]):
                current_idx = i
                break

        if current_idx is None:
            raise ValueError("Cannot advance from custom stage")

        if current_idx >= len(self.STAGES) - 1:
            raise ValueError("Already at production stage")

        # Advance to next stage
        next_stage = self.STAGES[current_idx + 1]

        # Update config
        config = self._load_config()
        if "v3" not in config:
            config["v3"] = {}

        config["v3"]["enabled"] = next_stage["enabled"]
        config["v3"]["max_skew_cents"] = next_stage["max_skew"]

        self._save_config(config)

        log.info(
            "canary_stage_advanced",
            from_stage=current["name"],
            to_stage=next_stage["name"],
            max_skew=next_stage["max_skew"],
            enabled=next_stage["enabled"],
        )

        return {**next_stage, "is_current": True}

    def retreat_stage(self) -> dict:
        """
        Retreat to the previous canary stage.

        Returns:
            New stage dict

        Raises:
            ValueError: If already at the first stage
        """
        current = self.get_current_stage()

        # Find current stage index
        current_idx = None
        for i, stage in enumerate(self.STAGES):
            if (stage["enabled"] == current["enabled"] and
                stage["max_skew"] == current["max_skew"]):
                current_idx = i
                break

        if current_idx is None:
            raise ValueError("Cannot retreat from custom stage")

        if current_idx <= 0:
            raise ValueError("Already at shadow stage")

        # Retreat to previous stage
        prev_stage = self.STAGES[current_idx - 1]

        # Update config
        config = self._load_config()
        if "v3" not in config:
            config["v3"] = {}

        config["v3"]["enabled"] = prev_stage["enabled"]
        config["v3"]["max_skew_cents"] = prev_stage["max_skew"]

        self._save_config(config)

        log.info(
            "canary_stage_retreated",
            from_stage=current["name"],
            to_stage=prev_stage["name"],
            max_skew=prev_stage["max_skew"],
            enabled=prev_stage["enabled"],
        )

        return {**prev_stage, "is_current": True}

    def emergency_kill(self) -> None:
        """
        Emergency kill switch — disable V3 immediately.
        Sets v3.enabled = False regardless of current stage.
        """
        config = self._load_config()

        if "v3" not in config:
            config["v3"] = {}

        config["v3"]["enabled"] = False

        self._save_config(config)

        log.warning(
            "canary_emergency_kill",
            message="V3 disabled via emergency kill switch",
        )
