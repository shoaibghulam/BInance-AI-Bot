"""ModelTrainer — collects closed trades, retrains per-bot WinProbModels.

Responsibilities:
  - `status(bot_id)`  -> the v2 `GET /api/bots/{id}/ml` insight payload.
  - `train(bot_id)`   -> retrain now from stored closed-trade features/outcomes.
  - `live_predictions(bot_id, scan_results)` -> win_prob per top candidate.
  - `maybe_retrain(bot_id)` -> retrain when enough NEW samples have accrued
    (min 50 to start; every 25 closed trades after).

Models persist to data/models/{bot_id}.joblib and load lazily on first use.
The DB stores each closed trade's `features_json` + outcome, so X/y are rebuilt
in chronological order (time-ordered, never shuffled) — matching the no-lookahead
contract. scikit-learn is always available; LightGBM/XGBoost are used if present.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

from app.ml.features import FEATURE_NAMES, to_vector
from app.ml.model import WinProbModel
from app.persistence.db import Database

logger = logging.getLogger("trader.ml.trainer")

MIN_SAMPLES_TO_START = 50
RETRAIN_EVERY = 25
# Minimum validation AUC before the model is allowed to VETO entries. Below
# this the model is treated as no-edge (coin-flip) and the bot trades on pure
# indicator rules instead of being starved by a noisy gate.
MIN_GATE_AUC = 0.55
MODELS_DIR = os.path.join("data", "models")


def _model_path(bot_id: str) -> str:
    """Filesystem path for a bot's persisted model."""
    return os.path.join(MODELS_DIR, f"{bot_id}.joblib")


