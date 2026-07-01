# Risk Management (BINDING) & Validation

> **This is the most important document in the system.** It is binding for **all five bots**.
> Strategies are interchangeable; risk control is not. A bot may not trade unless it complies with every rule here.

---

## Position sizing

- **Default: fixed-fractional / fixed % risk per trade.** Risk a constant small fraction of equity per trade; size the position so `(stop distance × position size) = that fraction`. CFA-cited norm: **≤ ~2% of total capital per trade**. Fixed-fractional sizing empirically gives lower max drawdown and lower return volatility than naive sizing.
- **Kelly caution.** Full Kelly maximizes long-run geometric growth but can produce **>50% drawdowns even with positive expectancy**, and *overestimating your edge makes Kelly overbet toward ruin*. Use **fractional Kelly (½ or ¼)**: half-Kelly keeps ~75% of the growth with ~50% less drawdown.
- For **DCA/martingale and grid** bots, size on the *aggregate* (fully-loaded, post-all-safety-orders / all-grid-levels) position, **not** the base order.

## Stop-loss / take-profit / max-drawdown

- Every position has a **pre-defined stop-loss** (ATR-based or structural) and **take-profit**; size is derived *from* the stop, never the reverse.
- Enforce a **max account drawdown** (e.g. 15–20% peak-to-trough) that halts new entries and triggers de-risking.
- Trailing stops lock gains once in profit.

## Leverage caps — why high leverage liquidates fast

- Cap leverage **low** (default ≤ 3–5×, never the exchange max).
- Liquidation occurs when margin falls below maintenance margin. **Higher leverage shrinks the distance from entry to liquidation**: at ~20× a ~5% adverse move can liquidate; at 3× it takes a far larger move. "Under 5× is a safer standard for volatile markets."

## Liquidation-price awareness & buffer

