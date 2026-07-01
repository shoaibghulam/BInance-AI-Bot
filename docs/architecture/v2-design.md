# The Trader — v2 Architecture & Contract (Scanner + Self-Learning ML + Pro Tabs)

> Builds on [system-overview.md](system-overview.md) and the existing `app/` foundation (config, broker, risk, kill switch, persistence, WS). **Extend, don't rewrite** those. This doc is the source of truth for the new pieces and the expanded API/WS/UI contract.

## What's new in v2
1. **Market scanner** — each bot no longer trades only BTC. It scans the **top ~30 USDⓈ-M perps by 24h volume**, computes indicators per symbol, applies the bot's entry filters, ranks the survivors, and only then trades. Re-ranked each cycle.
2. **Self-learning ML** — a **LightGBM** win-probability model (fallback: XGBoost → scikit-learn `LogisticRegression`, so it always runs). Every **closed trade** becomes a labeled example (win=1/loss=0). The model **retrains on its own trade history** on a schedule → it learns from its mistakes. The model acts as a **gate**: a scanned setup is only taken if `win_prob ≥ threshold`.
3. **Per-bot detail tabs** — Overview + one tab per bot, each showing: Performance, Live indicators & signals, Scanner results, AI model insight.
4. **Pro UI/UX** — tabbed/sidebar navigation, dense but clean trading-terminal feel, global KILL SWITCH persists across tabs.

---

## Backend components (new modules under `app/`)

```
app/
├── scanner/service.py     # top-N by 24h volume; per-symbol indicators; filter+rank per strategy
├── ml/
│   ├── features.py        # OHLCV+indicators → feature vector (SHARED by train & inference; no lookahead)
│   ├── model.py           # WinProbModel: train()/predict_proba()/save()/load()/metrics()/feature_importance()
│   └── trainer.py         # collect closed trades → labels → retrain on schedule; track accuracy over time
├── bots/manager.py        # EXTEND: per-bot stats, scanner-driven symbol selection, ML gate, per-bot positions
└── persistence/db.py      # EXTEND: trades(bot_id,symbol,side,entry,exit,pnl,outcome,features_json,opened/closed),
                           #         model_metrics(bot_id,ts,accuracy,auc,n_samples), scan_snapshots
```

**Indicator set (per scanned symbol):** EMA(50/200), RSI(14), MACD, Bollinger(20,2), ATR(14), VWAP, 24h volume, % change. Each bot defines which conditions form its entry filter (see [the-five-bots.md](../strategies/the-five-bots.md)). Compute with pandas (pandas-ta optional).

**ML feature vector** (per candidate setup): normalized indicator values + distances (price vs EMAs, %B, RSI level, MACD hist sign, ATR%, volume z-score, session flag). **Label** = outcome of the trade taken on that setup (TP hit=1 / SL hit=0). Time-ordered; never shuffle; fit scalers on train slice only.

**ML gate flow per bot tick:** scanner ranks coins → for each top candidate build features → `model.predict_proba()` → keep setups with `win_prob ≥ MIN_SIGNAL_CONFIDENCE` → risk engine sizes → broker places order (testnet). On close, record trade + features + outcome → trainer retrains when enough new samples (e.g. every 25 closed trades, min 50 to start; until then the bot uses pure indicator rules and the model reports "warming up").

**Safety unchanged:** all global risk caps, per-bot leverage cap, kill switch, testnet-default still apply. ML only *filters* entries; it never overrides risk limits or sizing.

---

## Expanded REST API (adds to [api-contract.md](../api-contract.md))

Bot `id`s: `day-trading`, `scalping`, `grid`, `dca`, `rebalancing` (use the strategy name as id in v2).

