# Pluggable Signal Engines

> Four interchangeable decision backends behind one `SignalEngine` interface:
> (a) technical-indicator rules, (b) Claude/LLM signals, (c) TradingView webhook alerts, (d) trained ML models.
> A strategy bot is written **once** against the interface and any engine plugs in.

## The contract

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

class Action(str, Enum):
    BUY = "BUY"; SELL = "SELL"; HOLD = "HOLD"; CLOSE = "CLOSE"

@dataclass(frozen=True)
class Signal:
    action: Action
    confidence: float        # 0.0 .. 1.0
    size_hint: float         # 0.0 .. 1.0 advisory; risk layer CLAMPS it
    reason: str              # human-readable, for logs/audit
    source: str              # which engine produced it

@dataclass(frozen=True)
class MarketData:
    symbol: str
    ohlcv: object            # DataFrame of CLOSED candles
    indicators: dict         # precomputed: rsi, macd, atr, bb_width, ...
    extra: dict | None = None  # news, funding rate, TV rating, webhook payload

class SignalEngine(ABC):
    name: str
    @abstractmethod
    def generate_signal(self, market_data: MarketData) -> Signal: ...
```

## The four implementations

- **`IndicatorRuleEngine`** — deterministic rules over `indicators` (e.g. EMA cross + RSI filter). Can pull TradingView's consensus rating from the **MCP server** as an extra input.
- **`LLMSignalEngine`** — renders `MarketData` → prompt, calls Claude with **structured output**, re-validates to `Signal`; defaults to `HOLD` on any failure. **Best used as a gate, not a scalper.**
- **`WebhookSignalEngine`** — computes nothing; serves the latest validated TradingView alert (written by the FastAPI webhook handler into a queue/store) as a `Signal`. Push source adapted to the pull interface.
- **`MLModelEngine`** — loads a trained XGBoost/LightGBM model, rebuilds the *same* features used in training, predicts, maps probability → `action` + `confidence`.

```python
class StrategyBot:
    def __init__(self, engine: SignalEngine, risk, broker):
        self.engine, self.risk, self.broker = engine, risk, broker
    def on_closed_bar(self, md: MarketData) -> None:
        sig = self.engine.generate_signal(md)          # ← swappable backend
        if sig.confidence < self.risk.min_confidence:
            return
        order = self.risk.size(sig, md)                # deterministic sizing/leverage
        if order:
            self.broker.execute(order)                 # Binance USDⓈ-M Futures
```

## Diagram

```
   Data: Binance klines · indicators · funding/OI · news · TradingView MCP (pull) · TV webhook (push)
                                   │ MarketData
       ┌───────────────┬──────────┼───────────────┬────────────────┐
       ▼               ▼          ▼               ▼                ▼
  Indicator(a)   LLM/Claude(b)  Webhook(c)     ML Model(d)
  RuleEngine     gate           push→pull      XGB/LGBM
       └───────────────┴──────────┴───────────────┴────────────────┘
                         ▼  all return the SAME Signal
                  SignalEngine (ABC) → StrategyBot (confidence filter)
                         → Risk/Position Sizer (clamps size_hint, sets leverage)
                         → Binance USDⓈ-M Broker (execute)

   Optional chain: Indicator/ML proposes ─► LLM gates ─► Risk ─► Broker
```

---

## (c) TradingView webhooks & MCP

**Hard constraints from TradingView** (design around these): only ports **80/443**, HTTPS required, server must respond within **~3s** (so **accept-and-enqueue, never place orders inline**), no IPv6, requests come only from a fixed IPv4 set.

**Pine alert message** — put a JSON template with placeholders in the alert's message box:
```json
{ "secret": "LONG_RANDOM_STRING", "ticker": "{{ticker}}", "action": "{{strategy.order.action}}",
  "price": {{close}}, "time": "{{timenow}}", "interval": "{{interval}}", "position_size": {{strategy.position_size}} }
```

**Three independent defenses (apply all):**
1. **Shared secret** in the body compared with `hmac.compare_digest` (Pine can't compute HMAC). If you front it with your own relay, that proxy can add a real `X-Signature` header.
2. **IP allowlist** — TradingView's published source IPs: `52.89.214.238, 34.212.75.30, 54.218.53.128, 52.32.178.7`. Allowlist at firewall **and** re-check in-app.
3. **Strict Pydantic validation** before acting: `action ∈ {buy,sell,close}`, known symbol, size within risk limits, fresh `time` (reject stale/replayed via timestamp window + idempotency key). **Never** put broker credentials in the alert body.

```python
import hmac, os
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
app = FastAPI(); TV_IPS = {"52.89.214.238","34.212.75.30","54.218.53.128","52.32.178.7"}
SECRET = os.environ["TV_WEBHOOK_SECRET"]
class TVAlert(BaseModel):
    secret: str; ticker: str; action: str; price: float; time: str
    interval: str | None = None; position_size: float | None = None
