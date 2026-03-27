"""Thread-safe event bus: bridges conductor background threads → asyncio WebSocket subscribers."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class EventBus:
    """
    emit() is safe to call from any thread; internally uses
    loop.call_soon_threadsafe so asyncio.Queue consumers are never
    touched from a foreign thread.
    """

    _MAX_HISTORY = 500

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[str]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._history: list[str] = []

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    def get_history(self) -> list[str]:
        return list(self._history)

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Emit an event. Safe to call from any thread."""
        payload = json.dumps(
            {
                "type": event_type,
                "ts": datetime.now(timezone.utc).isoformat(),
                "data": data or {},
            }
        )
        self._history.append(payload)
        if len(self._history) > self._MAX_HISTORY:
            self._history.pop(0)

        if self._loop is None or not self._loop.is_running():
            return

        def _put() -> None:
            for q in list(self._queues):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    logger.debug("WebSocket subscriber queue full; dropping event.")

        self._loop.call_soon_threadsafe(_put)


_bus = EventBus()


def get_event_bus() -> EventBus:
    return _bus
