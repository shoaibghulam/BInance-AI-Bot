# The Five Bots — Strategy Specifications

> Scope: **Leveraged USDⓈ-M perpetual futures only** (no spot, no COIN-M). Testnet-first.
> All numbers are **starting defaults to tune via backtest/walk-forward**, not guarantees.
> Every bot inherits the binding contract in [risk-management.md](risk-management.md).

| # | Bot | Best regime | Core mechanic |
|---|-----|-------------|---------------|
| 1 | Day trading | Trending intraday | Session-gated directional swings, flat by session end |
| 2 | Scalping (1–5m) | Liquid range / vol bursts | Many tiny trades, tight stops, fee-sensitive |
| 3 | Grid | Ranging / sideways | Ladder of buy-low/sell-high limit orders |
| 4 | DCA | Ranging / mean-reverting | Base order + averaging safety orders (martingale-ish) |
| 5 | Rebalancing | Rotating leadership | Trade a basket back to target weights on drift |

---

## 1. Day Trading bot

**Goal.** Intraday directional trading inside defined "active" session windows; **flat by end of session** — never holds across the low-liquidity overnight gap. Capture a few medium-quality swings per session with positive expectancy after fees and funding.

**Entry.**
- **Session gate (hard precondition):** only arm entries inside configured UTC windows overlapping high-volume sessions (London 07:00–11:00, US 13:00–17:00 UTC). Crypto is 24/7 but volume/volatility cluster around equity-session overlaps.
- **Trend filter:** price above/below 200-EMA on trade TF (e.g. 15m); optionally require 50-EMA > 200-EMA alignment.
- **Trigger:** pullback-to-trend — RSI(14) crossing up from <40 (long) / down from >60 (short), confirmed by MACD histogram flip, with price reclaiming VWAP.
- **Volatility:** size scaled by ATR(14); skip when ATR collapses (dead tape) or spikes past a ceiling (news shock).

**Exit.**
- TP: fixed R-multiple (1.5R–2R) or scale out (half at 1R, runner trails).
- SL: ATR-based (≈1.5×ATR) beyond recent swing structure.
- Trailing: activate at +1R, trail by ATR or EMA(20).
- **Time/session exit (mandatory):** force `reduceOnly` market-close of all positions N minutes before session end, regardless of P&L.

**Config knobs.** Session windows (UTC), trade TF, EMA lengths, RSI thresholds, MACD settings, VWAP anchor, ATR period & stop multiple, R-multiple TP, max trades/session, per-trade risk %, leverage cap, flat-by time.

**Futures concerns.** Use **isolated margin**; keep leverage ≤3–5×. Being flat by session end largely **avoids funding** (funding only applies if holding at the settlement timestamp, every 4–8h). Skip entries minutes before a funding settlement.

**Failure modes.** Intra-session chop (whipsaws through VWAP/EMA), over-trading into death-by-fees, holding past flat-by time into an overnight gap, forcing trades in low-vol windows the session gate should block.

---

## 2. Scalping bot (1–5 min)

**Goal.** Very-high-frequency 1–5m trading: many small trades, tight stops, small targets. Outcome driven by execution, spread/slippage, and **fees** more than big moves. Profitability hinges on win-rate × avg-win vs fees.

**Entry.**
- Timeframe: 1m primary, 5m context filter.
- *Mean-reversion variant:* Bollinger (20-SMA, 2σ) — long when price tags lower band while RSI/Stoch oversold (expect bounce to mid-band); mirror for shorts.
- *Momentum/VWAP variant:* VWAP as S/R, confirmed by MACD sign-flip within 4–5 candles.
- Liquidity filter: only high-volume symbols; avoid thin periods (spread eats the edge).

**Exit.**
- TP: small fixed target (mid-band, or a few ticks / 0.1–0.3%).
- SL: **tight**, just beyond recent swing, optionally small ATR stop sized to noise.
- **Time-stop:** abandon trades that don't work within ~5 minutes — scalps decay fast.

**Config knobs.** Liquidity-screened symbol whitelist, BB period/σ, RSI/Stoch thresholds, VWAP/MACD settings, tick/percent TP, swing/ATR stop, max-spread guard, time-stop, max open scalps, **maker-vs-taker preference** (prefer post-only/maker — fees dominate).

**Futures concerns.** **Fees and funding are existential** at this turnover. Taker fees compound brutally — a strategy that wins gross can lose net. Isolated margin, modest leverage: high leverage means a tiny adverse tick hits maintenance margin / liquidation.

**Failure modes.** Slippage/spread exceeding the tiny edge; fee drag flipping gross-positive to net-negative; a single fat-tail candle blowing the tight stop; over-leverage turning a 0.5% move into liquidation; parameters overfit to one volatility regime.

---

## 3. Grid trading bot (futures)

**Goal.** Place a ladder of buy/sell limit orders across a price range; profit from oscillation by repeatedly buying low / selling high within the band. Three directions:
- **Neutral:** shorts above reference price, longs below — profits from sideways chop, no directional bet.
- **Long:** first order is a buy; biased to an uptrending range.
- **Short:** first order is a sell; biased to a downtrending range.