@app.post("/tv-webhook")
async def tv_webhook(request: Request, alert: TVAlert):
    if request.client.host not in TV_IPS: raise HTTPException(403)
    if not hmac.compare_digest(alert.secret, SECRET): raise HTTPException(401)
    return {"ok": True}   # enqueue to a worker; do NOT place the order inline
```

**`atilaahmettaner/tradingview-mcp` is pull, not push** — an MCP server an LLM client calls on demand (stdio), wrapping the `tradingview-ta` library to return TradingView's **STRONG_BUY/BUY/NEUTRAL/SELL/STRONG_SELL** ratings + backtests. In this architecture: the **webhook is its own engine (c)**; the **MCP server is a *data input*** to engines (a)/(b), not a fourth push channel.

## (b) LLM / Claude signals

**Pattern:** use the LLM as a **structured classifier over assembled context**, not a tick-by-tick price oracle. Feed it summarized OHLCV + computed indicators (+ optional news + position/risk state); demand a constrained JSON object mapping 1:1 to `Signal` (`action, confidence, size_hint, reason`).

**Anthropic SDK (high level):** use the `anthropic` Python SDK with **structured outputs** so the response is schema-valid by construction (`client.messages.parse(...)` with a Pydantic model, or `output_config={"format": {"type":"json_schema","schema":...}}`). Put the large stable framing (system prompt + indicator definitions) first so **prompt caching** covers it. **Do not hardcode model IDs or pricing** — look up the current model/rates at build time (see the `claude-api` skill; current flagship = Opus 4.8 family, with Sonnet/Haiku for cheaper/faster pre-filters).

**Guardrails against hallucination:**
- Constrain output with `json_schema` + a Pydantic re-validation pass; **default to `HOLD`** on parse failure or out-of-range field or refusal.
- **Ground every decision in supplied numbers** — pass indicators *to* the model; require `reason` to cite supplied values. The model interprets, never invents price data.
- **Confidence floor + deterministic risk overlay** — below threshold → `HOLD`; the LLM never sets leverage or final size (`size_hint` is advisory, clamped by risk code).

**Why the LLM gates, not scalps:** a round-trip is hundreds of ms–seconds and costs per-token — orders of magnitude slower/pricier than an indicator rule. Use it to **filter/confirm/veto** slower-cadence decisions (regime check on a closed 5m–1h bar, news-risk veto); keep fast scalps on deterministic rules / ML. Pattern: **cheap engine proposes → LLM gates → risk layer sizes.** (This mirrors how `cbt-framework` uses Claude as strategy *author/validator*, not a per-tick generator.)

## (d) ML model signals

**Features (point-in-time only):** log returns + lagged/rolling returns; indicators (RSI/MACD/EMA stack/BB width/ATR/ADX/OBV via `pandas-ta`/`TA-Lib`); volatility/microstructure (rolling std, ATR-normalized range, volume z-score; for futures: funding rate, OI delta, book imbalance); calendar/regime flags. **Label:** sign of forward return, or triple-barrier (TP/stop/timeout).

**Models, in "try-first" order:** (1) **Logistic regression** baseline — if a tree ensemble can't beat it, the features/labels are the problem; (2) **XGBoost/LightGBM** — the workhorse for tabular OHLCV features (SHAP for importance); (3) **LSTM/Transformer** — only with genuine sequential signal + lots of data; far more overfit-prone.

**Time-series validation:** **walk-forward, never shuffle** (k-fold leaks the future); fit all transforms (scale/winsorize) on the train slice only, inside the loop; lag features to closed-bar data; **embargo/purge** a gap between train and test to kill label leakage.

**Realistic caveat (load-bearing):** ML rarely beats simple rules on price data without serious sustained work — brutal signal-to-noise, shifting regimes, OOS decay. Treat the ML engine as *one* `SignalEngine` to **benchmark head-to-head against the indicator engine under identical walk-forward + cost assumptions**; promote only if it wins **net of costs out-of-sample**.

---

## Sources
- [TradingView webhook config](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/) · [Webhooks announcement](https://www.tradingview.com/blog/en/webhooks-for-alerts-now-available-14054/) · [Webhook IP list](https://www.tradingconnector.com/setuptvwebhook) · [Reference receiver](https://github.com/fabston/TradingView-Webhook-Bot/blob/master/main.py)
- [tradingview-mcp](https://github.com/atilaahmettaner/tradingview-mcp) · [cbt-framework](https://github.com/Trade-With-Claude/cbt-framework)
- [Anthropic models](https://platform.claude.com/docs/en/about-claude/models/overview) · [pricing](https://platform.claude.com/docs/en/pricing) (look up at build time)
- [ML for Trading (stefan-jansen)](https://github.com/stefan-jansen/machine-learning-for-trading) · [Regime-aware LightGBM (MDPI)](https://www.mdpi.com/2079-9292/15/6/1334) · [Walk-forward XGBoost (arXiv)](https://arxiv.org/pdf/2601.08896)
