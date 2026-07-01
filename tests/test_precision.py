"""Precision/rounding tests — the wire-format and sizing safety net.

A sci-notation price or an over-max quantity reaching Binance is a rejected
(or wrong) order. These assert the exact behaviors that prevent -1102 / -4005.
"""

from decimal import Decimal

from app.broker.precision import (
    SymbolFilters,
    format_decimal,
    normalize_order,
    round_qty_down,
)


def _filters(tick="0.0001", step="1", min_qty="1", max_qty="100", min_notional="5"):
    return SymbolFilters(
        symbol="TEST",
        tick_size=Decimal(tick),
        step_size=Decimal(step),
        min_qty=Decimal(min_qty),
        max_qty=Decimal(max_qty),
        min_notional=Decimal(min_notional),
    )


def test_format_decimal_micro_price_no_scientific_notation():
    assert format_decimal(1.7e-06) == "0.0000017"
    assert "e" not in format_decimal(0.000001234).lower()


def test_format_decimal_strips_trailing_dot():
    assert format_decimal(Decimal("100.")) == "100"


def test_round_qty_down_truncates():
    assert round_qty_down(1.999, "0.001") == Decimal("1.999")
    assert round_qty_down(1.0009, "0.001") == Decimal("1.000")


def test_normalize_order_clamps_to_max_qty():
    out = normalize_order(price=100, qty=15_000, filters=_filters(max_qty="100"))
    assert out is not None
    assert out["qty"] <= Decimal("100")


def test_normalize_order_rejects_below_min_notional():
    # price*qty = 1*1 = 1 < min_notional 5 -> None
    out = normalize_order(price=1, qty=1, filters=_filters(min_notional="5", min_qty="1"))
    assert out is None
