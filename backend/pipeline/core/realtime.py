"""Small in-process event bus used by the browser SSE endpoint.

The application currently runs one API process.  Keeping this abstraction
behind a tiny interface makes the transport replaceable with Redis/pub-sub
later without changing callers.
"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(eq=False)
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any]]
    topics: frozenset[str]


class RealtimeBus:
    def __init__(self, history_size: int = 512, queue_size: int = 128) -> None:
        self._lock = threading.RLock()
        self._next_id = 0
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._subscribers: set[_Subscriber] = set()
        self._queue_size = queue_size
        self._snapshots: dict[str, Callable[[], Any]] = {}

    def register_snapshot(self, topic: str, provider: Callable[[], Any]) -> None:
        with self._lock:
            self._snapshots[topic] = provider

    def snapshot(self, topics: frozenset[str]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        with self._lock:
            providers = [(topic, self._snapshots.get(topic)) for topic in topics]
        for topic, provider in providers:
            if provider is None:
                continue
            try:
                result.append(self._event("snapshot", topic, f"snapshot:{topic}", provider()))
            except Exception:
                # A snapshot is a convenience for reconnect; a broken domain
                # must not prevent the SSE connection from serving other topics.
                continue
        return result

    def _event(self, event_type: str, topic: str, entity_id: str, payload: Any) -> dict[str, Any]:
        now = time.time()
        return {
            "id": str(self._next_id),
            "type": event_type,
            "topic": topic,
            "entityId": entity_id,
            "payload": self._public_payload(payload),
            "timestamp": now,
        }

    @classmethod
    def _public_payload(cls, value: Any) -> Any:
        blocked = {"token", "accesstoken", "refreshtoken", "cookie", "cookies", "password", "secret", "apikey", "api_key"}
        if isinstance(value, dict):
            return {
                str(key): cls._public_payload(item)
                for key, item in value.items()
                if str(key).replace("-", "").replace("_", "").lower() not in blocked
            }
        if isinstance(value, list):
            return [cls._public_payload(item) for item in value]
        return copy.deepcopy(value)

    def publish(self, topic: str, event_type: str, entity_id: str = "", payload: Any = None) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            event = self._event(event_type, topic, entity_id, payload)
            self._history.append(event)
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            if subscriber.topics and topic not in subscriber.topics:
                continue
            subscriber.loop.call_soon_threadsafe(self._offer, subscriber, event)
        return event

    @staticmethod
    def _offer(subscriber: _Subscriber, event: dict[str, Any]) -> None:
        try:
            subscriber.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                subscriber.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                subscriber.queue.put_nowait({
                    "id": event["id"],
                    "type": "snapshot.required",
                    "topic": event.get("topic", ""),
                    "entityId": "",
                    "payload": None,
                    "timestamp": time.time(),
                })
            except asyncio.QueueFull:
                pass

    def subscribe(self, loop: asyncio.AbstractEventLoop, topics: frozenset[str], after_id: int = 0) -> _Subscriber:
        subscriber = _Subscriber(loop, asyncio.Queue(maxsize=self._queue_size), topics)
        with self._lock:
            self._subscribers.add(subscriber)
            missed = [copy.deepcopy(event) for event in self._history if int(event["id"]) > after_id and (not topics or event["topic"] in topics)]
        for event in missed:
            self._offer(subscriber, event)
        return subscriber

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    @staticmethod
    def sse(event: dict[str, Any]) -> str:
        return f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


realtime = RealtimeBus()
