"""Grid build tests — full-ladder selection, reject degenerate micro-price coins.

Drives GridManager._build_grid in safe mode (broker disconnected → sim orders,
no API) so we can prove: a normal-priced coin builds a near-full ladder, while a
micro-price coin whose tick is too coarse for the band is rejected (so the grid
moves on to a coin it can actually grid).
"""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from app.bots.grid import GridManager, DEFAULT_GRID_CONFIG
from app.broker.precision import SymbolFilters


def _filters(tick, step="0.001", min_qty="0.001", min_notional="5"):
    return SymbolFilters(
        symbol="X", tick_size=Decimal(tick), step_size=Decimal(step),
        min_qty=Decimal(min_qty), max_qty=Decimal("1000000000"),
        min_notional=Decimal(min_notional),
    )


class _FakeBroker:
    connected = False

    def __init__(self, fmap):
        self._fmap = fmap

    async def get_exchange_filters(self, symbol):
        return self._fmap.get(symbol)


class _FakeM:
    def __init__(self, fmap):
        self._broker = _FakeBroker(fmap)
        self._risk = SimpleNamespace(_s=SimpleNamespace(max_notional_per_symbol_pct=30.0))
        self._db = None

    async def _account(self):
        return {"equity": 10000.0, "available": 8000.0}


def _bot():
    return SimpleNamespace(
        id="grid", leverage=5, config=dict(DEFAULT_GRID_CONFIG),
        stats=SimpleNamespace(open_positions=0), trades_today=0,
        last_signal="", touch=lambda: None,
    )


def test_normal_coin_builds_full_ladder():
    fmap = {"NORM": _filters("0.01")}
    mgr = GridManager(_FakeM(fmap))
    bot = _bot()
    row = {"symbol": "NORM", "last_price": 200.0, "_indicators": {"atr14": 2.0}}
    built = asyncio.run(mgr._build_grid(bot, row))
    assert built is True
    assert mgr._grid is not None
    # near-full ladder: at least grid_levels-1 distinct levels.
    assert len(mgr._grid.level_prices) >= int(DEFAULT_GRID_CONFIG["grid_levels"]) - 1


def test_micro_price_coin_rejected_as_degenerate():
    # tick 1e-7 vs a band of ~0.22e-6 → only ~3 distinct levels → rejected.
    fmap = {"MICRO": _filters("0.0000001", step="1", min_qty="1")}
    mgr = GridManager(_FakeM(fmap))
    bot = _bot()
    row = {"symbol": "MICRO", "last_price": 1.7e-6, "_indicators": {"atr14": 5.5e-8}}
    built = asyncio.run(mgr._build_grid(bot, row))
    assert built is False
    assert mgr._grid is None


def test_tick_picks_full_ladder_coin_over_micro():
    # MICRO has the higher score but can't grid; tick() must skip it and build NORM.
    fmap = {"MICRO": _filters("0.0000001", step="1", min_qty="1"),
            "NORM": _filters("0.01")}
    mgr = GridManager(_FakeM(fmap))
    bot = _bot()
    candidates = [
        {"symbol": "MICRO", "last_price": 1.7e-6, "score": 0.99,
         "_indicators": {"atr14": 5.5e-8}},
        {"symbol": "NORM", "last_price": 200.0, "score": 0.5,
         "_indicators": {"atr14": 2.0}},
    ]
    asyncio.run(mgr.tick(bot, candidates))
    assert mgr._grid is not None
    assert mgr._grid.symbol == "NORM"
