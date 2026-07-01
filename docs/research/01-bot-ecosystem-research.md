# Research: Trading-Bot Ecosystem & Reference Repos

> Raw research findings (cited). Synthesized into [../architecture/](../architecture/) and the recommendation below.
> Star counts approximate, as of late June 2026.

## Reference repos

### Trade-With-Claude/cbt-framework
- **What it is:** "CBT" = Claude-Based Trading. An AI-powered backtesting/strategy-development framework that runs *inside Claude Code* (not a standalone bot; a set of slash commands + agents). Automates strategy idea → backtest → optimization → deployment. [1]
- **How it uses Claude/LLM:** A Claude Code extension — Claude drives 21 slash commands (`/cbt:discover`, `/cbt:research`, `/cbt:eda`, `/cbt:plan`, `/cbt:build`, `/cbt:run`, `/cbt:optimize`, `/cbt:live`). Claude generates strategy code from build plans, runs analysis, validates research via MCP servers. State persists in YAML + session-handoff docs.
- **Architecture:** Dual engine — Pandas (default, <1M rows) or "Fast Engine" (Polars + NumPy + Numba). Structured dirs for data/src/experiments/visualizations/trade-logs.
- **Exchange / Futures / testnet:** Unified deployment over Bybit, Binance, Kraken, Hyperliquid. Binance includes **Spot, USDT-M, COIN-M futures**. **Testnet not mentioned** — safety relies on paper-trading default, drawdown kill-switches, position-size limits, rate limiting, env-file creds. [1]
- **Stats:** Python 88.8% / JS 11.2%. **License: MIT.** **~55 stars, 19 forks** (young). Requires Claude Code CLI, Node 16+, Python 3.8+.

### atilaahmettaner/tradingview-mcp
- **What it is:** An MCP server giving AI assistants (Claude, ChatGPT, Cursor) access to market data + TA tools — **no TradingView account/API key required.** [2]
- **How it "bridges" TradingView:** Does **not** scrape/automate the TradingView UI. Fetches market data server-side from public endpoints and re-implements TradingView-style indicator methodologies — "TradingView-flavored TA," independent of the platform.
- **MCP tools (30+):**
  - *Backtesting:* `backtest_strategy`, `compare_strategies`, `walk_forward_backtest_strategy` (9 strategies: RSI, Bollinger, MACD, EMA cross, Supertrend, Donchian, +3).
  - *Real-time / sentiment:* `yahoo_price`, `market_snapshot`, `market_sentiment`, `financial_news`, `combined_analysis`.
  - *TA:* `get_technical_analysis`, `get_bollinger_band_analysis`, `screen_stocks`, `get_candlestick_patterns`, `get_multi_timeframe_analysis`.
- **Stats:** Python 99.7%. **License: MIT.** **~3.3k stars, 706 forks.** Active. Pip-installable or self-hosted; paid cloud ($9–$29/mo). **Read/analysis only — does NOT place orders**, not a Binance Futures execution engine. [2]

## Ecosystem comparison