### `GET /api/bots/{id}` — full bot detail
```json
{ "id": "day-trading", "type": "day_trading", "status": "running", "enabled": true,
  "leverage": 5, "margin_type": "ISOLATED", "universe": "top30_volume",
  "config": { "timeframe": "15m", "stop_loss_atr": 1.5, "take_profit_r": 2.0,
              "indicators": ["EMA50/200","RSI14","MACD","VWAP","ATR14"], "min_win_prob": 0.55 },
  "performance": { "trades_total": 42, "wins": 25, "losses": 17, "win_rate": 0.595,
                   "realized_pnl": 18.4, "unrealized_pnl": 2.1, "max_drawdown_pct": 6.2,
                   "open_positions": 1, "equity_curve": [[ts, equity], ...] },
  "updated_at": "..." }
```

**Multi-order strategies (`grid`, `dca`) add a `strategy_state` key** to this payload
(other bots omit it). The `config` block also carries that strategy's extra tunables.

- **`dca`** — safety-order / averaging (martingale) model. Extra config:
  `base_order_pct`, `safety_order_count`, `safety_order_deviation_pct`,
  `safety_order_step_scale`, `safety_order_volume_scale`, `target_profit_pct`,
  `max_deviation_pct`. State:
  ```json
  "strategy_state": { "open_deals": [
    { "symbol": "SOLUSDT", "side": "LONG", "avg_entry": 145.2,
      "safety_orders_filled": 2, "target_price": 146.6, "total_qty": 0.875 } ] }
  ```
  A "deal" is one averaging cycle: a base MARKET order, then up to
  `safety_order_count` MARKET safety orders triggered at progressively wider
  adverse deviations (`deviation × step_scale^n`) with martingale sizing
  (`base × volume_scale^n`). Take-profit is measured off the **average** entry;
  on TP (or the `max_deviation_pct` hard stop) the **whole** position closes via
  reduceOnly and is recorded as ONE closed trade. The base is sized so the
  fully-loaded ladder stays within the per-symbol notional + margin/gross caps —
  if it can't fit, the deal is rejected.

- **`grid`** — real grid ladder. Extra config: `grid_levels`, `grid_span_atr`,
  `grid_mode`, `grid_stop_buffer_pct`. State:
  ```json
  "strategy_state": { "active": true, "symbol": "SOLUSDT", "band_low": 141.0,
    "band_high": 149.0, "active_levels": 5, "filled_levels": 3, "net_qty": 0.4 }
  ```
  Band = `[price − grid_span_atr×ATR14, price + grid_span_atr×ATR14]`, arithmetic
  spacing across `grid_levels`. Resting LIMIT buys below / sells above price; a
  filled buy seeds a sell one level up (and vice versa) — each completed
  round-trip is one closed trade. Total grid notional stays within the
  per-symbol cap. If price exits the band beyond `grid_stop_buffer_pct`, all grid
  orders are cancelled and the net position is flattened via reduceOnly.
```

### `GET /api/bots/{id}/scanner` — ranked scan results
```json
{ "scanned_at": "...", "universe_size": 30,
  "results": [ { "symbol": "SOLUSDT", "rank": 1, "score": 0.82, "passed": true,
                 "filters": { "trend_ema": true, "rsi_pullback": true, "vwap_reclaim": true, "atr_ok": true },
                 "win_prob": 0.61, "last_price": 145.2, "vol_24h": 1.2e9, "change_pct": 3.4 }, ... ] }
```

### `GET /api/bots/{id}/indicators` — live indicator states (top candidates)
```json
{ "ts": "...", "symbols": [ { "symbol": "SOLUSDT",
    "indicators": { "ema50": 143.1, "ema200": 138.4, "rsi14": 38.2, "macd_hist": 0.12,
                    "bb_pctb": 0.21, "atr14": 2.1, "vwap": 144.0, "price": 145.2 },
    "conditions": { "above_ema200": true, "rsi_oversold_cross": true, "macd_flip_up": true },
    "entry_ready": true } ] }
