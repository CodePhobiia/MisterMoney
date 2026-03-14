"""Online model weight adaptation via MWU (Paper 2 §4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from pmm1.math.ensemble import update_weights_mwu

log = structlog.get_logger()


class ModelWeightTracker:
    """Tracks per-model performance and adapts weights via MWU.

    Paper 2: with η = sqrt(2 ln K / T), regret ≤ sqrt(T ln K / 2).
    For 3 LLMs and 100 questions, per-question regret ≈ 0.074.
    """

    def __init__(
        self,
        models: list[str],
        min_weight: float = 0.05,
        eta: float = 0.1,
    ) -> None:
        self.models = models
        self.min_weight = min_weight
        self.eta = eta
        self.weights = {m: 1.0 / len(models) for m in models}
        self.history: list[dict[str, Any]] = []
        self._round = 0

    def update(self, losses: dict[str, float]) -> None:
        """Update weights after a market resolves.

        Args:
            losses: {model_name: brier_loss} for this observation.
        """
        self._round += 1
        current = [self.weights[m] for m in self.models]
        loss_list = [losses.get(m, 0.25) for m in self.models]
        updated = update_weights_mwu(
            current, loss_list,
            eta=self.eta, min_weight=self.min_weight,
            round_number=self._round,
        )
        for m, w in zip(self.models, updated):
            self.weights[m] = w
        self.history.append({
            "losses": losses,
            "weights_after": dict(self.weights),
        })
        log.info(
            "model_weights_updated",
            weights={m: round(w, 4) for m, w in self.weights.items()},
        )

    def get_weights(self) -> dict[str, float]:
        return dict(self.weights)

    def get_weight_list(self, models: list[str]) -> list[float]:
        """Get weights for a specific model list (for log_pool)."""
        return [self.weights.get(m, 0.1) for m in models]

    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump({
                "models": self.models,
                "weights": self.weights,
                "eta": self.eta,
                "min_weight": self.min_weight,
                "history_len": len(self.history),
            }, f, indent=2)

    def load(self, path: str) -> None:
        with open(path) as f:
            data = json.load(f)
        self.models = data["models"]
        self.weights = data["weights"]
        self.eta = data.get("eta", 0.1)
        self.min_weight = data.get("min_weight", 0.05)
