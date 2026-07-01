# Binance USDⓈ-M Futures API — Integration Guide

> Scope: USDⓈ-M (USDT/USDC-margined) perpetual & delivery futures. All REST paths use the `/fapi/*` namespace.
> Verified against `developers.binance.com` (docs current as of 2026-04).

## Testnet setup

| Item | Mainnet | Testnet |
|------|---------|---------|
| REST base host | `https://fapi.binance.com` | `https://testnet.binancefuture.com` (newer docs also list `https://demo-fapi.binance.com`) |
| WebSocket market streams | `wss://fstream.binance.com` | `wss://stream.binancefuture.com` (docs also list `wss://demo-fstream.binance.com`) |
| Paths | identical `/fapi/...` | identical |

- **Testnet exposes the same `/fapi/...` paths as mainnet** — only swap the base host. Not every production endpoint is mirrored.
- **Get testnet keys:** log into `https://testnet.binancefuture.com` (GitHub/email), open the API-key panel at the bottom of the trading screen, generate a **separate HMAC key/secret**. Testnet keys are distinct from mainnet — never mix. Account is pre-funded with mock USDT; balances reset periodically.
- **Host caveat:** the in-the-wild host every Python lib targets is `testnet.binancefuture.com`; Binance's newer docs introduce `demo-fapi.binance.com`. If a library's `testnet=True` fails, override the base URL. **Confirm with a public `GET /fapi/v1/ping` before signing anything.**

## Authentication

- **API key** → header `X-MBX-APIKEY` on every key-requiring request.
- **Signature (SIGNED/TRADE/USER_DATA):** HMAC SHA256 over `totalParams` (query string + body, in the exact order sent), using the **secret key** as the HMAC key. Append the hex digest as a final `signature=` param — it must be **last**.
- **Required on signed requests:** `timestamp` (ms). Optional `recvWindow` (default 5000 ms, **max 60000**). Sync clock against `GET /fapi/v1/time`.
- **Security types:** `NONE` (public), `MARKET_DATA`/`USER_STREAM` (key only), `TRADE`/`USER_DATA` (key + signature).
- **Signing pitfalls:** (1) signing a string that differs byte-for-byte from what you send — encode once, sign the encoded string, send that; (2) clock drift → `-1021`; (3) including `signature` inside `totalParams` (exclude, then append); (4) re-ordering params between sign and send; (5) mainnet key against testnet host → `-2015`.

## Core REST endpoints

All TRADE/USER_DATA endpoints need `timestamp` (+ optional `recvWindow`) and `signature`.

| Purpose | Method | Path | Notes |
|---------|--------|------|-------|
| Ping | GET | `/fapi/v1/ping` | Public. Weight 1. |
| Server time | GET | `/fapi/v1/time` | Clock-sync. |
| Exchange info & filters | GET | `/fapi/v1/exchangeInfo` | `symbols[].filters[]`, `pricePrecision`, `quantityPrecision`. |
| Klines | GET | `/fapi/v1/klines` | `symbol,interval,startTime,endTime,limit` (max **1500**). Intervals `1m…1M`. |
| Mark price & funding | GET | `/fapi/v1/premiumIndex` | `markPrice,indexPrice,lastFundingRate,nextFundingTime`. |
| Funding rate history | GET | `/fapi/v1/fundingRate` | max 1000. |
| Book ticker | GET | `/fapi/v1/ticker/bookTicker` | best bid/ask. |
| Account info | GET | `/fapi/v3/account` | **v3 current** (v2 deprecated). Weight 5. |
| Account balance | GET | `/fapi/v3/balance` | **v3.** Weight 5. |
| Position info / risk | GET | `/fapi/v3/positionRisk` | **v3.** `liquidationPrice`, entry, leverage, margin. |
| Change leverage | POST | `/fapi/v1/leverage` | `symbol,leverage` (1–125). Returns `maxNotionalValue`. |
| Change margin type | POST | `/fapi/v1/marginType` | `ISOLATED`\|`CROSSED`. Rejected if open position/orders. |
| Change position mode | POST | `/fapi/v1/positionSide/dual` | `dualSidePosition` true(Hedge)/false(One-way). |
| New order | POST | `/fapi/v1/order` | counts against order-rate limits (10s & 1m). |
| Cancel order | DELETE | `/fapi/v1/order` | `symbol` + `orderId`/`origClientOrderId`. |
| Cancel all open orders | DELETE | `/fapi/v1/allOpenOrders` | `symbol` required. |
| Open orders | GET | `/fapi/v1/openOrders` | **weight 40 if `symbol` omitted** vs 1 per symbol. |

### New Order — `type` values & mandatory params

Always required: `symbol`, `side` (`BUY`/`SELL`), `type`, `timestamp`.

| `type` | Additional mandatory params |
|--------|------------------------------|
| `LIMIT` | `timeInForce`, `quantity`, `price` |
| `MARKET` | `quantity` |
| `STOP` / `TAKE_PROFIT` | `quantity`, `price`, `stopPrice` |
| `STOP_MARKET` / `TAKE_PROFIT_MARKET` | `stopPrice` (or `closePosition=true`) |
| `TRAILING_STOP_MARKET` | `callbackRate` (0.1–10 %) |

