"""Feature engineering — SHARED by training and inference.

`build_features(indicators, context)` turns the scanner's closed-bar indicator
dict (see app/scanner/indicators.py) into a normalized, model-ready feature
vector. The SAME function runs at inference (scanner candidate) and at training
(replayed from a trade's stored `features_json`), so the two can never drift.

No lookahead: only closed-bar values are used. Distances are expressed as
fractions of price so they are scale-free across symbols. Missing inputs map to
neutral values (0.0 distance, 0.5 RSI level, etc.) rather than raising.
"""

from __future__ import annotations

from typing import Optional

# Canonical, ORDERED feature names. The model relies on a stable order, so this
# list is the single source of truth for the vector layout.
FEATURE_NAMES: list[str] = [
    "rsi_level",       # RSI14 scaled to 0..1
    "dist_ema50",      # (price - ema50) / price
    "dist_ema200",     # (price - ema200) / price
    "ema_alignment",   # (ema50 - ema200) / price  (trend strength/direction)
    "macd_hist_sign",  # -1 / 0 / +1
    "macd_hist_norm",  # macd_hist / price
    "bb_pctb",         # Bollinger %B (already ~0..1, clipped)
    "atr_pct",         # atr14 / price  (volatility regime)
    "dist_vwap",       # (price - vwap) / price
    "volume_z",        # volume z-score vs context window
    "session_flag",    # 1.0 inside a high-volume session window, else 0.0
    "side_long",       # 1.0 for a long candidate, 0.0 for short
]


def _num(value, default: float = 0.0) -> float:
    """Coerce to float, falling back to `default` for None/invalid."""
    try:
        if value is None:
            return default
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf guard
        return default
    return f


def _sign(value: float) -> float:
    """Return -1.0 / 0.0 / +1.0 for the sign of a float."""
    if value > 0:
        return 1.0
    if value < 0:
        return -1.0
    return 0.0


def build_features(indicators: dict, context: Optional[dict] = None) -> dict:
    """Build the normalized feature dict for one candidate setup.

    `indicators` is the per-symbol dict from compute_indicators(). `context` may
    carry: `side` ("LONG"/"SHORT"), `session_active` (bool), `volume_mean`,
    `volume_std` (for the z-score). All keys are optional.
    """
    context = context or {}
    price = _num(indicators.get("price"))
    if price <= 0:
        # Degenerate input: return an all-neutral vector so callers never crash.
        return {name: 0.0 for name in FEATURE_NAMES}

    rsi = _num(indicators.get("rsi14"), 50.0)
    ema50 = _num(indicators.get("ema50"), price)
    ema200 = _num(indicators.get("ema200"), price)
    macd_hist = _num(indicators.get("macd_hist"))
    bb_pctb = _num(indicators.get("bb_pctb"), 0.5)
    atr = _num(indicators.get("atr14"))
    vwap = _num(indicators.get("vwap"), price)
    volume = _num(indicators.get("volume"))

    vol_mean = _num(context.get("volume_mean"))
    vol_std = _num(context.get("volume_std"))
    volume_z = (volume - vol_mean) / vol_std if vol_std > 0 else 0.0

    side = str(context.get("side", "LONG")).upper()
    session_active = bool(context.get("session_active", False))

    features = {
        "rsi_level": max(0.0, min(1.0, rsi / 100.0)),
        "dist_ema50": (price - ema50) / price,
        "dist_ema200": (price - ema200) / price,
        "ema_alignment": (ema50 - ema200) / price,
        "macd_hist_sign": _sign(macd_hist),
        "macd_hist_norm": macd_hist / price,
        "bb_pctb": max(-0.5, min(1.5, bb_pctb)),
        "atr_pct": atr / price,
        "dist_vwap": (price - vwap) / price,
        "volume_z": max(-5.0, min(5.0, volume_z)),
        "session_flag": 1.0 if session_active else 0.0,
        "side_long": 1.0 if side == "LONG" else 0.0,
    }
    return features


def to_vector(features: dict) -> list[float]:
    """Project a feature dict onto the canonical ordered vector."""
    return [_num(features.get(name)) for name in FEATURE_NAMES]
