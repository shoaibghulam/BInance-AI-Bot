"""Deterministic indicator-rule signal engine.

EMA cross + RSI filter over a closed-candle DataFrame. Dependency-light: uses
`pandas_ta` if importable, otherwise computes EMA/RSI with plain pandas so the
engine always runs. Never raises into the bot loop — defaults to HOLD on any
data problem.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from app.signals.base import Action, MarketData, Signal, SignalEngine, hold

try:  # optional acceleration; not required
    import pandas_ta as _pandas_ta  # noqa: F401

    _HAS_PANDAS_TA = True
except Exception:  # pragma: no cover - import guard
    _HAS_PANDAS_TA = False


# --- Default rule parameters (kept few to resist overfitting) ---
DEFAULT_FAST_EMA = 9
DEFAULT_SLOW_EMA = 21
DEFAULT_RSI_PERIOD = 14
DEFAULT_RSI_OVERBOUGHT = 70.0
DEFAULT_RSI_OVERSOLD = 30.0
DEFAULT_CONFIDENCE = 0.6
DEFAULT_SIZE_HINT = 0.5


def _ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average via plain pandas (`adjust=False`)."""
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI via plain pandas (no external indicator lib required)."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


class IndicatorRuleEngine(SignalEngine):
    """EMA-cross with an RSI confirmation filter.

    BUY  : fast EMA crosses above slow EMA and RSI not overbought.
    SELL : fast EMA crosses below slow EMA and RSI not oversold.
    HOLD : otherwise (the safe default).
    """

    name = "indicator_rule"

    def __init__(
        self,
        fast_ema: int = DEFAULT_FAST_EMA,
        slow_ema: int = DEFAULT_SLOW_EMA,
        rsi_period: int = DEFAULT_RSI_PERIOD,
        rsi_overbought: float = DEFAULT_RSI_OVERBOUGHT,
        rsi_oversold: float = DEFAULT_RSI_OVERSOLD,
    ) -> None:
        if fast_ema >= slow_ema:
            raise ValueError("fast_ema must be < slow_ema")
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold

    def _close_series(self, market_data: MarketData) -> Optional[pd.Series]:
        """Extract a numeric close-price series, or None if unavailable."""
        df = market_data.ohlcv
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        if "close" not in df.columns:
            return None
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        min_bars = self.slow_ema + 2
        if len(close) < min_bars:
            return None
        return close.reset_index(drop=True)

    def generate_signal(self, market_data: MarketData) -> Signal:
        """Compute EMA cross + RSI filter and return a `Signal`."""
        close = self._close_series(market_data)
        if close is None:
            return hold(self.name, "insufficient candle data")

        fast = _ema(close, self.fast_ema)
        slow = _ema(close, self.slow_ema)
        rsi = _rsi(close, self.rsi_period)

        fast_now, fast_prev = float(fast.iloc[-1]), float(fast.iloc[-2])
        slow_now, slow_prev = float(slow.iloc[-1]), float(slow.iloc[-2])
        rsi_now = float(rsi.iloc[-1])

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if crossed_up and rsi_now < self.rsi_overbought:
            return Signal(
                action=Action.BUY,
                confidence=DEFAULT_CONFIDENCE,
                size_hint=DEFAULT_SIZE_HINT,
                reason=(
                    f"EMA{self.fast_ema} crossed above EMA{self.slow_ema}; "
                    f"RSI {rsi_now:.1f} < {self.rsi_overbought:.0f}"
                ),
                source=self.name,
            )
        if crossed_down and rsi_now > self.rsi_oversold:
            return Signal(
                action=Action.SELL,
                confidence=DEFAULT_CONFIDENCE,
                size_hint=DEFAULT_SIZE_HINT,
                reason=(
                    f"EMA{self.fast_ema} crossed below EMA{self.slow_ema}; "
                    f"RSI {rsi_now:.1f} > {self.rsi_oversold:.0f}"
                ),
                source=self.name,
            )

        return hold(
            self.name,
            f"no EMA cross (fast={fast_now:.2f}, slow={slow_now:.2f}, rsi={rsi_now:.1f})",
        )
