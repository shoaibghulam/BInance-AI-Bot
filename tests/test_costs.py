"""Trading-cost model tests — fees/slippage must reduce gross PnL.

Guards the fix that makes reported PnL and ML win/loss labels NET, not gross.
"""

from app.risk.costs import net_pnl, round_trip_cost


def test_round_trip_taker_costs_more_than_maker():
    taker = round_trip_cost(1000, 1000)  # market both legs
    maker = round_trip_cost(1000, 1000, entry_maker=True, exit_maker=True)
    assert taker > maker > 0


def test_net_pnl_is_below_gross_for_market_trade():
    net, cost = net_pnl(3.0, 100.0, 100.3, 10.0)  # 0.3% move, ~1000 notional
    assert cost > 0
    assert net < 3.0
    assert abs(net - (3.0 - cost)) < 1e-9


def test_thin_scalp_win_can_go_net_negative():
    # a 0.1% gross "win" on a market round-trip is eaten by ~0.12% costs
    net, cost = net_pnl(1.0, 100.0, 100.1, 10.0)
    assert net < 0  # gross +1.0 but net loss after fees+slippage


def test_maker_grid_cost_is_small():
    net, cost = net_pnl(3.0, 100.0, 100.3, 10.0, entry_maker=True, exit_maker=True)
    assert 0 < cost < round_trip_cost(1000, 1003)  # cheaper than taker
