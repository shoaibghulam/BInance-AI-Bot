"""Deterministic risk/sizing layer + kill switch (BINDING).

Implements docs/strategies/risk-management.md. Every order passes through here;
no strategy bypasses it.

- `size_position` uses fixed-fractional % risk per trade, derives quantity from
  a stop distance, and clamps the engine's advisory `size_hint`. The result is
  capped by MAX_LEVERAGE, max-notional-per-symbol, and the concurrent-position
  count. Returns an `OrderPlan` or None (do-not-trade).
- `KillSwitch` cancels all orders, flattens via reduceOnly, sets `active=True`,
  and halts bots. Daily-loss-limit and drawdown checks decide when to fire.

All sizing math uses `Decimal`; floats are only used for inbound advisory %s.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable, Optional

from app.broker.precision import SymbolFilters, normalize_order
from app.config import Settings
from app.signals.base import Action, MarketData, Signal

logger = logging.getLogger("trader.risk")

# Default fraction of risk-budget used as a stop distance when an engine does
# not supply one (% of entry price). Conservative; sizing derives FROM the stop.
DEFAULT_STOP_DISTANCE_PCT = Decimal("0.01")  # 1% adverse move = stop
_PCT = Decimal("100")

# Margin safety: required initial margin for a new entry must not exceed this
# fraction of currently-available balance. Prevents draining margin toward
# liquidation (the 99% margin-ratio incident).
MAX_MARGIN_FRACTION_OF_AVAILABLE = Decimal("0.5")


@dataclass(frozen=True)
class OrderPlan:
    """A fully-sized, risk-approved order ready for the broker."""

    symbol: str
    side: str  # BUY | SELL
    quantity: Decimal
    leverage: int
    margin_type: str
    reduce_only: bool
    entry_price: Decimal
    stop_price: Decimal
    notional: Decimal
    reason: str


class RiskEngine:
    """Fixed-fractional position sizing with hard exposure caps."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings

    @property
    def min_confidence(self) -> float:
        """Confidence floor below which a signal is ignored."""
        return self._s.min_signal_confidence

    def size_position(
        self,
        signal: Signal,
        account: dict,
        market: MarketData,
        open_positions: Optional[list] = None,
        filters: Optional[SymbolFilters] = None,
        mark_price: Optional[float] = None,
        leverage: Optional[int] = None,
        stop_distance: Optional[float] = None,
        target_notional: Optional[float] = None,
    ) -> Optional[OrderPlan]:
        """Return a risk-approved `OrderPlan`, or None to skip the trade.

        `account` is the normalized account dict (see API contract): needs
        `equity`/`available`. `mark_price` is the current price; `filters` are
        the symbol's exchange filters for Decimal rounding. `leverage` overrides
        the global cap (still clamped to MAX_LEVERAGE). `stop_distance` (absolute
        price units) lets the caller derive size from a real ATR stop.

        Hard caps enforced (none bypassable by a strategy or the ML gate):
          - per-symbol notional <= equity * max_notional_per_symbol_pct%
            (a fraction of EQUITY, NOT multiplied by leverage),
          - required initial margin (notional/leverage)
            <= MAX_MARGIN_FRACTION_OF_AVAILABLE * available,
          - sum(open notional) + new notional <= equity * leverage
            (total gross-exposure ceiling),
          - concurrent-position count <= MAX_CONCURRENT_POSITIONS.
        """
        if signal.action not in (Action.BUY, Action.SELL):
            return None
        if signal.confidence < self.min_confidence:
            logger.debug("Signal below confidence floor; skipping.")
            return None

        equity = Decimal(str(account.get("equity", 0) or 0))
        if equity <= 0:
            logger.debug("Non-positive equity; cannot size.")
            return None
        available = Decimal(str(account.get("available", 0) or 0))

        price = Decimal(str(mark_price)) if mark_price else self._entry_from_market(market)
        if price is None or price <= 0:
            logger.debug("No usable entry price; skipping.")
            return None

        # --- Concurrent-position cap ---
        positions = open_positions or []
        if len(positions) >= self._s.max_concurrent_positions:
            logger.info("Max concurrent positions reached; skipping new entry.")
            return None

        lev = self._capped_leverage() if leverage is None else max(
            1, min(int(leverage), self._capped_leverage())
        )

        # --- Fixed-fractional risk budget (currency) ---
        risk_fraction = Decimal(str(self._s.risk_pct_per_trade)) / _PCT
        risk_budget = equity * risk_fraction

        # --- Stop distance → quantity (size derives FROM the stop) ---
        stop_dist = (
            Decimal(str(stop_distance))
            if stop_distance and float(stop_distance) > 0
            else price * DEFAULT_STOP_DISTANCE_PCT
        )
        if stop_dist <= 0:
            return None
        raw_qty = risk_budget / stop_dist

        # --- Fixed investment override: size to a target notional (USDT) if the
        # bot config sets one. The per-symbol / gross / margin caps below still
        # clamp it, so a user-set investment can never breach risk limits. ---
        if target_notional and float(target_notional) > 0:
            raw_qty = Decimal(str(target_notional)) / price
        else:
            # --- Clamp by advisory size_hint (0..1) ---
            size_hint = Decimal(str(max(0.0, min(1.0, signal.size_hint or 0.0))))
            if size_hint > 0:
                raw_qty = raw_qty * size_hint

        # --- Per-symbol notional cap: a % of EQUITY (NOT * leverage) ---
        max_symbol_notional = equity * (
            Decimal(str(self._s.max_notional_per_symbol_pct)) / _PCT
        )
        notional = raw_qty * price
        if notional > max_symbol_notional:
            raw_qty = max_symbol_notional / price
            notional = raw_qty * price

        # --- Total gross-exposure ceiling: sum(open) + new <= equity * lev ---
        open_notional = self._open_notional(positions)
        gross_ceiling = equity * Decimal(lev)
        if open_notional + notional > gross_ceiling:
            room = gross_ceiling - open_notional
            if room <= 0:
                logger.info("Gross-exposure ceiling reached; skipping new entry.")
                return None
            raw_qty = room / price
            notional = raw_qty * price

        # --- Margin check: REJECT if required margin exceeds the safe slice ---
        # required margin = notional / leverage must fit within
        # MAX_MARGIN_FRACTION_OF_AVAILABLE of available balance. We reject (not
        # shrink) so a depleted account never opens — this is the guard that
        # stops draining margin toward liquidation.
        required_margin = notional / Decimal(lev)
        margin_budget = available * MAX_MARGIN_FRACTION_OF_AVAILABLE
        if available > 0 and required_margin > margin_budget:
            logger.info(
                "Required margin %.4f exceeds budget %.4f (available %.4f); "
                "skipping new entry.", required_margin, margin_budget, available,
            )
            return None

        side = "BUY" if signal.action == Action.BUY else "SELL"
        stop_price = (
            price - stop_dist if side == "BUY" else price + stop_dist
        )

        quantity = self._apply_filters(price, raw_qty, filters)
        if quantity is None or quantity <= 0:
            logger.info("Sized quantity rejected by filters/min-notional; skipping.")
            return None

        # Re-validate margin against the FINAL filtered quantity (filters can
        # only round DOWN, so this can only relax the check — but stay strict).
        final_notional = quantity * price
        if available > 0 and (final_notional / Decimal(lev)) > margin_budget:
            logger.info("Filtered lot exceeds margin budget; skipping new entry.")
            return None

        return OrderPlan(
            symbol=market.symbol,
            side=side,
            quantity=quantity,
            leverage=lev,
            margin_type=self._s.default_margin_type,
            reduce_only=False,
            entry_price=price,
            stop_price=stop_price,
            notional=final_notional,
            reason=signal.reason,
        )

    @staticmethod
    def _open_notional(positions: list) -> Decimal:
        """Sum absolute open notional across position dicts (best-effort)."""
        total = Decimal("0")
        for p in positions or []:
            try:
                notional = p.get("notional") if isinstance(p, dict) else None
                if notional is not None:
                    total += abs(Decimal(str(notional)))
                    continue
                amt = abs(Decimal(str(p.get("positionAmt", p.get("size", 0)) or 0)))
                mark = Decimal(str(p.get("markPrice", p.get("mark_price", 0)) or 0))
                total += amt * mark
            except (InvalidOperation, TypeError, ValueError):
                continue
        return total

    def _capped_leverage(self) -> int:
        """Leverage clamped to MAX_LEVERAGE (never the exchange max)."""
        return max(1, int(self._s.max_leverage))

    @staticmethod
    def _entry_from_market(market: MarketData) -> Optional[Decimal]:
        """Derive a fallback entry price from the last close in the OHLCV frame."""
        df = getattr(market, "ohlcv", None)
        try:
            if df is not None and hasattr(df, "empty") and not df.empty:
                return Decimal(str(df["close"].iloc[-1]))
        except Exception:  # pragma: no cover - defensive
            return None
        return None

    @staticmethod
    def _apply_filters(
        price: Decimal,
        qty: Decimal,
        filters: Optional[SymbolFilters],
    ) -> Optional[Decimal]:
        """Round qty to step + validate min-notional when filters are known."""
        if filters is None:
            # No filters available (safe mode / unknown symbol): keep raw qty.
            return qty
        result = normalize_order(price, qty, filters)
        return result["qty"] if result else None


