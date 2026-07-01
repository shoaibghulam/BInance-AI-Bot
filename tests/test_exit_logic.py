"""SL/TP exit-direction tests — inverted stops are the classic ruin bug.

Asserts long AND short stop-loss / take-profit fire on the correct side of the
mark, and that a position with neither level set never force-exits.
"""

from types import SimpleNamespace

from app.bots.manager import BotManager


def _bot(bot_id="day-trading"):
    return SimpleNamespace(id=bot_id)


def _snap(side, sl=None, tp=None):
    return {"side": side, "entry_price": 100.0, "stop_price": sl,
            "take_profit_price": tp, "opened_at_monotonic": None}


def test_long_stop_loss_fires_below_stop():
    snap = _snap("LONG", sl=90, tp=110)
    assert BotManager._exit_reason(_bot(), snap, 89) == "stop_loss"


def test_long_take_profit_fires_above_target():
    snap = _snap("LONG", sl=90, tp=110)
    assert BotManager._exit_reason(_bot(), snap, 111) == "take_profit"


def test_long_holds_inside_band():
    snap = _snap("LONG", sl=90, tp=110)
    assert BotManager._exit_reason(_bot(), snap, 100) is None


def test_short_stop_loss_fires_above_stop():
    snap = _snap("SHORT", sl=110, tp=90)
    assert BotManager._exit_reason(_bot(), snap, 111) == "stop_loss"


def test_short_take_profit_fires_below_target():
    snap = _snap("SHORT", sl=110, tp=90)
    assert BotManager._exit_reason(_bot(), snap, 89) == "take_profit"


def test_no_levels_never_exits():
    snap = _snap("LONG", sl=None, tp=None)
    assert BotManager._exit_reason(_bot(), snap, 1e9) is None
