"""
Asyncio-based event bus used for module communication.

This is the minimal baseline required for Week 1 of the modularization
effort. The implementation supports simple topic-based publish/subscribe,
bounded queues, and structured logging hooks so we can evolve towards the
full design in later phases.
"""

from __future__ import annotations

import asyncio
import logging
from asyncio import QueueEmpty
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .contracts import BasePayload, EventHandler

logger = logging.getLogger(__name__)


Handler = Callable[[str, BasePayload], Awaitable[None]]


@dataclass(frozen=True)
class Subscription:
    """Handle for a topic subscription."""

    topic: str
    handler: EventHandler


class EventBus:
    """
    Minimal asynchronous publish/subscribe bus.

    Topics are matched exactly for now; explicit fan-out topics can be added
    to approximate simple wildcard patterns until we introduce configurable
    routers.
    """

    def __init__(self, *, queue_size: int = 256) -> None:
        self._queue: asyncio.Queue[tuple[str, BasePayload]] = asyncio.Queue(maxsize=queue_size)
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def subscribe(self, topic: str, handler: Handler) -> Subscription:
        """Register an async handler for a topic."""
        self._subscribers[topic].append(handler)
        logger.debug("Subscribed handler %s to topic %s", handler, topic)
        return Subscription(topic=topic, handler=handler)

    def unsubscribe(self, subscription: Subscription) -> None:
        """Detach a previously registered handler."""
        handlers = self._subscribers.get(subscription.topic, [])
        if subscription.handler in handlers:
            handlers.remove(subscription.handler)
            logger.debug(
                "Unsubscribed handler %s from topic %s", subscription.handler, subscription.topic
            )

    async def publish(self, topic: str, payload: BasePayload) -> None:
        """Publish a payload for a specific topic."""
        await self._queue.put((topic, payload))
        logger.debug("Queued payload for topic %s", topic)

    async def start(self) -> None:
        """Start the dispatcher loop."""
        if self._dispatcher_task is None:
            self._stopping.clear()
            self._dispatcher_task = asyncio.create_task(self._dispatcher(), name="spyoncino-bus")
            logger.info("Event bus dispatcher started.")

    async def stop(self) -> None:
        """Stop the dispatcher loop and drain remaining events."""
        if self._dispatcher_task is None:
            return
        self._stopping.set()
        await self._queue.put(("", _StopPayload()))
        await self._dispatcher_task
        self._dispatcher_task = None
        logger.info("Event bus dispatcher stopped.")

    async def _dispatcher(self) -> None:
        """Internal dispatcher loop that fans out events to subscribers."""
        while not self._stopping.is_set():
            topic, payload = await self._queue.get()
            try:
                if isinstance(payload, _StopPayload):
                    break

                handlers = list(self._subscribers.get(topic, []))
                logger.debug("Dispatching payload on topic %s to %d handlers", topic, len(handlers))
                if not handlers:
                    continue

                await asyncio.gather(
                    *(handler(topic, payload) for handler in handlers), return_exceptions=False
                )
            finally:
                self._queue.task_done()
        # Drain queue without processing after stop signal.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except QueueEmpty:
                break
            else:
                self._queue.task_done()


class _StopPayload(BasePayload):
    """Sentinel payload to signal dispatcher shutdown."""

    pass
