"""DCA (safety-order / averaging) bot mechanics.

3Commas-style martingale: a small base order, then a ladder of safety orders
(SO) that average the entry as price moves adversely, pulling break-even toward
price so a modest bounce exits the whole deal at a target measured off the
AVERAGE entry. Reference: docs/strategies/the-five-bots.md §4.

CRITICAL risk (risk-management.md): size the base so the FULLY-LOADED ladder
(base + every safety order) stays within max_notional_per_symbol AND the
margin/gross caps. The martingale must never breach caps — if the fully-loaded
notional can't fit, the deal is rejected.

A "deal" is one full averaging cycle. On close (TP or hard stop) it is recorded
as ONE closed trade (pnl from the average entry), feeding the ML trainer.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from app.broker.precision import normalize_order
from app.risk.costs import net_pnl

logger = logging.getLogger("trader.bots.dca")

# Config defaults (tunable via POST /api/bots/dca/config).
DEFAULT_DCA_CONFIG = {
    "base_order_pct": 0.25,            # fraction of the risk budget for base order
    "safety_order_count": 4,
    "safety_order_deviation_pct": 1.0,  # first SO gap from base, %
    "safety_order_step_scale": 1.5,     # multiplies deviation for each next SO
    "safety_order_volume_scale": 1.5,   # martingale size coefficient
    "target_profit_pct": 1.0,           # off AVERAGE entry, %
    "max_deviation_pct": 12.0,          # hard-stop band from base entry
}


@dataclass
class DcaDeal:
    """State for one open averaging deal."""

    symbol: str
    side: str                 # LONG | SHORT
    base_price: float
    average_entry: float
    total_qty: float
    base_qty: float
    filled_safety_count: int
    next_trigger_price: float
    features: dict
    win_prob: Optional[float]
    trade_id: Optional[int] = None  # DB row id for the open deal
    opened_at: float = field(default_factory=time.monotonic)


def _so_deviation_levels(cfg: dict) -> list[float]:
    """Cumulative adverse-deviation % at which each SO #n (1-based) triggers.

    SO #1 sits at `deviation`; each subsequent SO adds deviation * step^n, so
    successive orders sit progressively further away (3Commas step scale).
    """
    dev = float(cfg["safety_order_deviation_pct"])
    step = float(cfg["safety_order_step_scale"])
    levels: list[float] = []
    cumulative = 0.0
    for n in range(int(cfg["safety_order_count"])):
        gap = dev * (step ** n)
        cumulative += gap
        levels.append(cumulative)
    return levels


def _so_qty_factor(cfg: dict, n: int) -> float:
    """Size multiple of the base for safety order #n (0-based): volume_scale^n."""
    return float(cfg["safety_order_volume_scale"]) ** n


def fully_loaded_qty(base_qty: float, cfg: dict) -> float:
    """Total quantity if the base + every safety order fills (martingale sum)."""
    total = base_qty
    for n in range(int(cfg["safety_order_count"])):
        total += base_qty * _so_qty_factor(cfg, n)
    return total


