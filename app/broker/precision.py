"""Decimal-based precision rounding for Binance USDⓈ-M Futures orders.

Rounding follows the exchange filters (NOT pricePrecision/quantityPrecision,
which are display hints):
- price -> nearest multiple of `tickSize` (PRICE_FILTER).
- quantity -> rounded DOWN to a multiple of `stepSize` (LOT_SIZE).
- min-notional -> `price * qty` must be >= MIN_NOTIONAL.

All arithmetic uses `Decimal` to avoid float artifacts that trigger -1111
(precision) and -4164 (notional) rejections.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional


def _to_decimal(value) -> Decimal:
    """Coerce str/int/float to Decimal via str() to avoid float binary noise."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def format_decimal(value) -> str:
    """Format a number as a PLAIN fixed-point decimal string for the API.

    Guarantees no scientific notation: a micro-price like 1.7e-06 becomes
    "0.00000170", never "1.7e-06" (which Binance rejects with -1102). Strips a
    trailing dot but preserves significant trailing zeros from the Decimal.
    """
    dec = _to_decimal(value)
    # `format(dec, 'f')` never uses exponent notation, unlike str(float).
    text = format(dec, "f")
    if text.endswith("."):
        text = text[:-1]
    return text


@dataclass(frozen=True)
class SymbolFilters:
    """Normalized order constraints parsed from exchangeInfo for one symbol."""

    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Optional[Decimal]
    min_notional: Decimal

    @classmethod
    def from_exchange_info(cls, symbol: str, symbol_info: dict) -> "SymbolFilters":
        """Build from a single `exchangeInfo.symbols[]` entry.

        Raises ValueError if the mandatory PRICE_FILTER / LOT_SIZE filters are
        missing — callers should treat that as "cannot size an order safely".
        """
        filters = {f.get("filterType"): f for f in symbol_info.get("filters", [])}

        price_filter = filters.get("PRICE_FILTER")
        lot_filter = filters.get("LOT_SIZE")
        if not price_filter or not lot_filter:
            raise ValueError(f"{symbol}: missing PRICE_FILTER/LOT_SIZE in exchangeInfo")

        notional_filter = filters.get("MIN_NOTIONAL") or {}
        # Binance uses "notional" (futures) or "minNotional" (some payloads).
        min_notional_raw = (
            notional_filter.get("notional")
            or notional_filter.get("minNotional")
            or "0"
        )

        # MARKET orders are bound by MARKET_LOT_SIZE (which usually has a SMALLER
        # maxQty than LOT_SIZE). We size MARKET entries, so the effective max is
        # the TIGHTER of the two maxQty values — this is what -4005 enforces.
        market_lot = filters.get("MARKET_LOT_SIZE") or {}
        lot_max = lot_filter.get("maxQty")
        market_max = market_lot.get("maxQty")
        max_candidates = [
            _to_decimal(v) for v in (lot_max, market_max) if v is not None
        ]
        effective_max = min(max_candidates) if max_candidates else None
        # The step we round to must also respect MARKET_LOT_SIZE's step.
        market_step = market_lot.get("stepSize")
        step_candidates = [_to_decimal(lot_filter["stepSize"])]
        if market_step is not None:
            step_candidates.append(_to_decimal(market_step))
        effective_step = max(step_candidates)  # coarser step is the binding one

        return cls(
            symbol=symbol,
            tick_size=_to_decimal(price_filter["tickSize"]),
            step_size=effective_step,
            min_qty=_to_decimal(lot_filter.get("minQty", "0")),
            max_qty=effective_max,
            min_notional=_to_decimal(min_notional_raw),
        )


def _round_to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    """Round `value` to the nearest multiple of `step` using `rounding` mode."""
    if step <= 0:
        return value
    quantized = (value / step).quantize(Decimal("1"), rounding=rounding) * step
    # Normalize to the step's exponent so the wire string has no spurious tail.
    return quantized.quantize(step.normalize(), rounding=rounding)


def round_price(price, tick_size) -> Decimal:
    """Round a price to the nearest multiple of `tickSize` (HALF_UP)."""
    try:
        return _round_to_step(_to_decimal(price), _to_decimal(tick_size), ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:  # defensive boundary
        raise ValueError(f"invalid price/tick_size: {price}/{tick_size}") from exc


def round_qty_down(qty, step_size) -> Decimal:
    """Round a quantity DOWN to a multiple of `stepSize` (never over-size)."""
    try:
        return _round_to_step(_to_decimal(qty), _to_decimal(step_size), ROUND_DOWN)
    except (InvalidOperation, ValueError) as exc:  # defensive boundary
        raise ValueError(f"invalid qty/step_size: {qty}/{step_size}") from exc


def validate_min_notional(price, qty, min_notional) -> bool:
    """True if `price * qty` meets the symbol's MIN_NOTIONAL floor."""
    return _to_decimal(price) * _to_decimal(qty) >= _to_decimal(min_notional)


def normalize_order(
    price,
    qty,
    filters: SymbolFilters,
) -> Optional[dict]:
    """Round price/qty to filters and validate min-qty + min-notional.

    Returns `{"price": Decimal, "qty": Decimal}` on success, or None if the
    resulting order would be rejected (below min qty or min notional).
    """
    rounded_price = round_price(price, filters.tick_size)
    rounded_qty = round_qty_down(qty, filters.step_size)

    # Clamp DOWN to maxQty FIRST so an over-max qty can never reach Binance
    # (the -4005 "Quantity greater than max quantity" rejection).
    if filters.max_qty is not None and rounded_qty > filters.max_qty:
        rounded_qty = round_qty_down(filters.max_qty, filters.step_size)

    if rounded_qty <= 0 or rounded_qty < filters.min_qty:
        return None
    # Re-assert the ceiling after rounding (defensive against rounding-up edge).
    if filters.max_qty is not None and rounded_qty > filters.max_qty:
        return None
    # If even the (clamped) max affordable lot cannot meet min-notional, skip.
    if not validate_min_notional(rounded_price, rounded_qty, filters.min_notional):
        return None

    return {"price": rounded_price, "qty": rounded_qty}