class ModelTrainer:
    """Owns per-bot models, training schedule, and inference."""

    def __init__(self, db: Database, min_win_prob: float = 0.55) -> None:
        self._db = db
        self._default_min_win_prob = min_win_prob
        self._models: dict[str, WinProbModel] = {}
        self._training: set[str] = set()
        self._last_trained_count: dict[str, int] = {}
        self._last_duration: dict[str, float] = {}  # bot_id -> last train seconds
        self._lock = asyncio.Lock()

    # --- Model access ----------------------------------------------------
    def _get_or_load(self, bot_id: str) -> Optional[WinProbModel]:
        """Return the in-memory model, loading from disk if persisted."""
        if bot_id in self._models:
            return self._models[bot_id]
        path = _model_path(bot_id)
        if os.path.exists(path):
            try:
                model = WinProbModel.load(path)
                self._models[bot_id] = model
                return model
            except Exception as exc:  # pragma: no cover - corrupt file
                logger.error("Failed to load model for %s: %s", bot_id, exc)
        return None

    # --- Training --------------------------------------------------------
    async def _build_xy(self, bot_id: str) -> tuple[list[list[float]], list[int]]:
        """Rebuild time-ordered X/y from stored closed-trade features."""
        trades = await self._db.closed_bot_trades_asc(bot_id)
        X: list[list[float]] = []
        y: list[int] = []
        for t in trades:
            raw = t.get("features_json")
            outcome = t.get("outcome")
            if not raw or outcome not in ("win", "loss"):
                continue
            try:
                feats = json.loads(raw)
            except (TypeError, ValueError):
                continue
            X.append(to_vector(feats))
            y.append(1 if outcome == "win" else 0)
        return X, y

    async def train(self, bot_id: str) -> dict:
        """Retrain `bot_id` now from its closed-trade history. Returns status."""
        async with self._lock:
            if bot_id in self._training:
                return await self.status(bot_id)
            self._training.add(bot_id)
        try:
            X, y = await self._build_xy(bot_id)
            n = len(X)
            if n < MIN_SAMPLES_TO_START:
                return await self.status(bot_id)
            model = WinProbModel(bot_id=bot_id)
            # CPU-bound fit: run in a worker thread so it never blocks the event
            # loop (which would freeze every bot tick + SL/TP check during a fit).
            import time as _time

            _t0 = _time.monotonic()
            metrics = await asyncio.to_thread(model.train, X, y)
            self._last_duration[bot_id] = _time.monotonic() - _t0
            model.save(_model_path(bot_id))
            self._models[bot_id] = model
            self._last_trained_count[bot_id] = n
            await self._db.record_model_metrics(
                bot_id=bot_id,
                accuracy=metrics.get("accuracy"),
                auc=metrics.get("auc"),
                n_samples=metrics.get("n_samples", n),
            )
            logger.info("Retrained %s: %s", bot_id, metrics)
        except Exception as exc:
            logger.error("Training failed for %s: %s", bot_id, exc)
            return {**await self.status(bot_id), "status": "error",
                    "error": str(exc)}
        finally:
            self._training.discard(bot_id)
        return await self.status(bot_id)

    async def maybe_retrain(self, bot_id: str) -> Optional[dict]:
        """Retrain if enough new closed trades have accrued; else no-op.

        Trains once at MIN_SAMPLES_TO_START, then every RETRAIN_EVERY trades.
        """
        n = await self._db.count_closed_bot_trades(bot_id)
        if n < MIN_SAMPLES_TO_START:
            return None
        last = self._last_trained_count.get(bot_id, 0)
        if last == 0 or (n - last) >= RETRAIN_EVERY:
            return await self.train(bot_id)
        return None

    # --- Inference -------------------------------------------------------
    def predict(self, bot_id: str, features: dict) -> Optional[float]:
        """Return P(win) for a feature dict, or None if no trained model."""
        model = self._get_or_load(bot_id)
        if model is None or not model.trained:
            return None
        return model.predict_proba(features)

    def gate_has_edge(self, bot_id: str) -> bool:
        """True only if the model may VETO trades — i.e. it has real edge.

        A model whose validation AUC is at/below coin-flip (~0.5) is noise;
        letting it block entries just starves the bot. We require AUC >=
        MIN_GATE_AUC before the win-probability threshold is enforced.
        """
        model = self._get_or_load(bot_id)
        if model is None or not model.trained:
            return False
        auc = (model.metrics() or {}).get("auc")
        return auc is not None and auc >= MIN_GATE_AUC

    def live_predictions(
        self, bot_id: str, scan_results: list[dict], top_n: int = 10
    ) -> list[dict]:
        """Win-prob per top scanner candidate; empty while warming up.

        Reuses the `_indicators` / `_volume_*` internals attached to scan rows
        by the scanner so features are built consistently with training.
        """
        from app.ml.features import build_features

        model = self._get_or_load(bot_id)
        if model is None or not model.trained:
            return []
        out: list[dict] = []
        for row in scan_results[:top_n]:
            indicators = row.get("_indicators")
            if not indicators:
                continue
            ctx = {
                "side": row.get("side", "LONG"),
                "session_active": row.get("_session_active", False),
                "volume_mean": row.get("_volume_mean", 0.0),
                "volume_std": row.get("_volume_std", 0.0),
            }
            feats = build_features(indicators, ctx)
            prob = model.predict_proba(feats)
            out.append({"symbol": row["symbol"], "win_prob": round(prob, 4)})
        return out

    # --- Status payload --------------------------------------------------
    async def status(self, bot_id: str, min_win_prob: Optional[float] = None) -> dict:
        """Build the v2 `GET /api/bots/{id}/ml` payload."""
        model = self._get_or_load(bot_id)
        n_closed = await self._db.count_closed_bot_trades(bot_id)
        history = await self._db.model_metrics_history(bot_id)
        history_pairs = [
            [m["ts"], m.get("accuracy")] for m in history if m.get("accuracy") is not None
        ]
        threshold = min_win_prob if min_win_prob is not None else self._default_min_win_prob

        if bot_id in self._training:
            status = "training"
        elif model is not None and model.trained:
            status = "trained"
        elif n_closed < MIN_SAMPLES_TO_START:
            status = "warming_up"
        else:
            status = "warming_up"

        metrics = model.metrics() if (model and model.trained) else {
            "accuracy": None, "auc": None, "brier": None, "n_samples": n_closed,
            "n_parameters": 0,
        }
        importance = model.feature_importance() if (model and model.trained) else []
        latest = history[-1] if history else None
        is_trained = bool(model and model.trained)

        return {
            "status": status,
            "model": model.backend if model else WinProbModel(bot_id).backend,
            "model_type": model.model_type() if is_trained else (
                (model or WinProbModel(bot_id)).model_type()),
            "calibrated": bool(is_trained and model.calibrated),
            "gate_active": self.gate_has_edge(bot_id),
            "n_parameters": int(metrics.get("n_parameters", 0) or 0),
            "train_duration_s": round(self._last_duration.get(bot_id, 0.0), 2),
            "training_samples": int(metrics.get("n_samples", n_closed) or n_closed),
            "n_samples": n_closed,
            "trained_at": latest["ts"] if latest else None,
            "metrics": {
                "accuracy": metrics.get("accuracy"),
                "auc": metrics.get("auc"),
                "brier": metrics.get("brier"),
                "history": history_pairs,
            },
            "feature_importance": [[name, round(w, 6)] for name, w in importance],
            "live_predictions": [],  # filled by caller with live scan results
            "min_win_prob": threshold,
            "samples_needed": max(0, MIN_SAMPLES_TO_START - n_closed),
            "feature_names": list(FEATURE_NAMES),
        }
