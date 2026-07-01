"""Per-symbol indicator computation for the scanner (closed-bar only).

Computes the v2 indicator set from a closed-candle OHLCV DataFrame using plain
pandas/numpy (no external TA dependency required): EMA(50/200), RSI(14), MACD,
Bollinger(20,2) %B, ATR(14), VWAP, last price, volume, % change.

All values are derived from CLOSED candles only — the most recent row is the
last fully-closed bar handed in by the market-data service — so there is no
lookahead. Returns plain floats (None when a series is too short).
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("trader.scanner.indicators")

# Indicator periods — kept canonical to match v2-design.md.
EMA_FAST = 50
EMA_SLOW = 200
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 20
BB_STDDEV = 2.0
ATR_PERIOD = 14

# Minimum bars to attempt the longest indicator (EMA200 needs the most warm-up).
MIN_BARS = EMA_SLOW + 2


def _ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (adjust=False matches charting platforms)."""
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI; neutral 50 fill where undefined."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Average true range (Wilder smoothing) from high/low/close."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price over the available window (typical price)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vol = df["volume"].cumsum().replace(0.0, np.nan)
    return (typical * df["volume"]).cumsum() / cum_vol


def _safe_float(value) -> Optional[float]:
    """Return a finite float, or None for NaN/inf/unconvertible values."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(f) or np.isinf(f):
        return None
    return f


def compute_indicators(df: pd.DataFrame) -> Optional[dict]:
    """Compute the v2 indicator set from a closed-candle DataFrame.

    Returns a dict of floats matching the v2 `indicators` block, or None when
    there is not enough data (callers treat None as "skip this symbol").
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    if not {"high", "low", "close", "volume"}.issubset(df.columns):
        return None

    close = pd.to_numeric(df["close"], errors="coerce")
    if close.dropna().shape[0] < MIN_BARS:
        return None

    ema_fast = _ema(close, EMA_FAST)
    ema_slow = _ema(close, EMA_SLOW)
    rsi = _rsi(close, RSI_PERIOD)

    macd_line = _ema(close, MACD_FAST) - _ema(close, MACD_SLOW)
    macd_signal = _ema(macd_line, MACD_SIGNAL)
    macd_hist = macd_line - macd_signal

    sma = close.rolling(BB_PERIOD).mean()
    std = close.rolling(BB_PERIOD).std(ddof=0)
    upper = sma + BB_STDDEV * std
    lower = sma - BB_STDDEV * std
    band_width = (upper - lower).replace(0.0, np.nan)
    bb_pctb = (close - lower) / band_width

    atr = _atr(df, ATR_PERIOD)
    vwap = _vwap(df)

    price = _safe_float(close.iloc[-1])
    if price is None or price <= 0:
        return None

    prev_macd_hist = _safe_float(macd_hist.iloc[-2]) if len(macd_hist) >= 2 else None
    prev_rsi = _safe_float(rsi.iloc[-2]) if len(rsi) >= 2 else None

    return {
        "price": price,
        "ema50": _safe_float(ema_fast.iloc[-1]),
        "ema200": _safe_float(ema_slow.iloc[-1]),
        "rsi14": _safe_float(rsi.iloc[-1]),
        "rsi14_prev": prev_rsi,
        "macd_hist": _safe_float(macd_hist.iloc[-1]),
        "macd_hist_prev": prev_macd_hist,
        "bb_pctb": _safe_float(bb_pctb.iloc[-1]),
        "atr14": _safe_float(atr.iloc[-1]),
        "vwap": _safe_float(vwap.iloc[-1]),
        "volume": _safe_float(df["volume"].iloc[-1]),
    }
