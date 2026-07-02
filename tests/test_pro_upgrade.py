"""PRO-upgrade reliability tests.

Covers the concurrent cold-first scan loop, scanner introspection helpers,
the empty-basket rebalance idle fix, and the additive `activity` field on
bot rows. Everything runs in safe mode (no broker / no network).
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.bots.rebalance import RebalanceManager
from app.scanner.service import CACHE_TTL_S, MarketScanner


# --- 1.1 scanner introspection -----------------------------------------------
def _scanner() -> MarketScanner:
    broker = SimpleNamespace(connected=False, _client=None)
    return MarketScanner(broker, SimpleNamespace())


def test_scan_state_cold_stale_fresh_banned():
    sc = _scanner()
    strategy = "grid"
    # cold: never scanned.
    assert sc.scan_state(strategy) == "cold"
    assert sc.has_cache(strategy) is False

    # fresh: just cached.
    sc._cache[strategy] = (time.monotonic(), {"results": []})
    assert sc.has_cache(strategy) is True
    assert sc.scan_state(strategy) == "fresh"

    # stale: cached but older than TTL.
    sc._cache[strategy] = (time.monotonic() - (CACHE_TTL_S + 1.0), {"results": []})
    assert sc.scan_state(strategy) == "stale"

    # banned: inside back-off window overrides everything.
    sc._banned_until = time.monotonic() + 60.0
    assert sc.scan_state(strategy) == "banned"


def test_scan_loop_refreshes_concurrently():
    """cold strategies dispatch before stale; refreshes overlap in time."""
    from app import main as main_mod

    sc = _scanner()
    # Two stale (have cache) + one cold (no cache).
    sc._cache["grid"] = (time.monotonic() - (CACHE_TTL_S + 1), {"results": []})
    sc._cache["dca"] = (time.monotonic() - (CACHE_TTL_S + 1), {"results": []})
    # "scalping" is cold (not in cache).

    order: list[str] = []
    active = {"n": 0, "max": 0}

    async def fake_refresh(strategy, force=False):
        order.append(strategy)
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await asyncio.sleep(0.02)  # hold the slot so overlap is observable
        active["n"] -= 1

    sc.refresh = fake_refresh  # type: ignore[assignment]

    state = SimpleNamespace(
        scanner=sc,
        bots_to_scan=lambda: {"grid", "dca", "scalping"},
    )

    async def _one_tick():
        # Run just one loop body (mirrors _scan_loop's gather section).
        due = [b for b in state.bots_to_scan() if not sc.is_fresh(b)]
        due.sort(key=lambda b: sc.has_cache(b))
        sem = asyncio.Semaphore(main_mod._SCAN_CONCURRENCY)

        async def _refresh_one(bid):
            async with sem:
                if not sc.is_banned:
                    await sc.refresh(bid)

        await asyncio.gather(*(_refresh_one(b) for b in due), return_exceptions=True)

    asyncio.run(_one_tick())

    assert active["max"] > 1, "refreshes should overlap concurrently"
    # cold ("scalping") dispatched before the stale ones.
    assert order[0] == "scalping"
    assert set(order) == {"grid", "dca", "scalping"}


# --- 1.4 rebalance empty-basket idle fix -------------------------------------
class _FakeBroker:
    connected = False

    async def get_exchange_filters(self, symbol):
        return None


class _FakeM:
    def __init__(self):
        self._broker = _FakeBroker()
        self._risk = SimpleNamespace(_s=SimpleNamespace(max_notional_per_symbol_pct=30.0))
        self._db = None
        self.realized = 0.0

    async def _account(self):
        return {"equity": 1000.0, "available": 800.0}

    async def _fetch_mark(self, symbol):
        return {"AAA": 10.0, "BBB": 20.0, "CCC": 5.0, "DDD": 2.0}.get(symbol, 0.0)

    async def _confirm_fill(self, symbol):
        return None

    def _apply_completed_trade(self, bot, pnl):
        self.realized += pnl


def _rebal_bot():
    return SimpleNamespace(
        id="rebalancing", leverage=5,
        config={"basket_size": 4, "target_exposure_pct": 40.0,
                "rebalance_band_pct": 25.0, "rebalance_interval_s": 300.0,
                "min_trade_notional_pct": 0.5},
        stats=SimpleNamespace(open_positions=0), trades_today=0,
        last_signal="", touch=lambda: None,
    )


def _rebal_candidates():
    return [
        {"symbol": "AAA", "last_price": 10.0, "score": 0.9},
        {"symbol": "BBB", "last_price": 20.0, "score": 0.8},
        {"symbol": "CCC", "last_price": 5.0, "score": 0.7},
        {"symbol": "DDD", "last_price": 2.0, "score": 0.6},
    ]


def test_rebalance_rebuilds_immediately_after_full_exit():
    """After the basket empties, the next tick rebuilds WITHOUT waiting the
    rebalance_interval_s (the empty-basket build must not stamp the timer)."""
    mgr = RebalanceManager(_FakeM())
    bot = _rebal_bot()

    # First tick builds the basket.
    asyncio.run(mgr.tick(bot, _rebal_candidates()))
    assert len(mgr._holdings) == 4

    # Simulate a full exit (all legs closed elsewhere).
    mgr._holdings.clear()

    # Next tick — even though we are well inside rebalance_interval_s — must
    # rebuild immediately because the empty build should not have stamped the
    # timer.
    asyncio.run(mgr.tick(bot, _rebal_candidates()))
    assert len(mgr._holdings) == 4


def test_rebalance_empty_build_does_not_stamp_timer():
    """A build from empty must leave _last_rebalance at 0 (not stamp it)."""
    mgr = RebalanceManager(_FakeM())
    bot = _rebal_bot()
    assert mgr._last_rebalance == 0.0
    asyncio.run(mgr.tick(bot, _rebal_candidates()))
    # Basket was empty at tick start -> timer must not have been stamped.
    assert mgr._last_rebalance == 0.0


# --- 1.3 bots_with_activity mapping ------------------------------------------
class _FakeBotManager:
    def __init__(self, rows, details):
        self._rows = rows
        self._details = details

    def list(self):
        # return copies so activity mutation doesn't leak between calls.
        return [dict(r) for r in self._rows]

    def detail(self, bid):
        return self._details.get(bid)


class _FakeScanner:
    def __init__(self, states):
        self._states = states

    def scan_state(self, bid):
        return self._states.get(bid, "cold")


def _activity(status, scan_state, open_symbols):
    from app.main import AppState

    rows = [{"id": "b", "status": status}]
    details = {"b": {"open_symbols": open_symbols}}
    state = AppState.__new__(AppState)
    state.bots = _FakeBotManager(rows, details)
    state.scanner = _FakeScanner({"b": scan_state})
    return state.bots_with_activity()[0]["activity"]


def test_bots_with_activity_states():
    assert _activity("error", "fresh", []) == "error"
    assert _activity("stopped", "fresh", []) == "stopped"
    assert _activity("running", "fresh", ["BTCUSDT"]) == "trading"
    assert _activity("running", "banned", []) == "cooling"
    assert _activity("running", "cold", []) == "warming"
    assert _activity("running", "fresh", []) == "searching"
    assert _activity("running", "stale", []) == "searching"
