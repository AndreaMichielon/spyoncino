"""
Asyncio-based event bus used for module communication.

This is the minimal baseline required for Week 1 of the modularization
effort. The implementation supports simple topic-based publish/subscribe,
bounded queues, and structured logging hooks so we can evolve towards the
full design in later phases.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from asyncio import QueueEmpty
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .contracts import BasePayload, BusStatus, EventHandler

logger = logging.getLogger(__name__)


Handler = Callable[[str, BasePayload], Awaitable[None]]
Interceptor = Callable[[str, BasePayload], Awaitable[tuple[str, BasePayload] | None]]


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

    def __init__(
        self,
        *,
        queue_size: int = 256,
        telemetry_topic: str = "status.bus",
        telemetry_interval: float = 5.0,
        telemetry_enabled: bool = True,
    ) -> None:
        self._queue: asyncio.Queue[tuple[str, BasePayload]] = asyncio.Queue(maxsize=queue_size)
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._telemetry_task: asyncio.Task[None] | None = None
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()
        self._telemetry_topic = telemetry_topic
        self._telemetry_interval = telemetry_interval
        self._telemetry_enabled = telemetry_enabled
        self._published_total = 0
        self._processed_total = 0
        self._dropped_total = 0
        self._interceptors: list[Interceptor] = []
        now = time.monotonic()
        self._last_publish_ts = now
        self._last_dispatch_ts = now

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

    def add_interceptor(self, interceptor: Interceptor) -> None:
        """Register an interceptor that can mutate or drop events prior to enqueuing."""
        self._interceptors.append(interceptor)
        logger.debug("Registered interceptor %s", interceptor)

    def remove_interceptor(self, interceptor: Interceptor) -> None:
        """Remove a previously registered interceptor."""
        with contextlib.suppress(ValueError):
            self._interceptors.remove(interceptor)

    async def publish(self, topic: str, payload: BasePayload) -> None:
        """Publish a payload for a specific topic."""
        self._published_total += 1
        self._last_publish_ts = time.monotonic()
        topic_to_publish = topic
        payload_to_publish = payload
        if self._interceptors:
            for interceptor in list(self._interceptors):
                try:
                    result = await interceptor(topic_to_publish, payload_to_publish)
                except Exception:  # pragma: no cover - defensive
                    logger.exception(
                        "Interceptor %s failed; continuing without changes.", interceptor
                    )
                    continue
                if result is None:
                    self._dropped_total += 1
                    logger.debug("Event dropped by interceptor %s on topic %s", interceptor, topic)
                    return
                topic_to_publish, payload_to_publish = result
        if self._queue.full():
            logger.warning("Event bus queue is full; publisher will wait for free space.")
        await self._queue.put((topic_to_publish, payload_to_publish))
        logger.debug("Queued payload for topic %s", topic_to_publish)

    async def start(self) -> None:
        """Start the dispatcher loop."""
        if self._dispatcher_task is None:
            self._stopping.clear()
            self._dispatcher_task = asyncio.create_task(self._dispatcher(), name="spyoncino-bus")
            logger.info("Event bus dispatcher started.")
        if self._telemetry_enabled and self._telemetry_task is None:
            self._telemetry_task = asyncio.create_task(
                self._telemetry_loop(), name="spyoncino-bus-telemetry"
            )

    async def stop(self) -> None:
        """Stop the dispatcher loop and drain remaining events."""
        if self._dispatcher_task is None:
            return
        self._stopping.set()
        await self._queue.put(("", _StopPayload()))
        await self._dispatcher_task
        self._dispatcher_task = None
        # Wait for any in-flight handler tasks to complete
        if self._handler_tasks:
            pending = list(self._handler_tasks)
            self._handler_tasks.clear()
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info("Event bus dispatcher stopped.")
        if self._telemetry_task:
            self._telemetry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._telemetry_task
            self._telemetry_task = None

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

                # Schedule handlers asynchronously to avoid deadlocks when handlers publish
                # back into the bus (queue backpressure while dispatcher awaits handlers).
                for handler in handlers:
                    task = asyncio.create_task(self._call_handler(handler, topic, payload))
                    self._handler_tasks.add(task)

                    def _on_done(t: asyncio.Task[None], _topic: str = topic) -> None:
                        # Remove from tracking set
                        self._handler_tasks.discard(t)
                        if t.cancelled():
                            return
                        exc = t.exception()
                        if exc is not None:
                            logger.exception(
                                "Subscriber handler failed on topic %s", _topic, exc_info=exc
                            )

                    task.add_done_callback(_on_done)
                self._processed_total += 1
                self._last_dispatch_ts = time.monotonic()
            finally:
                self._queue.task_done()
        # Drain queue without processing after stop signal.
        while not self._queue.empty():
            try:
                _topic, _payload = self._queue.get_nowait()
            except QueueEmpty:
                break
            else:
                self._dropped_total += 1
                self._queue.task_done()
        logger.info("Event bus dispatcher drained %d dropped events.", self._dropped_total)

    async def _telemetry_loop(self) -> None:
        """Emit periodic BusStatus payloads on the telemetry topic."""
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(self._telemetry_interval)
                status = self._build_status_payload()
                await self.publish(self._telemetry_topic, status)
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            return

    async def _call_handler(self, handler: Handler, topic: str, payload: BasePayload) -> None:
        """
        Invoke a handler that may be sync or async. If it returns an awaitable, await it;
        otherwise execute synchronously.
        """
        result = handler(topic, payload)
        if inspect.isawaitable(result):
            await result  # type: ignore[func-returns-value]

    def _build_status_payload(self) -> BusStatus:
        depth = self._queue.qsize()
        capacity = self._queue.maxsize
        subscriber_count = sum(len(handlers) for handlers in self._subscribers.values())
        topic_count = len(self._subscribers)
        lag = max(0.0, self._last_publish_ts - self._last_dispatch_ts)
        ratio = depth / capacity if capacity else 0.0
        if ratio >= 0.9:
            watermark = "critical"
        elif ratio >= 0.75:
            watermark = "high"
        else:
            watermark = "normal"
        return BusStatus(
            queue_depth=depth,
            queue_capacity=capacity,
            subscriber_count=subscriber_count,
            topic_count=topic_count,
            published_total=self._published_total,
            processed_total=self._processed_total,
            dropped_total=self._dropped_total,
            lag_seconds=lag,
            watermark=watermark,
        )


class _StopPayload(BasePayload):
    """Sentinel payload to signal dispatcher shutdown."""

    pass
