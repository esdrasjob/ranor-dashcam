"""
Event Bus - pub/sub interno entre JT808, FTP e API REST
Também distribui eventos para WebSocket clients (dashboard ao vivo)
"""

import asyncio
import json
import logging
from collections import deque
from datetime import datetime
from typing import Callable, Dict, List, Set

logger = logging.getLogger("ranor.events")


class EventBus:
    def __init__(self, max_history: int = 500):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._ws_clients: Set = set()
        self._history: deque = deque(maxlen=max_history)
        self._all_events: deque = deque(maxlen=max_history)

    async def publish(self, event_type: str, data: dict):
        event = {
            'type': event_type,
            'data': data,
            'ts': datetime.now().isoformat(),
        }
        self._history.append(event)
        self._all_events.append(event)

        # Notify subscribers
        for cb in self._subscribers.get(event_type, []):
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(data)
                else:
                    cb(data)
            except Exception as e:
                logger.error(f"Event subscriber error [{event_type}]: {e}")

        # Broadcast to WebSocket clients
        msg = json.dumps(event)
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    def subscribe(self, event_type: str, callback: Callable):
        self._subscribers.setdefault(event_type, []).append(callback)

    def add_ws_client(self, ws):
        self._ws_clients.add(ws)

    def remove_ws_client(self, ws):
        self._ws_clients.discard(ws)

    def get_history(self, event_type: str = None, limit: int = 100) -> list:
        events = list(self._history)
        if event_type:
            events = [e for e in events if e['type'] == event_type]
        return events[-limit:]

    def get_stats(self) -> dict:
        events = list(self._all_events)
        counts = {}
        for e in events:
            counts[e['type']] = counts.get(e['type'], 0) + 1
        return counts
