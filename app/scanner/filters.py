"""Per-strategy entry filters + scoring, derived from the-five-bots.md.

Each strategy exposes `evaluate(indicators, context) -> FilterResult`:
- `filters`: dict of named boolean checks (surfaced verbatim in the API),
- `passed`: whether the setup is tradeable (all hard filters true),
- `score`: 0..1 ranking score (higher = stronger setup),
- `side`: "LONG" | "SHORT" — the direction the setup implies.

These are deliberately simple, few-parameter rules (overfitting guard from
risk-management.md). They define the candidate set; the ML gate and risk engine
decide what actually trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# High-volume session windows (UTC) for the day-trading session gate.
LONDON_OPEN, LONDON_CLOSE = 7, 11
US_OPEN, US_CLOSE = 13, 17


@dataclass(frozen=True)
class FilterResult:
    """Outcome of evaluating one symbol against a strategy's filter set."""

    filters: dict
    passed: bool
    score: float
    side: str


def session_active(now: Optional[datetime] = None) -> bool:
    """True inside a London or US high-volume session window (UTC)."""
    now = now or datetime.now(timezone.utc)
    hour = now.hour
    return (LONDON_OPEN <= hour < LONDON_CLOSE) or (US_OPEN <= hour < US_CLOSE)


def _clamp01(value: float) -> float:
    """Clamp a float into [0, 1]."""
    return max(0.0, min(1.0, value))


def _g(ind: dict, key: str, default: float = 0.0) -> float:
    """Safe numeric getter for an indicator dict."""
    value = ind.get(key)
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _day_trading(ind: dict, ctx: dict) -> FilterResult:
    """Trend + RSI pullback + VWAP reclaim inside a session window."""
    price = _g(ind, "price")
    ema200 = _g(ind, "ema200", price)
    rsi = _g(ind, "rsi14", 50.0)
    rsi_prev = _g(ind, "rsi14_prev", rsi)
    macd_hist = _g(ind, "macd_hist")
    macd_prev = _g(ind, "macd_hist_prev", macd_hist)
    vwap = _g(ind, "vwap", price)
    atr_pct = _g(ind, "atr14") / price if price else 0.0

    above_ema200 = price > ema200
    side = "LONG" if above_ema200 else "SHORT"

    if side == "LONG":
        rsi_pullback = rsi_prev < 40.0 and rsi > rsi_prev
        macd_flip = macd_hist > 0 and macd_prev <= macd_hist
        vwap_reclaim = price >= vwap
    else:
        rsi_pullback = rsi_prev > 60.0 and rsi < rsi_prev
        macd_flip = macd_hist < 0 and macd_prev >= macd_hist
        vwap_reclaim = price <= vwap

    atr_ok = 0.001 <= atr_pct <= 0.08  # skip dead tape and news-shock spikes
    in_session = bool(ctx.get("session_active"))

    filters = {
        "session_open": in_session,
        "trend_ema": True,  # direction chosen from EMA200; always defined
        "rsi_pullback": rsi_pullback,
        "macd_flip": macd_flip,
        "vwap_reclaim": vwap_reclaim,
        "atr_ok": atr_ok,
    }
    passed = in_session and rsi_pullback and macd_flip and vwap_reclaim and atr_ok
    trend_strength = abs(price - ema200) / price if price else 0.0
    score = _clamp01(0.3 * vwap_reclaim + 0.3 * macd_flip + 0.2 * rsi_pullback
                     + 0.2 * _clamp01(trend_strength * 20))
    return FilterResult(filters=filters, passed=passed, score=score, side=side)


def _scalping(ind: dict, ctx: dict) -> FilterResult:
    """Bollinger mean-reversion: tag a band with RSI confirmation."""
    price = _g(ind, "price")
    bb_pctb = _g(ind, "bb_pctb", 0.5)
    rsi = _g(ind, "rsi14", 50.0)
    atr_pct = _g(ind, "atr14") / price if price else 0.0

    long_setup = bb_pctb <= 0.1 and rsi < 35.0
    short_setup = bb_pctb >= 0.9 and rsi > 65.0
    side = "LONG" if bb_pctb <= 0.5 else "SHORT"

    liquid = _g(ind, "volume") > 0  # scanner universe is already top-volume
    band_tag = long_setup or short_setup
    vol_sane = atr_pct <= 0.05

    filters = {
        "band_tag": band_tag,
        "rsi_extreme": (rsi < 35.0) or (rsi > 65.0),
        "liquid": liquid,
        "vol_sane": vol_sane,
    }
    passed = band_tag and liquid and vol_sane
    edge = max(0.0, 0.1 - bb_pctb) if side == "LONG" else max(0.0, bb_pctb - 0.9)
    score = _clamp01(0.6 * (edge * 10) + 0.4 * band_tag)
    return FilterResult(filters=filters, passed=passed, score=score, side=side)