- **`workingType`** = `MARK_PRICE`\|`CONTRACT_PRICE` (default CONTRACT_PRICE) selects the `stopPrice` trigger feed.
- **`reduceOnly`** can't be used in Hedge Mode and can't combine with `closePosition=true`.
- **`closePosition=true`** (STOP_MARKET/TAKE_PROFIT_MARKET only) closes the whole position on trigger; no `quantity`/`reduceOnly`.
- **`positionSide`**: `BOTH` (One-way default) or `LONG`/`SHORT` (**mandatory in Hedge Mode**).
- **`timeInForce`**: `GTC,IOC,FOK,GTX`(post-only)`,GTD`(needs `goodTillDate`).

## WebSocket streams

Bases: mainnet `wss://fstream.binance.com`; testnet `wss://stream.binancefuture.com`. Symbols **lowercase**.
- Single: `/ws/<streamName>` · Combined: `/stream?streams=<s1>/<s2>`
- Limits: connection valid **24h max**, up to **1024 streams/conn**, **≤10 inbound msgs/sec**.
- Market: `<sym>@kline_<interval>`, `<sym>@markPrice@1s` (funding+mark), `<sym>@aggTrade`, `<sym>@bookTicker`.
- **User data stream:** `POST /fapi/v1/listenKey` (start) → valid 60 min; `PUT` every ~30–60 min to extend; `DELETE` to close. Connect `wss://fstream.binance.com/ws/<listenKey>`. Delivers `ACCOUNT_UPDATE`, `ORDER_TRADE_UPDATE`, `ACCOUNT_CONFIG_UPDATE`, margin-call events.

## Rate limits

- **Weight (IP):** every response carries `X-MBX-USED-WEIGHT-1M`. **Read live limits from `exchangeInfo → rateLimits[]`** rather than hardcoding.
- **Order limits (account):** `X-MBX-ORDER-COUNT-10S` / `-1M`.
- **HTTP 429** = limit breached → back off, respect `Retry-After`. **HTTP 418** = IP ban (scales 2 min → 3 days).
- **Avoid bans:** prefer WebSocket over REST polling (Binance "strongly recommends"); watch `X-MBX-USED-WEIGHT-1M`; never retry-storm; use one shared rate-limiter across all bot tasks.

## Python library choice

| Library | Maintained? | Async? | Futures+testnet? | Verdict |
|---------|-------------|--------|------------------|---------|
| `binance-futures-connector` (official, old) | **Deprecated** (archived) | No | Yes | Avoid for new async work. |
| `binance-connector-python` (official, new modular) | Active, no stable releases yet | Partial | Yes | Watch for the future; not release-stable. |
| **`python-binance`** (sammchardy) | **Active** (v1.0.37, 2026-06) | **Yes** (`AsyncClient` + async WS) | **Yes** (`futures_*`, `testnet=True`) | **PRIMARY RECOMMENDATION.** |
| `ccxt` / `ccxt.pro` | Very active | Yes | Yes (`binanceusdm`, `set_sandbox_mode(True)`) | Second choice / multi-exchange; unified API hides futures-specific params. |

**→ Use `python-binance`** for an async FastAPI bot: actively maintained, first-class `AsyncClient`+WS, native futures semantics (`futures_change_leverage/margin_type/position_mode`, `positionSide`, `reduceOnly`), `testnet=True`. Use `ccxt` only for multi-exchange portability.

## Futures gotchas & precision

- **Leverage:** set via `POST /fapi/v1/leverage` *before* sizing; response `maxNotionalValue` is the cap. **Liquidation price** from `positionRisk` shifts with margin/mark price.
- **Maintenance margin** is **tiered** — higher notional → higher MMR → liquidation closer to entry. Don't assume fixed liquidation distance.
- **Margin type** (`ISOLATED` caps loss / `CROSSED` shares wallet) and **position mode** (one-way vs hedge) must be set *before* opening (rejected while anything is open). Hedge Mode: send `positionSide=LONG|SHORT`, **no `reduceOnly`**. One-way: `positionSide=BOTH`, `reduceOnly` allowed.
- **Precision via filters (CRITICAL):** read each symbol's `filters[]`:
  - `PRICE_FILTER` → `tickSize` (price must be a multiple).
  - `LOT_SIZE` / `MARKET_LOT_SIZE` → `stepSize`, `minQty`, `maxQty`.
  - `MIN_NOTIONAL` → `price × qty` ≥ `notional`.
  - **Do NOT use `pricePrecision`/`quantityPrecision` as the rounding step** (display hints only). Round price to `tickSize`, quantity **down** to `stepSize`, using **`Decimal` not float** — avoids `-1111` precision and `-4164` notional rejections.

## ⚠️ Verify before shipping code
1. **Testnet host** — ping both `testnet.binancefuture.com` and `demo-fapi.binance.com` before signing; libs differ.
2. **New Order `callbackRate` (0.1–10) and `priceProtect` casing** — confirm on the rendered New Order docs page (it's JS-rendered).

## Sources
- [General Info (signing, rate limits, recvWindow)](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info) · [Quick Start](https://developers.binance.com/docs/derivatives/quick-start)
- [Testnet](https://testnet.binancefuture.com/) · [WebSocket Market Streams](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams) · [User Data Streams](https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams)
- [New Order](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order) · [Change Leverage](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Change-Initial-Leverage) · [Change Margin Type](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Change-Margin-Type) · [Change Position Mode](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Change-Position-Mode)
- [Account V3](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Account-Information-V3) · [Balance V3](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Futures-Account-Balance-V3) · [Position Risk V3](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Position-Information-V3) · [Klines](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data) · [Mark Price](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price) · [Exchange Info](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information)
- Libraries: [python-binance](https://github.com/sammchardy/python-binance) · [binance-connector-python](https://github.com/binance/binance-connector-python) · [ccxt](https://github.com/ccxt/ccxt)
