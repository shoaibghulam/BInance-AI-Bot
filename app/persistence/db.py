"""Async SQLite persistence via aiosqlite.

Core tables: `trades`, `equity_snapshots`, `bot_state`. v2 adds the ML/scanner
tables: `bot_trades` (closed trades with stored feature vectors + outcome for
self-learning), `model_metrics` (accuracy/auc over time), and `scan_snapshots`.
The module is import-safe with no DB present; `init()` creates the file + schema
on startup. All timestamps are ISO-8601 UTC strings to match the API contract.

aiosqlite is imported lazily so this module parses even before deps install.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("trader.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id        TEXT,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    qty           REAL NOT NULL,
    price         REAL NOT NULL,
    reduce_only   INTEGER NOT NULL DEFAULT 0,
    pnl           REAL,
    order_id      TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    equity        REAL NOT NULL,
    balance       REAL,
    unrealized_pnl REAL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_state (
    bot_id        TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 0,
    pnl           REAL NOT NULL DEFAULT 0,
    trades_today  INTEGER NOT NULL DEFAULT 0,
    last_signal   TEXT,
    updated_at    TEXT NOT NULL
);

-- v2: per-bot closed trades with the feature vector + label for ML training.
CREATE TABLE IF NOT EXISTS bot_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id        TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    entry_price   REAL,
    exit_price    REAL,
    qty           REAL,
    pnl           REAL,
    outcome       TEXT,           -- 'win' | 'loss' | NULL (open)
    win_prob      REAL,
    features_json TEXT,
    reason        TEXT,
    stop_price    REAL,           -- persisted so SL survives a restart
    take_profit_price REAL,       -- persisted so TP survives a restart
    opened_at     TEXT NOT NULL,
    closed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_bot_trades_bot ON bot_trades(bot_id, id);

-- v2: model accuracy/auc history per bot (one row per retrain).
CREATE TABLE IF NOT EXISTS model_metrics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id        TEXT NOT NULL,
    ts            TEXT NOT NULL,
    accuracy      REAL,
    auc           REAL,
    n_samples     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_model_metrics_bot ON model_metrics(bot_id, id);

-- v2: ranked scanner snapshots (audit / replay), one row per bot per scan.
CREATE TABLE IF NOT EXISTS scan_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id        TEXT NOT NULL,
    scanned_at    TEXT NOT NULL,
    universe_size INTEGER,
    results_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_snapshots_bot ON scan_snapshots(bot_id, id);
"""


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Database:
    """Lightweight repository over an aiosqlite connection."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: Optional[Any] = None
        # Serializes write commits so concurrent bot tasks + the WS pusher
        # can't interleave execute/commit on the single shared connection.
        self._wlock = asyncio.Lock()

    async def init(self) -> None:
        """Open the connection and create tables if absent."""
        directory = os.path.dirname(os.path.abspath(self._db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)

        import aiosqlite  # lazy import

        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        # WAL + busy_timeout: concurrent readers/writers don't block each other
        # and a brief lock waits instead of raising "database is locked".
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(_SCHEMA)
        await self._migrate()
        await self._conn.commit()
        logger.info("SQLite initialized at %s (WAL)", self._db_path)

    async def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (idempotent)."""
        cursor = await self._conn.execute("PRAGMA table_info(bot_trades)")
        cols = {r[1] for r in await cursor.fetchall()}
        for col in ("stop_price", "take_profit_price"):
            if col not in cols:
                await self._conn.execute(
                    f"ALTER TABLE bot_trades ADD COLUMN {col} REAL"
                )
                logger.info("Migrated bot_trades: added column %s", col)

    async def close(self) -> None:
        """Close the connection (graceful shutdown)."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # --- Trades ----------------------------------------------------------
    async def record_trade(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        bot_id: Optional[str] = None,
        reduce_only: bool = False,
        pnl: Optional[float] = None,
        order_id: Optional[str] = None,
    ) -> None:
        """Insert a trade row."""
        if self._conn is None:
            return
        await self._conn.execute(
            """INSERT INTO trades
               (bot_id, symbol, side, qty, price, reduce_only, pnl, order_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                bot_id,
                symbol,
                side,
                float(qty),
                float(price),
                1 if reduce_only else 0,
                pnl,
                order_id,
                _utc_now_iso(),
            ),
        )
        await self._conn.commit()

    async def recent_trades(self, limit: int = 50) -> list[dict]:
        """Return the most recent trades, newest first."""
        if self._conn is None:
            return []
        cursor = await self._conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (int(limit),)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- Equity ----------------------------------------------------------
    async def record_equity(
        self,
        equity: float,
        balance: Optional[float] = None,
        unrealized_pnl: Optional[float] = None,
    ) -> None:
        """Insert an equity-curve snapshot."""
        if self._conn is None:
            return
        await self._conn.execute(
            """INSERT INTO equity_snapshots (equity, balance, unrealized_pnl, created_at)
               VALUES (?, ?, ?, ?)""",
            (float(equity), balance, unrealized_pnl, _utc_now_iso()),
        )
        await self._conn.commit()

    async def equity_curve(self, limit: int = 500) -> list[dict]:
        """Return recent equity snapshots, oldest first."""
        if self._conn is None:
            return []
        cursor = await self._conn.execute(
            "SELECT * FROM (SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT ?) "
            "ORDER BY id ASC",
            (int(limit),),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- Bot state -------------------------------------------------------
    async def upsert_bot_state(self, bot: dict) -> None:
        """Insert or update a bot's persisted state from a bot dict."""
        if self._conn is None:
            return
        await self._conn.execute(
            """INSERT INTO bot_state
               (bot_id, status, enabled, pnl, trades_today, last_signal, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bot_id) DO UPDATE SET
                 status=excluded.status, enabled=excluded.enabled, pnl=excluded.pnl,
                 trades_today=excluded.trades_today, last_signal=excluded.last_signal,
                 updated_at=excluded.updated_at""",
            (
                bot["id"],
                bot.get("status", "stopped"),
                1 if bot.get("enabled") else 0,
                float(bot.get("pnl", 0.0)),
                int(bot.get("trades_today", 0)),
                bot.get("last_signal", "HOLD"),
                _utc_now_iso(),
            ),
        )
        await self._conn.commit()

    async def read_bot_state(self, bot_id: str) -> Optional[dict]:
        """Return a single bot's persisted state, or None."""
        if self._conn is None:
            return None
        cursor = await self._conn.execute(
            "SELECT * FROM bot_state WHERE bot_id = ?", (bot_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # --- v2: per-bot trades (with features + outcome) --------------------
    async def open_bot_trade(
        self,
        bot_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        qty: float,
        win_prob: Optional[float],
        features: Optional[dict],
        reason: str = "",
        stop_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
    ) -> Optional[int]:
        """Insert an OPEN trade (outcome/exit null); returns its row id.

        `stop_price`/`take_profit_price` are persisted so a restart can
        rehydrate the position and keep enforcing its exits.
        """
        if self._conn is None:
            return None
        try:
            async with self._wlock:
                cursor = await self._conn.execute(
                    """INSERT INTO bot_trades
                       (bot_id, symbol, side, entry_price, qty, win_prob, features_json,
                        reason, stop_price, take_profit_price, opened_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        bot_id,
                        symbol,
                        side,
                        float(entry_price),
                        float(qty),
                        win_prob,
                        json.dumps(features) if features is not None else None,
                        reason,
                        float(stop_price) if stop_price is not None else None,
                        float(take_profit_price) if take_profit_price is not None else None,
                        _utc_now_iso(),
                    ),
                )
                await self._conn.commit()
                return int(cursor.lastrowid)
        except Exception as exc:
            logger.error("open_bot_trade failed for %s/%s: %s", bot_id, symbol, exc)
            return None

    async def open_bot_trades(self) -> list[dict]:
        """Return all OPEN bot trades (closed_at IS NULL) for startup reconcile."""
        if self._conn is None:
            return []
        cursor = await self._conn.execute(
            "SELECT * FROM bot_trades WHERE closed_at IS NULL ORDER BY id ASC"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def close_bot_trade(
        self,
        trade_id: int,
        exit_price: float,
        pnl: float,
        outcome: str,
        reason: str = "",
    ) -> None:
        """Finalize a trade row with exit price, pnl, outcome (win|loss)."""
        if self._conn is None:
            return
        try:
            async with self._wlock:
                await self._conn.execute(
                    """UPDATE bot_trades
                       SET exit_price = ?, pnl = ?, outcome = ?, reason = ?, closed_at = ?
                       WHERE id = ?""",
                    (
                        float(exit_price),
                        float(pnl),
                        outcome,
                        reason,
                        _utc_now_iso(),
                        int(trade_id),
                    ),
                )
                await self._conn.commit()
        except Exception as exc:
            logger.error("close_bot_trade failed for trade %s: %s", trade_id, exc)

    async def bot_trades(
        self, bot_id: str, limit: int = 100, closed_only: bool = False
    ) -> list[dict]:
        """Return a bot's trades, newest first (optionally only closed ones)."""
        if self._conn is None:
            return []
        clause = "AND outcome IS NOT NULL " if closed_only else ""
        cursor = await self._conn.execute(
            f"SELECT * FROM bot_trades WHERE bot_id = ? {clause}"
            "ORDER BY id DESC LIMIT ?",
            (bot_id, int(limit)),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def closed_bot_trades_asc(self, bot_id: str) -> list[dict]:
        """Return all CLOSED trades for a bot, oldest first (for time-ordered ML)."""
        if self._conn is None:
            return []
        cursor = await self._conn.execute(
            "SELECT * FROM bot_trades WHERE bot_id = ? AND outcome IS NOT NULL "
            "ORDER BY id ASC",
            (bot_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def count_closed_bot_trades(self, bot_id: str) -> int:
        """Count CLOSED trades for a bot."""
        if self._conn is None:
            return 0
        cursor = await self._conn.execute(
            "SELECT COUNT(*) AS n FROM bot_trades WHERE bot_id = ? "
            "AND outcome IS NOT NULL",
            (bot_id,),
        )
        row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    # --- v2: model metrics history --------------------------------------
    async def record_model_metrics(
        self,
        bot_id: str,
        accuracy: Optional[float],
        auc: Optional[float],
        n_samples: int,
    ) -> None:
        """Append one model-metrics row (one per retrain)."""
        if self._conn is None:
            return
        await self._conn.execute(
            """INSERT INTO model_metrics (bot_id, ts, accuracy, auc, n_samples)
               VALUES (?, ?, ?, ?, ?)""",
            (bot_id, _utc_now_iso(), accuracy, auc, int(n_samples)),
        )
        await self._conn.commit()

    async def model_metrics_history(
        self, bot_id: str, limit: int = 100
    ) -> list[dict]:
        """Return a bot's metrics history, oldest first."""
        if self._conn is None:
            return []
        cursor = await self._conn.execute(
            "SELECT * FROM (SELECT * FROM model_metrics WHERE bot_id = ? "
            "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (bot_id, int(limit)),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- v2: scanner snapshots ------------------------------------------
    async def record_scan_snapshot(
        self, bot_id: str, scanned_at: str, universe_size: int, results: list
    ) -> None:
        """Persist a ranked scan result for audit/replay."""
        if self._conn is None:
            return
        await self._conn.execute(
            """INSERT INTO scan_snapshots
               (bot_id, scanned_at, universe_size, results_json)
               VALUES (?, ?, ?, ?)""",
            (bot_id, scanned_at, int(universe_size), json.dumps(results)),
        )
        await self._conn.commit()
