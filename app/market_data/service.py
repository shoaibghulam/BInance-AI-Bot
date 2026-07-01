"""Market data service.

Fetches klines via REST and returns a pandas DataFrame of CLOSED candles.
Provides a placeholder for adding WebSocket kline/markPrice streams later
(the architecture prefers WS over REST polling to avoid rate-limit bans).

In safe mode (no broker connection) `get_klines` returns an empty DataFrame
with the expected columns rather than raising, so callers stay simple.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from app.broker.client import BrokerClient

logger = logging.getLogger("trader.market_data")

KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
]

_NUMERIC_COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume"]
MAX_KLINE_LIMIT = 1500  # Binance hard cap


class KlineFetchError(Exception):
    """Raised on a kline fetch failure the caller may want to handle.

    `is_ban` is True for a Binance -1003 (too many requests / IP rate-limit
    ban) so callers (e.g. the scanner) can back off rather than hammering the
    API. `raise_on_ban=False` callers keep the legacy "empty frame" behavior.
    """

    def __init__(self, message: str, is_ban: bool = False) -> None:
        super().__init__(message)
        self.is_ban = is_ban


def _is_rate_limit_ban(exc: Exception) -> bool:
    """True if `exc` is a Binance -1003 (too many requests / IP ban)."""
    code = getattr(exc, "code", None)
    if code == -1003:
        return True
    text = str(exc).lower()
    return "-1003" in text or "too many requests" in text or "banned until" in text


def _empty_klines_frame() -> pd.DataFrame:
    """Return an empty, correctly-typed klines DataFrame."""
    return pd.DataFrame(columns=["open_time", *_NUMERIC_COLUMNS, "close_time"])


class MarketDataService:
    """Provides OHLCV data to signal engines. WS streams added later."""

    def __init__(self, broker: BrokerClient) -> None:
        self._broker = broker

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 200,
        raise_on_ban: bool = True,
    ) -> pd.DataFrame:
        """Fetch klines and return a typed DataFrame of closed candles.

        Safe mode / ordinary transient errors → empty DataFrame (never raises),
        preserving the original contract. A Binance -1003 IP rate-limit ban is
        raised as `KlineFetchError(is_ban=True)` when `raise_on_ban` is set so a
        caller (the scanner) can abort its cycle and back off; pass
        `raise_on_ban=False` to fall back to the empty-frame behavior.
        """
        symbol = symbol.upper()
        limit = max(1, min(int(limit), MAX_KLINE_LIMIT))

        client = getattr(self._broker, "_client", None)
        if not self._broker.connected or client is None:
            return _empty_klines_frame()

        try:
            raw = await client.futures_klines(
                symbol=symbol, interval=interval, limit=limit
            )
        except Exception as exc:
            if _is_rate_limit_ban(exc):
                logger.warning("get_klines hit rate-limit ban for %s: %s", symbol, exc)
                if raise_on_ban:
                    raise KlineFetchError(str(exc), is_ban=True) from exc
                return _empty_klines_frame()
            logger.error("get_klines failed for %s %s: %s", symbol, interval, exc)
            return _empty_klines_frame()

        return self._to_dataframe(raw)

    @staticmethod
    def _to_dataframe(raw: list) -> pd.DataFrame:
        """Convert raw Binance kline rows to a numeric DataFrame."""
        if not raw:
            return _empty_klines_frame()
        df = pd.DataFrame(raw, columns=KLINE_COLUMNS)
        for col in _NUMERIC_COLUMNS:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        keep = ["open_time", *_NUMERIC_COLUMNS, "close_time"]
        return df[keep].reset_index(drop=True)

    async def start_streams(self, symbols: Optional[list[str]] = None) -> None:
        """Placeholder for WebSocket stream startup (klines / markPrice).

        Intentionally a no-op for now: REST `get_klines` is sufficient for the
        current bot stubs. WS wiring lands when bot loops go live.
        """
        logger.debug("WebSocket streams not yet enabled (symbols=%s).", symbols)

    async def stop_streams(self) -> None:
        """Placeholder for WebSocket stream teardown."""
        logger.debug("No WebSocket streams to stop.")