| Repo | Stars | License | Binance USDⓈ-M Futures? | Binance Testnet? | Strategies (our 5) | Backtest? | Verdict |
|---|---|---|---|---|---|---|---|
| [freqtrade](https://github.com/freqtrade/freqtrade) | ~51.9k | **GPL-3.0** | Yes (USDT-M) | Partial — futures testnet broken via CCXT (SAPI key error [4]); use dry-run | Day/scalp/DCA via custom strategies + FreqAI; no native grid/rebalance | Yes (strong) | **Port patterns, don't fork** (GPL). Best reference for exchange handling + backtesting. |
| [hummingbot](https://github.com/hummingbot/hummingbot) | ~19k | **Apache-2.0** | Yes (perpetual) | **Yes** — native `binance_perpetual_testnet` connector [5] | Market-making, grid, DCA via Strategy V2; rebalancing scriptable | Yes (V2 + Dashboard) | **Strongest adopt candidate** — permissive, testnet, futures, modular. |
| [jesse](https://github.com/jesse-ai/jesse) | ~8.1k | **MIT** | Yes (spot + USDT futures) | Paper trading, not Binance testnet | Custom code strategies (scalp/DCA); no turnkey grid | Yes (best-in-class, no look-ahead) | **Adopt/port** — MIT, excellent backtester. |
| [OctoBot](https://github.com/Drakkar-Software/OctoBot) | ~6.2k | **GPL-3.0** | Yes (via CCXT) | Not documented | Grid, DCA, TradingView signals, AI; no explicit scalping | Yes | **Reference only** (GPL). Good grid/DCA UX. |
| [passivbot](https://github.com/enarjord/passivbot) | ~2k | **Unlicense (public domain)** | Yes (perpetual) | Not mentioned | **Grid + DCA + martingale** (specialty) | Yes (evolutionary optimizer) | **Fork-friendly** — best source for grid/DCA math. |
| [OpenTrader](https://github.com/bludnic/opentrader) | ~1k | Apache-2.0 | via CCXT (spot-centric) | Paper trading | **Grid + DCA**, multi-bot UI | Yes | TS stack — UI/UX reference. |
| [tradingview-mcp](https://github.com/atilaahmettaner/tradingview-mcp) | ~3.3k | MIT | No (analysis only) | N/A | Signal/indicator source | Yes (signal backtests) | **Adopt as signal/MCP layer** — not execution. |
| [cbt-framework](https://github.com/Trade-With-Claude/cbt-framework) | ~55 | MIT | Yes (USDT-M) | Not mentioned | Strategy dev harness | Yes | **Reference** — Claude-Code workflow; too young to depend on. |

## Recommendation

**Build our own thin FastAPI framework, and selectively port — do not fork a heavyweight engine.**

1. **License safety.** GPL-3.0 (freqtrade, OctoBot) is viral — forking obligates open-sourcing. Lean on **permissive** ones: **hummingbot (Apache-2.0)**, **jesse (MIT)**, **passivbot (Unlicense/public-domain)**.
2. **Architecture:**
   - **Execution layer:** Thin wrapper over **`python-binance`** (native USDⓈ-M futures + testnet `https://testnet.binancefuture.com`). Lesson from freqtrade: Binance *futures testnet* is finicky through CCXT, so a direct python-binance client is more reliable for testnet-first dev.
   - **Backtesting:** Study/port **jesse** (MIT, no look-ahead); **passivbot** (public domain) for grid/DCA math.
   - **Strategy modularity:** Mirror **hummingbot Strategy V2** controller/executor pattern → maps to our 5 types.
   - **Signals (optional):** Plug in **tradingview-mcp** (MIT) for TA/sentiment — convenient since we're already in a Claude environment.
3. **If you'd rather not build the engine:** Adopt **hummingbot** — only full framework that is permissive (Apache-2.0) + Binance USDⓈ-M futures + working perpetual **testnet** + modular. Trade-off: heaviest.
4. **cbt-framework** worth watching as a Claude-Code-driven dev-workflow model, too immature to depend on.

**Bottom line:** Thin FastAPI/python-binance core (testnet-first) + ported MIT/Apache/public-domain strategy & backtest logic. Keep GPL projects as design references only.

## Sources
1. https://github.com/Trade-With-Claude/cbt-framework
2. https://github.com/atilaahmettaner/tradingview-mcp
3. https://github.com/freqtrade/freqtrade
4. https://github.com/freqtrade/freqtrade/issues/6909 (Binance futures testnet SAPI-key NotSupported)
5. https://hummingbot.org/exchanges/binance/ (`binance_perpetual_testnet` connector)
6. https://github.com/hummingbot/hummingbot
7. https://docs.jesse.trade/docs/supported-exchanges/
8. https://github.com/jesse-ai/jesse
9. https://github.com/Drakkar-Software/OctoBot
10. https://github.com/enarjord/passivbot
11. https://hummingbot.org/dashboard/backtest/
12. https://www.freqtrade.io/en/stable/sandbox-testing/
13. https://coincodecap.com/open-source-trading-bots-on-github
14. https://github.com/botcrypto-io/awesome-crypto-trading-bots