```

### `GET /api/bots/{id}/trades` — trade history & performance
```json
{ "trades": [ { "id": 101, "symbol": "SOLUSDT", "side": "LONG", "entry_price": 145.2,
   "exit_price": 148.9, "qty": 0.5, "pnl": 1.85, "outcome": "win", "win_prob": 0.61,
   "opened_at": "...", "closed_at": "...", "reason": "take_profit" } ],
  "summary": { "trades_total": 42, "win_rate": 0.595, "avg_win": 2.1, "avg_loss": -1.2, "profit_factor": 1.8 } }
```

### `GET /api/bots/{id}/ml` — AI model insight
```json
{ "status": "trained", "model": "lightgbm", "n_samples": 320, "trained_at": "...",
  "metrics": { "accuracy": 0.61, "auc": 0.64, "history": [[ts, accuracy], ...] },
  "feature_importance": [ ["rsi14", 0.21], ["dist_ema200", 0.18], ["macd_hist", 0.12], ... ],
  "live_predictions": [ { "symbol": "SOLUSDT", "win_prob": 0.61 } ],
  "min_win_prob": 0.55 }
```
`status` ∈ `warming_up | training | trained | error`. While `warming_up` (too few samples) the bot trades on pure indicator rules and `live_predictions` may be empty.

### `POST /api/bots/{id}/ml/train` — trigger a retrain → `{ "ok": true, "status": "training" }`
### `POST /api/bots/{id}/config` — update that bot's tunables (timeframe, SL/TP, min_win_prob, leverage) within global caps.

Existing endpoints (`/api/status`, `/api/account`, `/api/positions`, `/api/bots`, start/stop, `/api/config`, `/api/kill`, `/api/kill/reset`) stay as-is. `GET /api/bots` (list) stays the summary view used by the Overview tab.

## Expanded WebSocket `/ws`
Add frame `type`s (sent only for the currently-active bot, which the client announces): the client may send `{ "type": "subscribe", "bot": "day-trading" }`. Server then also pushes:
- `scanner` → `{ bot, results:[...] }` (every ~5s)
- `indicators` → `{ bot, symbols:[...] }` (every ~5s)
- `ml` → `{ bot, ...ml insight... }` (on retrain / periodically)
Plus the existing `account/positions/status/bots/log/equity` global frames.

---

## Frontend — pro tabbed UI (rebuild `frontend/`)
- **Left sidebar (or top tabs):** `Overview` + 5 bot tabs labeled by strategy with a status dot each. Global top bar (env badge, WS dot, equity, **KILL SWITCH**) persists across all tabs.
- **Overview tab:** the existing dashboard — account cards, all positions, equity sparkline, log console, all-bots summary grid with start/stop.
- **Each bot tab** (4 panels):
  1. **Header/controls:** status pill, Start/Stop, leverage, timeframe, SL/TP, `min_win_prob` (editable → `POST /api/bots/{id}/config`).
  2. **Performance:** trades, win rate (with bar), realized+unrealized PnL, profit factor, max drawdown, per-bot equity curve (canvas), recent trades table (with outcome + the win_prob the model gave).
  3. **Scanner results:** ranked table of the top-30 with pass/fail filter chips per coin, score, win_prob, last price, 24h vol, %chg. Highlight the ones `entry_ready`.
  4. **Live indicators:** per-candidate indicator values + condition checkmarks + `entry_ready` flag.
  5. **AI insight:** model status/badge, accuracy + AUC, accuracy-over-time mini chart, feature-importance bar chart (hand-drawn canvas), live win-prob per setup, **Retrain** button (`POST /api/bots/{id}/ml/train`).
- Same dark pro theme, vanilla HTML/CSS/JS, no deps, safe-mode clean, responsive, accessible.

## Honest expectation
The ML model needs **50+ closed testnet trades** before it predicts meaningfully, and profitability is never guaranteed — it must beat the pure-indicator baseline net of fees on out-of-sample data before being trusted. The UI surfaces this ("warming up", accuracy over time) so it's never a black box.
