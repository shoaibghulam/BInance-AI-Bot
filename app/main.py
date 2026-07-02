"""FastAPI application for The Trader.

Serves the JSON API (`/api/*`), a WebSocket (`/ws`), and the static dashboard
at `/`. Implements every endpoint in docs/api-contract.md exactly.

Boots cleanly with NO API keys (safe mode): the broker stays disconnected,
account/positions return empty/zero, and the UI still loads. A background task
pushes account/positions/status/equity over the WebSocket every ~2s.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.api.normalize import normalize_account, normalize_positions
from app.api.ws_manager import ConnectionManager, make_frame
from app.bots.manager import BotManager
from app.broker.client import BrokerClient
from app.config import settings
from app.market_data.service import MarketDataService
from app.ml.features import FEATURE_NAMES
from app.ml.trainer import MIN_SAMPLES_TO_START, ModelTrainer
from app.persistence.db import Database
from app.risk.engine import KillSwitch, RiskEngine
from app.scanner.service import MarketScanner
from app.signals.indicator_engine import IndicatorRuleEngine

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger("trader.main")

# Frontend lives next to app/ (owned by the UI agent). Resolve absolutely.
_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
_WS_PUSH_INTERVAL_S = 2.0


def _utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# Internal scan-row keys (prefixed `_`) are stripped from public payloads.
def _strip_internals(row: dict) -> dict:
    """Drop the scanner's `_`-prefixed internal keys from a result row."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _public_scan(scan: dict) -> dict:
    """Project a raw scan into the public `GET /api/bots/{id}/scanner` shape."""
    return {
        "scanned_at": scan.get("scanned_at"),
        "universe_size": scan.get("universe_size", 0),
        "results": [_strip_internals(r) for r in scan.get("results", [])],
    }


def _indicators_payload(scan: dict, top_n: int = 10) -> dict:
    """Build `GET /api/bots/{id}/indicators` from a raw scan's top candidates."""
    symbols = []
    for row in scan.get("results", [])[:top_n]:
        ind = row.get("_indicators") or {}
        symbols.append(
            {
                "symbol": row["symbol"],
                "indicators": {
                    "ema50": ind.get("ema50"),
                    "ema200": ind.get("ema200"),
                    "rsi14": ind.get("rsi14"),
                    "macd_hist": ind.get("macd_hist"),
                    "bb_pctb": ind.get("bb_pctb"),
                    "atr14": ind.get("atr14"),
                    "vwap": ind.get("vwap"),
                    "price": ind.get("price"),
                },
                "conditions": row.get("filters", {}),
                "entry_ready": bool(row.get("passed")),
            }
        )
    return {"ts": scan.get("scanned_at"), "symbols": symbols}


def _trades_payload(rows: list[dict]) -> dict:
    """Build `GET /api/bots/{id}/trades` (history + summary) from DB rows."""
    trades = []
    wins = losses = 0
    win_pnls: list[float] = []
    loss_pnls: list[float] = []
    for r in rows:
        outcome = r.get("outcome")
        pnl = r.get("pnl")
        trades.append(
            {
                "id": r.get("id"),
                "symbol": r.get("symbol"),
                "side": r.get("side"),
                "entry_price": r.get("entry_price"),
                "exit_price": r.get("exit_price"),
                "qty": r.get("qty"),
                "pnl": pnl,
                "outcome": outcome,
                "win_prob": r.get("win_prob"),
                "opened_at": r.get("opened_at"),
                "closed_at": r.get("closed_at"),
                "reason": r.get("reason"),
            }
        )
        if outcome == "win":
            wins += 1
            if pnl is not None:
                win_pnls.append(float(pnl))
        elif outcome == "loss":
            losses += 1
            if pnl is not None:
                loss_pnls.append(float(pnl))
    closed = wins + losses
    avg_win = round(sum(win_pnls) / len(win_pnls), 8) if win_pnls else 0.0
    avg_loss = round(sum(loss_pnls) / len(loss_pnls), 8) if loss_pnls else 0.0
    gross_win = sum(win_pnls)
    gross_loss = abs(sum(loss_pnls))
    profit_factor = round(gross_win / gross_loss, 4) if gross_loss > 0 else 0.0
    return {
        "trades": trades,
        "summary": {
            "trades_total": closed,
            "win_rate": round(wins / closed, 4) if closed else 0.0,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
        },
    }


