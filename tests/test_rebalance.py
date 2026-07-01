"""Rebalancing basket tests — equal-weight build + drift-band no-churn.

Drives RebalanceManager in safe mode (broker disconnected, so no live orders)
with a minimal fake BotManager, asserting it builds an equal-weight K-leg
basket within caps and does NOT churn when legs are already on target.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.bots.rebalance import RebalanceManager


class _FakeBroker:
    connected = False

    async def get_exchange_filters(self, symbol):
        return None  # safe-mode: qty passes through unrounded


class _FakeM:
    def __init__(self):
        self._broker = _FakeBroker()
        self._risk = SimpleNamespace(_s=SimpleNamespace(max_notional_per_symbol_pct=30.0))
        self._db = None
        self.realized = 0.0

    async def _account(self):
        return {"equity": 1000.0, "available": 800.0}

    async def _fetch_mark(self, symbol):
        return _PRICES.get(symbol, 0.0)

    async def _confirm_fill(self, symbol):
        return None

    def _apply_completed_trade(self, bot, pnl):
        self.realized += pnl


_PRICES = {"AAA": 10.0, "BBB": 20.0, "CCC": 5.0, "DDD": 2.0, "EEE": 1.0}


def _bot():
    return SimpleNamespace(
        id="rebalancing", leverage=5,
        config={"basket_size": 4, "target_exposure_pct": 40.0,
                "rebalance_band_pct": 25.0, "rebalance_interval_s": 300.0,
                "min_trade_notional_pct": 0.5},
        stats=SimpleNamespace(open_positions=0), trades_today=0,
        last_signal="", touch=lambda: None,
    )


def _candidates():
    return [
        {"symbol": "AAA", "last_price": 10.0, "score": 0.9},
        {"symbol": "BBB", "last_price": 20.0, "score": 0.8},
        {"symbol": "CCC", "last_price": 5.0, "score": 0.7},
        {"symbol": "DDD", "last_price": 2.0, "score": 0.6},
        {"symbol": "EEE", "last_price": 1.0, "score": 0.5},
    ]


def test_builds_equal_weight_basket_within_caps():
    mgr = RebalanceManager(_FakeM())
    bot = _bot()
    asyncio.run(mgr._rebalance(bot, _candidates()))
    # basket_size=4 → exactly 4 legs (top scorers AAA..DDD).
    assert len(mgr._holdings) == 4
    assert set(mgr._holdings) == {"AAA", "BBB", "CCC", "DDD"}
    # equity 1000 * 40% = 400 gross / 4 legs = 100 notional each (equal weight).
    for h in mgr._holdings.values():
        assert abs(h.qty * h.avg_entry - 100.0) < 1e-6


def test_no_churn_when_on_target():
    mgr = RebalanceManager(_FakeM())
    bot = _bot()
    asyncio.run(mgr._rebalance(bot, _candidates()))
    qtys = {s: h.qty for s, h in mgr._holdings.items()}
    # Second rebalance with identical state: every leg on target → no trades.
    traded = asyncio.run(mgr._rebalance(bot, _candidates()))
    assert traded is False
    assert {s: h.qty for s, h in mgr._holdings.items()} == qtys


def test_per_leg_respects_symbol_notional_cap():
    mgr = RebalanceManager(_FakeM())
    bot = _bot()
    # 1 huge leg would be 400 gross, but per-symbol cap = 30% * 1000 = 300.
    bot.config["basket_size"] = 1
    asyncio.run(mgr._rebalance(bot, _candidates()))
    (h,) = list(mgr._holdings.values())
    assert h.qty * h.avg_entry <= 300.0 + 1e-6
