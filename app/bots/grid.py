"""Grid trading bot mechanics.

Places a ladder of resting LIMIT orders across an ATR-defined band and profits
from oscillation: each filled buy seeds a sell one level up (and vice versa);
the inter-level spacing minus fees is the per-fill profit. Reference:
docs/strategies/the-five-bots.md §3.

Risk (risk-management.md): total grid notional (sum of all levels) must stay
within max_notional_per_symbol; per-level qty is sized accordingly. If price
exits the band beyond a stop buffer, all grid orders are cancelled and the net
position is flattened via reduceOnly. All prices/quantities are rounded with the
symbol filters.

Fill detection compares the broker's open orders between ticks: an order that
was open last tick and is gone this tick (and not cancelled by us) is treated as
filled. Each completed buy→sell (or sell→buy) round-trip is one closed trade.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from app.broker.precision import round_price, round_qty_down, validate_min_notional

logger = logging.getLogger("trader.bots.grid")

# Symbols whose grid setup fails (bad filters / degenerate band / order errors)
# are put on this cooldown so we don't retry + spam the API every tick.
GRID_FAIL_COOLDOWN_S = 300.0  # 5 minutes
# A grid must support at least this many levels to be worth running (else the
# coin is skipped for one the band can grid properly).
GRID_MIN_LEVELS = 3
# Each level's spacing must clear this multiple of the round-trip maker fee, so
# every completed oscillation nets at least (mult-1)x the fee.
GRID_MIN_SPACING_MULT = 2.0

DEFAULT_GRID_CONFIG = {
    "grid_levels": 10,  # MAX levels; the build uses as many as profitably fit
    "grid_span_atr": 2.0,       # band half-width in ATR multiples
    "grid_mode": "neutral",     # neutral | long | short
    "grid_stop_buffer_pct": 1.0,  # extra % beyond the band before flatten
}


@dataclass
class GridOrder:
    """A single resting grid order."""

    order_id: str
    side: str        # BUY | SELL
    price: float
    qty: float
    level: int       # 0..grid_levels-1


@dataclass
class GridState:
    """Active grid for one symbol."""

    symbol: str
    band_low: float
    band_high: float
    level_prices: list[float]
    per_level_qty: float
    orders: dict[str, GridOrder] = field(default_factory=dict)  # order_id -> order
    filled_levels: int = 0
    net_qty: float = 0.0          # signed inventory (long +, short −)
    avg_cost: float = 0.0         # avg price of current inventory
    realized: float = 0.0


class GridManager:
    """Drives the grid ladder for the `grid` bot. One active grid at a time."""

    def __init__(self, bot_manager) -> None:
        self._m = bot_manager
        self._grid: Optional[GridState] = None
        # symbol -> monotonic deadline until which the symbol is skipped after a
        # failed grid setup (prevents per-tick retry spam on bad symbols).
        self._cooldown: dict[str, float] = {}

    # --- Introspection ---------------------------------------------------
    def strategy_state(self) -> dict:
        """Surface grid state for GET /api/bots/grid detail."""
        g = self._grid
        if g is None:
            return {"active": False, "band_low": None, "band_high": None,
                    "active_levels": 0, "filled_levels": 0}
        return {
            "active": True,
            "symbol": g.symbol,
            "band_low": round(g.band_low, 8),
            "band_high": round(g.band_high, 8),
            "active_levels": len(g.orders),
            "filled_levels": g.filled_levels,
            "net_qty": round(g.net_qty, 8),
        }

    def reset(self) -> None:
        """Drop in-memory grid state (used on kill/halt so a restart is clean).

        The kill switch already cancels orders + flattens on the exchange; this
        clears our stale mirror so the grid rebuilds fresh instead of managing a
        phantom position.
        """
        self._grid = None

    async def _live_position_amt(self, symbol: str) -> float:
        """Signed live position amount for a symbol (0.0 if flat / safe mode)."""
        try:
            for p in await self._m._broker.get_positions():
                if p.get("symbol") == symbol:
                    return float(p.get("positionAmt", 0) or 0)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("live position fetch failed for %s: %s", symbol, exc)
        return 0.0

    def held_symbols(self) -> set[str]:
        """Symbols to exclude from candidates: the active grid + cooled-down."""
        now = time.monotonic()
        # Expire stale cooldowns lazily.
        self._cooldown = {s: d for s, d in self._cooldown.items() if d > now}
        excluded = set(self._cooldown.keys())
        if self._grid:
            excluded.add(self._grid.symbol)
        return excluded

    # --- Tick ------------------------------------------------------------
    async def tick(self, bot, candidates: list[dict]) -> None:
        """Manage the active grid, or build a new one from the best candidate."""
        if self._grid is not None:
            await self._manage_grid(bot, self._grid)
            bot.last_signal = "HOLD"
            bot.touch()
            return

        # Candidates already exclude cooled-down symbols (via held_symbols()).
        if not candidates:
            bot.last_signal = "HOLD"
            bot.touch()
            return
        # Try candidates BEST-FIRST until one supports a full ladder. A coin whose
        # tick size can't represent the configured levels (e.g. a micro-price
        # coin) is skipped + cooled down so the grid lands on a coin it can
        # actually grid with the maximum number of levels.
        ranked = sorted(
            candidates,
            key=lambda r: (r.get("win_prob") or 0.0, r.get("score", 0.0)),
            reverse=True,
        )
        built = False
        for row in ranked:
            if await self._build_grid(bot, row):
                built = True
                break
            self._cooldown[row["symbol"]] = time.monotonic() + GRID_FAIL_COOLDOWN_S
        bot.last_signal = "BUY" if built else "HOLD"
        bot.touch()

    # --- Grid lifecycle --------------------------------------------------
    async def _build_grid(self, bot, row: dict) -> bool:
        """Define the band, size per-level qty within caps, place LIMIT orders."""
        cfg = _grid_cfg(bot)
        symbol = row["symbol"]
        price = float(row.get("last_price") or 0.0)
        atr = float((row.get("_indicators") or {}).get("atr14") or 0.0)
        if price <= 0 or atr <= 0:
            return False

        span = float(cfg["grid_span_atr"]) * atr
        band_low = price - span
        band_high = price + span
        if band_low <= 0:
            return False
        levels_cap = int(cfg["grid_levels"])  # interpreted as the MAX levels
        if levels_cap < 2:
            return False

        # Total grid notional <= per-symbol cap; split across levels.
        account = await self._m._account()
        equity = float(account.get("equity", 0) or 0)
        available = float(account.get("available", 0) or 0)
        if equity <= 0:
            return False
        max_symbol = equity * (self._m._risk._s.max_notional_per_symbol_pct / 100.0)
        # Respect margin cap too: total notional/leverage <= 50% available.
        margin_cap_notional = (available * 0.5 * bot.leverage) if available > 0 else max_symbol
        total_notional = min(max_symbol, margin_cap_notional, equity * bot.leverage)
        if total_notional <= 0:
            return False

        filters = await self._m._broker.get_exchange_filters(symbol)
        band_width = band_high - band_low

        # MAXIMIZE the number of grids: use as many levels as profitably AND
        # technically fit the band, capped by grid_levels. The binding limits:
        #  - spacing: each level's gap must clear the round-trip cost (profit),
        #  - tick:    each level must be a distinct tickSize step (representable),
        #  - afford:  each level's notional must meet min-notional.
        from app.config import settings as _s
        round_trip_maker = 2.0 * (_s.maker_fee_pct / 100.0)
        min_spacing_price = price * GRID_MIN_SPACING_MULT * round_trip_maker
        max_by_spacing = (int(band_width // min_spacing_price) + 1
                          if min_spacing_price > 0 else levels_cap)
        if filters is not None and filters.tick_size > 0:
            max_by_tick = int(band_width / float(filters.tick_size))
        else:
            max_by_tick = levels_cap
        if filters is not None and filters.min_notional > 0:
            max_by_afford = int(total_notional // float(filters.min_notional))
        else:
            max_by_afford = levels_cap
        levels = max(2, min(levels_cap, max_by_spacing, max_by_tick, max_by_afford))
        if levels < GRID_MIN_LEVELS:
            logger.info(
                "Grid %s: band fits only %d levels (spacing=%d tick=%d afford=%d; "
                "need >=%d) — skipping for a coin that grids better.",
                symbol, levels, max_by_spacing, max_by_tick, max_by_afford,
                GRID_MIN_LEVELS,
            )
            return False
        per_level_notional = total_notional / levels
        per_level_qty = per_level_notional / price

        # Build the level prices ROUNDED to tickSize, dropping any that round to
        # 0 or collide with an adjacent level.
        step = band_width / (levels - 1)
        raw_prices = [band_low + i * step for i in range(levels)]
        level_prices = self._rounded_levels(raw_prices, filters)
        # After rounding, still require a meaningful ladder (allow at most one
        # level lost to rounding); else skip for a coin that grids cleanly.
        if len(level_prices) < max(GRID_MIN_LEVELS, levels - 1):
            logger.info(
                "Grid %s: band [%.8g, %.8g] yields only %d distinct levels after "
                "rounding; skipping for a fuller-ladder coin.",
                symbol, band_low, band_high, len(level_prices),
            )
            return False

        grid = GridState(symbol=symbol, band_low=band_low, band_high=band_high,
                         level_prices=level_prices, per_level_qty=per_level_qty)

        # Place resting LIMIT orders: buys below price, sells above (neutral).
        # Prices/qty are pre-rounded to the symbol filters here.
        placed = 0
        had_error = False
        for level, lvl_price in enumerate(level_prices):
            if abs(lvl_price - price) < step * 0.25:
                continue  # skip the level straddling current price
            side = "BUY" if lvl_price < price else "SELL"
            order, errored = await self._place_limit(
                symbol, side, lvl_price, per_level_qty, level, filters
            )
            if order is not None:
                grid.orders[order.order_id] = order
                placed += 1
            elif errored:
                had_error = True

        if placed == 0 or had_error:
            # Any order error → cancel whatever we placed and abandon (cooldown
            # is applied by the caller). Never leave a half-built grid resting.
            if placed and self._m._broker.connected:
                await self._m._broker.cancel_all(symbol)
            logger.warning(
                "Grid %s: setup failed (placed=%d, error=%s); abandoning + "
                "cooling down %.0fs.", symbol, placed, had_error, GRID_FAIL_COOLDOWN_S,
            )
            return False
        self._grid = grid
        bot.trades_today += 1
        bot.stats.open_positions = 1
        logger.info("Grid %s built: band [%.6g, %.6g], %d levels, %d orders.",
                    symbol, band_low, band_high, levels, placed)
        return True

    async def _manage_grid(self, bot, grid: GridState) -> None:
        """Detect fills, seed opposite orders, and flatten if band is breached."""
        mark = await self._m._fetch_mark(grid.symbol)
        if mark is None or mark <= 0:
            return

        # Stop: price beyond band + buffer -> cancel all + flatten.
        cfg = _grid_cfg(bot)
        buf = float(cfg["grid_stop_buffer_pct"]) / 100.0
        if mark < grid.band_low * (1 - buf) or mark > grid.band_high * (1 + buf):
            await self._teardown_grid(bot, grid, mark, "band_exit")
            return

        filters = await self._m._broker.get_exchange_filters(grid.symbol)
        if self._m._broker.connected:
            # An order missing from open-orders may be FILLED, CANCELED, or
            # EXPIRED. CONFIRM each disappeared order's real status before
            # treating it as a fill — misreading a cancel/expiry as a fill
            # fabricates inventory and PnL (the phantom-fill bug).
            open_now = await self._m._broker.get_open_orders(grid.symbol)
            open_ids = {str(o.get("orderId")) for o in open_now}
            gone = [o for oid, o in list(grid.orders.items()) if oid not in open_ids]
            for order in gone:
                info = await self._m._broker.get_order(grid.symbol, order.order_id)
                status = (info or {}).get("status")
                if status == "FILLED":
                    # Use the exchange's real fill price/qty if available.
                    avg = float((info or {}).get("avgPrice") or 0) or order.price
                    exq = float((info or {}).get("executedQty") or 0) or order.qty
                    order.price, order.qty = avg, exq
                    await self._on_fill(bot, grid, order, filters)
                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    # CONFIRMED dead (e.g. cancelled by a kill switch / manually)
                    # — drop from tracking, do NOT apply as a fill.
                    grid.orders.pop(order.order_id, None)
                # status None/unknown (API hiccup / rate-limit ban): KEEP the
                # order — never drop on uncertainty (that orphaned real orders).

            # RECONCILE to exchange truth: if the ladder is empty (all orders
            # gone), the grid can't oscillate. Sync inventory to the LIVE
            # position and either flatten a real leftover or reset to rebuild —
            # this recovers from an external flatten (kill switch / manual) that
            # left our in-memory state stale.
            if not grid.orders:
                live_amt = await self._live_position_amt(grid.symbol)
                if abs(live_amt) > 0:
                    grid.net_qty = live_amt
                    await self._teardown_grid(bot, grid, mark, "ladder_empty")
                else:
                    logger.info("Grid %s: ladder empty and flat on exchange — "
                                "resetting to rebuild a fresh grid.", grid.symbol)
                    self._grid = None
                    bot.stats.open_positions = 0
        else:
            # Safe mode / simulation: treat any order the mark has crossed as filled.
            crossed = [o for oid, o in list(grid.orders.items()) if _crossed(o, mark)]
            for order in crossed:
                await self._on_fill(bot, grid, order, filters)

    async def _on_fill(self, bot, grid: GridState, order: GridOrder,
                       filters) -> None:
        """Apply a filled order: update inventory, seed the opposite level."""
        grid.orders.pop(order.order_id, None)
        grid.filled_levels += 1

        signed = order.qty if order.side == "BUY" else -order.qty
        prev_qty = grid.net_qty
        new_qty = prev_qty + signed

        # Round-trip realized pnl when a fill reduces/closes inventory.
        if prev_qty != 0 and (prev_qty > 0) != (signed > 0):
            closed_qty = min(abs(prev_qty), abs(signed))
            direction = 1.0 if prev_qty > 0 else -1.0
            gross = (order.price - grid.avg_cost) * closed_qty * direction
            # NET of fees; both grid legs are resting LIMIT fills (maker, no slip).
            pnl, _cost = net_pnl(gross, grid.avg_cost, order.price, closed_qty,
                                 entry_maker=True, exit_maker=True)
            grid.realized += pnl
            self._m._apply_completed_trade(bot, pnl)
            if self._m._db is not None:
                await self._record_roundtrip(bot, grid, order, closed_qty, pnl)
            if abs(signed) <= abs(prev_qty):
                pass  # inventory shrank; avg_cost unchanged
            else:
                grid.avg_cost = order.price  # flipped; new basis
        else:
            # Adding to inventory: update volume-weighted avg cost.
            total = abs(prev_qty) + abs(signed)
            grid.avg_cost = (
                (grid.avg_cost * abs(prev_qty)) + (order.price * abs(signed))
            ) / total if total > 0 else order.price
        grid.net_qty = new_qty

        # Seed the opposite order one level away to capture the spacing.
        step = (grid.band_high - grid.band_low) / (len(grid.level_prices) - 1)
        if order.side == "BUY":
            new_price, new_side = order.price + step, "SELL"
        else:
            new_price, new_side = order.price - step, "BUY"
        if grid.band_low <= new_price <= grid.band_high:
            seeded, _ = await self._place_limit(grid.symbol, new_side, new_price,
                                                grid.per_level_qty, order.level, filters)
            if seeded is not None:
                grid.orders[seeded.order_id] = seeded

    async def _teardown_grid(self, bot, grid: GridState, mark: float,
                             reason: str) -> None:
        """Cancel all grid orders + flatten net inventory via reduceOnly.

        The flatten order result is CHECKED: if the exchange rejects the close
        while inventory is non-zero, the grid is KEPT (not cleared) and torn
        down again next tick — we never abandon a grid whose net position is
        still live on the exchange (that was the orphaned-inventory bug).
        """
        if self._m._broker.connected:
            await self._m._broker.cancel_all(grid.symbol)
            # Reconcile inventory to the LIVE position so we never send a phantom
            # reduceOnly (which Binance rejects -2022) when an external flatten
            # already closed us.
            grid.net_qty = await self._live_position_amt(grid.symbol)
        close_side = "SELL" if grid.net_qty > 0 else "BUY"
        if self._m._broker.connected:
            if grid.net_qty != 0:
                res = await self._m._broker.place_order(
                    symbol=grid.symbol, side=close_side, order_type="MARKET",
                    quantity=str(abs(grid.net_qty)), reduce_only=True,
                )
                if not isinstance(res, dict) or not res.get("ok"):
                    reason_txt = res.get("reason") if isinstance(res, dict) else res
                    logger.error(
                        "Grid %s flatten REJECTED (%s) — grid kept, will retry "
                        "teardown next tick (net_qty=%.8g).",
                        grid.symbol, reason_txt, grid.net_qty,
                    )
                    return
        # Realize any remaining inventory at the mark (NET of costs; maker entries,
        # market flatten exit).
        if grid.net_qty != 0:
            direction = 1.0 if grid.net_qty > 0 else -1.0
            gross = (mark - grid.avg_cost) * abs(grid.net_qty) * direction
            pnl, _cost = net_pnl(gross, grid.avg_cost, mark, abs(grid.net_qty),
                                 entry_maker=True, exit_maker=False)
            grid.realized += pnl
            self._m._apply_completed_trade(bot, pnl)
            if self._m._db is not None:
                await self._record_roundtrip(
                    bot, grid,
                    GridOrder(order_id="flatten", side=close_side,
                              price=mark, qty=abs(grid.net_qty), level=-1),
                    abs(grid.net_qty), pnl,
                )
        logger.info("Grid %s torn down (%s): net realized %.6g over %d fills.",
                    grid.symbol, reason, grid.realized, grid.filled_levels)
        self._grid = None
        bot.stats.open_positions = 0

    # --- Helpers ---------------------------------------------------------
    @staticmethod
    def _rounded_levels(raw_prices: list[float], filters) -> list[float]:
        """Round each level price to tickSize, drop <=0 and adjacent duplicates.

        Returns distinct, ascending, positive level prices. Without filters
        (safe mode / unknown symbol) the raw prices are used as-is.
        """
        rounded: list[float] = []
        for p in raw_prices:
            if filters is not None:
                rp = float(round_price(p, filters.tick_size))
            else:
                rp = p
            if rp <= 0:
                continue
            # Skip a level that collides with the previous (rounded) level.
            if rounded and abs(rp - rounded[-1]) <= 0:
                continue
            rounded.append(rp)
        return rounded

    async def _place_limit(self, symbol: str, side: str, price: float, qty: float,
                           level: int, filters) -> tuple[Optional[GridOrder], bool]:
        """Round + place a resting LIMIT order.

        Returns `(order_or_None, errored)`. `errored` is True only when the
        broker rejected/raised — a level skipped for filter reasons (qty rounds
        to 0, below min-notional) returns `(None, False)` so it doesn't abandon
        the whole grid; a real order error returns `(None, True)`.
        """
        rp, rq = price, qty
        if filters is not None:
            rp = float(round_price(price, filters.tick_size))
            rq = float(round_qty_down(qty, filters.step_size))
            if filters.max_qty is not None and rq > float(filters.max_qty):
                rq = float(round_qty_down(filters.max_qty, filters.step_size))
            if rp <= 0 or rq < float(filters.min_qty) or rq <= 0:
                return None, False
            if not validate_min_notional(rp, rq, filters.min_notional):
                return None, False
        elif rq <= 0 or rp <= 0:
            return None, False

        if self._m._broker.connected:
            try:
                res = await self._m._broker.place_order(
                    symbol=symbol, side=side, order_type="LIMIT",
                    quantity=rq, price=rp, time_in_force="GTC",
                )
            except Exception as exc:
                logger.warning("Grid %s limit order raised: %s", symbol, exc)
                return None, True
            if not isinstance(res, dict) or not res.get("ok"):
                logger.warning("Grid %s limit order failed: %s", symbol,
                               res.get("reason") if isinstance(res, dict) else res)
                return None, True
            order_id = str(res.get("result", {}).get("orderId") or f"{side}-{level}")
        else:
            order_id = f"sim-{side}-{level}-{rp}"
        return GridOrder(order_id=order_id, side=side, price=rp, qty=rq, level=level), False

    async def _record_roundtrip(self, bot, grid: GridState, order: GridOrder,
                                qty: float, pnl: float) -> None:
        """Persist a grid round-trip as a closed trade row."""
        side = "LONG" if order.side == "SELL" else "SHORT"  # the inventory side closed
        outcome = "win" if pnl >= 0 else "loss"
        trade_id = await self._m._db.open_bot_trade(
            bot_id=bot.id, symbol=grid.symbol, side=side,
            entry_price=grid.avg_cost, qty=qty, win_prob=None,
            features={}, reason="grid_roundtrip",
        )
        if trade_id is not None:
            await self._m._db.close_bot_trade(
                trade_id=trade_id, exit_price=order.price, pnl=pnl,
                outcome=outcome, reason="grid_roundtrip",
            )


def _grid_cfg(bot) -> dict:
    """Merge grid defaults with the bot's config (config overrides defaults)."""
    merged = dict(DEFAULT_GRID_CONFIG)
    for k in DEFAULT_GRID_CONFIG:
        if k in bot.config and bot.config[k] is not None:
            merged[k] = bot.config[k]
    return merged


def _crossed(order: GridOrder, mark: float) -> bool:
    """Simulation fill rule: True if the mark has reached a resting order."""
    if order.side == "BUY":
        return mark <= order.price
    return mark >= order.price
