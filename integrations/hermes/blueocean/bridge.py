"""Synchronous access to the BlueOcean MCP server.

Hermes calls a memory provider synchronously from its own threads, while the
MCP client is async and holds anyio task groups open for the life of a
session. This module runs one event loop on one background thread, keeps a
single long-lived ClientSession on it, and offers two sync entry points:
``call`` (bounded wait for a result) and ``submit`` (fire and forget,
delivered in order by a writer task).

A circuit breaker stops a stopped server from costing every Hermes turn the
full call timeout. A dead session (server restarted) is replaced by one
reconnect inside the same call.

Imports nothing from Hermes and no sibling module. The thread factory is
injected: the provider passes Hermes's ``spawn_context_thread``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

logger = logging.getLogger(__name__)

CALL_TIMEOUT_S = 5.0
COOLDOWN_S = 30.0
QUEUE_SIZE = 200

SpawnFn = Callable[[Callable[[], None], str], threading.Thread]


class BridgeUnavailable(Exception):
    """The server could not be reached, refused us, or did not answer in time."""


class ToolCallError(BridgeUnavailable):
    """The server answered but the tool reported an error. Not an outage."""


class Breaker:
    """Skip calls for ``cooldown`` seconds after a failure.

    ``failure`` and ``success`` return True on the transition into and out
    of an outage, so the caller logs once per outage rather than per call.
    """

    def __init__(self, cooldown: float = COOLDOWN_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._cooldown = cooldown
        self._clock = clock
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def remaining(self) -> float:
        with self._lock:
            if self._opened_at is None:
                return 0.0
            return max(0.0, self._cooldown - (self._clock() - self._opened_at))

    def allow(self) -> bool:
        return self.remaining() == 0.0

    def failure(self) -> bool:
        with self._lock:
            first = self._opened_at is None
            self._opened_at = self._clock()
            return first

    def success(self) -> bool:
        with self._lock:
            recovered = self._opened_at is not None
            self._opened_at = None
            return recovered


def _leaves(exc: BaseException) -> Iterator[BaseException]:
    nested = getattr(exc, "exceptions", None)
    if nested:
        for inner in nested:
            yield from _leaves(inner)
    else:
        yield exc


def describe(exc: BaseException, url: str) -> str:
    """A log-ready reason. The MCP client reports a 401 only as MCPError."""
    leaves = list(_leaves(exc))
    names = {type(e).__name__ for e in leaves}
    if "ConnectError" in names or any(isinstance(e, OSError) for e in leaves):
        return f"BlueOcean unreachable at {url}"
    if "MCPError" in names:
        return "BlueOcean rejected the request (check BLUEOCEAN_AUTH_TOKEN)"
    return f"BlueOcean call failed: {type(leaves[0]).__name__}"


def _decode(result: Any) -> Any:
    text = "".join(getattr(block, "text", "") for block in result.content)
    if result.is_error:  # mcp 2.x field name; 1.x called it isError
        raise ToolCallError(text or "tool reported an error")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _plain_thread(target: Callable[[], None], name: str) -> threading.Thread:
    return threading.Thread(target=target, name=name, daemon=True)


class Bridge:
    def __init__(self, url: str, token: str, *, call_timeout: float = CALL_TIMEOUT_S,
                 cooldown: float = COOLDOWN_S, queue_size: int = QUEUE_SIZE,
                 spawn: SpawnFn | None = None) -> None:
        self._url = url
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._call_timeout = call_timeout
        self._cooldown = cooldown
        self._queue_size = queue_size
        self._spawn = spawn or _plain_thread
        self._breaker = Breaker(cooldown)
        self._start_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Touched only on the loop thread:
        self._queue: asyncio.Queue | None = None
        self._connect_lock: asyncio.Lock | None = None
        self._session: ClientSession | None = None
        self._owner: asyncio.Task | None = None
        self._closing: asyncio.Event | None = None
        self._tasks: set[asyncio.Task] = set()  # retired owners still closing, the writer
        self._dropping = False

    # -- sync surface ------------------------------------------------------

    def call(self, tool: str, args: dict, *, timeout: float | None = None) -> Any:
        if not self._breaker.allow():
            raise BridgeUnavailable("BlueOcean paused after a recent failure")
        wait = self._call_timeout if timeout is None else timeout
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(self._call_with_retry(tool, args), loop)
        try:
            return future.result(timeout=wait)
        except concurrent.futures.TimeoutError:
            future.cancel()
            if self._breaker.failure():
                logger.warning("BlueOcean did not answer within %.0fs; pausing calls for %.0fs",
                               wait, self._cooldown)
            raise BridgeUnavailable("BlueOcean timed out") from None

    def submit(self, tool: str, args: dict) -> None:
        loop = self._ensure_loop()
        loop.call_soon_threadsafe(self._enqueue, tool, args)

    def drain(self, timeout: float) -> bool:
        """Wait for queued writes, including the one in flight. False on timeout."""
        if self._loop is None or self._queue is None:
            return True
        future = asyncio.run_coroutine_threadsafe(self._queue.join(), self._loop)
        try:
            future.result(timeout=timeout)
            return True
        except concurrent.futures.TimeoutError:
            future.cancel()
            return False

    # -- loop thread -------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def run() -> None:
                asyncio.set_event_loop(loop)
                self._queue = asyncio.Queue(maxsize=self._queue_size)
                self._connect_lock = asyncio.Lock()
                self._keep(loop.create_task(self._writer()))
                loop.call_soon(ready.set)
                loop.run_forever()

            self._spawn(run, "blueocean-bridge").start()
            if not ready.wait(5):
                raise BridgeUnavailable("BlueOcean bridge thread did not start")
            self._loop = loop
            return loop

    def _keep(self, task: asyncio.Task) -> asyncio.Task:
        """Hold a strong reference until the task ends; the loop holds only weak ones."""
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _call_with_retry(self, tool: str, args: dict) -> Any:
        last: BaseException | None = None
        attempt_timeout = self._call_timeout / 2  # two attempts fit in call()'s deadline
        for _ in range(2):  # the second attempt runs on a fresh session
            session: ClientSession | None = None
            try:
                session = await asyncio.wait_for(self._ensure_session(), attempt_timeout)
                result = await asyncio.wait_for(session.call_tool(tool, args), attempt_timeout)
            except Exception as exc:  # noqa: BLE001 - any failure: reconnect once, then describe()
                last = exc
                await self._reset_session(session)
                continue
            if self._breaker.success():
                logger.info("BlueOcean reachable again at %s", self._url)
            return _decode(result)
        message = describe(last, self._url)
        if self._breaker.failure():
            logger.warning("%s; pausing BlueOcean calls for %.0fs", message, self._cooldown)
        raise BridgeUnavailable(message) from last

    async def _ensure_session(self) -> ClientSession:
        assert self._connect_lock is not None
        async with self._connect_lock:
            if self._session is not None:
                return self._session
            ready: asyncio.Future = asyncio.get_running_loop().create_future()
            self._closing = asyncio.Event()
            self._owner = self._keep(asyncio.create_task(self._own_session(ready, self._closing)))
            return await ready

    async def _own_session(self, ready: asyncio.Future, closing: asyncio.Event) -> None:
        """Enter and exit the session in this one task: anyio requires a task
        group to be exited by the task that entered it."""
        mine: ClientSession | None = None
        try:
            async with AsyncExitStack() as stack:
                client = await stack.enter_async_context(
                    create_mcp_http_client(headers=self._headers))
                streams = await stack.enter_async_context(
                    streamable_http_client(self._url, http_client=client))
                mine = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await mine.initialize()
                if closing.is_set() or self._owner is not asyncio.current_task():
                    # Retired while connecting: its caller gave up, or a newer
                    # owner took over. Close (leave the stack) rather than
                    # replace the current session or linger.
                    if not ready.done():
                        ready.set_exception(
                            BridgeUnavailable("BlueOcean session was retired while connecting"))
                    return
                self._session = mine
                if not ready.done():
                    ready.set_result(mine)
                await closing.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except BaseException as exc:  # noqa: BLE001 - handed to the waiting caller via ready
            if not ready.done():
                ready.set_exception(exc)
            else:
                logger.debug("BlueOcean session ended: %s", describe(exc, self._url))
        finally:
            if self._session is mine:
                self._session = None

    async def _reset_session(self, failed: ClientSession | None) -> None:
        """Retire ``failed``, the session the failing attempt called on, unless
        another caller has already replaced it: tearing down the replacement
        would break that caller's in-flight call.

        ``failed`` is None when the attempt failed while obtaining a session.
        Then an owner still connecting is cancelled, since it only checks
        ``closing`` once connected. This runs straight after the failing
        attempt released the connect lock, with no await in between, so the
        owner it retires is the one that attempt was waiting for. If the lock
        is held, another caller is still waiting on that owner and its own
        attempt decides; cancelling would hand it a CancelledError.

        Retired owners close in the background (``_keep`` holds them), so the
        retry is not delayed by a slow close.
        """
        if failed is not self._session:
            return
        if failed is None and self._connect_lock is not None and self._connect_lock.locked():
            return
        owner, closing = self._owner, self._closing
        self._session = None
        self._owner = None
        self._closing = None
        if closing is not None:
            closing.set()
        if failed is None and owner is not None:
            owner.cancel()

    def _enqueue(self, tool: str, args: dict) -> None:
        assert self._queue is not None
        if self._queue.full():
            self._queue.get_nowait()
            self._queue.task_done()
            if not self._dropping:
                logger.warning("BlueOcean write queue full (%d); dropping the oldest writes",
                               self._queue_size)
                self._dropping = True
        self._queue.put_nowait((tool, args))

    async def _writer(self) -> None:
        assert self._queue is not None
        while True:
            tool, args = await self._queue.get()
            try:
                await self._deliver(tool, args)
            except Exception:
                logger.warning("BlueOcean write failed", exc_info=True)
            finally:
                self._queue.task_done()
            if self._queue.empty():
                self._dropping = False

    async def _deliver(self, tool: str, args: dict) -> None:
        """Keep one write until it lands: a short outage must not lose it."""
        while True:
            wait = self._breaker.remaining()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            try:
                await self._call_with_retry(tool, args)
                return
            except ToolCallError as exc:
                logger.warning("BlueOcean rejected %s: %s", tool, exc)
                return
            except BridgeUnavailable:
                await asyncio.sleep(0.05)