- Compute the **liquidation price on every position** and require a **minimum buffer** — e.g. liquidation price must sit at least X% *beyond* the planned stop-loss, so the **stop always fires first**.
- Isolated (long), approx: `Liq ≈ Entry × [1 − AllocatedMargin / (PositionSize × Leverage)]` — simpler to predict.
- Cross-margin liquidation depends on **total wallet balance, aggregate unrealized P&L, and all open positions** combined.
- For **grid/DCA**, evaluate liquidation at the **fully-loaded** position (all levels / all safety orders filled), never the first entry.
- **Prefer isolated margin by default** so one position cannot drain the wallet. (Rebalancing may opt into cross for capital efficiency — see [the-five-bots.md](the-five-bots.md#5-rebalancing-bot-multi-position-portfolio).)

## Funding-rate awareness

- Perpetual funding settles **every 4–8h** (Binance moved several USDⓈ-M perps to 4h on 2023-10-12) and is **peer-to-peer** — Binance takes no cut; positive rate = longs pay shorts, negative = shorts pay longs.
- **No position at the settlement timestamp = no funding.**
- Rules: track each symbol's upcoming funding rate; avoid opening/holding into an adverse high-magnitude settlement; for held positions (grid/DCA/rebalance) include **accrued funding in live P&L and break-even**.

## Daily loss limit + kill switch / circuit breaker

- **Hard daily loss limit** (e.g. 3–5% of equity): on breach → cancel all open orders, optionally flatten via `reduceOnly`, and **disable new entries until next session/day**.
- Separate **circuit breaker** on anomalies (API error storm, fill divergence, price-feed gap, margin ratio → 100%) → flatten + halt + alert.

## Exposure caps

- Cap (a) number of simultaneous positions/deals, (b) total **gross notional vs equity** (portfolio leverage ceiling), and (c) **max notional per symbol** so no single market dominates.
- For DCA/grid, count the **fully-loaded ladder** against these caps, not the initial order.

## Reduce-only & emergency-close

- **All** exits, de-risking, kill-switch flattens, and session/grid closes use **`reduceOnly`** orders — they can only shrink a position, never flip or enlarge it.
- Maintain a tested **emergency-close** routine (cancel-all-orders → market `reduceOnly` close-all) reachable **manually** and **auto-fired by the circuit breaker**.

---

## Backtesting & validation

- **Testnet first (mandatory).** Validate every bot end-to-end on **Binance Futures Testnet** (order placement, fills, `reduceOnly`, leverage/margin mode, funding accrual, kill switch) before any mainnet capital. See [../integrations/binance-futures-api.md](../integrations/binance-futures-api.md).
- **Realistic backtest costs.** Include taker/maker **fees, slippage, and funding** in every backtest — for scalping/grid/DCA these often decide net profitability. Use history across **multiple regimes** (bull, bear, range): months minimum, ideally a full cycle.
- **Walk-forward analysis (WFA).** Optimize in-sample, validate on the *next* out-of-sample window, roll forward, repeat. Track **Walk-Forward Efficiency**: strong IS + collapsing OOS = overfit.
- **Avoid overfitting.** Few parameters; limit optimizer search (e.g. **< 500 epochs**) and parameter precision (Freqtrade caps decimals at 3 — finer values overfit); prefer parameters **stable across adjacent windows** over the single best IS number.
- **Avoid lookahead / data-snooping.** Never let a candle's future leak into its own signal; respect a warm-up/startup-candle period and drop the unstable window before scoring. Repeated re-optimization on the same OOS set becomes in-sample (a form of overfitting).
- **Promotion ladder:** backtest → walk-forward → **testnet paper run** (live data, fake money, enough days to see funding/edge cases) → small-size mainnet → scale only after live metrics match backtest within tolerance.

---

## Sources

**Funding:** [Binance Funding Rates FAQ](https://www.binance.com/en/support/faq/introduction-to-binance-futures-funding-rates-360033525031) · [What Is Futures Funding Rate (Binance Blog)](https://www.binance.com/en/blog/futures/what-is-futures-funding-rate-and-why-it-matters-421499824684903247)
**Margin / leverage / liquidation:** [Leverage & Margin of USDⓈ-M (FAQ)](https://www.binance.com/en/support/faq/detail/360033162192) · [Isolated vs Cross (Binance Academy)](https://academy.binance.com/en/articles/what-are-isolated-margin-and-cross-margin-in-crypto-trading) · [Liquidation explained (TradersUnion)](https://tradersunion.com/brokers/crypto/view/binance/liquidation/) · [Cross vs Isolated tutorial](https://siddharthgiri.medium.com/binance-cross-margin-vs-isolated-margin-usd-m-futures-trading-tutorial-1a45a2d74c46) · [Leverage explained (WunderTrading)](https://wundertrading.com/journal/en/learn/article/binance-leverage)
**Grid:** [Long/Short Grid (Binance)](https://www.binance.com/en/support/faq/what-is-long-short-grid-trading-904e47602a3941b99e960a31e152a986) · [Futures Grid FAQ](https://www.binance.com/en/support/faq/what-is-futures-grid-trading-f4c453bab89648beb722aa26634120c3) · [Grid guide (Binance Blog)](https://www.binance.com/en/blog/futures/stepbystep-guide-to-grid-trading-on-binance-futures-1221278002770616377) · [Trailing Up/Down](https://www.binance.com/en/support/faq/how-to-use-the-trailing-up-and-trailing-down-functions-in-usd%E2%93%A2-m-futures-grid-trading-7a7bb22420404385991dee3a0930207d)
**DCA / safety orders:** [3Commas DCA settings](https://help.3commas.io/en/articles/3108940-dca-bot-interface-and-main-settings) · [Averaging by indicators](https://help.3commas.io/en/articles/9663694-dca-bot-averaging-orders-by-technical-indicators) · [DCA calculator (MoneyButton)](https://www.moneybutton.pro/post/dca-safety-orders-calculator) · [Passivbot — how it works](https://www.passivbot.com/en/latest/how-it-works/)
**Scalping / day:** [1-Min Scalping (FXOpen)](https://fxopen.com/blog/en/1-minute-scalping-trading-strategies-with-examples/) · [VWAP/MACD/RSI scalping (5paisa)](https://www.5paisa.com/blog/Inside-the-one-minute-scalping-approach-how-ultra-short-term-traders-operate) · [Scalping types (CapMint)](https://www.capmint.com/learn/articles/types-of-scalping-trading-strategies)
**Sizing / Kelly:** [Sizing explained (PyQuantLab)](https://pyquantlab.medium.com/how-to-size-your-trades-fixed-percent-fractional-and-kelly-position-sizing-explained-3695b443ecfc) · [Kelly vs Fixed Fractional](https://medium.com/@tmapendembe_28659/kelly-criterion-vs-fixed-fractional-which-risk-model-maximizes-long-term-growth-972ecb606e6c) · [Kelly in markets (Atlas Peak)](https://www.atlaspeakresearch.com/report/07bf72)
**Backtesting / WFA / overfitting:** [Freqtrade Hyperopt](https://www.freqtrade.io/en/stable/hyperopt/) · [Strategy Customization](https://www.freqtrade.io/en/stable/strategy-customization/) · [Walk-Forward Efficiency (Kiploks)](https://kiploks.com/research/walk-forward-efficiency-wfe-explained-what-it-means-and-how-to-read-it)
