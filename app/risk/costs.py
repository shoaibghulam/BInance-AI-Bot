"""Trading-cost model: fees + slippage applied to every realized PnL.

Binance USDⓈ-M charges a fee on notional PER SIDE (taker for market fills,
maker for resting-limit fills). Market orders also suffer adverse slippage.
Every bot's close path runs its gross PnL through `round_trip_cost` so the
persisted PnL — and therefore the win/loss label the ML model trains on — is
NET of costs, not gross. Reference: docs/strategies/risk-management.md.

Costs are read from `settings` at call time (not captured at import) so a
config change is reflected without reconstructing callers.
"""

from __future__ import annotations

from app.config import settings


def _rate(maker: bool) -> float:
    """Fee rate as a fraction of notional for a maker or taker fill."""
    pct = settings.maker_fee_pct if maker else settings.taker_fee_pct
    return float(pct) / 100.0


def _slip_rate() -> float:
    """Slippage as a fraction of notional (applied to taker/market legs only)."""
    return float(settings.slippage_pct) / 100.0


def round_trip_cost(
    entry_notional: float,
    exit_notional: float,
    *,
    entry_maker: bool = False,
    exit_maker: bool = False,
) -> float:
    """Total cost (fees + slippage) of a round-trip, in quote currency.

    `*_maker=True` for legs that fill as resting LIMIT orders (grid), which pay
    the maker fee and incur no slippage. MARKET legs (default) pay taker + slip.
    Always >= 0.
    """
    en = abs(float(entry_notional))
    xn = abs(float(exit_notional))
    fee = en * _rate(entry_maker) + xn * _rate(exit_maker)
    slip = (0.0 if entry_maker else en * _slip_rate()) + (
        0.0 if exit_maker else xn * _slip_rate()
    )
    return fee + slip


def net_pnl(
    gross_pnl: float,
    entry_price: float,
    exit_price: float,
    qty: float,
    *,
    entry_maker: bool = False,
    exit_maker: bool = False,
) -> tuple[float, float]:
    """Return `(net_pnl, cost)` for a closed trade.

    `net_pnl = gross_pnl - round_trip_cost`. The cost is computed from the
    entry/exit notionals. Use the returned net value for both the persisted
    PnL and the win/loss outcome label.
    """
    entry_notional = float(entry_price) * abs(float(qty))
    exit_notional = float(exit_price) * abs(float(qty))
    cost = round_trip_cost(
        entry_notional, exit_notional, entry_maker=entry_maker, exit_maker=exit_maker
    )
    return float(gross_pnl) - cost, cost