def _grid(ind: dict, ctx: dict) -> FilterResult:
    """Ranging detector: flat EMAs + mid Bollinger %B + contained ATR."""
    price = _g(ind, "price")
    ema50 = _g(ind, "ema50", price)
    ema200 = _g(ind, "ema200", price)
    bb_pctb = _g(ind, "bb_pctb", 0.5)
    atr_pct = _g(ind, "atr14") / price if price else 0.0

    ema_spread = abs(ema50 - ema200) / price if price else 1.0
    ranging = ema_spread < 0.01  # EMAs tightly coiled => sideways
    mid_band = 0.25 <= bb_pctb <= 0.75
    atr_ok = atr_pct <= 0.04
    side = "LONG" if price <= ema50 else "SHORT"  # neutral grid bias by location

    filters = {
        "ranging": ranging,
        "mid_band": mid_band,
        "atr_contained": atr_ok,
    }
    passed = ranging and mid_band and atr_ok
    score = _clamp01(0.5 * (1.0 - min(1.0, ema_spread * 100)) + 0.3 * mid_band
                     + 0.2 * atr_ok)
    return FilterResult(filters=filters, passed=passed, score=score, side=side)


def _dca(ind: dict, ctx: dict) -> FilterResult:
    """Mean-reversion base-order entry: oversold dip below VWAP/lower band."""
    price = _g(ind, "price")
    rsi = _g(ind, "rsi14", 50.0)
    bb_pctb = _g(ind, "bb_pctb", 0.5)
    vwap = _g(ind, "vwap", price)

    oversold = rsi < 35.0
    below_vwap = price < vwap
    near_lower = bb_pctb <= 0.2
    side = "LONG"  # base order is a long dip-buy by default

    filters = {
        "rsi_oversold": oversold,
        "below_vwap": below_vwap,
        "near_lower_band": near_lower,
    }
    passed = oversold and (below_vwap or near_lower)
    score = _clamp01(0.5 * (max(0.0, 35.0 - rsi) / 35.0) + 0.3 * below_vwap
                     + 0.2 * near_lower)
    return FilterResult(filters=filters, passed=passed, score=score, side=side)


def _rebalancing(ind: dict, ctx: dict) -> FilterResult:
    """Drift/trend screen: pick basket leaders/laggards by trend alignment.

    Rebalancing has no classic indicator entry — the trigger is weight drift.
    For the scanner we surface trend-leadership so the basket view is meaningful;
    every liquid symbol is a candidate (passed=True) and score ranks leadership.
    """
    price = _g(ind, "price")
    ema50 = _g(ind, "ema50", price)
    ema200 = _g(ind, "ema200", price)

    leading = ema50 > ema200
    momentum = (ema50 - ema200) / price if price else 0.0
    side = "LONG" if leading else "SHORT"

    filters = {
        "is_candidate": True,
        "trend_leader": leading,
    }
    score = _clamp01(0.5 + momentum * 10)  # centered around neutral leadership
    return FilterResult(filters=filters, passed=True, score=score, side=side)


_STRATEGIES = {
    "day-trading": _day_trading,
    "scalping": _scalping,
    "grid": _grid,
    "dca": _dca,
    "rebalancing": _rebalancing,
}


def evaluate(strategy: str, indicators: dict, context: Optional[dict] = None) -> FilterResult:
    """Evaluate `indicators` against `strategy`'s filter set.

    Raises ValueError for an unknown strategy so callers fail loudly.
    """
    fn = _STRATEGIES.get(strategy)
    if fn is None:
        raise ValueError(f"unknown strategy: {strategy}")
    return fn(indicators, context or {})


def strategy_ids() -> list[str]:
    """Canonical strategy/bot ids in display order."""
    return list(_STRATEGIES.keys())
