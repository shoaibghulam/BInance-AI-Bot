"""Historical backfill trainer — bootstrap the model from months of real data.

Instead of waiting for 50+ live testnet trades per bot, this replays each
strategy over REAL historical klines (fetched from Binance's PUBLIC mainnet
kline endpoint — no auth, and the indicator features are symbol-agnostic so they
transfer to testnet trading), generates labeled (features -> win/loss) examples
by simulating each entry's SL/TP/horizon outcome NET of fees, and trains the
bot's WinProbModel on that large dataset.

Progress (symbols done, examples generated, %+duration, per-bot accuracy) is
exposed via a shared `BackfillProgress` so the dashboard can show a live bar.
It runs off the event loop (see trainer wiring) so it never stalls trading.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from app.config import settings
from app.ml.features import build_features
from app.ml.model import WinProbModel
from app.scanner import filters as strat_filters
from app.scanner import indicators as ind
from app.scanner.service import STRATEGY_TIMEFRAME

logger = logging.getLogger("trader.ml.backfill")

MAINNET_FAPI = "https://fapi.binance.com"
KLINES_PER_CALL = 1500
MAX_BARS_PER_SYMBOL = 8000     # bound work; ~83d @15m, ~333d @1h, ~5.5d @1m
DEFAULT_UNIVERSE = 12          # top-N liquid mainnet USDT perps
HORIZON_BARS = 48              # forward bars to resolve a simulated trade
_MIN_STOP_PCT, _MAX_STOP_PCT, _DEFAULT_STOP_PCT = 0.003, 0.10, 0.015
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
}


@dataclass
class BackfillProgress:
    """Live state of a running/last backfill job (surfaced to the dashboard)."""

    status: str = "idle"            # idle | running | done | error
    bots: list = field(default_factory=list)
    lookback_days: int = 0
    current_bot: Optional[str] = None
    current_symbol: Optional[str] = None
    symbols_total: int = 0
    symbols_done: int = 0
    examples: int = 0
    started_at: Optional[float] = None   # monotonic
    duration_s: float = 0.0
    results: dict = field(default_factory=dict)  # bot -> {samples, accuracy, auc}
    error: Optional[str] = None

    def snapshot(self) -> dict:
        pct = 0.0
        if self.symbols_total > 0:
            pct = round(100.0 * self.symbols_done / self.symbols_total, 1)
        return {
            "status": self.status,
            "bots": list(self.bots),
            "lookback_days": self.lookback_days,
            "current_bot": self.current_bot,
            "current_symbol": self.current_symbol,
            "symbols_total": self.symbols_total,
            "symbols_done": self.symbols_done,
            "progress_pct": pct,
            "examples": self.examples,
            "duration_s": round(self.duration_s, 1),
            "results": self.results,
            "error": self.error,
        }


def _sane_stop_pct(atr: float, price: float, stop_loss_atr: float) -> float:
    """ATR stop as a fraction of price, clamped to a sane band."""
    if not price or price <= 0:
        return _DEFAULT_STOP_PCT
    raw = (stop_loss_atr * atr) / price if atr and atr > 0 else _DEFAULT_STOP_PCT
    if raw != raw:  # NaN
        raw = _DEFAULT_STOP_PCT
    return max(_MIN_STOP_PCT, min(_MAX_STOP_PCT, raw))


def _indicator_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorized per-bar indicator columns (reuses scanner indicator math)."""
    out = pd.DataFrame(index=df.index)
    close = pd.to_numeric(df["close"], errors="coerce")
    out["price"] = close
    out["ema50"] = ind._ema(close, ind.EMA_FAST)
    out["ema200"] = ind._ema(close, ind.EMA_SLOW)
    rsi = ind._rsi(close, ind.RSI_PERIOD)
    out["rsi14"] = rsi
    out["rsi14_prev"] = rsi.shift(1)
    macd = ind._ema(close, ind.MACD_FAST) - ind._ema(close, ind.MACD_SLOW)
    hist = macd - ind._ema(macd, ind.MACD_SIGNAL)
    out["macd_hist"] = hist
    out["macd_hist_prev"] = hist.shift(1)
    sma = close.rolling(ind.BB_PERIOD).mean()
    std = close.rolling(ind.BB_PERIOD).std(ddof=0)
    width = (2 * ind.BB_STDDEV * std).replace(0.0, np.nan)
    out["bb_pctb"] = (close - (sma - ind.BB_STDDEV * std)) / width
    out["atr14"] = ind._atr(df, ind.ATR_PERIOD)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    out["vwap"] = (typical.rolling(20).mean())  # bar-local VWAP proxy
    out["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    out["vol_mean"] = out["volume"].rolling(20).mean()
    out["vol_std"] = out["volume"].rolling(20).std(ddof=0)
    return out


def _round_trip_fee_frac() -> float:
    """Round-trip cost as a fraction of notional (taker both legs + slippage)."""
    taker = settings.taker_fee_pct / 100.0
    slip = settings.slippage_pct / 100.0
    return 2.0 * (taker + slip)


def _simulate(highs, lows, closes, i: int, side: str,
              stop_pct: float, tp_pct: float, horizon: int) -> Optional[int]:
    """Return 1 (net win) / 0 (net loss) for a trade opened at bar i, or None."""
    entry = closes[i]
    if entry <= 0:
        return None
    fee = _round_trip_fee_frac()
    if side == "LONG":
        sl, tp = entry * (1 - stop_pct), entry * (1 + tp_pct)
    else:
        sl, tp = entry * (1 + stop_pct), entry * (1 - tp_pct)
    end = min(len(closes) - 1, i + horizon)
    for j in range(i + 1, end + 1):
        if side == "LONG":
            if lows[j] <= sl:
                ret = (sl - entry) / entry
                return 1 if ret - fee > 0 else 0
            if highs[j] >= tp:
                ret = (tp - entry) / entry
                return 1 if ret - fee > 0 else 0
        else:
            if highs[j] >= sl:
                ret = (entry - sl) / entry
                return 1 if ret - fee > 0 else 0
            if lows[j] <= tp:
                ret = (entry - tp) / entry
                return 1 if ret - fee > 0 else 0
    # Time-stop: outcome from the close at the horizon, net of fees.
    exit_price = closes[end]
    ret = ((exit_price - entry) / entry) if side == "LONG" else ((entry - exit_price) / entry)
    return 1 if ret - fee > 0 else 0


class HistoricalTrainer:
    """Backfills labeled examples from real history and trains each bot's model."""

    def __init__(self, bot_manager, db) -> None:
        self._m = bot_manager
        self._db = db
        self.progress = BackfillProgress()

    def is_running(self) -> bool:
        return self.progress.status == "running"

    async def run(self, bot_ids: list[str], lookback_days: int,
                  universe: int = DEFAULT_UNIVERSE) -> dict:
        """Fetch history, generate labels per strategy, train each model."""
        import httpx

        p = self.progress = BackfillProgress(
            status="running", bots=list(bot_ids), lookback_days=lookback_days,
            started_at=time.monotonic(),
        )
        try:
            async with httpx.AsyncClient(base_url=MAINNET_FAPI, timeout=20.0) as client:
                symbols = await self._universe(client, universe)
                if not symbols:
                    raise RuntimeError("could not fetch mainnet universe")
                # One shared kline cache per (symbol, interval) across bots.
                for bot_id in bot_ids:
                    await self._backfill_bot(client, bot_id, symbols, lookback_days, p)
            p.status = "done"
        except Exception as exc:  # never crash the app; report on the job
            logger.error("Backfill failed: %s", exc, exc_info=True)
            p.status = "error"
            p.error = str(exc)
        finally:
            if p.started_at is not None:
                p.duration_s = time.monotonic() - p.started_at
        return p.snapshot()

    async def _backfill_bot(self, client, bot_id, symbols, lookback_days, p) -> None:
        bot = self._m.get(bot_id)
        if bot is None:
            return
        cfg = bot.config
        interval = STRATEGY_TIMEFRAME.get(bot_id, "15m")
        stop_loss_atr = float(cfg.get("stop_loss_atr", 1.5) or 1.5)
        take_profit_r = float(cfg.get("take_profit_r", 1.5) or 1.5)
        p.current_bot = bot_id
        p.symbols_total = len(symbols)
        p.symbols_done = 0

        X: list[list[float]] = []
        y: list[int] = []
        for sym in symbols:
            p.current_symbol = sym
            try:
                df = await self._klines(client, sym, interval, lookback_days)
                if df is not None and len(df) > ind.MIN_BARS + HORIZON_BARS:
                    # Offload the CPU-heavy replay so the event loop (and live
                    # trading/exits) stays responsive during a long backfill.
                    import asyncio

                    await asyncio.to_thread(
                        self._label_symbol, df, bot_id, stop_loss_atr,
                        take_profit_r, X, y,
                    )
            except Exception as exc:  # skip a bad symbol, keep going
                logger.debug("backfill %s/%s skipped: %s", bot_id, sym, exc)
            p.symbols_done += 1
            p.examples = len(y)

        result = {"samples": len(y), "wins": int(sum(y)), "accuracy": None, "auc": None}
        # Train only with both classes and enough data.
        if len(y) >= 40 and len(set(y)) >= 2:
            import asyncio

            model = WinProbModel(bot_id=bot_id)
            metrics = await asyncio.to_thread(model.train, X, y)
            from app.ml.trainer import _model_path

            model.save(_model_path(bot_id))
            # register in the live trainer cache + persist metrics
            self._m._trainer._models[bot_id] = model
            self._m._trainer._last_trained_count[bot_id] = len(y)
            if self._db is not None:
                await self._db.record_model_metrics(
                    bot_id=bot_id, accuracy=metrics.get("accuracy"),
                    auc=metrics.get("auc"), n_samples=len(y),
                )
            result["accuracy"] = metrics.get("accuracy")
            result["auc"] = metrics.get("auc")
            result["n_parameters"] = metrics.get("n_parameters")
            logger.info("Backfill trained %s on %d historical examples (acc=%s).",
                        bot_id, len(y), metrics.get("accuracy"))
        else:
            result["skipped"] = "need >=40 examples with both win and loss"
        p.results[bot_id] = result

    def _label_symbol(self, df, bot_id, stop_loss_atr, take_profit_r, X, y) -> None:
        """Replay the strategy over one symbol's history, appending (X, y)."""
        ind_df = _indicator_frame(df)
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)
        times = df["open_time"].to_numpy() if "open_time" in df.columns else None
        n = len(df)
        i = ind.MIN_BARS
        while i < n - HORIZON_BARS:
            row = ind_df.iloc[i]
            if not np.isfinite(row.get("atr14", np.nan)) or row["price"] <= 0:
                i += 1
                continue
            indicators = {
                "price": float(row["price"]), "ema50": float(row["ema50"]),
                "ema200": float(row["ema200"]), "rsi14": float(row["rsi14"]),
                "rsi14_prev": float(row["rsi14_prev"]) if np.isfinite(row["rsi14_prev"]) else float(row["rsi14"]),
                "macd_hist": float(row["macd_hist"]),
                "macd_hist_prev": float(row["macd_hist_prev"]) if np.isfinite(row["macd_hist_prev"]) else 0.0,
                "bb_pctb": float(row["bb_pctb"]) if np.isfinite(row["bb_pctb"]) else 0.5,
                "atr14": float(row["atr14"]), "vwap": float(row["vwap"]) if np.isfinite(row["vwap"]) else float(row["price"]),
                "volume": float(row["volume"]),
            }
            when = None
            if times is not None:
                when = datetime.fromtimestamp(times[i] / 1000.0, tz=timezone.utc)
            ctx_session = strat_filters.session_active(when) if when else True
            res = strat_filters.evaluate(bot_id, indicators,
                                         {"session_active": ctx_session})
            if not res.passed:
                i += 1
                continue
            stop_pct = _sane_stop_pct(indicators["atr14"], indicators["price"], stop_loss_atr)
            tp_pct = take_profit_r * stop_pct
            label = _simulate(highs, lows, closes, i, res.side, stop_pct, tp_pct, HORIZON_BARS)
            if label is None:
                i += 1
                continue
            feats = build_features(indicators, {
                "side": res.side, "session_active": ctx_session,
                "volume_mean": float(row["vol_mean"]) if np.isfinite(row["vol_mean"]) else 0.0,
                "volume_std": float(row["vol_std"]) if np.isfinite(row["vol_std"]) else 0.0,
            })
            from app.ml.features import to_vector

            X.append(to_vector(feats))
            y.append(int(label))
            # jump past the trade horizon so examples don't overlap/correlate.
            i += HORIZON_BARS

    async def _universe(self, client, n: int) -> list[str]:
        """Top-N mainnet USDT perps by 24h quote volume (public endpoint)."""
        r = await client.get("/fapi/v1/ticker/24hr")
        r.raise_for_status()
        rows = [
            (t.get("symbol", ""), float(t.get("quoteVolume", 0) or 0))
            for t in r.json()
            if t.get("symbol", "").endswith("USDT")
        ]
        rows.sort(key=lambda kv: kv[1], reverse=True)
        return [s for s, _ in rows[:n]]

    async def _klines(self, client, symbol, interval, lookback_days) -> Optional[pd.DataFrame]:
        """Fetch up to MAX_BARS_PER_SYMBOL recent klines over the lookback."""
        step = _INTERVAL_MS.get(interval, 900_000)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = now_ms - lookback_days * 86_400_000
        rows: list = []
        cursor = start_ms
        while cursor < now_ms and len(rows) < MAX_BARS_PER_SYMBOL:
            r = await client.get("/fapi/v1/klines", params={
                "symbol": symbol, "interval": interval,
                "startTime": cursor, "limit": KLINES_PER_CALL,
            })
            if r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
            rows.extend(batch)
            cursor = batch[-1][0] + step
            if len(batch) < KLINES_PER_CALL:
                break
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "qav", "trades", "tbav", "tbqv", "ignore",
        ])
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna(subset=["close"]).reset_index(drop=True)
