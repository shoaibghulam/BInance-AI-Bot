"""Rebalancing bot — a real multi-position basket held to target weights.

Unlike the other bots this is NOT a single directional entry. It maintains a
basket of K long positions at EQUAL target weights and, on a schedule, trades
the delta back to target — trimming overweights (reduceOnly SELL) and adding to
underweights (BUY) — to harvest the rebalancing premium and cap concentration.
Reference: docs/strategies/the-five-bots.md §5.

Design choices (kept deliberately safe for testnet futures):
- LONG-ONLY equal weight across the top-K scanner leaders (no shorts).
- Total basket gross notional <= equity * target_exposure_pct, AND each leg
  <= the per-symbol notional cap, AND total margin <= 50% of available. If the
  basket can't fit, exposure is scaled down — never breached.
- COMBINED trigger: a scheduled check (rebalance_interval_s) that only trades a
  leg whose weight has drifted past rebalance_band_pct (relative) — minimising
  fee churn. A leg below min_trade_notional is skipped (no dust trades).
- Every realized trim/exit is recorded as a closed trade (NET of fees) so the
  ML trainer and per-bot stats stay consistent with the other bots.
- A symbol that drops out of the target basket is fully exited (reduceOnly).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.broker.precision import normalize_order
from app.risk.costs import net_pnl

logger = logging.getLogger("trader.bots.rebalance")

DEFAULT_REBALANCE_CONFIG = {
    "basket_size": 4,              # number of equal-weight legs
    "target_exposure_pct": 40.0,  # total basket gross notional as % of equity
    "rebalance_band_pct": 25.0,   # relative drift that triggers a trim/add (%)
    "rebalance_interval_s": 300.0,  # scheduled check cadence (seconds)
    "min_trade_notional_pct": 0.5,  # skip deltas smaller than this % of equity
}


@dataclass
class Holding:
    """One open basket leg (long-only)."""

    symbol: str
    qty: float
    avg_entry: float


class RebalanceManager:
    """Maintains the `rebalancing` bot's target-weight basket."""

    def __init__(self, bot_manager) -> None:
        self._m = bot_manager
        self._holdings: dict[str, Holding] = {}
        self._last_rebalance: float = 0.0  # monotonic; 0 forces a first build

    # --- Introspection ---------------------------------------------------
    def strategy_state(self) -> dict:
        """Surface basket state for GET /api/bots/rebalancing detail."""
        legs = [
            {
                "symbol": h.symbol,
                "qty": round(h.qty, 8),
                "avg_entry": round(h.avg_entry, 8),
                "notional": round(h.qty * h.avg_entry, 4),
            }
            for h in self._holdings.values()
        ]
        return {
            "basket": legs,
            "basket_size": len(legs),
            "total_notional": round(sum(l["notional"] for l in legs), 4),
        }

    def held_symbols(self) -> set[str]:
        """Basket members (used to surface holdings; selection sees all)."""
        return set(self._holdings.keys())

    def reset(self) -> None:
        """Drop in-memory basket (kill/halt already flattened on the exchange)."""
        self._holdings.clear()
        self._last_rebalance = 0.0

    # --- Tick ------------------------------------------------------------
    async def tick(self, bot, candidates: list[dict]) -> None:
        """Scheduled, drift-gated rebalance toward equal target weights."""
        now = time.monotonic()
        interval = float(_rebal_cfg(bot)["rebalance_interval_s"])
        # Rebalance on schedule, or immediately if the basket is empty (build).
        if self._holdings and (now - self._last_rebalance) < interval:
            bot.last_signal = "HOLD"
            bot.touch()
            return
        self._last_rebalance = now
        traded = await self._rebalance(bot, candidates)
        bot.last_signal = "BUY" if traded else "HOLD"
        bot.touch()

    # --- Core rebalance --------------------------------------------------
    async def _rebalance(self, bot, candidates: list[dict]) -> bool:
        """Converge the basket to equal target weights within all caps."""
        cfg = _rebal_cfg(bot)
        account = await self._m._account()
        equity = float(account.get("equity", 0) or 0)
        available = float(account.get("available", 0) or 0)
        if equity <= 0:
            return False

        k = max(1, int(cfg["basket_size"]))
        # Target basket = top-K scanner leaders, preferring current holdings to
        # reduce churn. candidates are already gated/passed and ranked by score.
        ranked = sorted(candidates, key=lambda r: r.get("score", 0.0), reverse=True)
        target_symbols: list[str] = []
        for r in ranked:
            sym = r.get("symbol")
            if sym and sym not in target_symbols:
                target_symbols.append(sym)
            if len(target_symbols) >= k:
                break
        # If the scan is thin, keep existing holdings in the basket.
        for sym in self._holdings:
            if sym not in target_symbols and len(target_symbols) < k:
                target_symbols.append(sym)
        if not target_symbols:
            return False

        # Per-leg target notional: equal weight of the (capped) gross exposure.
        s = self._m._risk._s
        gross = equity * (float(cfg["target_exposure_pct"]) / 100.0)
        # Total margin must fit the safe slice of available margin.
        if available > 0:
            gross = min(gross, available * 0.5 * bot.leverage)
        per_symbol_cap = equity * (s.max_notional_per_symbol_pct / 100.0)
        per_leg_target = min(gross / len(target_symbols), per_symbol_cap)
        if per_leg_target <= 0:
            return False
        min_trade = equity * (float(cfg["min_trade_notional_pct"]) / 100.0)
        band = float(cfg["rebalance_band_pct"]) / 100.0

        traded = False
        # 1. Exit holdings that fell out of the target basket.
        for sym in list(self._holdings.keys()):
            if sym not in target_symbols:
                if await self._exit_leg(bot, sym):
                    traded = True

        # 2. Converge each target leg toward its equal-weight target.
        for sym in target_symbols:
            row = next((r for r in candidates if r.get("symbol") == sym), None)
            price = float((row or {}).get("last_price") or 0.0) if row else 0.0
            if price <= 0:
                price = await self._m._fetch_mark(sym) or 0.0
            if price <= 0:
                continue
            if await self._converge_leg(bot, sym, price, per_leg_target,
                                        min_trade, band):
                traded = True

        bot.stats.open_positions = len(self._holdings)
        return traded

    async def _converge_leg(self, bot, symbol: str, price: float,
                            target_notional: float, min_trade: float,
                            band: float) -> bool:
        """Trade one leg's delta toward target (add if under, trim if over)."""
        holding = self._holdings.get(symbol)
        current_qty = holding.qty if holding else 0.0
        current_notional = current_qty * price
        delta_notional = target_notional - current_notional

        # Dust / within-band guard: skip tiny deltas, and (for existing legs)
        # only act once relative drift exceeds the band — this curbs fee churn.
        if abs(delta_notional) < min_trade:
            return False
        if holding is not None and target_notional > 0:
            rel_drift = abs(current_notional - target_notional) / target_notional
            if rel_drift < band:
                return False

        delta_qty = abs(delta_notional) / price
        filters = await self._m._broker.get_exchange_filters(symbol)
        rq = _round_qty(price, delta_qty, filters)
        if rq is None or rq <= 0:
            return False

        if delta_notional > 0:
            return await self._add_leg(bot, symbol, price, rq, filters)
        return await self._trim_leg(bot, symbol, price, rq)

    async def _add_leg(self, bot, symbol: str, price: float, qty: float,
                       filters) -> bool:
        """BUY to increase (or open) a leg; update VWAP average entry."""
        fill_price, fill_qty = price, qty
        if self._m._broker.connected:
            if not await self._place(symbol, "BUY", qty, reduce_only=False):
                return False
            confirmed = await self._m._confirm_fill(symbol)
            if confirmed is not None and confirmed[1] > 0:
                fill_price = confirmed[1]  # position avg entry (authoritative)
        holding = self._holdings.get(symbol)
        if holding is None:
            self._holdings[symbol] = Holding(symbol=symbol, qty=fill_qty,
                                             avg_entry=fill_price)
        elif self._m._broker.connected and confirmed is not None and confirmed[0] > 0:
            holding.qty = confirmed[0]
            holding.avg_entry = fill_price
        else:
            new_total = holding.qty + fill_qty
            holding.avg_entry = (
                (holding.avg_entry * holding.qty) + (fill_price * fill_qty)
            ) / new_total
            holding.qty = new_total
        logger.info("Rebalance ADD %s qty %.6g @ %.6g.", symbol, fill_qty, fill_price)
        return True

    async def _trim_leg(self, bot, symbol: str, price: float, qty: float) -> bool:
        """reduceOnly SELL to trim an overweight leg; realize NET pnl on the trim."""
        holding = self._holdings.get(symbol)
        if holding is None:
            return False
        trim_qty = min(qty, holding.qty)
        if trim_qty <= 0:
            return False
        if self._m._broker.connected:
            if not await self._place(symbol, "SELL", trim_qty, reduce_only=True):
                return False
        gross = (price - holding.avg_entry) * trim_qty  # long-only
        pnl, _cost = net_pnl(gross, holding.avg_entry, price, trim_qty)
        holding.qty -= trim_qty
        await self._record_realized(bot, symbol, holding.avg_entry, price,
                                    trim_qty, pnl, "rebalance_trim")
        if holding.qty <= 0:
            self._holdings.pop(symbol, None)
        logger.info("Rebalance TRIM %s qty %.6g @ %.6g pnl=%.6g.",
                    symbol, trim_qty, price, pnl)
        return True

    async def _exit_leg(self, bot, symbol: str) -> bool:
        """Fully exit a leg that left the basket; realize NET pnl."""
        holding = self._holdings.get(symbol)
        if holding is None:
            return False
        mark = await self._m._fetch_mark(symbol)
        if mark is None or mark <= 0:
            return False
        if self._m._broker.connected:
            if not await self._place(symbol, "SELL", holding.qty, reduce_only=True):
                logger.error("Rebalance EXIT %s rejected — leg kept, retry next "
                             "rebalance.", symbol)
                return False
        gross = (mark - holding.avg_entry) * holding.qty
        pnl, _cost = net_pnl(gross, holding.avg_entry, mark, holding.qty)
        await self._record_realized(bot, symbol, holding.avg_entry, mark,
                                    holding.qty, pnl, "rebalance_exit")
        self._holdings.pop(symbol, None)
        logger.info("Rebalance EXIT %s @ %.6g pnl=%.6g.", symbol, mark, pnl)
        return True

    # --- Helpers ---------------------------------------------------------
    async def _place(self, symbol: str, side: str, qty: float,
                     reduce_only: bool) -> bool:
        """Market order; True only on a confirmed ok result."""
        try:
            res = await self._m._broker.place_order(
                symbol=symbol, side=side, order_type="MARKET",
                quantity=str(qty), reduce_only=reduce_only,
            )
        except Exception as exc:
            logger.error("Rebalance %s %s order raised: %s", side, symbol, exc)
            return False
        if not isinstance(res, dict) or not res.get("ok"):
            logger.error("Rebalance %s %s order failed: %s", side, symbol,
                         res.get("reason") if isinstance(res, dict) else res)
            return False
        return True

    async def _record_realized(self, bot, symbol: str, entry: float,
                               exit_price: float, qty: float, pnl: float,
                               reason: str) -> None:
        """Persist a realized trim/exit as one closed trade + fold into stats."""
        self._m._apply_completed_trade(bot, pnl)
        bot.trades_today += 1
        if self._m._db is not None:
            outcome = "win" if pnl >= 0 else "loss"
            trade_id = await self._m._db.open_bot_trade(
                bot_id=bot.id, symbol=symbol, side="LONG", entry_price=entry,
                qty=qty, win_prob=None, features={}, reason=reason,
            )
            if trade_id is not None:
                await self._m._db.close_bot_trade(
                    trade_id=trade_id, exit_price=exit_price, pnl=pnl,
                    outcome=outcome, reason=reason,
                )


def _rebal_cfg(bot) -> dict:
    """Merge rebalancing defaults with the bot's config (config overrides)."""
    merged = dict(DEFAULT_REBALANCE_CONFIG)
    for key in DEFAULT_REBALANCE_CONFIG:
        if key in bot.config and bot.config[key] is not None:
            merged[key] = bot.config[key]
    return merged


def _round_qty(price: float, qty: float, filters) -> Optional[float]:
    """Round qty to filters (maxQty/step/min-notional); None if rejected."""
    if filters is None:
        return qty if qty > 0 else None
    result = normalize_order(Decimal(str(price)), Decimal(str(qty)), filters)
    return float(result["qty"]) if result else None
