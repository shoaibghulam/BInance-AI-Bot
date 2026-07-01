# Configuration, Secrets & Safety Operations

> Local-first (Windows PC). Covers environment config, API-key handling, the testnet→mainnet promotion gate,
> and the kill-switch runbook. Pairs with [../strategies/risk-management.md](../strategies/risk-management.md).

## Environment config (`.env`, never committed)

A single source of truth, loaded by `app/config.py` (Pydantic `BaseSettings`). Ship a `.env.example` with **placeholder** values; the real `.env` is git-ignored.

```ini
# --- Environment selection ---
TRADING_ENV=testnet            # testnet | mainnet   (mainnet requires explicit change)
BINANCE_TESTNET_KEY=...        # from testnet.binancefuture.com (distinct from mainnet)
BINANCE_TESTNET_SECRET=...
BINANCE_MAINNET_KEY=           # leave EMPTY until ready for live
BINANCE_MAINNET_SECRET=

# --- Global risk caps (enforced by risk engine; bots cannot exceed) ---
MAX_LEVERAGE=5
RISK_PCT_PER_TRADE=1.0         # % of equity risked per trade
MAX_DAILY_LOSS_PCT=4.0         # kill switch trips here
MAX_ACCOUNT_DRAWDOWN_PCT=20.0
MAX_CONCURRENT_POSITIONS=5
MAX_NOTIONAL_PER_SYMBOL_PCT=30.0
DEFAULT_MARGIN_TYPE=ISOLATED
MIN_SIGNAL_CONFIDENCE=0.55

# --- Optional engines ---
TV_WEBHOOK_SECRET=             # long random string (TradingView alerts)
ANTHROPIC_API_KEY=             # only if using the LLM engine

# --- Ops ---
DB_PATH=./data/trader.db
LOG_LEVEL=INFO
```

## API-key handling rules

- **Never hardcode keys**; never commit `.env`; never put broker keys in a TradingView alert body.
- Create the testnet key/secret at `https://testnet.binancefuture.com` — they are **distinct** from mainnet keys. Mixing them → `-2015 invalid API-key`.
- For mainnet later: create the key with **Futures enabled, withdrawals DISABLED**, and **IP-restrict** it to your machine/VPS.
- Treat any leaked key as compromised → delete/rotate immediately on the Binance API page.
- Store the file with restrictive permissions; consider OS keychain / a secrets manager when moving to a VPS.

## Testnet → mainnet promotion gate

Mainnet is **off by default**. Promote only after the full ladder (see [risk-management § Backtesting](../strategies/risk-management.md#backtesting--validation)):

1. ✅ Backtested with **fees + slippage + funding** across bull/bear/range history.
2. ✅ Walk-forward validated (stable out-of-sample, not overfit).
3. ✅ **Testnet paper run** for enough days to observe funding settlements, kill switch, reconnects, precision rounding, and `reduceOnly` exits.
4. ✅ Live metrics on testnet match backtest within tolerance.
5. ➡️ Switch `TRADING_ENV=mainnet`, fund a **small** amount, keep leverage low, watch closely.
6. ➡️ Scale only after live mainnet results hold up.

**Hard rule:** flipping to mainnet should be a conscious, logged action (the app should log a loud warning and ideally require a confirm step), never a silent default.

## Kill switch / circuit breaker runbook

The kill switch is a first-class component, independent of any bot. It must be:

- **Auto-triggered** on: daily-loss limit breach (`MAX_DAILY_LOSS_PCT`), account drawdown breach, margin ratio approaching liquidation, API-error storm, price-feed gap, or fill/position divergence.
- **Manually triggerable** from the dashboard (a prominent **KILL** button) and from a CLI/endpoint.

**On trigger, in order:**
1. **Stop new entries** — disable all bots immediately.
2. **Cancel all open orders** — `DELETE /fapi/v1/allOpenOrders` per symbol.
3. **Flatten positions** — market `reduceOnly` close-all (or `closePosition=true` STOP_MARKET as backup).
4. **Halt** — stay disabled until a human re-enables (don't auto-resume).
5. **Alert** — log loudly + notify (desktop/email/webhook).

**Recovery checklist after a trip:**
- Confirm flat: `GET /fapi/v3/positionRisk` shows no positions; `GET /fapi/v1/openOrders` empty.
- Read the audit log to find the cause.
- Reconcile account equity vs expected; investigate any divergence before re-enabling.
- Re-enable bots only after the root cause is understood.

## Local run (Windows)

```powershell
# one-time
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # then fill in testnet keys

# run (testnet)
uvicorn app.main:app --reload --port 8000
# dashboard: http://localhost:8000
```

- The bot only trades while this process runs (local PC). For always-on, port to a VPS (systemd/Docker) later — the design keeps this clean.
- Keep the machine awake / disable sleep if running intraday sessions.

## Reliability notes (local-first)

- **Reconnect logic** on every WebSocket (klines + user-data); refresh the `listenKey` every ~30–60 min.
- **Idempotency** on order placement (client order IDs) so a reconnect/retry can't double-fire.
- **State recovery** on restart: on boot, reconcile open positions/orders from Binance against the local DB before resuming any bot.
- **Clock sync**: periodically offset local time vs `GET /fapi/v1/time` to avoid `-1021` recvWindow errors.
