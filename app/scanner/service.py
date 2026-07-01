"""MarketScanner — top-N USDⓈ-M perps, per-symbol indicators, filter + rank.

Flow per refresh(strategy):
  1. Universe: top ~30 USDT-margined perps by 24h quote volume
     (Binance GET /fapi/v1/ticker/24hr, sorted, sliced).
  2. Per symbol: fetch klines via the existing MarketDataService (bounded
     concurrency + per-call timeout), compute the v2 indicator set.
  3. Apply the strategy's filter set -> pass/fail per filter + score + side.
  4. Rank survivors by score (passed first), assign rank, cache the v2 shape.

CRITICAL — HTTP must never block on a fresh fetch:
  - `cached(strategy)` returns the LAST good scan instantly (or a cold
    "scanning" payload with `results: []`, `scanned_at: None`). Used by every
    REST/WS read path.
  - `refresh(strategy)` does the network work; it is called ONLY from the
    background scan loop in app.main, never from a request handler.

BAN/ERROR resilience: a Binance -1003 (IP rate-limit ban) or timeout during a
refresh aborts that cycle and keeps serving the last good cache. The scanner
exposes `banned_until` so the background loop can back off. In SAFE MODE (no
broker connection) refresh produces an empty result set gracefully.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from app.broker.client import BrokerClient
from app.market_data.service import KlineFetchError, MarketDataService
from app.scanner import filters as strat_filters
from app.scanner.indicators import compute_indicators

logger = logging.getLogger("trader.scanner")

UNIVERSE_SIZE = 30
KLINE_LIMIT = 250  # enough warm-up for EMA200
CACHE_TTL_S = 30.0  # refresh at most every 30s per strategy (keeps REST weight low)
KLINE_CONCURRENCY = 5  # max simultaneous kline fetches per scan
KLINE_TIMEOUT_S = 8.0  # per-symbol kline call timeout
BATCH_PAUSE_S = 0.2  # small delay between concurrent batches
BAN_BACKOFF_S = 60.0  # back off this long after a -1003 ban
_QUOTE_SUFFIX = "USDT"
# Per-strategy default klines timeframe (matches each bot's trade TF).
STRATEGY_TIMEFRAME = {
    "day-trading": "15m",
    "scalping": "1m",
    "grid": "15m",
    "dca": "1h",
    "rebalancing": "4h",
}


def _utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _empty_scan() -> dict:
    """Cold/empty scan payload in the valid v2 contract shape."""
    return {"scanned_at": None, "universe_size": 0, "results": []}


class MarketScanner:
    """Scans the top-volume USDⓈ-M universe and ranks it per strategy.

    Reads (`cached`) are instant and non-blocking; writes (`refresh`) do the
    network work and are driven by the background loop only.
    """

    def __init__(self, broker: BrokerClient, market_data: MarketDataService) -> None:
        self._broker = broker
        self._market_data = market_data
        self._cache: dict[str, tuple[float, dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._banned_until: float = 0.0  # monotonic deadline; 0 = not banned

    # --- Non-blocking read path (used by HTTP + WS) ---------------------
    def cached(self, strategy: str) -> dict:
        """Return the last good scan for `strategy` INSTANTLY (never fetches).

        Cold cache → a valid empty/"scanning" payload. Raises ValueError for an
        unknown strategy so callers can 404.
        """
        if strategy not in strat_filters.strategy_ids():
            raise ValueError(f"unknown strategy: {strategy}")
        cached = self._cache.get(strategy)
        return cached[1] if cached else _empty_scan()

    def is_fresh(self, strategy: str) -> bool:
        """True if the cached scan is younger than CACHE_TTL_S."""
        cached = self._cache.get(strategy)
        return bool(cached and (time.monotonic() - cached[0]) < CACHE_TTL_S)

    @property
    def banned_until(self) -> float:
        """Monotonic deadline until which scanning is backed off (0 if clear)."""
        return self._banned_until

    @property
    def is_banned(self) -> bool:
        """True while inside the rate-limit back-off window."""
        return time.monotonic() < self._banned_until

    # --- Network write path (used by the background loop ONLY) ----------
    async def refresh(self, strategy: str, force: bool = False) -> dict:
        """Fetch a fresh scan for `strategy` and update the cache.

        Honors CACHE_TTL_S (no-op refresh returns the cache) unless `force`.
        On a ban/timeout, keeps the last good cache and returns it. Never
        raises into the caller for transient network problems.
        """
        if strategy not in strat_filters.strategy_ids():
            raise ValueError(f"unknown strategy: {strategy}")

        if self.is_banned and not force:
            return self.cached(strategy)
        if self.is_fresh(strategy) and not force:
            return self.cached(strategy)

        lock = self._locks.setdefault(strategy, asyncio.Lock())
        async with lock:
            if self.is_fresh(strategy) and not force:
                return self.cached(strategy)
            try:
                result = await self._do_scan(strategy)
            except KlineFetchError as exc:
                if exc.is_ban:
                    self._banned_until = time.monotonic() + BAN_BACKOFF_S
                    logger.warning(
                        "Scanner hit rate-limit ban during %s scan; backing off "
                        "%.0fs and serving last cache.", strategy, BAN_BACKOFF_S,
                    )
                else:
                    logger.warning(
                        "Scanner kline error during %s scan (%s); serving last cache.",
                        strategy, exc,
                    )
                return self.cached(strategy)
            except Exception as exc:  # never let a refresh crash the loop
                logger.error("Scanner refresh failed for %s: %s", strategy, exc)
                return self.cached(strategy)
            self._cache[strategy] = (time.monotonic(), result)
            return result

    async def _do_scan(self, strategy: str) -> dict:
        """Perform a fresh scan (no cache). May raise KlineFetchError."""
        timeframe = STRATEGY_TIMEFRAME.get(strategy, "15m")
        tickers = await self._top_universe()
        if not tickers:
            return _empty_scan()

        context_base = {"session_active": strat_filters.session_active()}
        rows: list[dict] = []

        # Bounded-concurrency kline fetches: cap simultaneous REST calls and add
        # a small pause between batches so we never trip the IP rate limit.
        semaphore = asyncio.Semaphore(KLINE_CONCURRENCY)

        async def fetch_one(ticker: dict) -> Optional[dict]:
            async with semaphore:
                return await self._scan_symbol(strategy, ticker, timeframe, context_base)

        for batch_start in range(0, len(tickers), KLINE_CONCURRENCY):
            batch = tickers[batch_start:batch_start + KLINE_CONCURRENCY]
            results = await asyncio.gather(*(fetch_one(t) for t in batch))
            rows.extend(r for r in results if r is not None)
            if batch_start + KLINE_CONCURRENCY < len(tickers):
                await asyncio.sleep(BATCH_PAUSE_S)

        # Rank: passed setups first, then by score desc.
        rows.sort(key=lambda r: (r["passed"], r["score"]), reverse=True)
        for idx, row in enumerate(rows, start=1):
            row["rank"] = idx

        return {
            "scanned_at": _utc_now_iso(),
            "universe_size": len(tickers),
            "results": rows,
        }

    async def _scan_symbol(
        self, strategy: str, ticker: dict, timeframe: str, context_base: dict
    ) -> Optional[dict]:
        """Compute indicators + filters for one symbol; return a result row.

        Re-raises a ban so the whole cycle aborts (one banned symbol means the
        IP is banned for all). A non-ban timeout/error for a single symbol just
        drops that symbol (returns None).
        """
        symbol = ticker["symbol"]
        try:
            df = await asyncio.wait_for(
                self._market_data.get_klines(symbol, interval=timeframe, limit=KLINE_LIMIT),
                timeout=KLINE_TIMEOUT_S,
            )
        except KlineFetchError as exc:
            if exc.is_ban:
                raise  # abort the entire cycle; loop will back off
            logger.debug("kline error for %s (skipping symbol): %s", symbol, exc)
            return None
        except asyncio.TimeoutError:
            logger.debug("kline fetch timed out for %s (skipping symbol).", symbol)
            return None

        indicators = compute_indicators(df)
        if indicators is None:
            return None

        # Volume z-score context from the kline window.
        vol_mean = float(df["volume"].mean()) if not df.empty else 0.0
        vol_std = float(df["volume"].std(ddof=0)) if not df.empty else 0.0

        eval_ctx = dict(context_base)
        result = strat_filters.evaluate(strategy, indicators, eval_ctx)

        return {
            "symbol": symbol,
            "rank": 0,  # assigned after sort
            "score": round(result.score, 4),
            "passed": result.passed,
            "filters": result.filters,
            "side": result.side,
            "win_prob": None,  # filled by the ML gate in the bot manager
            "last_price": round(indicators["price"], 8),
            "vol_24h": ticker["quote_volume"],
            "change_pct": ticker["change_pct"],
            # Internals reused downstream (features/indicators tabs); not part of
            # the documented payload but harmless extra keys.
            "_indicators": indicators,
            "_volume_mean": vol_mean,
            "_volume_std": vol_std,
            "_session_active": eval_ctx["session_active"],
        }

    async def _top_universe(self) -> list[dict]:
        """Top-N USDT perps by 24h quote volume; [] in safe mode.

        Raises KlineFetchError on a -1003 ban so refresh() backs off.
        """
        client = getattr(self._broker, "_client", None)
        if not self._broker.connected or client is None:
            return []
        try:
            raw = await asyncio.wait_for(
                client.futures_ticker(), timeout=KLINE_TIMEOUT_S
            )  # GET /fapi/v1/ticker/24hr
        except asyncio.TimeoutError:
            raise KlineFetchError("ticker24h timed out", is_ban=False)
        except Exception as exc:
            if _is_rate_limit_ban(exc):
                raise KlineFetchError(str(exc), is_ban=True)
            logger.error("ticker24h fetch failed: %s", exc)
            return []

        rows: list[dict] = []
        for t in raw or []:
            symbol = t.get("symbol", "")
            if not symbol.endswith(_QUOTE_SUFFIX):
                continue
            try:
                quote_volume = float(t.get("quoteVolume", 0) or 0)
                last_price = float(t.get("lastPrice", 0) or 0)
                change_pct = float(t.get("priceChangePercent", 0) or 0)
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "quote_volume": quote_volume,
                    "last_price": last_price,
                    "change_pct": change_pct,
                }
            )
        rows.sort(key=lambda r: r["quote_volume"], reverse=True)
        return rows[:UNIVERSE_SIZE]


def _is_rate_limit_ban(exc: Exception) -> bool:
    """True if `exc` is a Binance -1003 (too many requests / IP ban)."""
    code = getattr(exc, "code", None)
    if code == -1003:
        return True
    text = str(exc).lower()
    return "-1003" in text or "too many requests" in text or "banned until" in text