async def _train_and_broadcast(state: "AppState", bot_id: str) -> None:
    """Retrain a bot's model then push a fresh `ml` frame to subscribers."""
    try:
        await state.trainer.train(bot_id)
        bot = state.bots.get(bot_id)
        min_wp = bot.min_win_prob if bot else None
        payload = await state.trainer.status(bot_id, min_win_prob=min_wp)
        scan = state.scanner.cached(bot_id)
        payload["live_predictions"] = state.trainer.live_predictions(
            bot_id, scan.get("results", [])
        )
        await state.ws.broadcast(make_frame("ml", {"bot": bot_id, **payload}))
    except Exception as exc:  # never let a background task crash silently
        logger.error("Background train for %s failed: %s", bot_id, exc)


# --- Runtime-overridable config (seeded from .env risk caps) ---------------
class RiskConfig(BaseModel):
    """Mutable risk caps mirrored from settings; matches GET/POST /api/config."""

    model_config = ConfigDict(extra="ignore")

    # Hard ceilings: a runtime config update can never exceed these, so a bad
    # (or hostile) POST /api/config cannot set 125x leverage or disable a cap.
    max_leverage: int = Field(ge=1, le=20)
    risk_pct_per_trade: float = Field(gt=0, le=5.0)
    max_daily_loss_pct: float = Field(gt=0, le=20.0)
    max_account_drawdown_pct: float = Field(gt=0, le=50.0)
    max_concurrent_positions: int = Field(ge=1, le=10)
    max_notional_per_symbol_pct: float = Field(gt=0, le=50.0)
    default_margin_type: Literal["ISOLATED", "CROSSED"]
    min_signal_confidence: float = Field(ge=0.5, le=1.0)


class BackfillRequest(BaseModel):
    """Body for POST /api/model/backfill (historical training)."""

    model_config = ConfigDict(extra="ignore")

    lookback_days: int = Field(default=90, ge=7, le=365)
    bots: list[str] | None = None
    universe: int | None = Field(default=12, ge=1, le=40)


class BotConfigUpdate(BaseModel):
    """Per-bot tunables for POST /api/bots/{id}/config (all optional)."""

    model_config = ConfigDict(extra="ignore")

    timeframe: str | None = None
    stop_loss_atr: float | None = None
    take_profit_r: float | None = None
    investment_usdt: float | None = Field(default=None, ge=0)
    stop_loss_pct: float | None = Field(default=None, ge=0, le=50)
    take_profit_pct: float | None = Field(default=None, ge=0, le=100)
    min_win_prob: float | None = None
    leverage: int | None = None
    margin_type: str | None = None
    max_open_positions: int | None = None
    # DCA tunables
    base_order_pct: float | None = None
    safety_order_count: int | None = None
    safety_order_deviation_pct: float | None = None
    safety_order_step_scale: float | None = None
    safety_order_volume_scale: float | None = None
    target_profit_pct: float | None = None
    max_deviation_pct: float | None = None
    # Grid tunables
    grid_levels: int | None = None
    grid_span_atr: float | None = None
    grid_mode: str | None = None
    grid_stop_buffer_pct: float | None = None


