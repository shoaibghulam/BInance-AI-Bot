# The Trader

A self-learning **Binance USDⓈ-M Futures** trading bot system — **testnet-first**.
Five strategy bots (Day Trading, Scalping, Grid, DCA, Rebalancing) that **scan the top-30 coins by volume**, filter by technical indicators, and gate entries with a **self-training ML model** (LightGBM/scikit-learn) that learns from every closed trade. FastAPI backend + a pro, multi-tab dashboard (plain HTML/CSS/JS, no build step).

> ⚠️ **Leveraged futures can liquidate fast and lose more than your margin.** This runs on **testnet (fake money) by default**. Do not switch to mainnet until you've validated. Nothing here is financial advice. See [docs/strategies/risk-management.md](docs/strategies/risk-management.md).

---

## Requirements

- **Python 3.11+** (tested on 3.12)
- Windows, macOS, or Linux
- A free **Binance Futures testnet** account (for live data) — optional; the app runs in safe mode without keys.

---

## Quick start (Windows — easiest)

```powershell
cd "C:\Users\SHOAIB GHULAM\Documents\The Trader"
./run.ps1
```

`run.ps1` automatically: creates the virtual environment, installs dependencies, copies `.env.example` → `.env` on first run, and starts the server. Then open **http://127.0.0.1:8000/**.

---

## Manual setup (any OS)

```bash
# 1. From the project root, create + activate a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create your env file
cp .env.example .env      # Windows: copy .env.example .env

# 4. Run the server
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000/** in your browser.

> Tip: add `--reload` during development to auto-restart on code changes.

---

## Connecting to the Binance testnet (free, fake money)

Without keys the dashboard runs in **safe mode** (UI works, shows zeros, banner: *"Add Binance testnet keys"*). To get live testnet data and trading:

1. Go to **https://testnet.binancefuture.com** and log in (GitHub/email).
2. Open the **API Key** panel at the bottom of the trading screen and generate a key/secret.
3. Put them in your `.env`:
   ```ini
   TRADING_ENV=testnet
   BINANCE_TESTNET_KEY=your_key_here
   BINANCE_TESTNET_SECRET=your_secret_here
   ```
4. Restart the server. The env badge flips to **TESTNET (connected)** and your balance/positions stream in.

`.env` is git-ignored — your keys are never committed.

---

## Configuration (`.env`)

| Key | Meaning |
|---|---|
| `TRADING_ENV` | `testnet` (default) or `mainnet`. **Keep on testnet** until validated. |
| `BINANCE_TESTNET_KEY` / `_SECRET` | Testnet API credentials. |
| `BINANCE_MAINNET_KEY` / `_SECRET` | Leave empty until going live. |
| `MAX_LEVERAGE` | Hard cap on leverage (default 5). Bots cannot exceed it. |
| `RISK_PCT_PER_TRADE` | % of equity risked per trade (default 1.0). |
| `MAX_DAILY_LOSS_PCT` | Daily loss limit — trips the kill switch (default 4.0). |
| `MAX_ACCOUNT_DRAWDOWN_PCT` | Max peak-to-trough drawdown (default 20.0). |
| `MAX_CONCURRENT_POSITIONS` | Cap on simultaneous positions (default 5). |
| `MIN_SIGNAL_CONFIDENCE` | Min ML win-probability to take a setup (default 0.55). |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | Optional free local LLM (off by default). |
| `DB_PATH`, `HOST`, `PORT`, `LOG_LEVEL` | Ops settings. |

All risk caps are also editable per-bot in the dashboard (clamped to these global limits).

---

## Using the dashboard

- **Overview tab** — account balance, equity sparkline, open positions, all-bots summary, log console, and the global **KILL SWITCH** (cancels all orders, flattens via `reduceOnly`, halts bots).
- **Bot tabs** (Day Trading / Scalping / Grid / DCA / Rebalancing) — each shows:
  - **Controls** — start/stop + editable config (timeframe, SL×ATR, TP-R, leverage, min win-prob)
  - **Performance** — trades, win rate, PnL, drawdown, per-bot equity curve, recent trades
  - **Scanner** — the ranked top-30 coins with pass/fail filter chips
  - **Live indicators** — EMA/RSI/MACD/BB/ATR/VWAP + entry-condition checkmarks
  - **AI Insight** — model accuracy, feature importance, live win-probabilities, **Retrain** button

The AI model shows **"warming up"** until it has ~50 closed trades — until then bots use pure indicator rules.

---

## Project structure

```
The Trader/
├── app/                      # FastAPI backend
│   ├── main.py               # app, REST API, WebSocket, background scan loop
│   ├── config.py             # .env settings
│   ├── broker/               # python-binance wrapper (testnet) + Decimal precision
│   ├── market_data/          # klines / market data service
│   ├── scanner/              # top-30 universe, indicators, per-strategy filters
│   ├── signals/              # SignalEngine + indicator engine
│   ├── ml/                   # self-learning model: features, model, trainer
│   ├── risk/                 # position sizing, exposure caps, kill switch
│   ├── bots/                 # 5-bot manager + per-bot stats
│   ├── persistence/          # SQLite (trades, equity, model metrics)
│   └── api/                  # response normalization, WS connection manager
├── frontend/                 # dashboard (index.html, style.css, app.js)
├── docs/                     # architecture, strategy specs, API contract, research
├── data/                     # SQLite DB + trained models (created at runtime)
├── requirements.txt
├── run.ps1                   # one-command launcher (Windows)
└── .env.example              # copy to .env and fill in
```

---

## Optional extras (free, off by default)

- **Local LLM (Ollama):** install [Ollama](https://ollama.com), `ollama pull llama3.1`, set `OLLAMA_BASE_URL`/`OLLAMA_MODEL` in `.env`. Acts as a slow signal *gate* — no paid API.
- **Faster ML model:** `pip install lightgbm` (the model auto-uses it; falls back to scikit-learn otherwise).
- **Technical-indicator library:** `pip install pandas-ta` (the indicator engine falls back to plain pandas without it).

---

## Troubleshooting

- **Dashboard shows the kill-switch popup / looks broken** → hard-refresh the browser (**Ctrl + Shift + R**) to clear cached CSS/JS.
- **Badge says "Not connected"** → add testnet keys to `.env` and restart.
- **`-1003 / too many requests` in logs** → the scanner self-throttles (30s cache, concurrency caps, 60s ban back-off); it will recover automatically. Avoid running many instances against one IP.
- **`ModuleNotFoundError`** → activate the venv and re-run `pip install -r requirements.txt`.
- **Port 8000 in use** → change `PORT` in `.env` or pass `--port 8001` to uvicorn.

---

## Documentation

- System design → [docs/architecture/system-overview.md](docs/architecture/system-overview.md) · [docs/architecture/v2-design.md](docs/architecture/v2-design.md)
- Strategies & risk → [docs/strategies/](docs/strategies/)
- Binance API / testnet → [docs/integrations/binance-futures-api.md](docs/integrations/binance-futures-api.md)
- API contract → [docs/api-contract.md](docs/api-contract.md)

---

## Safety checklist before mainnet

1. Backtest + walk-forward validate each strategy.
2. Run on **testnet** for days — confirm fills, funding, kill switch, ML accuracy.
3. Only then set `TRADING_ENV=mainnet` with a **small** balance and low leverage.
4. Keep the kill switch and daily-loss limit active at all times.
