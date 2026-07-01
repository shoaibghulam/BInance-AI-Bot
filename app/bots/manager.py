"""Bot registry + lifecycle (the five v2 bots).

Bot ids are the strategy names: day-trading, scalping, grid, dca, rebalancing.
Each bot carries v2 config (timeframe, stop_loss_atr, take_profit_r, indicators,
min_win_prob, leverage, margin_type) and live performance stats (trades_total,
wins/losses, win_rate, realized/unrealized pnl, max_drawdown, open_positions,
equity_curve).

Per-bot tick:
  scanner.cached(strategy) -> top candidates -> build_features -> ML gate
  (win_prob >= min_win_prob; if model warming up, pure indicator rules) ->
  risk.size_position -> (testnet) place order via broker.

Order placement stays behind the existing safe checks. In safe mode (no broker)
the full path is exercised and stats updated, but no live order is sent. On a
simulated/real close the trade + features + outcome are recorded so the trainer
can retrain. The kill switch halts every loop while active.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.broker.client import BrokerClient
from app.market_data.service import MarketDataService
from app.ml.features import build_features
from app.ml.trainer import ModelTrainer
from app.persistence.db import Database
from app.risk.costs import net_pnl
from app.risk.engine import KillSwitch, RiskEngine
from app.scanner import filters as strat_filters
from app.scanner.service import MarketScanner
from app.signals.base import Action, MarketData, Signal

logger = logging.getLogger("trader.bots")

_LOOP_INTERVAL_S = 5.0
_LOOP_ERROR_COOLDOWN_S = 10.0  # self-heal delay after an unhandled loop error
_VALID_STATUSES = {"running", "stopped", "error"}
_TOP_CANDIDATES = 5  # how many ranked survivors the tick considers

# Bot type (contract `type`) per strategy id.
_BOT_TYPE = {
    "day-trading": "day_trading",
    "scalping": "scalping",
    "grid": "grid",
    "dca": "dca",
    "rebalancing": "rebalancing",
}

# Default per-bot cap on simultaneously-open positions. 1 each * 5 bots = 5,
# matching the global MAX_CONCURRENT_POSITIONS. Prevents pyramiding.
_DEFAULT_MAX_OPEN_POSITIONS = 1
# Time-stops (minutes) for short-horizon strategies; None = no time-stop.
_TIME_STOP_MINUTES = {"day-trading": 240, "scalping": 5}

# SL sanity band as a FRACTION of entry price. An ATR-derived stop is clamped
# into [MIN, MAX]; a missing/zero/NaN/absurd ATR falls back to DEFAULT.
_MIN_STOP_PCT = 0.003   # 0.3% — floor (prevents fee-eaten micro-stops)
_MAX_STOP_PCT = 0.10    # 10%  — ceiling (prevents the −73% INUSDT blowup)
_DEFAULT_STOP_PCT = 0.015  # 1.5% fallback when ATR is unusable

# Strategy-specific tunable keys (validated in update_config).
_DCA_NUMERIC = (
    "base_order_pct", "safety_order_count", "safety_order_deviation_pct",
    "safety_order_step_scale", "safety_order_volume_scale", "target_profit_pct",
    "max_deviation_pct",
)
_GRID_NUMERIC = ("grid_levels", "grid_span_atr", "grid_stop_buffer_pct")
_REBAL_NUMERIC = (
    "basket_size", "target_exposure_pct", "rebalance_band_pct",
    "rebalance_interval_s", "min_trade_notional_pct",
)

# Per-bot starting config defaults (tunable via POST /api/bots/{id}/config).
_DEFAULT_CONFIG = {
    "day-trading": {
        "timeframe": "15m", "stop_loss_atr": 1.5, "take_profit_r": 2.0,
        "max_open_positions": _DEFAULT_MAX_OPEN_POSITIONS,
        "indicators": ["EMA50/200", "RSI14", "MACD", "VWAP", "ATR14"],
    },
    "scalping": {
        # TP:SL was 1.0:1.0 — after ~0.08% round-trip fees on a 0.3% stop that
        # needs a 63-70% win rate just to break even. Asymmetric 1.8R TP drops
        # break-even to ~40%, which is reachable for a mean-reversion scalper.
        "timeframe": "1m", "stop_loss_atr": 1.0, "take_profit_r": 1.8,
        "max_open_positions": _DEFAULT_MAX_OPEN_POSITIONS,
        "indicators": ["Bollinger", "RSI14", "VWAP", "MACD", "ATR14"],
    },
    "grid": {
        "timeframe": "15m", "stop_loss_atr": 2.0, "take_profit_r": 1.0,
        "max_open_positions": _DEFAULT_MAX_OPEN_POSITIONS,
        "indicators": ["EMA50/200", "Bollinger", "ATR14"],
        # Real grid mechanics (see app/bots/grid.py). grid_levels is the MAX;
        # the build uses as many levels as profitably fit the band.
        "grid_levels": 10, "grid_span_atr": 2.0, "grid_mode": "neutral",
        "grid_stop_buffer_pct": 1.0,
    },
    "dca": {
        "timeframe": "1h", "stop_loss_atr": 3.0, "take_profit_r": 1.0,
        "max_open_positions": _DEFAULT_MAX_OPEN_POSITIONS,
        "indicators": ["RSI14", "Bollinger", "VWAP"],
        # Safety-order / averaging mechanics (see app/bots/dca.py).
        "base_order_pct": 0.25, "safety_order_count": 4,
        "safety_order_deviation_pct": 1.0, "safety_order_step_scale": 1.5,
        "safety_order_volume_scale": 1.5, "target_profit_pct": 1.0,
        "max_deviation_pct": 12.0,
    },
    "rebalancing": {
        "timeframe": "4h", "stop_loss_atr": 2.0, "take_profit_r": 1.5,
        "max_open_positions": _DEFAULT_MAX_OPEN_POSITIONS,
        "indicators": ["EMA50/200"],
        # Real basket mechanics (see app/bots/rebalance.py).
        "basket_size": 4, "target_exposure_pct": 40.0,
        "rebalance_band_pct": 25.0, "rebalance_interval_s": 300.0,
        "min_trade_notional_pct": 0.5,
    },
}


def _utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class BotStats:
    """Mutable per-bot performance accumulator."""

    trades_total: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    open_positions: int = 0
    peak_equity: float = 0.0
    max_drawdown_pct: float = 0.0
    equity_curve: list = field(default_factory=list)  # [[iso_ts, equity], ...]

    @property
    def win_rate(self) -> float:
        """Wins / total closed trades (0.0 when none closed)."""
        closed = self.wins + self.losses
        return round(self.wins / closed, 4) if closed else 0.0


class Bot:
    """A single strategy bot: config, status, live stats, and loop task."""

    def __init__(self, bot_id: str, leverage: int, margin_type: str,
                 min_win_prob: float) -> None:
        self.id = bot_id
        self.type = _BOT_TYPE[bot_id]
        self.leverage = leverage
        self.margin_type = margin_type
        self.min_win_prob = min_win_prob
        self.status = "stopped"
        self.enabled = False
        self.last_signal = "HOLD"
        self.trades_today = 0
        self.updated_at = _utc_now_iso()
        self.config = dict(_DEFAULT_CONFIG[bot_id])
        self.stats = BotStats()
        # Map of open trade row id -> snapshot used to close it later.
        self.open_trades: dict[int, dict] = {}
        self._task: Optional[asyncio.Task] = None

    def touch(self) -> None:
        """Refresh `updated_at`."""
        self.updated_at = _utc_now_iso()

    def held_symbols(self) -> set[str]:
        """Symbols this bot currently has an open trade in (dedup guard)."""
        return {t["symbol"] for t in self.open_trades.values()}

    @property
    def max_open_positions(self) -> int:
        """Per-bot cap on simultaneously-open positions."""
        return int(self.config.get("max_open_positions", _DEFAULT_MAX_OPEN_POSITIONS))

    def summary_dict(self) -> dict:
        """`GET /api/bots` summary row (Overview tab)."""
        return {
            "id": self.id,
            "type": self.type,
            "symbol": "top30_volume",
            "status": self.status,
            "enabled": self.enabled,
            "leverage": self.leverage,
            "pnl": round(self.stats.realized_pnl + self.stats.unrealized_pnl, 8),
            "trades_today": self.trades_today,
            "last_signal": self.last_signal,
            "updated_at": self.updated_at,
        }

    def _config_payload(self) -> dict:
        """Config block for the detail payload, incl. strategy-specific keys."""
        cfg = {
            "timeframe": self.config["timeframe"],
            "stop_loss_atr": self.config["stop_loss_atr"],
            "take_profit_r": self.config["take_profit_r"],
            "max_open_positions": self.config.get(
                "max_open_positions", _DEFAULT_MAX_OPEN_POSITIONS
            ),
            "indicators": list(self.config["indicators"]),
            "min_win_prob": self.min_win_prob,
        }
        # Surface the strategy-specific tunables for grid / dca / rebalancing.
        if self.id == "dca":
            extra_keys = _DCA_NUMERIC
        elif self.id == "grid":
            extra_keys = (*_GRID_NUMERIC, "grid_mode")
        elif self.id == "rebalancing":
            extra_keys = _REBAL_NUMERIC
        else:
            extra_keys = ()
        for key in extra_keys:
            if key in self.config:
                cfg[key] = self.config[key]
        return cfg

    def detail_dict(self) -> dict:
        """`GET /api/bots/{id}` full detail."""
        s = self.stats
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "enabled": self.enabled,
            "leverage": self.leverage,
            "margin_type": self.margin_type,
            "universe": "top30_volume",
            "config": self._config_payload(),
            "performance": {
                "trades_total": s.trades_total,
                "wins": s.wins,
                "losses": s.losses,
                "win_rate": s.win_rate,
                "realized_pnl": round(s.realized_pnl, 8),
                "unrealized_pnl": round(s.unrealized_pnl, 8),
                "max_drawdown_pct": round(s.max_drawdown_pct, 4),
                "open_positions": s.open_positions,
                "equity_curve": s.equity_curve[-200:],
            },
            "updated_at": self.updated_at,
        }


class BotManager:
    """Owns the five bots, their loops, and the shared trading dependencies."""

    def __init__(
        self,
        broker: BrokerClient,
        market_data: MarketDataService,
        risk: RiskEngine,
        engine,
        scanner: Optional[MarketScanner] = None,
        trainer: Optional[ModelTrainer] = None,
        db: Optional[Database] = None,
    ) -> None:
        self._broker = broker
        self._market_data = market_data
        self._risk = risk
        self._engine = engine
        self._scanner = scanner or MarketScanner(broker, market_data)
        self._db = db
        if trainer is not None:
            self._trainer = trainer
        elif db is not None:
            self._trainer = ModelTrainer(db)
        else:
            self._trainer = None
        self._kill_switch: Optional[KillSwitch] = None
        self._bots: dict[str, Bot] = {}
        self._register_default_bots()
        # Strategy managers for the multi-order bots (lazy import avoids cycle).
        from app.bots.dca import DcaManager
        from app.bots.grid import GridManager
        from app.bots.rebalance import RebalanceManager

        self._dca = DcaManager(self)
        self._grid = GridManager(self)
        self._rebalance = RebalanceManager(self)

    # --- Wiring ----------------------------------------------------------
    def attach_kill_switch(self, kill_switch: KillSwitch) -> None:
        """Wire the kill switch so loops halt while it is active."""
        self._kill_switch = kill_switch

    @property
    def scanner(self) -> MarketScanner:
        """Shared market scanner (for the API layer)."""
        return self._scanner

    @property
    def trainer(self) -> Optional[ModelTrainer]:
        """Shared ML trainer (for the API layer)."""
        return self._trainer

    def _register_default_bots(self) -> None:
        """Create the five canonical strategy bots."""
        leverage = self._risk._capped_leverage()
        margin = self._risk._s.default_margin_type
        min_wp = self._risk._s.min_signal_confidence
        for bot_id in strat_filters.strategy_ids():
            self._bots[bot_id] = Bot(
                bot_id=bot_id,
                leverage=leverage,
                margin_type=margin,
                min_win_prob=min_wp,
            )

    # --- Public API ------------------------------------------------------
    def list(self) -> list[dict]:
        """Return all bots as summary rows for `GET /api/bots`."""
        return [bot.summary_dict() for bot in self._bots.values()]

    def get(self, bot_id: str) -> Optional[Bot]:
        """Return a bot by id, or None."""
        return self._bots.get(bot_id)

    def detail(self, bot_id: str) -> Optional[dict]:
        """Return a bot's full detail dict (with strategy_state for grid/dca)."""
        bot = self._bots.get(bot_id)
        if bot is None:
            return None
        detail = bot.detail_dict()
        if bot_id == "dca":
            detail["strategy_state"] = self._dca.strategy_state()
        elif bot_id == "grid":
            detail["strategy_state"] = self._grid.strategy_state()
        elif bot_id == "rebalancing":
            detail["strategy_state"] = self._rebalance.strategy_state()
        return detail

    def update_config(self, bot_id: str, payload: dict) -> Optional[dict]:
        """Apply a tunable-config update within global caps; return detail.

        Honors global MAX_LEVERAGE; never lets a bot exceed risk caps.
        """
        bot = self._bots.get(bot_id)
        if bot is None:
            return None
        if "timeframe" in payload and payload["timeframe"]:
            bot.config["timeframe"] = str(payload["timeframe"])
        if "stop_loss_atr" in payload and payload["stop_loss_atr"] is not None:
            bot.config["stop_loss_atr"] = max(0.1, float(payload["stop_loss_atr"]))
        if "take_profit_r" in payload and payload["take_profit_r"] is not None:
            bot.config["take_profit_r"] = max(0.1, float(payload["take_profit_r"]))
        if ("max_open_positions" in payload
                and payload["max_open_positions"] is not None):
            # Clamp to the global concurrent-position cap.
            cap = int(self._risk._s.max_concurrent_positions)
            bot.config["max_open_positions"] = max(
                1, min(int(payload["max_open_positions"]), cap)
            )
        if "min_win_prob" in payload and payload["min_win_prob"] is not None:
            bot.min_win_prob = max(0.0, min(1.0, float(payload["min_win_prob"])))
        if "leverage" in payload and payload["leverage"] is not None:
            requested = int(payload["leverage"])
            cap = self._risk._capped_leverage()
            bot.leverage = max(1, min(requested, cap))
        if "margin_type" in payload and payload["margin_type"]:
            mt = str(payload["margin_type"]).upper()
            if mt in ("ISOLATED", "CROSSED"):
                bot.margin_type = mt

        # Strategy-specific tunables for grid / dca / rebalancing (positive nums).
        if bot_id == "grid":
            strat_keys = _GRID_NUMERIC
        elif bot_id == "dca":
            strat_keys = _DCA_NUMERIC
        elif bot_id == "rebalancing":
            strat_keys = _REBAL_NUMERIC
        else:
            strat_keys = ()
        for key in strat_keys:
            if key in payload and payload[key] is not None:
                bot.config[key] = self._coerce_positive(key, payload[key])
        if bot_id == "grid" and payload.get("grid_mode") in ("neutral", "long", "short"):
            bot.config["grid_mode"] = payload["grid_mode"]

        bot.touch()
        return self.detail(bot_id)

    @staticmethod
    def _coerce_positive(key: str, value):
        """Coerce a tunable to a sane positive int (counts) or float."""
        if key in ("safety_order_count", "grid_levels", "basket_size"):
            return max(1, int(value))
        return max(0.0, float(value))

    async def start(self, bot_id: str) -> Optional[dict]:
        """Start a bot's loop. Returns its summary dict, or None if unknown."""
        bot = self._bots.get(bot_id)
        if bot is None:
            return None
        if self._kill_switch is not None and self._kill_switch.active:
            bot.status = "error"
            bot.touch()
            logger.warning("Refusing to start %s: kill switch active.", bot_id)
            return bot.summary_dict()
        if bot.status != "running":
            bot.status = "running"
            bot.enabled = True
            bot.touch()
            bot._task = asyncio.create_task(self._run_loop(bot))
            logger.info("Bot %s started.", bot_id)
        return bot.summary_dict()

    async def stop(self, bot_id: str) -> Optional[dict]:
        """Stop a bot's loop. Returns its summary dict, or None if unknown."""
        bot = self._bots.get(bot_id)
        if bot is None:
            return None
        await self._cancel_task(bot)
        bot.status = "stopped"
        bot.enabled = False
        bot.touch()
        logger.info("Bot %s stopped.", bot_id)
        return bot.summary_dict()

    async def halt_all(self) -> int:
        """Stop every running bot (used by the kill switch). Returns count."""
        count = 0
        for bot in self._bots.values():
            if bot.status == "running":
                await self._cancel_task(bot)
                count += 1
            bot.status = "stopped"
            bot.enabled = False
            bot.touch()
        # Clear multi-order strategy state: the kill switch flattens positions +
        # cancels orders on the exchange, so the in-memory grid/deals/basket are
        # now stale — dropping them prevents managing a phantom after restart.
        self._grid.reset()
        self._dca.reset()
        self._rebalance.reset()
        for b in self._bots.values():
            b.open_trades.clear()
            b.stats.open_positions = 0
        logger.warning("Halted %d bots; cleared strategy state.", count)
        return count

    async def shutdown(self) -> None:
        """Cancel all loops on app shutdown."""
        for bot in self._bots.values():
            await self._cancel_task(bot)

    # --- Startup reconciliation ------------------------------------------
    async def reconcile_on_startup(self) -> dict:
        """Make the exchange and our state consistent after a (re)start.

        Single-entry bots (day-trading/scalping) whose open DB row has a
        persisted stop AND a matching live position are REHYDRATED and the bot
        auto-started, so their SL/TP keep firing across a restart. Anything that
        cannot be safely resumed — multi-order bots (grid/dca/rebalancing) whose
        in-memory ladder/basket is gone, rows with no live position, or live
        positions with no DB row — is flattened (reduceOnly) and its row closed.
        Never leaves an unmanaged position behind.
        """
        summary = {"rehydrated": 0, "flattened": 0, "closed_rows": 0}
        if not self._broker.connected or self._db is None:
            return summary
        try:
            positions = await self._broker.get_positions()
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Startup reconcile: get_positions failed: %s", exc)
            return summary

        live = {
            p.get("symbol"): p for p in positions
            if float(p.get("positionAmt", 0) or 0) != 0.0
        }
        open_rows = await self._db.open_bot_trades()
        import time as _time

        rows_symbols: set[str] = set()   # symbols that have any open DB row
        resumed_symbols: set[str] = set()  # symbols rehydrated (keep their pos)
        flattened: set[str] = set()      # symbols already flattened (dedupe)
        resumed_bots: set[str] = set()
        marks: dict[str, float] = {}     # per-symbol mark cache (1 fetch/symbol)
        single_entry = {"day-trading", "scalping"}
        for row in open_rows:
            bot_id, sym = row.get("bot_id"), row.get("symbol")
            rows_symbols.add(sym)
            bot = self._bots.get(bot_id)
            pos = live.get(sym)
            can_resume = (
                bot is not None and bot_id in single_entry
                and pos is not None and row.get("stop_price") is not None
                and sym not in resumed_symbols  # one row per symbol resumes
            )
            if can_resume:
                amt = abs(float(pos.get("positionAmt", 0) or 0))
                entry = float(pos.get("entryPrice", 0) or 0) or float(
                    row.get("entry_price") or 0)
                tp = row.get("take_profit_price")
                bot.open_trades[int(row["id"])] = {
                    "symbol": sym, "side": row["side"], "entry_price": entry,
                    "qty": amt, "stop_price": float(row["stop_price"]),
                    "take_profit_price": float(tp) if tp is not None else None,
                    "opened_at_monotonic": _time.monotonic(),
                }
                bot.stats.open_positions = len(bot.open_trades)
                resumed_symbols.add(sym)
                resumed_bots.add(bot_id)
                summary["rehydrated"] += 1
                logger.critical(
                    "Reconcile: REHYDRATED %s %s (trade %s) — SL/TP resumed.",
                    bot_id, sym, row["id"])
            else:
                # Flatten the live position ONCE per symbol (stale duplicate rows
                # for the same symbol must not re-fire reduceOnly → -2022 spam).
                if (pos is not None and sym not in flattened
                        and sym not in resumed_symbols):
                    await self._flatten_symbol(
                        sym, float(pos.get("positionAmt", 0) or 0))
                    flattened.add(sym)
                    summary["flattened"] += 1
                if sym not in marks:
                    marks[sym] = await self._fetch_mark(sym) or 0.0
                mark = marks[sym] or float(row.get("entry_price") or 0)
                await self._close_reconcile_row(bot_id, row, mark)
                summary["closed_rows"] += 1

        # Live positions with NO open DB row at all → orphans → flatten once.
        for sym, pos in live.items():
            if sym not in rows_symbols and sym not in flattened:
                await self._flatten_symbol(
                    sym, float(pos.get("positionAmt", 0) or 0))
                flattened.add(sym)
                summary["flattened"] += 1
                logger.critical(
                    "Reconcile: flattened ORPHAN %s (no DB record).", sym)

        # Auto-start bots that resumed a position so exits are actively managed.
        for bot_id in resumed_bots:
            await self.start(bot_id)
        logger.info("Startup reconcile complete: %s", summary)
        return summary

    async def _flatten_symbol(self, symbol: str, amt: float) -> None:
        """Cancel orders + reduceOnly MARKET close of one symbol's position."""
        if not symbol or amt == 0.0:
            return
        try:
            await self._broker.cancel_all(symbol)
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("reconcile cancel_all %s: %s", symbol, exc)
        side = "SELL" if amt > 0 else "BUY"
        res = await self._broker.place_order(
            symbol=symbol, side=side, order_type="MARKET",
            quantity=str(abs(amt)), reduce_only=True,
        )
        if not isinstance(res, dict) or not res.get("ok"):
            logger.error("Reconcile: flatten %s FAILED (%s).", symbol,
                         res.get("reason") if isinstance(res, dict) else res)

    async def _close_reconcile_row(self, bot_id, row: dict, exit_price: float) -> None:
        """Mark a stale open row closed (NET pnl) and fold it into bot stats."""
        entry = float(row.get("entry_price") or 0)
        qty = float(row.get("qty") or 0)
        if entry > 0 and qty > 0 and exit_price > 0:
            direction = 1.0 if row.get("side") == "LONG" else -1.0
            gross = (float(exit_price) - entry) * qty * direction
            pnl, _cost = net_pnl(gross, entry, float(exit_price), qty)
        else:
            pnl = 0.0
        outcome = "win" if pnl >= 0 else "loss"
        await self._db.close_bot_trade(
            trade_id=int(row["id"]), exit_price=float(exit_price or entry),
            pnl=pnl, outcome=outcome, reason="reconcile_restart",
        )
        bot = self._bots.get(bot_id)
        if bot is not None:
            self._apply_completed_trade(bot, pnl)

    # --- Internal --------------------------------------------------------
    async def _cancel_task(self, bot: Bot) -> None:
        """Cancel and await a bot's running task, if any."""
        task = bot._task
        bot._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Bot %s task ended with: %s", bot.id, exc)

    async def _run_loop(self, bot: Bot) -> None:
        """Bot loop: scan → ML gate → risk size → (testnet) place; never crashes."""
        try:
            while True:
                if self._kill_switch is not None and self._kill_switch.active:
                    bot.status = "stopped"
                    bot.enabled = False
                    bot.touch()
                    return
                await self._tick(bot)
                await asyncio.sleep(_LOOP_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let a bot crash the app
            # A bot managing open positions must NOT die permanently on a
            # transient error (DB hiccup, network blip) — that would leave its
            # positions unmanaged with no SL/TP firing. Log loudly and SELF-HEAL
            # after a short cooldown so exit management resumes.
            logger.error("Bot %s loop error: %s — self-healing in %.0fs.",
                         bot.id, exc, _LOOP_ERROR_COOLDOWN_S, exc_info=True)
            bot.status = "error"
            bot.touch()
            try:
                await asyncio.sleep(_LOOP_ERROR_COOLDOWN_S)
            except asyncio.CancelledError:
                raise
            bot.status = "running"
            bot.touch()
            bot._task = asyncio.create_task(self._run_loop(bot))

    async def _tick(self, bot: Bot) -> None:
        """One bot tick. Single-entry bots use the standard path; grid/dca have
        their own multi-order strategy managers.

        All bots first read the cached scan (never a network fetch). grid/dca
        keep their own held-symbol/position bookkeeping inside their managers.
        """
        scan = self._scanner.cached(bot.id)
        results = scan.get("results", [])

        # Grid and DCA run dedicated multi-order mechanics.
        if bot.id == "dca":
            candidates = self._gated_candidates(bot, results, self._dca.held_symbols())
            await self._dca.tick(bot, candidates)
            return
        if bot.id == "grid":
            candidates = self._gated_candidates(bot, results, self._grid.held_symbols())
            await self._grid.tick(bot, candidates)
            return
        if bot.id == "rebalancing":
            # Basket selection must see ALL passed candidates (incl. current
            # holdings), so don't exclude held symbols here.
            candidates = self._gated_candidates(bot, results, set())
            await self._rebalance.tick(bot, candidates)
            return

        # --- Single-entry bots (day-trading / scalping / rebalancing) -------
        # 0. RECONCILE phantom internal opens against live exchange positions.
        await self._reconcile(bot)
        # 1. EXIT MANAGEMENT against a FRESH per-position mark.
        await self._manage_exits(bot)
        # 2. ENTRY — respect the per-bot open-position cap.
        if len(bot.open_trades) >= bot.max_open_positions:
            bot.last_signal = "HOLD"
            bot.touch()
            return

        candidates = self._gated_candidates(bot, results, bot.held_symbols())
        if not candidates:
            bot.last_signal = "HOLD"
            bot.touch()
            return

        # "Best coin" = highest score (scanner already ranks passed-first/score).
        best = max(candidates, key=lambda r: (r.get("win_prob") or 0.0, r.get("score", 0.0)))
        decision = self._gate(bot, best)
        placed = False
        if decision is not None:
            placed = await self._attempt_entry(bot, best, decision)

        bot.last_signal = "BUY" if placed else "HOLD"
        bot.touch()

    def _gated_candidates(self, bot: Bot, results: list[dict],
                          held: set[str]) -> list[dict]:
        """PASSED, not-held candidates with ML features attached + win_prob set.

        Applies the ML gate (win_prob >= min_win_prob when a model is trained;
        otherwise the pure indicator filter that already passed). Attaches
        `_features` so the strategy managers can persist them for the trainer.
        """
        out: list[dict] = []
        for row in results[:_TOP_CANDIDATES]:
            if not row.get("passed") or row.get("symbol") in held:
                continue
            decision = self._gate(bot, row)
            if decision is None:
                continue
            row["_features"] = decision["features"]
            out.append(row)
        return out

    async def _reconcile(self, bot: Bot) -> None:
        """Drop internal opens with no matching live exchange position.

        Only acts when the broker is connected (safe mode keeps simulated
        opens). An order Binance rejected leaves a phantom internal open; this
        removes it so the bot's open count tracks reality and no phantom exit
        fires. The phantom row is NOT recorded as a closed trade (it never
        opened on the exchange) — it is simply discarded.
        """
        if not self._broker.connected or not bot.open_trades:
            return
        try:
            positions = await self._broker.get_positions()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("reconcile get_positions failed for %s: %s", bot.id, exc)
            return
        live_symbols = {
            p.get("symbol") for p in positions
            if float(p.get("positionAmt", 0) or 0) != 0.0
        }
        for trade_id, snap in list(bot.open_trades.items()):
            if snap["symbol"] not in live_symbols:
                bot.open_trades.pop(trade_id, None)
                logger.warning(
                    "Bot %s reconcile: dropped phantom open %s (trade %s) — "
                    "no live exchange position.", bot.id, snap["symbol"], trade_id,
                )
        bot.stats.open_positions = len(bot.open_trades)

    async def _manage_exits(self, bot: Bot) -> None:
        """Close any open trade that hit its SL, TP, or time-stop.

        Checks against a FRESH per-position mark (light last-close fetch), not
        the up-to-30s-stale scan cache, so exits fire on current price.
        """
        # Iterate over a snapshot — close_trade mutates bot.open_trades.
        for trade_id, snap in list(bot.open_trades.items()):
            mark = await self._fetch_mark(snap["symbol"])
            if mark is None or mark <= 0:
                # SL/TP cannot be evaluated this tick — the position is
                # momentarily UNPROTECTED (e.g. rate-limit ban blanks the mark).
                # Warn loudly (not silent) so a persistent blackout is visible.
                logger.warning(
                    "Bot %s: no mark for OPEN %s — SL/TP unchecked this tick "
                    "(position unprotected until price returns).",
                    bot.id, snap["symbol"],
                )
                continue
            reason = self._exit_reason(bot, snap, mark)
            if reason is None:
                continue
            if self._broker.connected:
                # reduceOnly MARKET close of this exact position. CHECK the
                # result: if Binance rejects the close, do NOT mark the trade
                # closed — leave it open and retry next tick. Recording a close
                # while the exchange position is still live orphans it (no SL/TP).
                close_side = "SELL" if snap["side"] == "LONG" else "BUY"
                res = await self._broker.place_order(
                    symbol=snap["symbol"],
                    side=close_side,
                    order_type="MARKET",
                    quantity=str(snap["qty"]),
                    reduce_only=True,
                )
                if not isinstance(res, dict) or not res.get("ok"):
                    reason_txt = res.get("reason") if isinstance(res, dict) else res
                    logger.error(
                        "Bot %s exit close REJECTED for %s (%s) — trade kept "
                        "open, retrying next tick.", bot.id, snap["symbol"], reason_txt,
                    )
                    continue
            await self.close_trade(bot.id, trade_id, exit_price=mark, reason=reason)

    @staticmethod
    def _exit_reason(bot: Bot, snap: dict, mark: float) -> Optional[str]:
        """Return 'stop_loss' | 'take_profit' | 'time_stop', or None to hold."""
        entry = snap["entry_price"]
        sl = snap.get("stop_price")
        tp = snap.get("take_profit_price")
        is_long = snap["side"] == "LONG"

        if sl is not None:
            if (is_long and mark <= sl) or (not is_long and mark >= sl):
                return "stop_loss"
        if tp is not None:
            if (is_long and mark >= tp) or (not is_long and mark <= tp):
                return "take_profit"

        time_stop_min = _TIME_STOP_MINUTES.get(bot.id)
        opened = snap.get("opened_at_monotonic")
        if time_stop_min and opened is not None:
            import time as _time

            if (_time.monotonic() - opened) >= time_stop_min * 60:
                return "time_stop"
        return None

    async def _fetch_mark(self, symbol: str) -> Optional[float]:
        """Light mark-price fetch fallback (last close), or None on failure."""
        try:
            df = await self._market_data.get_klines(
                symbol, interval="1m", limit=2, raise_on_ban=False
            )
            if df is not None and not df.empty:
                return float(df["close"].iloc[-1])
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("mark fetch failed for %s: %s", symbol, exc)
        return None

    def _gate(self, bot: Bot, row: dict) -> Optional[dict]:
        """Apply the ML gate. Returns a decision dict or None to skip.

        Trained model: require win_prob >= bot.min_win_prob.
        Warming up: fall back to the pure indicator rule (filter already passed).
        """
        indicators = row.get("_indicators") or {}
        ctx = {
            "side": row.get("side", "LONG"),
            "session_active": row.get("_session_active", False),
            "volume_mean": row.get("_volume_mean", 0.0),
            "volume_std": row.get("_volume_std", 0.0),
        }
        features = build_features(indicators, ctx)

        win_prob = None
        if self._trainer is not None:
            win_prob = self._trainer.predict(bot.id, features)

        if win_prob is not None:
            row["win_prob"] = round(win_prob, 4)
            if win_prob < bot.min_win_prob:
                return None
        # else: warming up — pure indicator rule already passed in the scanner.

        return {"features": features, "win_prob": win_prob, "side": ctx["side"]}

    async def _attempt_entry(self, bot: Bot, row: dict, decision: dict) -> bool:
        """Size via the risk engine and place the entry. Returns True if opened."""
        symbol = row["symbol"]
        side = decision["side"]
        action = Action.BUY if side == "LONG" else Action.SELL
        signal = Signal(
            action=action,
            confidence=max(bot.min_win_prob, float(row.get("score", 0.0))),
            size_hint=float(row.get("score", 0.5)),
            reason=f"{bot.id} scanner rank {row.get('rank')}",
            source=f"scanner:{bot.id}",
        )

        df = await self._market_data.get_klines(
            symbol, interval=bot.config["timeframe"], limit=50, raise_on_ban=False
        )
        market = MarketData(symbol=symbol, ohlcv=df, indicators=row.get("_indicators", {}),
                            extra=None)
        account = await self._account()
        filters = await self._broker.get_exchange_filters(symbol)

        # SL distance from ATR(14), CLAMPED into a sane % band of entry price.
        entry_price = float(row.get("last_price") or 0.0)
        atr = (row.get("_indicators") or {}).get("atr14")
        stop_distance = self._sane_stop_distance(entry_price, atr,
                                                 bot.config["stop_loss_atr"])
        if stop_distance is None:
            return False  # no usable entry price

        plan = self._risk.size_position(
            signal=signal,
            account=account,
            market=market,
            open_positions=await self._broker.get_positions(),
            filters=filters,
            mark_price=row.get("last_price"),
            leverage=bot.leverage,
            stop_distance=stop_distance,
        )
        if plan is None:
            return False

        # SL / TP from the entry. sl_dist comes from the clamped distance the
        # plan used; TP = take_profit_r * sl_dist (so TP% is bounded too).
        sl_dist = abs(float(plan.entry_price) - float(plan.stop_price))
        tp_dist = bot.config["take_profit_r"] * sl_dist

        # CONNECTED: record the open ONLY on a confirmed real fill. If the order
        # raises or returns an error/None, log and bail — never record a phantom.
        fill_price = float(plan.entry_price)
        fill_qty = float(plan.quantity)
        if self._broker.connected:
            await self._broker.set_leverage(symbol, bot.leverage)
            try:
                res = await self._broker.place_order(
                    symbol=symbol,
                    side=plan.side,
                    order_type="MARKET",
                    quantity=str(plan.quantity),
                )
            except Exception as exc:
                logger.error("Bot %s entry order raised for %s: %s — not recording.",
                             bot.id, symbol, exc)
                return False
            if not isinstance(res, dict) or not res.get("ok"):
                reason = res.get("reason") if isinstance(res, dict) else res
                logger.error("Bot %s entry order failed for %s (%s) — not recording.",
                             bot.id, symbol, reason)
                return False
            # Confirm via the resulting position; use actual filled qty/entry.
            confirmed = await self._confirm_fill(symbol)
            if confirmed is None:
                logger.error("Bot %s order for %s did not confirm a live position "
                             "— not recording.", bot.id, symbol)
                return False
            confirmed_qty, confirmed_entry = confirmed
            fill_qty = confirmed_qty
            if confirmed_entry > 0:
                fill_price = confirmed_entry

        # SL/TP computed from the ACTUAL fill price (using the clamped distances).
        if side == "LONG":
            stop_price = fill_price - sl_dist
            take_profit_price = fill_price + tp_dist
        else:
            stop_price = fill_price + sl_dist
            take_profit_price = fill_price - tp_dist
        logger.info(
            "Bot %s entry %s %s @ %.6g: SL %.4f%% / TP %.4f%% of fill.",
            bot.id, side, symbol, fill_price,
            sl_dist / fill_price * 100.0, tp_dist / fill_price * 100.0,
        )

        await self._open_trade(bot, row, plan, decision, stop_price,
                               take_profit_price, fill_price, fill_qty)
        return True

    @staticmethod
    def _sane_stop_distance(entry_price: float, atr, stop_loss_atr: float) -> Optional[float]:
        """ATR-based stop distance clamped to [_MIN_STOP_PCT, _MAX_STOP_PCT].

        Returns an absolute price distance, or None if entry_price is unusable.
        A missing/zero/NaN/absurd ATR uses the _DEFAULT_STOP_PCT fallback.
        """
        if not entry_price or entry_price <= 0:
            return None
        try:
            atr_val = float(atr) if atr is not None else 0.0
        except (TypeError, ValueError):
            atr_val = 0.0
        if atr_val != atr_val or atr_val in (float("inf"), float("-inf")):
            atr_val = 0.0

        if atr_val > 0:
            raw_pct = (stop_loss_atr * atr_val) / entry_price
        else:
            raw_pct = _DEFAULT_STOP_PCT
        clamped_pct = max(_MIN_STOP_PCT, min(_MAX_STOP_PCT, raw_pct))
        return entry_price * clamped_pct

    async def _confirm_fill(self, symbol: str) -> Optional[tuple[float, float]]:
        """Return (abs filled qty, entry price) for a live position, or None.

        Reads the broker's current positions and matches the symbol. None means
        no live position was found (treat as an unfilled / rejected order).
        """
        try:
            positions = await self._broker.get_positions()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("confirm_fill get_positions failed for %s: %s", symbol, exc)
            return None
        for p in positions:
            if p.get("symbol") != symbol:
                continue
            amt = abs(float(p.get("positionAmt", 0) or 0))
            if amt <= 0:
                return None
            entry = float(p.get("entryPrice", 0) or 0)
            return (amt, entry if entry > 0 else 0.0)
        return None

    async def _open_trade(
        self, bot: Bot, row: dict, plan, decision: dict,
        stop_price: float, take_profit_price: float,
        fill_price: float, fill_qty: float,
    ) -> None:
        """Persist an OPEN trade + update per-bot open-position stats.

        Uses the CONFIRMED fill price/qty (real position) when connected, or the
        planned values in safe mode.
        """
        if self._db is None:
            return
        trade_id = await self._db.open_bot_trade(
            bot_id=bot.id,
            symbol=row["symbol"],
            side=decision["side"],
            entry_price=float(fill_price),
            qty=float(fill_qty),
            win_prob=decision.get("win_prob"),
            features=decision["features"],
            reason=plan.reason,
            stop_price=float(stop_price),
            take_profit_price=float(take_profit_price),
        )
        if trade_id is None:
            return
        import time as _time

        bot.open_trades[trade_id] = {
            "symbol": row["symbol"],
            "side": decision["side"],
            "entry_price": float(fill_price),
            "qty": float(fill_qty),
            "stop_price": stop_price,
            "take_profit_price": take_profit_price,
            "opened_at_monotonic": _time.monotonic(),
        }
        bot.stats.open_positions = len(bot.open_trades)
        bot.trades_today += 1
        bot.touch()
        logger.info(
            "Bot %s opened %s %s @ %.6g (win_prob=%s, SL=%.6g, TP=%.6g).",
            bot.id, decision["side"], row["symbol"], fill_price,
            decision.get("win_prob"), stop_price, take_profit_price,
        )

    async def close_trade(
        self, bot_id: str, trade_id: int, exit_price: float, reason: str = "manual"
    ) -> Optional[dict]:
        """Close an open trade: compute pnl/outcome, persist, update stats, retrain.

        Exposed for tests and the (future) exit-management path. Returns the
        closed-trade dict or None.
        """
        bot = self._bots.get(bot_id)
        if bot is None or self._db is None:
            return None
        snap = bot.open_trades.pop(trade_id, None)
        if snap is None:
            return None

        direction = 1.0 if snap["side"] == "LONG" else -1.0
        gross = (float(exit_price) - snap["entry_price"]) * snap["qty"] * direction
        # NET of fees+slippage (market entry + market exit). The outcome label
        # MUST be on net pnl — it is the ML training target.
        pnl, cost = net_pnl(
            gross, snap["entry_price"], float(exit_price), snap["qty"],
        )
        outcome = "win" if pnl >= 0 else "loss"

        await self._db.close_bot_trade(
            trade_id=trade_id, exit_price=float(exit_price), pnl=pnl,
            outcome=outcome, reason=reason,
        )
        self._apply_completed_trade(bot, pnl)
        bot.stats.open_positions = len(bot.open_trades)

        if self._trainer is not None:
            await self._trainer.maybe_retrain(bot_id)

        return {"trade_id": trade_id, "pnl": round(pnl, 8), "outcome": outcome}

    def _apply_completed_trade(self, bot: Bot, pnl: float) -> None:
        """Fold one completed trade's pnl into per-bot stats + equity curve.

        Shared by the single-entry close path and the grid/dca strategy managers
        so trades_total/wins/losses/realized_pnl/equity_curve stay consistent.
        """
        s = bot.stats
        s.trades_total += 1
        s.realized_pnl += float(pnl)
        if float(pnl) >= 0:
            s.wins += 1
        else:
            s.losses += 1
        self._update_equity_curve(bot)
        bot.touch()

    def _update_equity_curve(self, bot: Bot) -> None:
        """Append a realized-pnl point and update the bot's max-drawdown."""
        s = bot.stats
        equity = s.realized_pnl
        s.equity_curve.append([_utc_now_iso(), round(equity, 8)])
        if equity > s.peak_equity:
            s.peak_equity = equity
        if s.peak_equity > 0:
            dd = (s.peak_equity - equity) / s.peak_equity * 100.0
            s.max_drawdown_pct = max(s.max_drawdown_pct, dd)

    async def _account(self) -> dict:
        """Normalized account dict for sizing (zeroed in safe mode)."""
        from app.api.normalize import normalize_account

        raw = await self._broker.get_account()
        return normalize_account(raw)