**Fill logic.** Auto-place orders at each level between lower/upper bound (arithmetic or geometric spacing). Each filled buy seeds a sell one level up and vice versa; the inter-level spread minus fees = per-fill profit. **Trailing up/down** extends the range to follow a drifting trend.

**Exit.** Per-grid: round-trip closes when the paired order fills. Whole-bot: stop-on-price (exits band), TP on cumulative P&L, **hard stop-loss price below the lower bound (critical on futures)**, or scheduled close. Always close via `reduceOnly`.

**Config knobs.** Upper/lower bound, grid count, spacing mode, direction, per-grid investment, leverage, trailing toggles, stop-loss price, take-profit. Initial margin ≈ grid count × leverage × range.

**Futures concerns.** The defining risk: a sustained move **against** the accumulated grid position drives it toward the **liquidation price fast** — Binance warns positions "can swiftly approach the liquidation price" if the market trends out of range. Use **isolated margin** to cap blast radius. Funding accrues on the net open position every 4–8h — a long-biased grid in positive funding bleeds continuously.

**Failure modes.** Strong trend out of range (grid keeps adding to the losing side until liquidation), range too wide (capital spread thin, low fill rate) or too narrow (price escapes immediately), ignoring funding on a directional grid. A neutral grid in a strong trend is a slow liquidation machine.

---

## 4. DCA bot (averaging / safety-orders model)

**Goal.** 3Commas-style: open a small **base order**, then a ladder of **safety orders** that average the entry down (long) / up (short) as price moves adversely, pulling break-even toward current price so it exits on a modest bounce. ⚠️ Despite the "DCA" label, growing safety-order sizes make this effectively **martingale**: high win-rate on small targets at the cost of rare, very large losers.

**Entry.** Base order opens on a signal (RSI oversold, TA indicator, or TradingView webhook). Safety orders trigger on **price deviation** from base/average (optionally TA-gated).

**Exit.** **Target profit %** measured off the *average* entry (not the base) — each bounce after averaging hits target, then the deal closes and restarts. A **hard stop-loss** (max deviation / max safety orders reached) is essential on futures or the martingale runs into liquidation.

**Config knobs.**
- **Base order volume** — initial entry size.
- **Safety order volume** + **volume scale (martingale coefficient)** — size of SO *n* = base_SO × coef^n.
- **Price deviation to open first SO** — % gap from base to SO #1.
- **Safety order step scale** — multiplies deviation so each SO sits progressively further away.
- **Max safety orders** — caps total exposure.
- **Target profit %**, **stop-loss %**, leverage, margin type.

**Futures concerns.** Martingale + leverage is the most liquidation-prone pattern here: each safety order **increases size and pushes the liquidation price closer** — exactly when already losing. Isolated margin + hard floor on max safety orders / account allocation. Compute liquidation price *after the final planned safety order*, not the base entry.

**Failure modes.** A sustained one-way move exhausts all safety orders → large leveraged position liquidates (martingale ruin); too much capital per deal; multiple DCA deals drawing down together in a market-wide selloff.

---

## 5. Rebalancing bot (multi-position portfolio)

**Goal.** Keep a basket of futures positions at **target weights** (e.g. 40% BTC / 30% ETH / 30% SOL, or a long/short basket); when realized weights drift past a threshold, trade back to target — trimming winners, adding to laggards. Harvest the "rebalancing premium" and control concentration, rather than predict direction.

**Trigger (no classic indicator entry — the trigger is drift).**
- **Threshold:** act when a position's weight deviates from target beyond a band (e.g. ±5% absolute / ±25% relative).
- **Periodic:** act on a schedule (daily/weekly at a fixed UTC time).
- **Combined:** scheduled check that only trades if drift exceeds the band (reduces churn/fees).
- Optionally gate basket net exposure by a regime filter (reduce gross when realized vol spikes).

**Rebalance logic.** Compute target notional per symbol from equity × target weight, issue delta orders (mostly `reduceOnly` trims on overweights, adds on underweights). "Exit" is continuous convergence; a portfolio-level max-drawdown stop flattens everything.

**Config knobs.** Target weights, drift band, schedule, min-trade size (avoid dust churn), per-rebalance turnover cap, leverage per leg / portfolio gross cap, margin mode, fee/funding-aware skip.

**Futures concerns.** **Cross vs isolated** is a real choice: cross lets winners' unrealized P&L cushion losers (capital-efficient) but risks the whole wallet; isolated walls off each leg. Funding is per-leg/per-direction — a multi-long basket pays funding on every leg in positive funding; a long/short basket can be funding-neutral. Under cross, evaluate liquidation at the **portfolio** level (margin ratio = wallet balance / total maintenance margin).

**Failure modes.** Rebalancing into a crashing asset (averaging into a structural loser), over-frequent rebalancing churning away the premium, correlated drawdown (crypto correlations → ~1 in selloffs), leverage stacking across legs quietly exceeding liquidation tolerance.

---

*Sources for this document are consolidated in [risk-management.md § Sources](risk-management.md#sources).*
