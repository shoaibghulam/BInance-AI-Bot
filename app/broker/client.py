"""Async wrapper over python-binance `AsyncClient` for USDⓈ-M Futures.

Safe mode: if no API key/secret is configured, the client never crashes the
app. `connected` stays False, account/positions return empty, and trading
methods are no-ops that return structured "not connected" results. The UI can
still boot and prompt the user to add testnet keys.

python-binance is imported lazily so the module imports cleanly even if the
package is not installed yet (parse/boot must not require network or the lib).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.broker.precision import SymbolFilters, format_decimal
from app.config import settings

logger = logging.getLogger("trader.broker")


class BrokerClient:
    """Thin async facade over python-binance futures calls.

    All exchange calls are wrapped so a transient API error is logged and
    surfaced as a safe value rather than bubbling into the request handler.
    """

    def __init__(self) -> None:
        self._client: Optional[Any] = None
        self.connected: bool = False
        self._filters_cache: dict[str, SymbolFilters] = {}

    # --- Lifecycle -------------------------------------------------------
    async def connect(self) -> bool:
        """Create the AsyncClient if credentials exist; else stay in safe mode.

        Returns True when an authenticated client is live, False otherwise.
        Never raises — a failed connect leaves the app in safe mode.
        """
        if not settings.has_credentials:
            logger.warning(
                "No Binance %s credentials set — running in SAFE MODE (read-only).",
                settings.env_label,
            )
            self.connected = False
            return False

        try:
            from binance import AsyncClient  # lazy import

            self._client = await AsyncClient.create(
                api_key=settings.api_key,
                api_secret=settings.api_secret,
                testnet=settings.is_testnet,
            )
            # Prove connectivity with a public ping before trusting the client.
            await self._client.futures_ping()
            self.connected = True
            logger.info("Broker connected to Binance %s.", settings.env_label)
            return True
        except Exception as exc:  # broad: any failure → safe mode, never crash
            logger.error("Broker connect failed (%s); staying in safe mode.", exc)
            await self._safe_close()
            self.connected = False
            return False

    async def close(self) -> None:
        """Close the underlying client connection (graceful shutdown)."""
        await self._safe_close()
        self.connected = False

    async def _safe_close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close_connection()
            except Exception as exc:  # pragma: no cover - cleanup best-effort
                logger.debug("Error closing broker client: %s", exc)
            finally:
                self._client = None

    # --- Account / positions --------------------------------------------
    async def get_account(self) -> dict:
        """Return raw futures account info, or {} in safe mode / on error."""
        if not self._ready():
            return {}
        try:
            return await self._client.futures_account()
        except Exception as exc:
            logger.error("get_account failed: %s", exc)
            return {}

    async def get_positions(self) -> list[dict]:
        """Return open positions (positionAmt != 0), or [] in safe mode.

        Retries transient errors with backoff — a one-off failure must NOT read
        as "flat" (that dropped confirmed fills / orphaned grid inventory).
        """
        if not self._ready():
            return []
        import asyncio

        last_exc = None
        for attempt in range(3):
            try:
                raw = await self._client.futures_position_information()
                return [p for p in raw if float(p.get("positionAmt", 0) or 0) != 0.0]
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))
        # repr() so empty-message exceptions still identify the type.
        logger.error("get_positions failed after retries: %r", last_exc)
        return []

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        """Return open orders for a symbol (or all), or [] in safe mode."""
        if not self._ready():
            return []
        try:
            kwargs = {"symbol": symbol} if symbol else {}
            return await self._client.futures_get_open_orders(**kwargs)
        except Exception as exc:
            logger.error("get_open_orders failed: %s", exc)
            return []

    async def get_order(self, symbol: str, order_id) -> Optional[dict]:
        """Return one order's current state (status/executedQty/avgPrice), or None.

        Used by the grid to CONFIRM an order actually FILLED before applying it
        as a fill — an order can leave the open-orders set because it was
        cancelled or expired, not filled, and must not be misread as a fill.
        """
        if not self._ready():
            return None
        try:
            return await self._client.futures_get_order(
                symbol=symbol, orderId=int(order_id)
            )
        except Exception as exc:
            logger.error("get_order failed for %s/%s: %s", symbol, order_id, exc)
            return None

    # --- Config (leverage / margin) -------------------------------------
    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Set initial leverage for a symbol (1–125, capped upstream by risk)."""
        if not self._ready():
            return {"ok": False, "reason": "not_connected"}
        try:
            res = await self._client.futures_change_leverage(
                symbol=symbol, leverage=int(leverage)
            )
            return {"ok": True, "result": res}
        except Exception as exc:
            logger.error("set_leverage failed for %s: %s", symbol, exc)
            return {"ok": False, "reason": str(exc)}

    async def set_margin_type(self, symbol: str, margin_type: str) -> dict:
        """Set ISOLATED/CROSSED margin (rejected if a position/order is open)."""
        if not self._ready():
            return {"ok": False, "reason": "not_connected"}
        try:
            res = await self._client.futures_change_margin_type(
                symbol=symbol, marginType=margin_type.upper()
            )
            return {"ok": True, "result": res}
        except Exception as exc:
            # Binance returns -4046 "No need to change margin type" — benign.
            logger.warning("set_margin_type for %s: %s", symbol, exc)
            return {"ok": False, "reason": str(exc)}

    # --- Orders ----------------------------------------------------------
    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Optional[str] = None,
        price: Optional[str] = None,
        reduce_only: bool = False,
        time_in_force: Optional[str] = None,
        stop_price: Optional[str] = None,
        close_position: bool = False,
        **extra: Any,
    ) -> dict:
        """Place a futures order. Prices/quantities should be pre-rounded strings.

        Quantities/prices are sent as strings to preserve Decimal precision.
        """
        if not self._ready():
            return {"ok": False, "reason": "not_connected"}

        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type.upper(),
        }
        # Format every numeric param as a PLAIN fixed-point string. str(float)
        # emits scientific notation for micro-price coins (e.g. "1.7e-06"),
        # which Binance rejects with -1102. format_decimal guarantees plain
        # decimals like "0.00000170". This is the central safety net.
        if quantity is not None:
            params["quantity"] = format_decimal(quantity)
        if price is not None:
            params["price"] = format_decimal(price)
        if stop_price is not None:
            params["stopPrice"] = format_decimal(stop_price)
        if time_in_force is not None:
            params["timeInForce"] = time_in_force
        if reduce_only:
            params["reduceOnly"] = "true"
        if close_position:
            params["closePosition"] = "true"
        params.update(extra)

        try:
            res = await self._client.futures_create_order(**params)
            return {"ok": True, "result": res}
        except Exception as exc:
            logger.error("place_order failed for %s: %s", symbol, exc)
            return {"ok": False, "reason": str(exc)}

    async def cancel_all(self, symbol: str) -> dict:
        """Cancel all open orders for a symbol."""
        if not self._ready():
            return {"ok": False, "reason": "not_connected"}
        try:
            res = await self._client.futures_cancel_all_open_orders(symbol=symbol)
            return {"ok": True, "result": res}
        except Exception as exc:
            logger.error("cancel_all failed for %s: %s", symbol, exc)
            return {"ok": False, "reason": str(exc)}

    async def flatten_all(self) -> dict:
        """Emergency close-all: market `reduceOnly` close of every open position.

        Used by the kill switch. Cancels each symbol's orders first, then sends
        an opposite-side reduceOnly MARKET order to flatten. Best-effort: one
        symbol's failure does not abort the others.
        """
        if not self._ready():
            return {"ok": False, "reason": "not_connected", "closed": 0}

        positions = await self.get_positions()
        closed = 0
        errors: list[str] = []
        for pos in positions:
            symbol = pos.get("symbol")
            amt = float(pos.get("positionAmt", 0) or 0)
            if not symbol or amt == 0.0:
                continue
            await self.cancel_all(symbol)
            side = "SELL" if amt > 0 else "BUY"
            res = await self.place_order(
                symbol=symbol,
                side=side,
                order_type="MARKET",
                quantity=str(abs(amt)),
                reduce_only=True,
            )
            if res.get("ok"):
                closed += 1
            else:
                errors.append(f"{symbol}: {res.get('reason')}")

        return {"ok": not errors, "closed": closed, "errors": errors}

    # --- Exchange filters ------------------------------------------------
    async def get_exchange_filters(self, symbol: str) -> Optional[SymbolFilters]:
        """Return cached/parsed `SymbolFilters` for a symbol, or None on failure."""
        symbol = symbol.upper()
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        if not self._ready():
            return None
        try:
            info = await self._client.futures_exchange_info()
            for sym_info in info.get("symbols", []):
                if sym_info.get("symbol") == symbol:
                    filters = SymbolFilters.from_exchange_info(symbol, sym_info)
                    self._filters_cache[symbol] = filters
                    return filters
            logger.warning("Symbol %s not found in exchangeInfo.", symbol)
            return None
        except Exception as exc:
            logger.error("get_exchange_filters failed for %s: %s", symbol, exc)
            return None

    # --- Internal --------------------------------------------------------
    def _ready(self) -> bool:
        """True when an authenticated client is live."""
        return self.connected and self._client is not None