class DcaManager:
    """Drives DCA deals for the `dca` bot. One open deal per symbol at a time."""

    def __init__(self, bot_manager) -> None:
        self._m = bot_manager  # back-reference to BotManager for shared deps
        # symbol -> DcaDeal
        self._deals: dict[str, DcaDeal] = {}

    # --- Introspection for the API detail payload ----------------------
    def strategy_state(self) -> dict:
        """Surface DCA deal state for GET /api/bots/dca detail."""
        deals = []
        for d in self._deals.values():
            deals.append({
                "symbol": d.symbol,
                "side": d.side,
                "avg_entry": round(d.average_entry, 8),
                "safety_orders_filled": d.filled_safety_count,
                "target_price": round(self._target_price(d), 8),
                "total_qty": round(d.total_qty, 8),
            })
        return {"open_deals": deals}

    def held_symbols(self) -> set[str]:
        """Symbols with an active deal (so the bot won't double-enter)."""
        return set(self._deals.keys())

    def reset(self) -> None:
        """Drop in-memory deals (kill/halt already flattened on the exchange)."""
        self._deals.clear()

    # --- Tick ------------------------------------------------------------
    async def tick(self, bot, candidates: list[dict]) -> None:
        """Manage open deals, then maybe open ONE new deal from candidates."""
        # 1. Manage every open deal (safety orders, TP, hard stop).
        for symbol in list(self._deals.keys()):
            await self._manage_deal(bot, self._deals[symbol])

        # 2. New deal only if none open (per-bot single-deal cap) and a
        #    candidate the bot isn't already running passes.
        if self._deals:
            bot.last_signal = "HOLD"
            bot.touch()
            return
        fresh = [c for c in candidates if c.get("symbol") not in self._deals]
        if not fresh:
            bot.last_signal = "HOLD"
            bot.touch()
            return
        best = max(fresh, key=lambda r: (r.get("win_prob") or 0.0, r.get("score", 0.0)))
        opened = await self._open_deal(bot, best)
        bot.last_signal = "BUY" if opened else "HOLD"
        bot.touch()

    # --- Deal lifecycle --------------------------------------------------
    async def _open_deal(self, bot, row: dict) -> bool:
        """Open the base order of a new deal if the fully-loaded ladder fits."""
        cfg = _dca_cfg(bot)
        symbol = row["symbol"]
        side = row.get("side", "LONG")
        entry = float(row.get("last_price") or 0.0)
        if entry <= 0:
            return False

        account = await self._m._account()
        equity = float(account.get("equity", 0) or 0)
        available = float(account.get("available", 0) or 0)
        if equity <= 0:
            return False

        # Base qty from a fraction of the risk budget, then VERIFY the
        # fully-loaded ladder fits inside the per-symbol notional + margin caps.
        risk_budget = equity * (self._m._risk._s.risk_pct_per_trade / 100.0)
        base_notional = risk_budget * float(cfg["base_order_pct"])
        base_qty = base_notional / entry
        if base_qty <= 0:
            return False

        loaded_qty = fully_loaded_qty(base_qty, cfg)
        loaded_notional = loaded_qty * entry
        if not self._ladder_fits(loaded_notional, equity, available, bot.leverage):
            logger.info(
                "DCA %s: fully-loaded ladder %.2f exceeds caps; rejecting deal.",
                symbol, loaded_notional,
            )
            return False

        filters = await self._m._broker.get_exchange_filters(symbol)
        base_qty = _round_qty(entry, base_qty, filters)
        if base_qty is None:
            logger.info("DCA %s: base qty rejected by filters; skipping.", symbol)
            return False

        # Place the base MARKET order; record open ONLY on a confirmed fill.
        fill_price, fill_qty = entry, base_qty
        if self._m._broker.connected:
            ok = await self._place_market(symbol, side, base_qty)
            if not ok:
                return False
            confirmed = await self._m._confirm_fill(symbol)
            if confirmed is None:
                logger.error("DCA %s: base order unconfirmed; not recording.", symbol)
                return False
            fill_qty, conf_entry = confirmed
            if conf_entry > 0:
                fill_price = conf_entry

        features = row.get("_features") or {}
        win_prob = row.get("win_prob")
        levels = _so_deviation_levels(cfg)
        next_trigger = self._deviation_price(side, fill_price, levels[0]) if levels else None

        trade_id = None
        if self._m._db is not None:
            trade_id = await self._m._db.open_bot_trade(
                bot_id=bot.id, symbol=symbol, side=side,
                entry_price=fill_price, qty=fill_qty, win_prob=win_prob,
                features=features, reason="dca_base",
            )

        self._deals[symbol] = DcaDeal(
            symbol=symbol, side=side, base_price=fill_price,
            average_entry=fill_price, total_qty=fill_qty, base_qty=fill_qty,
            filled_safety_count=0,
            next_trigger_price=next_trigger if next_trigger else fill_price,
            features=features, win_prob=win_prob, trade_id=trade_id,
        )
        bot.trades_today += 1
        bot.stats.open_positions = 1
        logger.info("DCA %s opened base %s @ %.6g (target %.6g).",
                    symbol, side, fill_price, self._target_price(self._deals[symbol]))
        return True

    async def _manage_deal(self, bot, deal: DcaDeal) -> None:
        """One deal step: safety order, take-profit, or hard stop."""
        mark = await self._m._fetch_mark(deal.symbol)
        if mark is None or mark <= 0:
            return

        cfg = _dca_cfg(bot)
        # Take-profit off AVERAGE entry.
        target = self._target_price(deal)
        hit_tp = (deal.side == "LONG" and mark >= target) or \
                 (deal.side == "SHORT" and mark <= target)
        if hit_tp:
            await self._close_deal(bot, deal, mark, "take_profit")
            return

        # Adverse safety-order trigger.
        levels = _so_deviation_levels(cfg)
        if deal.filled_safety_count < len(levels):
            trig = deal.next_trigger_price
            adverse = (deal.side == "LONG" and mark <= trig) or \
                      (deal.side == "SHORT" and mark >= trig)
            if adverse:
                await self._fill_safety_order(bot, deal, mark, cfg, levels)
                return

        # Hard stop: ladder exhausted AND beyond max deviation.
        max_dev = float(cfg["max_deviation_pct"])
        stop_price = self._deviation_price(deal.side, deal.base_price, max_dev)
        beyond = (deal.side == "LONG" and mark <= stop_price) or \
                 (deal.side == "SHORT" and mark >= stop_price)
        if deal.filled_safety_count >= len(levels) and beyond:
            await self._close_deal(bot, deal, mark, "stop_loss")

    async def _fill_safety_order(self, bot, deal: DcaDeal, mark: float,
                                 cfg: dict, levels: list[float]) -> None:
        """Place the next safety order, recompute average entry + total qty."""
        n = deal.filled_safety_count  # 0-based index of the SO being placed
        so_qty = deal.base_qty * _so_qty_factor(cfg, n)
        filters = await self._m._broker.get_exchange_filters(deal.symbol)
        so_qty_r = _round_qty(mark, so_qty, filters)
        if so_qty_r is None or so_qty_r <= 0:
            return

        if self._m._broker.connected:
            ok = await self._place_market(deal.symbol, deal.side, so_qty_r)
            if not ok:
                return
            # Prefer the exchange's authoritative position average + size after
            # the fill (accounts for real slippage); fall back to a local VWAP.
            confirmed = await self._m._confirm_fill(deal.symbol)
            if confirmed is not None and confirmed[0] > 0 and confirmed[1] > 0:
                deal.total_qty = confirmed[0]
                deal.average_entry = confirmed[1]
                deal.filled_safety_count += 1
            else:
                self._average_in(deal, mark, so_qty_r)
        else:
            self._average_in(deal, mark, so_qty_r)

        # Advance the next trigger to the next cumulative deviation level.
        if deal.filled_safety_count < len(levels):
            deal.next_trigger_price = self._deviation_price(
                deal.side, deal.base_price, levels[deal.filled_safety_count]
            )
        logger.info(
            "DCA %s SO#%d filled %.6g @ %.6g -> avg %.6g, qty %.6g.",
            deal.symbol, deal.filled_safety_count, so_qty_r, mark,
            deal.average_entry, deal.total_qty,
        )

    async def _close_deal(self, bot, deal: DcaDeal, exit_price: float,
                          reason: str) -> None:
        """Close the WHOLE position via reduceOnly; record ONE deal trade.

        The close order result is CHECKED: if the exchange rejects the
        reduceOnly close, the deal is left intact and retried next tick — we
        never mark a deal closed (and never free its slot) while the position
        is still live on the exchange.
        """
        if self._m._broker.connected:
            close_side = "SELL" if deal.side == "LONG" else "BUY"
            res = await self._m._broker.place_order(
                symbol=deal.symbol, side=close_side, order_type="MARKET",
                quantity=str(deal.total_qty), reduce_only=True,
            )
            if not isinstance(res, dict) or not res.get("ok"):
                reason_txt = res.get("reason") if isinstance(res, dict) else res
                logger.error(
                    "DCA %s close REJECTED (%s) — deal kept open, will retry.",
                    deal.symbol, reason_txt,
                )
                return
        direction = 1.0 if deal.side == "LONG" else -1.0
        gross = (float(exit_price) - deal.average_entry) * deal.total_qty * direction
        # NET of fees+slippage; base + all safety orders + close are MARKET (taker).
        pnl, _cost = net_pnl(gross, deal.average_entry, float(exit_price), deal.total_qty)
        outcome = "win" if pnl >= 0 else "loss"

        if self._m._db is not None and deal.trade_id is not None:
            await self._m._db.close_bot_trade(
                trade_id=deal.trade_id, exit_price=float(exit_price), pnl=pnl,
                outcome=outcome, reason=reason,
            )
        self._deals.pop(deal.symbol, None)
        self._m._apply_completed_trade(bot, pnl)
        bot.stats.open_positions = len(self._deals)
        logger.info("DCA %s closed (%s) pnl=%.6g (avg %.6g, qty %.6g).",
                    deal.symbol, reason, pnl, deal.average_entry, deal.total_qty)
        if self._m._trainer is not None:
            await self._m._trainer.maybe_retrain(bot.id)

    # --- Helpers ---------------------------------------------------------
    def _target_price(self, deal: DcaDeal) -> float:
        cfg_pct = self._target_pct
        return deal.average_entry * (
            1 + cfg_pct / 100.0 if deal.side == "LONG" else 1 - cfg_pct / 100.0
        )

    @property
    def _target_pct(self) -> float:
        bot = self._m.get("dca")
        return float(_dca_cfg(bot)["target_profit_pct"])

    @staticmethod
    def _average_in(deal: DcaDeal, fill_price: float, so_qty: float) -> None:
        """Local volume-weighted average-in (safe-mode / unconfirmed fallback)."""
        new_total = deal.total_qty + so_qty
        deal.average_entry = (
            (deal.average_entry * deal.total_qty) + (fill_price * so_qty)
        ) / new_total
        deal.total_qty = new_total
        deal.filled_safety_count += 1

    @staticmethod
    def _deviation_price(side: str, ref: float, dev_pct: float) -> float:
        """Price `dev_pct`% adverse from `ref` (down for long, up for short)."""
        frac = dev_pct / 100.0
        return ref * (1 - frac) if side == "LONG" else ref * (1 + frac)

    def _ladder_fits(self, loaded_notional: float, equity: float,
                     available: float, leverage: int) -> bool:
        """True iff the fully-loaded ladder respects per-symbol + margin caps."""
        s = self._m._risk._s
        max_symbol = equity * (s.max_notional_per_symbol_pct / 100.0)
        if loaded_notional > max_symbol:
            return False
        # Required margin for the fully-loaded position must fit the safe slice.
        required_margin = loaded_notional / max(1, int(leverage))
        if available > 0 and required_margin > available * 0.5:
            return False
        # Gross-exposure ceiling (single bot, single symbol here).
        if loaded_notional > equity * leverage:
            return False
        return True

    async def _place_market(self, symbol: str, side: str, qty: float) -> bool:
        """Place a base/SO MARKET order; True only on a confirmed ok result."""
        order_side = "BUY" if side == "LONG" else "SELL"
        try:
            res = await self._m._broker.place_order(
                symbol=symbol, side=order_side, order_type="MARKET",
                quantity=str(qty),
            )
        except Exception as exc:
            logger.error("DCA %s market order raised: %s", symbol, exc)
            return False
        if not isinstance(res, dict) or not res.get("ok"):
            logger.error("DCA %s market order failed: %s", symbol,
                         res.get("reason") if isinstance(res, dict) else res)
            return False
        return True


def _dca_cfg(bot) -> dict:
    """Merge DCA defaults with the bot's config (config overrides defaults)."""
    merged = dict(DEFAULT_DCA_CONFIG)
    for k in DEFAULT_DCA_CONFIG:
        if k in bot.config and bot.config[k] is not None:
            merged[k] = bot.config[k]
    return merged


def _round_qty(price: float, qty: float, filters) -> Optional[float]:
    """Round qty to filters (maxQty/step/min-notional); None if rejected."""
    if filters is None:
        return qty if qty > 0 else None
    result = normalize_order(Decimal(str(price)), Decimal(str(qty)), filters)
    return float(result["qty"]) if result else None