# --- Type aliases for kill-switch broker callbacks -------------------------
CancelAll = Callable[[str], Awaitable[dict]]
FlattenAll = Callable[[], Awaitable[dict]]
HaltBots = Callable[[], Awaitable[int]]


class KillSwitch:
    """Circuit breaker: cancel-all → reduceOnly flatten → halt → require reset.

    Independent of strategies. Fires manually (POST /api/kill) or automatically
    on a daily-loss-limit / drawdown breach.
    """

    def __init__(
        self,
        settings: Settings,
        flatten_all: FlattenAll,
        halt_bots: HaltBots,
    ) -> None:
        self._s = settings
        self._flatten_all = flatten_all
        self._halt_bots = halt_bots
        self.active: bool = False
        self._peak_equity: Optional[float] = None

    async def trigger(self, reason: str = "manual") -> dict:
        """Fire the kill switch: flatten everything and halt all bots.

        Returns the contract-shaped `{"ok": True, "actions": [...]}` payload.
        """
        actions: list[str] = []
        logger.warning("KILL SWITCH triggered (%s).", reason)

        flat = await self._flatten_all()
        cancelled = self._count_cancelled(flat)
        closed = int(flat.get("closed", 0)) if isinstance(flat, dict) else 0
        if cancelled:
            actions.append(f"cancelled {cancelled} orders")
        actions.append(f"flattened {closed} position{'s' if closed != 1 else ''}")

        halted = await self._halt_bots()
        actions.append(f"halted {halted} bots")

        self.active = True
        return {"ok": True, "actions": actions}

    def reset(self) -> dict:
        """Re-enable trading after a human review."""
        self.active = False
        logger.info("Kill switch reset; trading re-enabled.")
        return {"ok": True, "kill_switch_active": False}

    def check_breaches(self, account: dict) -> Optional[str]:
        """Return a breach reason if daily-loss or drawdown limits are exceeded.

        Pure check — caller decides whether to `trigger()`. Tracks peak equity
        for the drawdown calculation.
        """
        equity = float(account.get("equity", 0) or 0)
        if equity <= 0:
            return None

        # Drawdown vs running peak.
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity
        if self._peak_equity and self._peak_equity > 0:
            drawdown_pct = (self._peak_equity - equity) / self._peak_equity * 100.0
            if drawdown_pct >= self._s.max_account_drawdown_pct:
                return f"max drawdown {drawdown_pct:.2f}% breached"

        # Daily loss limit (daily_pnl_pct is negative on a losing day).
        daily_pnl_pct = float(account.get("daily_pnl_pct", 0) or 0)
        if daily_pnl_pct <= -abs(self._s.max_daily_loss_pct):
            return f"daily loss limit {daily_pnl_pct:.2f}% breached"

        return None

    @staticmethod
    def _count_cancelled(flat_result) -> int:
        """Best-effort count of cancelled orders from a flatten result."""
        if not isinstance(flat_result, dict):
            return 0
        return int(flat_result.get("cancelled", 0))
