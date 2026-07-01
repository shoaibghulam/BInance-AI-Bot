# The Trader — System Documentation

A personal automated trading system for **Binance USDⓈ-M Futures only** (no spot).
**Testnet-first.** FastAPI backend · plain HTML/CSS frontend · runs local-first, portable to a VPS.

Supports five bots — **day trading, 1–5 min scalping, grid, DCA, rebalancing** — each driven by a
**pluggable signal engine**: technical-indicator rules, Claude/LLM signals, TradingView webhook alerts, or a trained ML model.

> ⚠️ **Risk notice.** Leveraged futures can liquidate fast and lose more than your margin. This system is for
> trading **your own** account, **testnet-first**. Nothing here is financial advice. Do not run on mainnet until
> you have completed the full validation ladder. Risk management ([strategies/risk-management.md](strategies/risk-management.md)) is binding.

## Read in this order

1. **[architecture/system-overview.md](architecture/system-overview.md)** — the big picture: design principles, tech stack, component map, project structure, data flow, build order.
2. **[architecture/signal-engines.md](architecture/signal-engines.md)** — the `SignalEngine` interface and the four pluggable backends (indicator / LLM / webhook / ML), with code + diagram.
3. **[strategies/the-five-bots.md](strategies/the-five-bots.md)** — spec for each of the five bots (entry/exit/config/futures concerns/failure modes).
4. **[strategies/risk-management.md](strategies/risk-management.md)** — ⭐ **the most important doc.** Position sizing, leverage caps, liquidation buffer, funding, kill switch, exposure caps, backtesting & validation.
5. **[integrations/binance-futures-api.md](integrations/binance-futures-api.md)** — testnet setup, auth/signing, REST endpoints, WebSocket streams, rate limits, `python-binance` choice, precision rounding.
6. **[operations/config-and-secrets.md](operations/config-and-secrets.md)** — `.env` config, API-key handling, testnet→mainnet promotion gate, kill-switch runbook, local run.

## Folder map

```
docs/
├── README.md                         # this index
├── architecture/
│   ├── system-overview.md            # stack, components, project structure, data flow
│   └── signal-engines.md             # pluggable SignalEngine (indicator/LLM/webhook/ML)
├── strategies/
│   ├── the-five-bots.md              # day · scalping · grid · DCA · rebalancing specs
│   └── risk-management.md            # BINDING risk rules + backtesting/validation
├── integrations/
│   └── binance-futures-api.md        # Binance USDⓈ-M Futures API + testnet
├── operations/
│   └── config-and-secrets.md         # env, secrets, promotion gate, kill switch, local run
└── research/
    └── 01-bot-ecosystem-research.md  # cited repo/ecosystem research + recommendation
```

## Key decisions (from research)

| Decision | Choice | Source |
|----------|--------|--------|
| Engine vs build | **Build thin FastAPI core**, port logic from permissive repos; avoid GPL (freqtrade/OctoBot) | [research/01](research/01-bot-ecosystem-research.md) |
| Exchange library | **`python-binance`** (async, native futures, `testnet=True`) | [binance-futures-api](integrations/binance-futures-api.md) |
| Reuse sources | hummingbot (Apache-2.0, has perpetual testnet), jesse (MIT, backtester), passivbot (public-domain, grid/DCA math) | [research/01](research/01-bot-ecosystem-research.md) |
| Signal design | One `SignalEngine` interface, 4 interchangeable backends; LLM **gates**, doesn't scalp | [signal-engines](architecture/signal-engines.md) |
| Risk | Isolated margin, ≤3–5× leverage, fixed-fractional ≤2%/trade, hard daily-loss kill switch | [risk-management](strategies/risk-management.md) |
| Environment | Testnet by default; mainnet by explicit, logged opt-in | [config-and-secrets](operations/config-and-secrets.md) |

## Reference repos

- **[Trade-With-Claude/cbt-framework](https://github.com/Trade-With-Claude/cbt-framework)** — Claude-Code strategy dev/backtest workflow (MIT). Model for using Claude as strategy *author/validator*.
- **[atilaahmettaner/tradingview-mcp](https://github.com/atilaahmettaner/tradingview-mcp)** — pull-based MCP server returning TradingView-style TA ratings (MIT). A *data input*, not an execution engine.
- Full ecosystem comparison (freqtrade, hummingbot, jesse, OctoBot, passivbot, …): [research/01](research/01-bot-ecosystem-research.md).

---
*Status: research + documentation phase complete. No code written yet — implementation begins from [architecture/system-overview.md § Build order](architecture/system-overview.md#build-order-when-coding-starts--out-of-scope-for-this-doc-pass).*
