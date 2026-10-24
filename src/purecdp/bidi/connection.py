'''One WebDriver BiDi endpoint over a websocket: commands, events, session.

Framing (the whole codec)::

    out  {"id": N, "method": "module.command", "params": {...}}
    in   {"type": "success", "id": N, "result": {...}}
         {"type": "error",   "id": N, "error": "<code>", "message": "...",
                             "stacktrace"?: "..."}
         {"type": "event",   "method": "module.event", "params": {...}}

No session multiplexing: one BiDi session per connection, and commands
address a browsing context through their own ``context`` parameter. Events
arrive only for what ``session.subscribe`` asked for.
'''

from __future__ import annotations

import asyncio
import collections
import typing
import warnings
from contextlib import suppress
from dataclasses import dataclass

from .. import _fastjson
from ..connection import Transport
from ..errors import (CDPCommandTimeout, CDPConnectionClosed, CDPProtocolError,
                      PureCDPError)
from ..transport.websocket import WebSocketTransport


class BidiCommandError(PureCDPError):
    '''A command error reported by the browser: BiDi error codes are strings
    (``"invalid argument"``, ``"no such frame"``, ``"unknown command"``, …).'''

    def __init__(self, code: str, message: str, stacktrace: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.stacktrace = stacktrace

    def __str__(self) -> str:
        return f'[{self.code}] {self.message}'


@dataclass(slots=True)
class BidiEvent:
    method: str
    params: dict


class BidiEventStream:
    '''Buffered async iterator over events whose method is in ``methods`` (all
    events when empty). Same contract as :class:`purecdp.connection.EventStream`:
    close it (or leave the ``with`` block) to unsubscribe.'''

    def __init__(self, owner: BidiConnection, methods: tuple[str, ...],
                 buffer_size: int):
        self._owner = owner
        self._methods = frozenset(methods)
        self._buffer_size = buffer_size
        self._queue: collections.deque[BidiEvent] = collections.deque()
        self._warned = False
        self._wake = asyncio.Event()
        self._ended: Exception | None = None
        self._done = False

    def wants(self, method: str) -> bool:
        return not self._methods or method in self._methods

    def _push(self, event: BidiEvent) -> None:
        if self._done:
            return
        if len(self._queue) >= self._buffer_size:
            self._queue.popleft()
            if not self._warned:  # same contract as the CDP EventStream
                self._warned = True
                warnings.warn(
                    f'BidiEventStream buffer full ({self._buffer_size}); '
                    'dropping oldest event', RuntimeWarning, stacklevel=2)
        self._queue.append(event)
        self._wake.set()

    def _finish(self, exc: Exception | None) -> None:
        self._done, self._ended = True, exc
        self._wake.set()

    def close(self) -> None:
        self._owner._listeners.discard(self)
        self._finish(None)

    def __enter__(self) -> BidiEventStream:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __aiter__(self) -> BidiEventStream:
        return self

    async def __anext__(self) -> BidiEvent:
        while True:
            if self._queue:
                return self._queue.popleft()
            if self._done:
                if self._ended is not None:
                    raise self._ended
                raise StopAsyncIteration
            self._wake.clear()
            await self._wake.wait()


class BidiConnection:
    '''Commands and events over one BiDi websocket. Async context manager;
    :meth:`new_session` must run before any other command.'''

    def __init__(self, transport: Transport):
        self._transport = transport
        self._next_id = 1
        self._futures: dict[int, asyncio.Future] = {}
        self._pending_methods: dict[int, str] = {}
        self._listeners: set[BidiEventStream] = set()
        self._reader_task: asyncio.Task | None = None
        self._closed_reason: str | None = None
        #: (event, context) pairs already subscribed; context None = browser-wide
        self._subscribed: set[tuple[str, str | None]] = set()
        #: Browsing contexts the browser reported destroyed (tab closed by the
        #: user, ``window.close()``, ``browsingContext.close`` from anyone) —
        #: the whole subtree of each ``contextDestroyed`` event. Drivers
        #: consult it for ``alive``; needs ``browsingContext.contextDestroyed``
        #: subscribed (``BidiDriver.arm`` does).
        self.destroyed_contexts: set[str] = set()
        #: ``session.new`` result: the browser's capabilities (browserName,
        #: browserVersion, userAgent, …) and the session id.
        self.capabilities: dict = {}
        self.session_id: str | None = None
        #: Bound EVERY command (seconds); None = unbounded.
        self.default_command_timeout: float | None = None

    # -- lifecycle -----------------------------------------------------------

    async def open(self) -> None:
        if self._reader_task is None and self._closed_reason is None:
            self._reader_task = asyncio.create_task(
                self._read_loop(), name='purecdp-bidi-reader')

    async def __aenter__(self) -> BidiConnection:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._reader_task is not None:
            task, self._reader_task = self._reader_task, None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._abort('connection closed')
        with suppress(Exception):
            await self._transport.close()

    @property
    def closed(self) -> bool:
        return self._closed_reason is not None

    # -- commands ------------------------------------------------------------

    async def call(self, method: str, params: dict | None = None, *,
                   timeout: float | None = None) -> dict:
        '''Send ``method`` and return its ``result`` dict; a browser error
        raises :class:`BidiCommandError`.'''
        if self._closed_reason is not None:
            raise CDPConnectionClosed(self._closed_reason)
        if timeout is None:
            timeout = self.default_command_timeout
        cmd_id = self._next_id
        self._next_id += 1
        wire = _fastjson.dumps({'id': cmd_id, 'method': method,
                                'params': params or {}})
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._futures[cmd_id] = future
        self._pending_methods[cmd_id] = method
        try:
            if timeout is None:
                await self._transport.send(wire)
                return await future
            try:
                async with asyncio.timeout(timeout):
                    await self._transport.send(wire)
                    return await future
            except TimeoutError:
                raise CDPCommandTimeout(
                    f'{method} did not return within {timeout:g}s') from None
        finally:
            self._futures.pop(cmd_id, None)
            self._pending_methods.pop(cmd_id, None)

    async def new_session(self, capabilities: dict | None = None) -> dict:
        '''``session.new`` — the BiDi handshake. Returns and remembers the
        browser's capabilities.'''
        # CapabilitiesRequest = {alwaysMatch?, firstMatch?}: a flat dict here
        # is silently ignored by the browser (Firefox echoed
        # acceptInsecureCerts: false for a flat request).
        result = await self.call('session.new', {
            'capabilities': {'alwaysMatch': dict(capabilities or {})}})
        self.session_id = result.get('sessionId')
        self.capabilities = dict(result.get('capabilities', {}))
        return self.capabilities

    async def subscribe(self, events: typing.Sequence[str],
                        contexts: typing.Sequence[str] | None = None) -> None:
        '''``session.subscribe`` for what is not subscribed yet. Scope it to
        ``contexts`` whenever you can: on a browser that is somebody's (many
        tabs), a browser-wide network/log subscription is a firehose purecdp
        would only filter back out. A browser-wide subscription covers every
        context; a context-scoped one is remembered per (event, context).'''
        if contexts is None:
            wanted = [e for e in events if (e, None) not in self._subscribed]
            if wanted:
                await self.call('session.subscribe', {'events': wanted})
                self._subscribed.update((e, None) for e in wanted)
            return
        for ctx in contexts:
            wanted = [e for e in events
                      if (e, None) not in self._subscribed and (e, ctx) not in self._subscribed]
            if wanted:
                await self.call('session.subscribe', {'events': wanted, 'contexts': [ctx]})
                self._subscribed.update((e, ctx) for e in wanted)

    # -- events --------------------------------------------------------------

    def listen(self, *methods: str, buffer_size: int = 256) -> BidiEventStream:
        '''Subscribe locally to events by method name (all when none given).
        Pair with :meth:`subscribe` so the browser actually sends them.'''
        stream = BidiEventStream(self, methods, buffer_size)
        if self._closed_reason is not None:
            stream._finish(None)
        else:
            self._listeners.add(stream)
        return stream

    async def wait_for(self, *methods: str,
                       predicate: typing.Callable[[BidiEvent], bool] | None = None,
                       timeout: float | None = None) -> BidiEvent:
        async with asyncio.timeout(timeout):
            with self.listen(*methods) as stream:
                async for event in stream:
                    if predicate is None or predicate(event):
                        return event
        raise CDPConnectionClosed(self._closed_reason or 'event stream ended')

    # -- reader --------------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            async for raw in self._transport:
                self._handle(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._abort(f'transport error: {exc!r}')
        else:
            self._abort('transport closed by peer')

    def _handle(self, raw: str) -> None:
        try:
            msg = _fastjson.loads(raw)
        except (ValueError, TypeError) as exc:
            raise CDPProtocolError(f'unparseable message: {exc}') from exc
        if not isinstance(msg, dict):
            raise CDPProtocolError(f'message is not an object: {msg!r}')
        kind = msg.get('type')
        if kind == 'event':
            event = BidiEvent(method=msg.get('method', ''),
                              params=msg.get('params', {}))
            if event.method == 'browsingContext.contextDestroyed':
                self._note_destroyed(event.params)
            for stream in list(self._listeners):
                if stream.wants(event.method):
                    stream._push(event)
            return
        if kind in ('success', 'error') and 'id' in msg:
            cmd_id = msg['id']
            self._pending_methods.pop(cmd_id, None)
            future = self._futures.get(cmd_id)
            if future is None or future.done():
                return  # late reply after a timeout: dropped
            if kind == 'success':
                future.set_result(msg.get('result', {}))
            else:
                future.set_exception(BidiCommandError(
                    code=str(msg.get('error', 'unknown error')),
                    message=str(msg.get('message', '')),
                    stacktrace=msg.get('stacktrace')))
            return
        raise CDPProtocolError(f'message is neither response nor event: {msg!r}')

    def _note_destroyed(self, info: dict) -> None:
        stack = [info]
        while stack:
            node = stack.pop()
            if node.get('context') is not None:
                self.destroyed_contexts.add(str(node['context']))
            stack.extend(node.get('children') or [])

    def _abort(self, reason: str) -> None:
        if self._closed_reason is not None:
            return
        self._closed_reason = reason
        for future in list(self._futures.values()):
            if not future.done():
                future.set_exception(CDPConnectionClosed(reason))
        for stream in list(self._listeners):
            stream._finish(None)
        self._listeners.clear()


async def connect(url: str, *, capabilities: dict | None = None) -> BidiConnection:
    '''Open a BiDi connection to ``ws://host:port/session`` and run the
    ``session.new`` handshake.'''
    transport = await WebSocketTransport.connect(url)
    connection = BidiConnection(transport)
    await connection.open()
    try:
        await connection.new_session(capabilities)
    except BaseException:
        await connection.aclose()
        raise
    return connection
