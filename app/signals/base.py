"""SignalEngine contract.

Matches docs/architecture/signal-engines.md exactly: an `Action` enum, frozen
`Signal` and `MarketData` dataclasses, and a `SignalEngine` ABC whose single
abstract method is `generate_signal(market_data) -> Signal`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):
    """Discrete trade action a signal engine can recommend."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    CLOSE = "CLOSE"


@dataclass(frozen=True)
class Signal:
    """An immutable engine decision.

    `size_hint` and `confidence` are advisory in [0, 1]; the deterministic risk
    layer clamps `size_hint` and never lets an engine set leverage or final size.
    """

    action: Action
    confidence: float  # 0.0 .. 1.0
    size_hint: float  # 0.0 .. 1.0 advisory; risk layer CLAMPS it
    reason: str  # human-readable, for logs/audit
    source: str  # which engine produced it


@dataclass(frozen=True)
class MarketData:
    """Closed-candle market snapshot handed to an engine on each bar."""

    symbol: str
    ohlcv: object  # DataFrame of CLOSED candles
    indicators: dict  # precomputed: rsi, macd, atr, bb_width, ...
    extra: dict | None = None  # news, funding rate, TV rating, webhook payload


class SignalEngine(ABC):
    """Abstract decision backend. Implementations are interchangeable."""

    name: str = "base"

    @abstractmethod
    def generate_signal(self, market_data: MarketData) -> Signal:
        """Return a `Signal` for the given closed-bar `MarketData`."""
        raise NotImplementedError


def hold(source: str, reason: str = "no signal") -> Signal:
    """Convenience constructor for a safe default HOLD signal."""
    return Signal(
        action=Action.HOLD,
        confidence=0.0,
        size_hint=0.0,
        reason=reason,
        source=source,
    )
