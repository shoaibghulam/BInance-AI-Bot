"""WebSocket connection manager + frame helpers.

Tracks connected clients and broadcasts contract-shaped frames
`{ "type": <t>, "data": <payload>, "ts": <iso8601> }`. Dead sockets are pruned
on send failure so one broken client never blocks the broadcast.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger("trader.ws")


def _utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def make_frame(frame_type: str, data: Any) -> dict:
    """Build a contract-shaped WS frame."""
    return {"type": frame_type, "data": data, "ts": _utc_now_iso()}


class ConnectionManager:
    """Manages active WebSocket clients and broadcasts to all of them."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a new client."""
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        logger.debug("WS client connected (total=%d).", len(self._clients))

    async def disconnect(self, websocket: WebSocket) -> None:
        """Deregister a client."""
        async with self._lock:
            self._clients.discard(websocket)
        logger.debug("WS client disconnected (total=%d).", len(self._clients))

    @property
    def count(self) -> int:
        """Number of currently connected clients."""
        return len(self._clients)

    async def send_personal(self, websocket: WebSocket, frame: dict) -> None:
        """Send a single frame to one client (best-effort)."""
        try:
            await websocket.send_json(frame)
        except Exception as exc:  # pragma: no cover - transport error
            logger.debug("Personal WS send failed: %s", exc)
            await self.disconnect(websocket)

    async def broadcast(self, frame: dict) -> None:
        """Send a frame to all clients, pruning any that fail."""
        async with self._lock:
            targets = list(self._clients)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(frame)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
