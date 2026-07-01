"""Normalize raw Binance payloads into the exact API-contract JSON shapes.

Keeps `main.py` thin. Every function tolerates empty/missing input (safe mode)
and returns the zeroed/empty contract shape rather than raising.
"""

from __future__ import annotations


def _f(value, default: float = 0.0) -> float:
    """Coerce to float, defaulting on None/blank/invalid."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_account(raw: dict, daily_pnl: float = 0.0) -> dict:
    """Map futures account info to the `GET /api/account` shape.

    Safe mode (`raw == {}`) yields all-zero fields.
    """
    balance = _f(raw.get("totalWalletBalance"))
    available = _f(raw.get("availableBalance"))
    unrealized = _f(raw.get("totalUnrealizedProfit"))
    margin_used = _f(raw.get("totalPositionInitialMargin"))
    equity = _f(raw.get("totalMarginBalance")) or (balance + unrealized)
    margin_ratio = (margin_used / equity) if equity > 0 else 0.0
    daily_pnl_pct = (daily_pnl / equity * 100.0) if equity > 0 else 0.0

    return {
        "balance": round(balance, 8),
        "available": round(available, 8),
        "unrealized_pnl": round(unrealized, 8),
        "margin_used": round(margin_used, 8),
        "margin_ratio": round(margin_ratio, 6),
        "equity": round(equity, 8),
        "daily_pnl": round(daily_pnl, 8),
        "daily_pnl_pct": round(daily_pnl_pct, 6),
    }


def normalize_positions(raw: list[dict]) -> list[dict]:
    """Map raw position-risk rows to the `GET /api/positions` list shape."""
    out: list[dict] = []
    for p in raw or []:
        amt = _f(p.get("positionAmt"))
        if amt == 0.0:
            continue
        entry = _f(p.get("entryPrice"))
        mark = _f(p.get("markPrice"))
        leverage = int(_f(p.get("leverage"), 1)) or 1
        unrealized = _f(p.get("unRealizedProfit"))
        notional = abs(amt) * mark
        cost_basis = abs(amt) * entry
        pnl_pct = (unrealized / cost_basis * 100.0) if cost_basis > 0 else 0.0
        out.append(
            {
                "symbol": p.get("symbol", ""),
                "side": "LONG" if amt > 0 else "SHORT",
                "size": abs(amt),
                "entry_price": round(entry, 8),
                "mark_price": round(mark, 8),
                "liquidation_price": round(_f(p.get("liquidationPrice")), 8),
                "leverage": leverage,
                "unrealized_pnl": round(unrealized, 8),
                "unrealized_pnl_pct": round(pnl_pct, 4),
                "margin_type": (p.get("marginType") or "ISOLATED").upper(),
                "notional": round(notional, 8),
            }
        )
    return out
