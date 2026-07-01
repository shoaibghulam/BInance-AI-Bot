# System Architecture Overview

> **The Trader** — a personal automated trading system for **Binance USDⓈ-M Futures only** (no spot).
> Testnet-first. FastAPI backend + plain HTML/CSS frontend. Runs local-first, portable to a VPS later.

## Design principles

1. **Testnet by default, mainnet by explicit opt-in.** A single config flag selects the environment; mainnet requires a deliberate, logged switch. See [../operations/config-and-secrets.md](../operations/config-and-secrets.md).
2. **Risk layer is non-negotiable.** Every order passes through the deterministic risk/sizing layer defined in [../strategies/risk-management.md](../strategies/risk-management.md). No strategy bypasses it.
3. **Strategies and signals are pluggable.** Bots consume the [SignalEngine](signal-engines.md) interface; engines are interchangeable.
4. **Thin core, ported logic.** Build a thin FastAPI/`python-binance` core; port grid/DCA math & backtest design from permissive repos (hummingbot/jesse/passivbot) — see [../research/01-bot-ecosystem-research.md](../research/01-bot-ecosystem-research.md). Avoid GPL code (freqtrade/OctoBot) in this private codebase.

## Recommended tech stack

| Layer | Choice | Why |
|-------|--------|-----|
| Backend framework | **FastAPI** (async) | Async fits WebSocket streams + concurrent bots; great Pydantic validation. |
| Exchange client | **`python-binance`** (`AsyncClient`) | Active, async, native futures + `testnet=True`. See [../integrations/binance-futures-api.md](../integrations/binance-futures-api.md). |
| Indicators | **`pandas` + `pandas-ta`** (or TA-Lib) | Standard, fast, well-known. |
| ML (optional engine) | `scikit-learn` → `xgboost`/`lightgbm` | Baseline-first; trees as workhorse. |
| LLM (optional engine) | `anthropic` SDK, structured output | Gating/regime filter, not hot-path scalping. |
| Persistence | **SQLite** (local) → Postgres if it grows | Trades, config, bot state, audit log. Single-user local needs nothing heavier. |
| Realtime push to UI | FastAPI **WebSocket** / SSE | Live balance, positions, P&L, logs. |
| Frontend | **Plain HTML + CSS** (+ vanilla JS for fetch/WS) | Per requirement. Dashboard talks to the API. |
| Process mgmt (local) | `uvicorn` + a run script | VPS later: systemd / Docker / pm2. |
| Scheduling | `asyncio` tasks / `APScheduler` | Bot loops, funding checks, rebalance cron. |

## Component map

```
┌──────────────────────────────────────────────────────────────────────┐
│  Frontend (HTML/CSS/JS)  — dashboard: balances, positions, P&L,        │
│  bot on/off, config, live log, KILL SWITCH button                      │
└───────────────┬───────────────────────────────▲───────────────────────┘
        REST + WebSocket                          │ live updates
┌───────────────▼───────────────────────────────┴───────────────────────┐
│  FastAPI backend                                                        │
│  ┌────────────┐  ┌──────────────┐  ┌───────────────┐  ┌──────────────┐ │
│  │ API routes │  │ Bot manager  │  │ Signal engines│  │ Risk engine  │ │
│  │ /tv-webhook│→ │ (start/stop, │→ │ (a/b/c/d)     │→ │ sizing, caps,│ │
│  │ /bots /cfg │  │  schedules)  │  │               │  │ kill switch  │ │
│  └────────────┘  └──────┬───────┘  └───────────────┘  └──────┬───────┘ │
│  ┌────────────────────┐ │  ┌───────────────────────┐         │         │
│  │ Market data service│ │  │ Broker (python-binance│◄────────┘         │
│  │ (WS klines/mark/    │ │  │ AsyncClient): orders, │                   │
│  │  user-data stream)  │ │  │ leverage, margin, pos │                   │
│  └─────────┬──────────┘ │  └───────────┬───────────┘                   │
│            │            │              │                                │
│  ┌─────────▼────────────▼──────────────▼──────────┐   ┌──────────────┐ │
│  │ Persistence (SQLite): trades, config, state,    │   │ Audit/logging│ │
│  │ bot runs, funding, equity curve                 │   │ + alerts     │ │
│  └─────────────────────────────────────────────────┘   └──────────────┘ │
└───────────────┬─────────────────────────────────────────────────────────┘
                │ HTTPS REST + WSS
        ┌───────▼────────────────────────┐
        │ Binance USDⓈ-M Futures          │  testnet.binancefuture.com (default)
        │ (testnet → mainnet by config)   │  fapi.binance.com (opt-in)
        └─────────────────────────────────┘
```

## Suggested project structure

```
the-trader/
├── docs/                      # ← this documentation
├── app/
│   ├── main.py                # FastAPI app, routes, WS endpoints
│   ├── config.py              # env-driven settings (testnet flag, limits)
│   ├── broker/                # python-binance wrapper (orders, leverage, precision rounding)
│   ├── market_data/           # WS streams, kline cache, funding tracker
│   ├── signals/               # SignalEngine ABC + indicator/llm/webhook/ml engines
│   ├── risk/                  # sizing, exposure caps, liquidation buffer, kill switch
│   ├── bots/                  # day, scalping, grid, dca, rebalancing
│   ├── persistence/           # SQLite models + repository layer
│   └── backtest/              # backtester (cost+funding aware), walk-forward
├── frontend/                  # index.html, style.css, app.js (dashboard)
├── tests/                     # unit + integration (testnet)
├── .env.example               # documents required secrets (no real keys)
└── run.ps1 / run.sh           # local launch
```

## Data flow (one bot tick)

1. **Market data service** keeps closed candles + mark/funding fresh via WebSocket (preferred over REST polling — avoids rate-limit bans).
2. On a **closed bar**, the bot builds a `MarketData` (OHLCV + precomputed indicators + extras like funding/TV-rating/webhook).
3. The configured **SignalEngine** returns a `Signal`.
4. Below the confidence floor → do nothing. Otherwise the **risk engine** sizes the order (fixed-fractional %, leverage cap, exposure caps, liquidation buffer) — clamping the engine's advisory `size_hint`.
5. The **broker** rounds price/qty to symbol filters (`tickSize`/`stepSize`, `Decimal`), sets leverage/margin if needed, places the order (`reduceOnly` for exits).
6. Fills arrive on the **user-data stream**; persistence records the trade; the frontend updates live over WebSocket.
7. The **kill switch / circuit breaker** runs independently: daily-loss limit, margin-ratio watch, API-error storm → cancel-all + `reduceOnly` flatten + halt + alert.

## Build order (when coding starts — out of scope for this doc pass)

1. Broker wrapper + testnet connection + precision rounding (prove order/cancel on testnet).
2. Market data service (WS) + persistence.
3. Risk engine + kill switch (before any strategy).
4. SignalEngine ABC + IndicatorRuleEngine.
5. One bot end-to-end (day trading) on testnet.
6. Backtester (cost + funding aware) + walk-forward.
7. Remaining bots (scalping, grid, DCA, rebalancing).
8. Webhook, LLM, ML engines.
9. Frontend dashboard.