class AppState:
    """Container for shared singletons wired at startup."""

    def __init__(self) -> None:
        self.started_at: float = time.monotonic()
        self.broker = BrokerClient()
        self.market_data = MarketDataService(self.broker)
        self.risk = RiskEngine(settings)
        self.engine = IndicatorRuleEngine()
        self.db = Database(settings.db_path)
        self.scanner = MarketScanner(self.broker, self.market_data)
        self.trainer = ModelTrainer(self.db, min_win_prob=settings.min_signal_confidence)
        self.bots = BotManager(
            self.broker,
            self.market_data,
            self.risk,
            self.engine,
            scanner=self.scanner,
            trainer=self.trainer,
            db=self.db,
        )
        from app.ml.backfill import HistoricalTrainer

        self.historical = HistoricalTrainer(self.bots, self.db)
        self.kill_switch = KillSwitch(
            settings=settings,
            flatten_all=self.broker.flatten_all,
            halt_bots=self.bots.halt_all,
        )
        self.bots.attach_kill_switch(self.kill_switch)
        self.ws = ConnectionManager()
        self.config = RiskConfig(**settings.risk_caps())
        self._push_task: asyncio.Task | None = None
        self._scan_task: asyncio.Task | None = None
        # Set of bot ids any client is currently subscribed to (for WS pushes).
        self.active_bots: set[str] = set()
        # Daily-PnL baseline (UTC) so the daily-loss kill-switch can actually
        # fire. Reset at the first account read of each new UTC day.
        self._day_key: str = ""
        self._day_baseline_equity: float = 0.0

    def _daily_pnl(self, equity: float) -> float:
        """Realized+unrealized PnL since the start of the current UTC day."""
        if equity <= 0:
            return 0.0
        key = _utc_now_iso()[:10]  # YYYY-MM-DD
        if key != self._day_key:
            self._day_key = key
            self._day_baseline_equity = equity
        return equity - self._day_baseline_equity

    def bots_to_scan(self) -> set[str]:
        """Strategies the scan loop should refresh: running OR WS-subscribed."""
        running = {b["id"] for b in self.bots.list() if b.get("status") == "running"}
        return running | set(self.active_bots)

    def bots_with_activity(self) -> list[dict]:
        """bots.list() rows + additive backend-truth `activity` per bot.

        `activity` is one of: error | stopped | trading | cooling | warming |
        searching. It is derived from (status, open positions, scan_state) so
        the UI can show a live per-bot state for ALL bots without guessing.
        """
        rows = self.bots.list()
        for r in rows:
            bid = r["id"]
            scan = self.scanner.scan_state(bid)
            detail = self.bots.detail(bid)
            open_syms = detail.get("open_symbols", []) if detail else []
            open_n = len(open_syms)
            r["open_symbols"] = open_syms  # additive: lets the UI fleet strip show live coins
            if r["status"] == "error":
                r["activity"] = "error"
            elif r["status"] != "running":
                r["activity"] = "stopped"
            elif open_n > 0:
                r["activity"] = "trading"
            elif scan == "banned":
                r["activity"] = "cooling"
            elif scan == "cold":
                r["activity"] = "warming"
            else:
                r["activity"] = "searching"
        return rows

    # --- Payload builders (used by REST + WS) ---
    async def status_payload(self) -> dict:
        """Build the `GET /api/status` payload."""
        return {
            "env": settings.env_label,
            "connected": self.broker.connected,
            "kill_switch_active": self.kill_switch.active,
            "server_time": _utc_now_iso(),
            "uptime_s": int(time.monotonic() - self.started_at),
        }

    async def account_payload(self) -> dict:
        """Build the `GET /api/account` payload (zeroed in safe mode).

        Computes daily PnL vs the UTC-day baseline so `daily_pnl_pct` is real
        and the daily-loss kill-switch breach check can fire.
        """
        raw = await self.broker.get_account()
        acct = normalize_account(raw)
        daily = self._daily_pnl(acct["equity"])
        if daily:
            acct = normalize_account(raw, daily_pnl=daily)
        return acct

    async def positions_payload(self) -> list[dict]:
        """Build the `GET /api/positions` payload (empty in safe mode)."""
        raw = await self.broker.get_positions()
        return normalize_positions(raw)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown: init db, connect broker (safe mode ok), run WS pusher."""
    state: AppState = app.state.app_state
    try:
        await state.db.init()
    except Exception as exc:  # db failure must not block safe-mode boot
        logger.error("DB init failed (continuing): %s", exc)
    # MAINNET GUARDRAIL: refuse to connect to live trading unless explicitly
    # confirmed. A TRADING_ENV typo must never silently place real-money orders.
    mainnet_blocked = False
    if not settings.is_testnet:
        if settings.mainnet_confirmed.strip().lower() != "yes":
            logger.critical(
                "************************************************************\n"
                "*** MAINNET selected but MAINNET_CONFIRMED != 'yes'.      ***\n"
                "*** Refusing to connect — staying in SAFE MODE (no live   ***\n"
                "*** orders). Set MAINNET_CONFIRMED=yes to trade real money.***\n"
                "************************************************************"
            )
            mainnet_blocked = True
        else:
            logger.critical(
                "*** MAINNET MODE ACTIVE — LIVE MONEY. Real orders will be placed. ***"
            )
    if not mainnet_blocked:
        await state.broker.connect()  # safe mode if no keys; never raises
        # RESTART-SAFETY: reconcile exchange truth vs our state before any loop
        # runs — rehydrate single-entry positions (SL/TP survive a restart),
        # flatten anything that can't be safely resumed.
        try:
            await state.bots.reconcile_on_startup()
        except Exception as exc:  # never block boot on reconcile
            logger.error("Startup reconcile error (continuing): %s", exc)
    state._push_task = asyncio.create_task(_ws_pusher(state))
    state._scan_task = asyncio.create_task(_scan_loop(state))
    logger.info("The Trader started (env=%s, connected=%s).",
                settings.env_label, state.broker.connected)
    try:
        yield
    finally:
        for task in (state._push_task, state._scan_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await state.bots.shutdown()
        await state.broker.close()
        await state.db.close()
        logger.info("The Trader shut down cleanly.")


_SCAN_LOOP_INTERVAL_S = 5.0  # wake cadence; per-strategy refresh honors CACHE_TTL_S
_SCAN_CONCURRENCY = 3   # max strategies refreshed at once (2 on mainnet if -1003 risk)


async def _scan_loop(state: AppState) -> None:
    """Background loop: refresh the scan cache for running/active bots only.

    Never called from an HTTP path — this is the ONLY driver of fresh scans.
    Honors the scanner's CACHE_TTL_S (a no-op refresh just returns the cache)
    and backs off automatically while the scanner is in a -1003 ban window, so
    requests always read instantly from cache and we never hammer the API.

    Refreshes due strategies concurrently (bounded by _SCAN_CONCURRENCY),
    dispatching cold (never-scanned) strategies before stale ones so a
    freshly-subscribed bot gets its first scan without waiting behind others.
    Each `refresh` self-serializes via its per-strategy lock and re-checks the
    ban/fresh guards, so the concurrency here never double-scans.
    """
    while True:
        try:
            if not state.scanner.is_banned:
                due = [b for b in state.bots_to_scan() if not state.scanner.is_fresh(b)]
                due.sort(key=lambda b: state.scanner.has_cache(b))  # cold (False) first
                sem = asyncio.Semaphore(_SCAN_CONCURRENCY)

                async def _refresh_one(bid: str) -> None:
                    async with sem:
                        if not state.scanner.is_banned:
                            await state.scanner.refresh(bid)

                await asyncio.gather(*(_refresh_one(b) for b in due),
                                     return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the scan loop die
            logger.error("Scan loop tick failed: %s", exc)
        await asyncio.sleep(_SCAN_LOOP_INTERVAL_S)


async def _push_active_bot_frames(state: AppState) -> None:
    """Push scanner/indicators/ml frames for each subscribed bot (~every 5s)."""
    for bot_id in list(state.active_bots):
        bot = state.bots.get(bot_id)
        if bot is None:
            continue
        try:
            scan = state.scanner.cached(bot_id)
            await state.ws.broadcast(
                make_frame("scanner", {"bot": bot_id, **_public_scan(scan)})
            )
            await state.ws.broadcast(
                make_frame("indicators", {"bot": bot_id, **_indicators_payload(scan)})
            )
            ml = await state.trainer.status(bot_id, min_win_prob=bot.min_win_prob)
            ml["live_predictions"] = state.trainer.live_predictions(
                bot_id, scan.get("results", [])
            )
            await state.ws.broadcast(make_frame("ml", {"bot": bot_id, **ml}))
        except Exception as exc:  # never let one bot break the push loop
            logger.debug("active-bot push for %s failed: %s", bot_id, exc)


async def _ws_pusher(state: AppState) -> None:
    """Background loop: push status/account/positions/equity every ~2s.

    Per-bot scanner/indicators/ml frames for subscribed bots push every ~5s.
    """
    last_status: dict | None = None
    tick = 0
    while True:
        tick += 1
        try:
            if state.ws.count > 0:
                account = await state.account_payload()
                positions = await state.positions_payload()
                status = await state.status_payload()

                await state.ws.broadcast(make_frame("account", account))
                await state.ws.broadcast(make_frame("positions", positions))
                await state.ws.broadcast(
                    make_frame("equity", {"equity": account["equity"]})
                )
                if status != last_status:
                    await state.ws.broadcast(make_frame("status", status))
                    last_status = status

                # Auto circuit-breaker: fire on a daily-loss / drawdown breach.
                if not state.kill_switch.active:
                    breach = state.kill_switch.check_breaches(account)
                    if breach:
                        result = await state.kill_switch.trigger(reason=breach)
                        await state.ws.broadcast(
                            make_frame("log", {"level": "error",
                                               "msg": f"KILL SWITCH: {breach}"})
                        )
                        await state.ws.broadcast(make_frame("bots", state.bots_with_activity()))
                        logger.warning("Auto kill switch fired: %s (%s)", breach, result)

                if account["equity"] > 0:
                    await state.db.record_equity(
                        equity=account["equity"],
                        balance=account["balance"],
                        unrealized_pnl=account["unrealized_pnl"],
                    )

                # Per-bot frames for subscribed bots roughly every ~5s.
                if state.active_bots and tick % 3 == 0:
                    await _push_active_bot_frames(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the pusher die
            logger.error("WS pusher tick failed: %s", exc)
        await asyncio.sleep(_WS_PUSH_INTERVAL_S)


def create_app() -> FastAPI:
    """Application factory: wire state, middleware, routes, static mount."""
    app = FastAPI(title="The Trader", version="0.1.0", lifespan=lifespan)
    app.state.app_state = AppState()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost",
            "http://127.0.0.1",
            f"http://localhost:{settings.port}",
            f"http://127.0.0.1:{settings.port}",
        ],
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _register_routes(app)
    _mount_frontend(app)
    return app


def _register_routes(app: FastAPI) -> None:
    """Register all REST + WS endpoints from the API contract."""

    def st() -> AppState:
        return app.state.app_state

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Contract-shaped error response; never leak secrets."""
        logger.error("Unhandled error on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content={"error": "internal error", "detail": str(exc)},
        )

    @app.get("/api/status")
    async def get_status() -> dict:
        return await st().status_payload()

    @app.get("/api/account")
    async def get_account() -> dict:
        return await st().account_payload()

    @app.get("/api/positions")
    async def get_positions() -> list[dict]:
        return await st().positions_payload()

    @app.get("/api/bots")
    async def get_bots() -> list[dict]:
        return st().bots_with_activity()

    # --- v2: per-bot detail endpoints -----------------------------------
    def _not_found(bot_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=404, content={"error": "bot not found", "detail": bot_id}
        )

    @app.get("/api/bots/{bot_id}")
    async def get_bot_detail(bot_id: str):
        detail = st().bots.detail(bot_id)
        if detail is None:
            return _not_found(bot_id)
        return detail

    @app.get("/api/bots/{bot_id}/scanner")
    async def get_bot_scanner(bot_id: str):
        state = st()
        if state.bots.get(bot_id) is None:
            return _not_found(bot_id)
        scan = state.scanner.cached(bot_id)
        return _public_scan(scan)

    @app.get("/api/bots/{bot_id}/indicators")
    async def get_bot_indicators(bot_id: str):
        state = st()
        if state.bots.get(bot_id) is None:
            return _not_found(bot_id)
        scan = state.scanner.cached(bot_id)
        return _indicators_payload(scan)

    @app.get("/api/bots/{bot_id}/trades")
    async def get_bot_trades(bot_id: str):
        state = st()
        if state.bots.get(bot_id) is None:
            return _not_found(bot_id)
        rows = await state.db.bot_trades(bot_id, limit=200)
        return _trades_payload(rows)

    @app.get("/api/bots/{bot_id}/ml")
    async def get_bot_ml(bot_id: str):
        state = st()
        bot = state.bots.get(bot_id)
        if bot is None:
            return _not_found(bot_id)
        payload = await state.trainer.status(bot_id, min_win_prob=bot.min_win_prob)
        scan = state.scanner.cached(bot_id)
        payload["live_predictions"] = state.trainer.live_predictions(
            bot_id, scan.get("results", [])
        )
        return payload

    @app.post("/api/bots/{bot_id}/ml/train")
    async def post_bot_ml_train(bot_id: str):
        state = st()
        if state.bots.get(bot_id) is None:
            return _not_found(bot_id)
        # Kick off training in the background so the request returns promptly.
        asyncio.create_task(_train_and_broadcast(state, bot_id))
        return {"ok": True, "status": "training"}

    @app.get("/api/model")
    async def get_model_overview() -> dict:
        """Aggregate self-learning model view across all bots (AI Model tab)."""
        state = st()
        bots = [b["id"] for b in state.bots.list()]
        per_bot = []
        total_samples = total_params = 0
        acc_weighted = acc_weight = 0.0
        trained_count = 0
        for bid in bots:
            bot = state.bots.get(bid)
            ml = await state.trainer.status(bid, min_win_prob=bot.min_win_prob)
            n = int(ml.get("n_samples", 0) or 0)
            # "training data" = what the model actually trained on (historical
            # backfill count when present, else live closed-trade count).
            trained_n = int(ml.get("training_samples", n) or n)
            params = int(ml.get("n_parameters", 0) or 0)
            acc = ml.get("metrics", {}).get("accuracy")
            is_trained = ml.get("status") == "trained"
            total_samples += trained_n if is_trained else n
            total_params += params
            if is_trained:
                trained_count += 1
                if acc is not None and trained_n > 0:
                    acc_weighted += acc * trained_n
                    acc_weight += trained_n
            per_bot.append({
                "bot": bid, "status": ml.get("status"),
                "model_type": ml.get("model_type"), "calibrated": ml.get("calibrated"),
                "n_samples": n, "training_samples": trained_n if is_trained else n,
                "n_parameters": params,
                "train_duration_s": ml.get("train_duration_s", 0.0),
                "accuracy": acc, "auc": ml.get("metrics", {}).get("auc"),
                "brier": ml.get("metrics", {}).get("brier"),
                "accuracy_history": ml.get("metrics", {}).get("history", []),
                "samples_needed": ml.get("samples_needed", 0),
                "min_win_prob": ml.get("min_win_prob"),
            })
        # backend/type from any bot (all share the same backend detection).
        sample_ml = per_bot[0] if per_bot else {}
        return {
            "backend": (await state.trainer.status(bots[0])).get("model") if bots else "sklearn",
            "model_type": sample_ml.get("model_type"),
            "approach": "Self-learning win-probability gate — gradient-boosted "
                        "trees, recency-weighted, Platt-calibrated, retrained on "
                        "the bot's own closed trades. Learns from every outcome.",
            "bots_total": len(bots),
            "bots_trained": trained_count,
            "total_training_samples": total_samples,
            "total_parameters": total_params,
            "avg_accuracy": round(acc_weighted / acc_weight, 4) if acc_weight else None,
            "min_samples_to_train": MIN_SAMPLES_TO_START,
            "feature_names": list(FEATURE_NAMES),
            "per_bot": per_bot,
        }

    @app.post("/api/model/train")
    async def post_model_train_all() -> dict:
        """Trigger a retrain of EVERY bot's model (AI Model tab 'Train more')."""
        state = st()
        started = []
        for b in state.bots.list():
            asyncio.create_task(_train_and_broadcast(state, b["id"]))
            started.append(b["id"])
        return {"ok": True, "status": "training", "bots": started}

    @app.post("/api/model/backfill")
    async def post_model_backfill(payload: BackfillRequest) -> dict:
        """Train models on REAL historical data (backtest → labeled examples)."""
        state = st()
        if state.historical.is_running():
            return {"ok": False, "status": "running",
                    "detail": "a backfill is already in progress"}
        ids = payload.bots or [b["id"] for b in state.bots.list()]
        ids = [i for i in ids if state.bots.get(i) is not None]
        if not ids:
            return JSONResponse(status_code=400,
                                content={"error": "no valid bots", "detail": payload.bots})
        days = max(7, min(int(payload.lookback_days), 365))

        async def _run():
            await state.historical.run(ids, days, universe=payload.universe or 12)
            # push refreshed model overview so the UI updates when done.
            try:
                await state.ws.broadcast(make_frame("bots", state.bots_with_activity()))
            except Exception:
                pass

        asyncio.create_task(_run())
        return {"ok": True, "status": "running", "bots": ids, "lookback_days": days}

    @app.get("/api/model/backfill")
    async def get_model_backfill() -> dict:
        """Live backfill progress (status, %+duration, examples, per-bot results)."""
        return st().historical.progress.snapshot()

    @app.post("/api/bots/{bot_id}/config")
    async def post_bot_config(bot_id: str, payload: BotConfigUpdate):
        state = st()
        detail = state.bots.update_config(bot_id, payload.model_dump(exclude_none=True))
        if detail is None:
            return _not_found(bot_id)
        await state.ws.broadcast(make_frame("bots", state.bots_with_activity()))
        return detail

    @app.post("/api/bots/{bot_id}/start")
    async def start_bot(bot_id: str):
        bot = await st().bots.start(bot_id)
        if bot is None:
            return JSONResponse(
                status_code=404,
                content={"error": "bot not found", "detail": bot_id},
            )
        await st().ws.broadcast(make_frame("bots", st().bots_with_activity()))
        return {"ok": True, "bot": bot}

    @app.post("/api/bots/{bot_id}/stop")
    async def stop_bot(bot_id: str):
        bot = await st().bots.stop(bot_id)
        if bot is None:
            return JSONResponse(
                status_code=404,
                content={"error": "bot not found", "detail": bot_id},
            )
        await st().ws.broadcast(make_frame("bots", st().bots_with_activity()))
        return {"ok": True, "bot": bot}

    @app.post("/api/bots/start-all")
    async def start_all_bots():
        """Start every bot at once (Overview 'Start all')."""
        state = st()
        started = []
        for b in state.bots.list():
            await state.bots.start(b["id"])
            started.append(b["id"])
        await state.ws.broadcast(make_frame("bots", state.bots_with_activity()))
        return {"ok": True, "started": started}

    @app.post("/api/bots/stop-all")
    async def stop_all_bots():
        """Stop every bot at once."""
        state = st()
        for b in state.bots.list():
            await state.bots.stop(b["id"])
        await state.ws.broadcast(make_frame("bots", state.bots_with_activity()))
        return {"ok": True}

    @app.get("/api/config")
    async def get_config() -> dict:
        return st().config.model_dump()

    @app.post("/api/config")
    async def post_config(payload: RiskConfig) -> dict:
        st().config = payload
        # CRITICAL: write back to the live `settings` singleton that the
        # RiskEngine and grid/dca managers actually read (self._risk._s.*).
        # Without this, a config change updates the UI but NOT enforcement.
        for field, value in payload.model_dump().items():
            setattr(settings, field, value)
        await st().ws.broadcast(
            make_frame("log", {"level": "info", "msg": "config updated (enforced)"})
        )
        return st().config.model_dump()

    @app.post("/api/kill")
    async def kill() -> dict:
        result = await st().kill_switch.trigger(reason="manual")
        await st().ws.broadcast(
            make_frame("log", {"level": "error", "msg": "KILL SWITCH triggered"})
        )
        await st().ws.broadcast(make_frame("bots", st().bots_with_activity()))
        await st().ws.broadcast(make_frame("status", await st().status_payload()))
        return result

    @app.post("/api/kill/reset")
    async def kill_reset(resume: bool = True) -> dict:
        state = st()
        result = state.kill_switch.reset()
        # Resume the bots that were running when the kill halted them, so a
        # reset doesn't silently leave bots off (pass ?resume=false to skip).
        resumed = await state.bots.resume_halted() if resume else []
        result["resumed"] = resumed
        await state.ws.broadcast(
            make_frame("log", {"level": "info",
                               "msg": f"kill switch reset (resumed {len(resumed)} bots)"})
        )
        await state.ws.broadcast(make_frame("status", await state.status_payload()))
        await state.ws.broadcast(make_frame("bots", state.bots_with_activity()))
        return result

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        state = st()
        await state.ws.connect(websocket)
        try:
            # Send an immediate snapshot so the UI paints without waiting.
            await state.ws.send_personal(
                websocket, make_frame("status", await state.status_payload())
            )
            await state.ws.send_personal(
                websocket, make_frame("account", await state.account_payload())
            )
            await state.ws.send_personal(
                websocket, make_frame("positions", await state.positions_payload())
            )
            await state.ws.send_personal(websocket, make_frame("bots", state.bots_with_activity()))

            while True:
                msg = await websocket.receive_json()
                if not isinstance(msg, dict):
                    continue
                msg_type = msg.get("type")
                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                elif msg_type == "subscribe":
                    bot_id = msg.get("bot")
                    if state.bots.get(bot_id) is not None:
                        state.active_bots.add(bot_id)
                        # Send an immediate snapshot for the newly-active bot.
                        scan = state.scanner.cached(bot_id)
                        await state.ws.send_personal(
                            websocket,
                            make_frame("scanner", {"bot": bot_id, **_public_scan(scan)}),
                        )
                        await state.ws.send_personal(
                            websocket,
                            make_frame(
                                "indicators",
                                {"bot": bot_id, **_indicators_payload(scan)},
                            ),
                        )
                        bot = state.bots.get(bot_id)
                        ml = await state.trainer.status(
                            bot_id, min_win_prob=bot.min_win_prob
                        )
                        ml["live_predictions"] = state.trainer.live_predictions(
                            bot_id, scan.get("results", [])
                        )
                        await state.ws.send_personal(
                            websocket, make_frame("ml", {"bot": bot_id, **ml})
                        )
        except WebSocketDisconnect:
            await state.ws.disconnect(websocket)
        except Exception as exc:  # malformed frame etc. — drop the client
            logger.debug("WS client error: %s", exc)
            await state.ws.disconnect(websocket)


def _mount_frontend(app: FastAPI) -> None:
    """Mount the static dashboard at `/` if the frontend dir exists.

    The directory is owned by the UI agent and may be empty at boot; we only
    mount when it exists so the backend never crashes on a missing folder.
    """
    if os.path.isdir(_FRONTEND_DIR):
        app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
    else:  # pragma: no cover - frontend dir is expected to exist
        logger.warning("Frontend dir not found at %s; UI not mounted.", _FRONTEND_DIR)


# Module-level ASGI app for `uvicorn app.main:app`.
app = create_app()
