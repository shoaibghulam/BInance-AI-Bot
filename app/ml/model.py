"""WinProbModel — win-probability classifier with graceful backend fallback.

Backend preference: LightGBM -> XGBoost -> scikit-learn LogisticRegression.
scikit-learn is guaranteed present (see requirements.txt); lightgbm/xgboost are
optional. The chosen backend is recorded in `self.backend`.

The model predicts P(win) for a candidate trade setup from the shared feature
vector (app/ml/features.py). Training uses a TIME-ORDERED split (never shuffle)
and fits the standardizing scaler on the TRAIN slice only to avoid leakage.

Persistence is via joblib to data/models/{bot_id}.joblib.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np

from app.ml.features import FEATURE_NAMES

logger = logging.getLogger("trader.ml.model")

# Fraction of (time-ordered) samples held out at the end for validation.
VALIDATION_FRACTION = 0.25
_MIN_VALIDATION = 5
# Minimum validation-tail size before probability calibration is applied.
_MIN_CALIBRATION = 20


def _detect_backend() -> str:
    """Return the best available model backend name."""
    try:
        import lightgbm  # noqa: F401

        return "lightgbm"
    except Exception:
        pass
    try:
        import xgboost  # noqa: F401

        return "xgboost"
    except Exception:
        pass
    return "sklearn"


class WinProbModel:
    """Binary win/loss probability model over the shared feature vector."""

    def __init__(self, bot_id: str, backend: Optional[str] = None) -> None:
        self.bot_id = bot_id
        self.backend = backend or _detect_backend()
        self.feature_names = list(FEATURE_NAMES)
        self._estimator = None      # the (possibly calibrated) predictor
        self._raw_estimator = None  # uncalibrated base (for importances/params)
        self._scaler = None
        self._calibrated = False
        self._n_parameters = 0
        self._metrics: dict = {"accuracy": None, "auc": None, "n_samples": 0,
                               "brier": None, "n_parameters": 0}
        self._trained = False

    # --- Construction helpers -------------------------------------------
    def _new_estimator(self):
        """Instantiate the underlying estimator for the active backend.

        Kept deliberately SHALLOW + regularized: small tabular data overfits
        fast, and the tree-vs-NN literature (Grinsztajn 2022; Shwartz-Ziv &
        Armon 2022) shows a well-regularized GBDT is the best choice here.
        `class_weight="balanced"` counters win/loss imbalance.
        """
        if self.backend == "lightgbm":
            import lightgbm as lgb

            return lgb.LGBMClassifier(
                n_estimators=200,
                num_leaves=15,
                max_depth=4,
                learning_rate=0.05,
                min_child_samples=5,
                subsample=0.9,
                colsample_bytree=0.9,
                reg_lambda=1.0,
                class_weight="balanced",
                verbose=-1,
            )
        if self.backend == "xgboost":
            import xgboost as xgb

            return xgb.XGBClassifier(
                n_estimators=200,
                max_depth=3,
                learning_rate=0.05,
                reg_lambda=1.0,
                eval_metric="logloss",
                verbosity=0,
            )
        from sklearn.linear_model import LogisticRegression

        return LogisticRegression(max_iter=1000, C=1.0, class_weight="balanced")

    @staticmethod
    def _recency_weights(n: int) -> "np.ndarray":
        """Linear recency weights (oldest≈0.5 → newest≈1.0) over n samples.

        Privileges recent regime without hard-forgetting old trades — gentler
        and more stable than a hard rolling window on small data.
        """
        if n <= 1:
            return np.ones(max(1, n), dtype=float)
        return np.linspace(0.5, 1.0, n, dtype=float)

    # --- Training --------------------------------------------------------
    def train(self, X: list[list[float]], y: list[int]) -> dict:
        """Fit the model on time-ordered samples; return `metrics()`.

        `X` rows must be in the canonical FEATURE_NAMES order and chronological
        (oldest first) so the tail split is a genuine out-of-sample window.
        Raises ValueError on empty/degenerate input so the trainer can report a
        clear error rather than persisting a useless model.
        """
        from sklearn.preprocessing import StandardScaler

        X_arr = np.asarray(X, dtype=float)
        y_arr = np.asarray(y, dtype=int)
        if X_arr.ndim != 2 or X_arr.shape[0] == 0:
            raise ValueError("training requires a non-empty 2D feature matrix")
        if X_arr.shape[0] != y_arr.shape[0]:
            raise ValueError("X and y length mismatch")
        if len(np.unique(y_arr)) < 2:
            raise ValueError("training needs both win and loss examples")

        n = X_arr.shape[0]
        n_val = max(_MIN_VALIDATION, int(round(n * VALIDATION_FRACTION)))
        n_val = min(n_val, n - 1)  # always keep >=1 training row
        split = n - n_val
        X_train, X_val = X_arr[:split], X_arr[split:]
        y_train, y_val = y_arr[:split], y_arr[split:]

        # Fit scaler on the TRAIN slice ONLY (no leakage from validation).
        scaler = StandardScaler().fit(X_train)
        X_train_s = scaler.transform(X_train)
        X_val_s = scaler.transform(X_val)

        # Single-class training slice after the time split → fit on everything.
        if len(np.unique(y_train)) < 2:
            base = self._new_estimator()
            base.fit(scaler.transform(X_arr), y_arr,
                     sample_weight=self._recency_weights(n))
            self._scaler = scaler
            self._raw_estimator = base
            self._estimator = base
            self._calibrated = False
            self._n_parameters = self._count_parameters(base)
            self._trained = True
            self._metrics = self._score(X_val_s, y_val, n)
            return self.metrics()

        # Fit the base estimator with recency sample-weights.
        base = self._new_estimator()
        base.fit(X_train_s, y_train, sample_weight=self._recency_weights(split))

        # Probability CALIBRATION (Platt/sigmoid) so the 0.55 gate is a real
        # probability. cv="prefit" calibrates the already-fit base on the
        # time-ordered validation tail. Isotonic overfits <1000 samples, so
        # sigmoid is used until there is much more data. Skipped when the tail
        # is too small or single-class.
        predictor = base
        calibrated = False
        if n_val >= _MIN_CALIBRATION and len(np.unique(y_val)) >= 2:
            try:
                from sklearn.calibration import CalibratedClassifierCV

                try:
                    # sklearn >= 1.6: wrap the already-fit base as frozen.
                    from sklearn.frozen import FrozenEstimator

                    cal = CalibratedClassifierCV(
                        FrozenEstimator(base), method="sigmoid")
                except Exception:
                    # Older sklearn: cv="prefit" calibrates a fitted estimator.
                    cal = CalibratedClassifierCV(base, method="sigmoid", cv="prefit")
                cal.fit(X_val_s, y_val)
                predictor = cal
                calibrated = True
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("calibration skipped: %s", exc)

        self._scaler = scaler
        self._raw_estimator = base
        self._estimator = predictor
        self._calibrated = calibrated
        self._n_parameters = self._count_parameters(base)
        self._trained = True
        self._metrics = self._score(X_val_s, y_val, n)
        logger.info(
            "Trained %s model for %s (n=%d, params=%d, calibrated=%s, acc=%s, auc=%s).",
            self.backend, self.bot_id, n, self._n_parameters, calibrated,
            self._metrics["accuracy"], self._metrics["auc"],
        )
        return self.metrics()

    def _count_parameters(self, estimator) -> int:
        """Family-appropriate effective parameter count.

        GBDT → total leaf count across all trees (its effective parameters).
        Linear → number of coefficients + intercept.
        """
        try:
            if self.backend == "lightgbm":
                booster = estimator.booster_
                # sum of leaves across all trees
                info = booster.dump_model()
                leaves = 0
                for tree in info.get("tree_info", []):
                    leaves += int(tree.get("num_leaves", 0))
                return leaves
            if self.backend == "xgboost":
                booster = estimator.get_booster()
                # each dumped tree line with "leaf=" is one leaf parameter
                dumps = booster.get_dump()
                return sum(d.count("leaf=") for d in dumps)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("param count failed: %s", exc)
        # Linear fallback: coefficients + intercept.
        if hasattr(estimator, "coef_"):
            n = int(np.asarray(estimator.coef_).size)
            if hasattr(estimator, "intercept_"):
                n += int(np.asarray(estimator.intercept_).size)
            return n
        return 0

    def _score(self, X_val_s, y_val, n_samples: int) -> dict:
        """Validation accuracy + AUC + Brier (calibration) on the time tail."""
        from sklearn.metrics import (
            accuracy_score,
            brier_score_loss,
            roc_auc_score,
        )

        accuracy = auc = brier = None
        try:
            preds = self._estimator.predict(X_val_s)
            accuracy = float(accuracy_score(y_val, preds))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("accuracy scoring failed: %s", exc)
        try:
            if len(np.unique(y_val)) >= 2:
                proba = self._estimator.predict_proba(X_val_s)[:, 1]
                auc = float(roc_auc_score(y_val, proba))
                brier = float(brier_score_loss(y_val, proba))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("auc/brier scoring failed: %s", exc)
        return {
            "accuracy": accuracy, "auc": auc, "brier": brier,
            "n_samples": int(n_samples), "n_parameters": int(self._n_parameters),
        }

    # --- Inference -------------------------------------------------------
    def predict_proba(self, features: dict | list[float]) -> float:
        """Return P(win) in [0, 1] for one feature dict/vector.

        Returns 0.5 (neutral) when the model is untrained so callers can treat
        an unwarmed model as "no opinion" without special-casing None.
        """
        if not self._trained or self._estimator is None or self._scaler is None:
            return 0.5
        vec = self._as_vector(features)
        X_s = self._scaler.transform(np.asarray([vec], dtype=float))
        try:
            proba = self._estimator.predict_proba(X_s)[0, 1]
            return float(max(0.0, min(1.0, proba)))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("predict_proba failed: %s", exc)
            return 0.5

    def _as_vector(self, features) -> list[float]:
        """Coerce a feature dict or list into the canonical ordered vector."""
        if isinstance(features, dict):
            from app.ml.features import to_vector

            return to_vector(features)
        return [float(v) for v in features]

    # --- Introspection ---------------------------------------------------
    @property
    def trained(self) -> bool:
        """True once `train()` has produced a usable estimator."""
        return self._trained

    def metrics(self) -> dict:
        """Return `{accuracy, auc, brier, n_samples, n_parameters}`."""
        return dict(self._metrics)

    @property
    def n_parameters(self) -> int:
        """Effective parameter count of the trained model (0 if untrained)."""
        return int(self._n_parameters)

    @property
    def calibrated(self) -> bool:
        """True if probabilities are Platt/sigmoid-calibrated."""
        return self._calibrated

    def model_type(self) -> str:
        """Human-readable model description for the dashboard."""
        names = {
            "lightgbm": "Gradient-Boosted Trees (LightGBM)",
            "xgboost": "Gradient-Boosted Trees (XGBoost)",
            "sklearn": "Logistic Regression (scikit-learn)",
        }
        base = names.get(self.backend, self.backend)
        return base + (" + Platt calibration" if self._calibrated else "")

    def feature_importance(self) -> list[tuple[str, float]]:
        """Return `[(feature_name, weight)]` sorted by absolute weight desc."""
        if not self._trained or self._estimator is None:
            return []
        weights = self._raw_importances()
        if weights is None:
            return []
        pairs = list(zip(self.feature_names, (float(w) for w in weights)))
        pairs.sort(key=lambda kv: abs(kv[1]), reverse=True)
        return pairs

    def _raw_importances(self):
        """Pull importances/coeffs from the UNCALIBRATED base estimator.

        The calibrated wrapper hides feature_importances_/coef_, so read the
        raw base estimator captured at train time.
        """
        est = self._raw_estimator or self._estimator
        if hasattr(est, "feature_importances_"):
            imp = np.asarray(est.feature_importances_, dtype=float)
            total = imp.sum()
            return (imp / total).tolist() if total > 0 else imp.tolist()
        if hasattr(est, "coef_"):
            return np.asarray(est.coef_, dtype=float).ravel().tolist()
        return None

    # --- Persistence -----------------------------------------------------
    def save(self, path: str) -> None:
        """Persist estimator + scaler + metadata via joblib."""
        import joblib

        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {
            "bot_id": self.bot_id,
            "backend": self.backend,
            "feature_names": self.feature_names,
            "estimator": self._estimator,
            "raw_estimator": self._raw_estimator,
            "scaler": self._scaler,
            "calibrated": self._calibrated,
            "n_parameters": self._n_parameters,
            "metrics": self._metrics,
            "trained": self._trained,
        }
        joblib.dump(payload, path)
        logger.info("Saved %s model to %s", self.bot_id, path)

    @classmethod
    def load(cls, path: str) -> "WinProbModel":
        """Load a persisted model; raises FileNotFoundError if absent."""
        import joblib

        if not os.path.exists(path):
            raise FileNotFoundError(path)
        payload = joblib.load(path)
        model = cls(bot_id=payload.get("bot_id", "unknown"),
                    backend=payload.get("backend"))
        model.feature_names = payload.get("feature_names", list(FEATURE_NAMES))
        model._estimator = payload.get("estimator")
        model._raw_estimator = payload.get("raw_estimator") or payload.get("estimator")
        model._scaler = payload.get("scaler")
        model._calibrated = bool(payload.get("calibrated", False))
        model._n_parameters = int(payload.get("n_parameters", 0) or 0)
        model._metrics = payload.get("metrics", {"accuracy": None, "auc": None,
                                                 "n_samples": 0})
        model._trained = bool(payload.get("trained"))
        return model
