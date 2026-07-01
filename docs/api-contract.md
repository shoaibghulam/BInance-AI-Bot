# API Contract (v0) — backend ⇄ frontend source of truth

FastAPI serves both the JSON API (`/api/*`), a WebSocket (`/ws`), and the static dashboard at `/`.
**Safe mode:** if no API keys are set, the backend still starts and serves the UI; `status.connected=false`
and account/positions return empty/zero. The UI must render cleanly in this state (show "Add testnet keys").

All money/number fields are JSON numbers (floats). All timestamps are ISO-8601 strings (UTC).

## REST endpoints

### `GET /api/status`
```json
{ "env": "testnet", "connected": true, "kill_switch_active": false,
  "server_time": "2026-06-30T12:00:00Z", "uptime_s": 1234 }
```

### `GET /api/account`
```json
{ "balance": 10000.0, "available": 9500.0, "unrealized_pnl": 25.5,
  "margin_used": 500.0, "margin_ratio": 0.05, "equity": 10025.5,
  "daily_pnl": 12.3, "daily_pnl_pct": 0.12 }
```

### `GET /api/positions`
```json
[ { "symbol": "BTCUSDT", "side": "LONG", "size": 0.01, "entry_price": 60000.0,
    "mark_price": 60500.0, "liquidation_price": 54000.0, "leverage": 5,
    "unrealized_pnl": 5.0, "unrealized_pnl_pct": 0.83, "margin_type": "ISOLATED",
    "notional": 605.0 } ]
```

### `GET /api/bots`
```json
[ { "id": "scalp-btc", "type": "scalping", "symbol": "BTCUSDT", "status": "stopped",
    "enabled": false, "leverage": 5, "pnl": 0.0, "trades_today": 0,
    "last_signal": "HOLD", "updated_at": "2026-06-30T12:00:00Z" } ]
```
Bot `type` ∈ `day_trading | scalping | grid | dca | rebalancing`. `status` ∈ `running | stopped | error`.

### `POST /api/bots/{id}/start` → `{ "ok": true, "bot": {<bot object>} }`
### `POST /api/bots/{id}/stop`  → `{ "ok": true, "bot": {<bot object>} }`

### `GET /api/config` / `POST /api/config`
Returns/accepts the global risk caps (from `.env`, runtime-overridable):
```json
{ "max_leverage": 5, "risk_pct_per_trade": 1.0, "max_daily_loss_pct": 4.0,
  "max_account_drawdown_pct": 20.0, "max_concurrent_positions": 5,
  "max_notional_per_symbol_pct": 30.0, "default_margin_type": "ISOLATED",
  "min_signal_confidence": 0.55 }
```

### `POST /api/kill`  — trigger the kill switch (cancel all orders, flatten via reduceOnly, halt bots)
`{ "ok": true, "actions": ["cancelled 3 orders", "flattened 1 position", "halted 5 bots"] }`

### `POST /api/kill/reset` — re-enable trading after a human review
`{ "ok": true, "kill_switch_active": false }`

## WebSocket `/ws`
Server pushes JSON frames `{ "type": <t>, "data": <payload>, "ts": <iso8601> }` where `type` ∈:
- `status`    → same shape as `GET /api/status`
- `account`   → same shape as `GET /api/account`
- `positions` → same shape as `GET /api/positions`
- `bots`      → same shape as `GET /api/bots`
- `log`       → `{ "level": "info|warn|error", "msg": "..." }`
- `equity`    → `{ "equity": 10025.5 }` (for the live equity sparkline)

Client may send `{ "type": "ping" }`; server replies `{ "type": "pong" }`.
Push cadence: account/positions every ~2s, status on change, log on event, equity each account tick.

## Errors
All errors: HTTP status + `{ "error": "message", "detail": "..." }`. Never leak secrets.
