'''Layer 1½: asyncio Connection/Session over an abstract Transport.

Wraps the sans-I/O :class:`purecdp.engine.Engine` with the things asyncio is for:
a reader task, futures for pending commands, and per-session event streams.
Modern CDP is multi-session — one socket to the browser, then
``Target.attachToTarget(flatten=True)`` per target, with page-level messages
carrying a ``sessionId`` — so :class:`Connection` (the socket) and
:class:`Session` (one attachment) are separate objects. Browser-level traffic
uses ``Connection.root``, a session with no id; Connection delegates
execute/listen/wait_for to it for convenience.

Transports (M3) only need to satisfy the tiny :class:`Transport` protocol,
which is also what lets tests drive a Connection from a scripted in-memory
transport (tests/support.py).
'''

from __future__ import annotations

import asyncio
import collections
import typing
import warnings
from contextlib import suppress

from .engine import (
    CommandGenerator,
    CommandFailure,
    CommandResult,
    Engine,
    EventReceived,
)
from .errors import CDPConnectionClosed, CDPSessionClosed

#: Target.* events the Connection acts on regardless of listeners — these are
#: always parsed so session bookkeeping (attach/detach/crash/info) stays live.
_LIFECYCLE_METHODS = frozenset({
    'Target.attachedToTarget',
    'Target.detachedFromTarget',
    'Target.targetCrashed',
    'Target.targetInfoChanged',
})


class Transport(typing.Protocol):
    '''What Connection needs from a transport: send strings, iterate incoming
    strings (one CDP message each), close. Iteration ending = peer closed.'''

    async def send(self, message: str) -> None: ...
    async def close(self) -> None: ...
    def __aiter__(self) -> typing.AsyncIterator[str]: ...


def _wanted_methods(event_types: tuple[type, ...]) -> frozenset[str] | None:
    '''Map listen(*types) to the CDP method names it wants, or None ("all")
    when no types are given or a type carries no EVENT_METHOD (e.g. listening
    for UnknownEvent, which can arrive under any method).'''
    if not event_types:
        return None
    methods = set()
    for t in event_types:
        method = getattr(t, 'EVENT_METHOD', None)
        if method is None:
            return None
        methods.add(method)
    return frozenset(methods)


class EventStream:
    '''Async iterator over one session's events, optionally type-filtered.

    Subscribe **before** triggering the thing you expect events from; events
    that arrive while nobody listens are dropped by design (CDP is a firehose).
    Buffering is bounded: when full, the oldest event is dropped and a
    RuntimeWarning is emitted once. Iteration ends (StopAsyncIteration) when
    the session detaches, or raises if the connection aborted.

    Usable as a context manager; otherwise call :meth:`close` when done.
    '''

    def __init__(
        self,
        session: Session,
        event_types: tuple[type, ...],
        buffer_size: int,
    ):
        self._session = session
        self._types = event_types
        #: Method names this stream wants, or None for "all events". Lets the
        #: dispatcher decide whether to parse an event before building it.
        self._wanted_methods = _wanted_methods(event_types)
        self._buffer_size = buffer_size
        self._items: collections.deque[typing.Any] = collections.deque()
        self._wakeup = asyncio.Event()
        self._ended = False
        self._end_exc: Exception | None = None
        self._warned = False

    @property
    def end_exception(self) -> Exception | None:
        return self._end_exc

    def _push(self, event: typing.Any) -> None:
        if self._ended:
            return
        if self._types and not isinstance(event, self._types):
            return
        if len(self._items) >= self._buffer_size:
            self._items.popleft()
            if not self._warned:
                self._warned = True
                warnings.warn(
                    f'EventStream buffer full ({self._buffer_size}); '
                    'dropping oldest event',
                    RuntimeWarning,
                    stacklevel=2,
                )
        self._items.append(event)
        self._wakeup.set()

    def _finish(self, exc: Exception | None) -> None:
        self._ended = True
        self._end_exc = exc
        self._wakeup.set()

    def close(self) -> None:
        '''Stop receiving; buffered events remain readable until exhausted.'''
        self._session._listeners.discard(self)
        self._finish(None)

    def __enter__(self) -> EventStream:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __aiter__(self) -> EventStream:
        return self

    async def __anext__(self) -> typing.Any:
        while True:
            if self._items:
                return self._items.popleft()
            if self._ended:
                if self._end_exc is not None:
                    raise self._end_exc
                raise StopAsyncIteration
            self._wakeup.clear()
            await self._wakeup.wait()


class Session:
    '''One CDP session: the browser-level root (session_id None) or one
    flat-mode target attachment.'''

    def __init__(self, connection: Connection, session_id: str | None):
        self.connection = connection
        self.session_id = session_id
        #: Target this session is attached to; filled by attach()/auto-attach.
        self.target_id: str | None = None
        #: Latest protocol.target.TargetInfo, kept fresh via
        #: Target.attachedToTarget / Target.targetInfoChanged.
        self.target_info: typing.Any = None
        self._listeners: set[EventStream] = set()
        self._closed_reason: str | None = None

    @property
    def closed(self) -> bool:
        return self._closed_reason is not None

    async def execute(self, cmd: CommandGenerator) -> typing.Any:
        '''Run one generated command on this session and return its typed
        result. Raises CDPError if the browser reports an error.'''
        if self.closed and self.session_id is not None:
            raise CDPSessionClosed(self._closed_reason)
        return await self.connection._execute(cmd, self.session_id)

    async def detach(self) -> None:
        '''Detach from the target, leaving it running (vs. closing it).'''
        from .protocol import target as _target

        if self.session_id is None:
            raise ValueError('cannot detach the browser-level root session')
        await self.connection._execute(
            _target.detach_from_target(
                session_id=_target.SessionID(self.session_id)
            ),
            None,  # detachFromTarget goes to the browser, not the dying session
        )

    async def set_auto_attach(
        self, *, wait_for_debugger: bool = False, filter: typing.Any = None
    ) -> None:
        '''Auto-attach to targets related to this one (popups, OOPIFs,
        workers; every new target when called on the browser-level session).
        New sessions appear in connection.sessions via Target.attachedToTarget;
        paused ones are resumed automatically unless
        ``connection.resume_waiting_targets`` is False.'''
        from .protocol import target as _target

        await self.execute(
            _target.set_auto_attach(
                auto_attach=True,
                wait_for_debugger_on_start=wait_for_debugger,
                flatten=True,
                filter=filter,
            )
        )

    def listen(self, *event_types: type, buffer_size: int = 256) -> EventStream:
        '''Subscribe to this session's events (all of them if no types given).'''
        stream = EventStream(self, event_types, buffer_size)
        if self.closed:
            stream._finish(None)
        else:
            self._listeners.add(stream)
        return stream

    async def wait_for(
        self,
        *event_types: type,
        predicate: typing.Callable[[typing.Any], bool] | None = None,
        timeout: float | None = None,
    ) -> typing.Any:
        '''Return the next matching event. Subscribes immediately — but only
        to events from now on, so call this (or listen()) before triggering.'''
        async with asyncio.timeout(timeout):
            with self.listen(*event_types) as stream:
                async for event in stream:
                    if predicate is None or predicate(event):
                        return event
                raise self._end_error('event stream ended while waiting')

    def _end_error(self, context: str) -> Exception:
        if self.session_id is None:
            return CDPConnectionClosed(f'{context}: {self._closed_reason}')
        return CDPSessionClosed(f'{context}: {self._closed_reason}')

    def _wants(self, method: str) -> bool:
        '''Does any listener want this event method (before we parse it)?'''
        for stream in self._listeners:
            wanted = stream._wanted_methods
            if wanted is None or method in wanted:
                return True
        return False

    def _dispatch_event(self, event: typing.Any) -> None:
        for stream in list(self._listeners):
            stream._push(event)

    def _close(self, reason: str, exc: Exception | None) -> None:
        if self.closed:
            return
        self._closed_reason = reason
        for stream in list(self._listeners):
            stream._finish(exc)
        self._listeners.clear()


class Connection:
    '''One socket-level CDP endpoint over a :class:`Transport`.

    Use as an async context manager (or call open()/aclose()); a background
    reader task dispatches incoming traffic to sessions.
    '''

    def __init__(self, transport: Transport):
        self._transport = transport
        self._engine = Engine()
        self._futures: dict[int, asyncio.Future] = {}
        self._closed_reason: str | None = None
        self._reader_task: asyncio.Task | None = None
        self.root = Session(self, session_id=None)
        self._sessions: dict[str, Session] = {}
        self._tasks: set[asyncio.Task] = set()
        #: When a target attaches paused (waitForDebuggerOnStart), send
        #: Runtime.runIfWaitingForDebugger automatically.
        self.resume_waiting_targets = True
        #: async hook(session) run on each target that attaches paused, before
        #: it is resumed — see add_target_init_hook.
        self._target_init_hooks: list[typing.Callable] = []
        #: async hook(session) run on every attach (any depth) — see
        #: add_session_hook; used to propagate auto-attach down nested OOPIFs.
        self._session_hooks: list[typing.Callable] = []

    # -- lifecycle -----------------------------------------------------------

    async def open(self) -> None:
        if self._reader_task is None and self._closed_reason is None:
            self._reader_task = asyncio.create_task(
                self._read_loop(), name='purecdp-connection-reader'
            )

    async def __aenter__(self) -> Connection:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        '''Close the connection: stop reading, fail anything pending, close
        the transport. Idempotent.'''
        if self._reader_task is not None:
            task, self._reader_task = self._reader_task, None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        for task in list(self._tasks):
            task.cancel()
        self._abort('connection closed')
        with suppress(Exception):
            await self._transport.close()

    @property
    def closed(self) -> bool:
        return self._closed_reason is not None

    # -- sessions ------------------------------------------------------------

    @property
    def sessions(self) -> dict[str, Session]:
        '''Live attachments by session id (snapshot copy).'''
        return dict(self._sessions)

    def session(self, session_id: str) -> Session:
        '''Get (or create a handle for) the session with this id.'''
        session = self._sessions.get(session_id)
        if session is None:
            session = self._sessions[session_id] = Session(self, session_id)
        return session

    async def attach(self, target_id: str) -> Session:
        '''Attach to a target in flat mode and return its Session.'''
        from .protocol import target as _target

        session_id = await self.execute(
            _target.attach_to_target(
                target_id=_target.TargetID(str(target_id)), flatten=True
            )
        )
        session = self.session(str(session_id))
        session.target_id = str(target_id)
        return session

    # -- browser-level convenience (delegates to the root session) -----------

    async def execute(self, cmd: CommandGenerator) -> typing.Any:
        return await self.root.execute(cmd)

    def listen(self, *event_types: type, buffer_size: int = 256) -> EventStream:
        return self.root.listen(*event_types, buffer_size=buffer_size)

    async def wait_for(
        self,
        *event_types: type,
        predicate: typing.Callable[[typing.Any], bool] | None = None,
        timeout: float | None = None,
    ) -> typing.Any:
        return await self.root.wait_for(
            *event_types, predicate=predicate, timeout=timeout
        )

    async def set_auto_attach(
        self, *, wait_for_debugger: bool = False, filter: typing.Any = None
    ) -> None:
        await self.root.set_auto_attach(
            wait_for_debugger=wait_for_debugger, filter=filter
        )

    # -- internals -----------------------------------------------------------

    async def _execute(
        self, cmd: CommandGenerator, session_id: str | None
    ) -> typing.Any:
        if self._closed_reason is not None:
            raise CDPConnectionClosed(self._closed_reason)
        cmd_id, wire = self._engine.start_command(cmd, session_id)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._futures[cmd_id] = future
        try:
            await self._transport.send(wire)
            return await future
        finally:
            self._futures.pop(cmd_id, None)

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
        happening = self._engine.receive(raw)
        match happening:
            case CommandResult(id=cmd_id, value=value):
                future = self._futures.get(cmd_id)
                if future is not None and not future.done():
                    future.set_result(value)
            case CommandFailure(id=cmd_id, error=error):
                future = self._futures.get(cmd_id)
                if future is not None and not future.done():
                    future.set_exception(error)
            case EventReceived() as received:
                session = (self.root if received.session_id is None
                           else self.session(received.session_id))
                lifecycle = received.method in _LIFECYCLE_METHODS
                wanted = session._wants(received.method)
                # parse (EventReceived.event) only if someone needs it
                if lifecycle or wanted:
                    event = received.event
                    if wanted:
                        session._dispatch_event(event)
                    if lifecycle:
                        self._handle_lifecycle(event)

    def _handle_lifecycle(self, event: typing.Any) -> None:
        '''Track Target.* lifecycle regardless of which session announced it
        (browser-level auto-attach and per-session OOPIF/worker auto-attach
        arrive on different sessions but mean the same thing).'''
        method = getattr(event, 'EVENT_METHOD', None)
        if method == 'Target.attachedToTarget':
            session = self.session(str(event.session_id))
            session.target_id = str(event.target_info.target_id)
            session.target_info = event.target_info
            for hook in list(self._session_hooks):
                self._spawn(self._run_session_hook(hook, session))
            if event.waiting_for_debugger and self.resume_waiting_targets:
                self._spawn(self._resume_target(session))
        elif method == 'Target.detachedFromTarget':
            session = self._sessions.pop(str(event.session_id), None)
            if session is not None:
                session._close('session detached', None)
        elif method == 'Target.targetCrashed':
            crashed_id = str(event.target_id)
            for session in list(self._sessions.values()):
                if session.target_id == crashed_id:
                    self._sessions.pop(session.session_id, None)
                    session._close(
                        'target crashed', CDPSessionClosed('target crashed')
                    )
        elif method == 'Target.targetInfoChanged':
            changed_id = str(event.target_info.target_id)
            for session in self._sessions.values():
                if session.target_id == changed_id:
                    session.target_info = event.target_info

    def _spawn(self, coro: typing.Coroutine) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_session_hook(
        self, hook: typing.Callable, session: Session
    ) -> None:
        with suppress(Exception):  # a bad hook must not disturb dispatch
            await hook(session)

    def add_session_hook(
        self, hook: typing.Callable[[Session], typing.Awaitable[None]]
    ) -> typing.Callable[[], None]:
        '''Register ``hook(session)`` run (fire-and-forget) whenever *any* target
        attaches — paused or not, at any depth. Used to propagate auto-attach
        down nested out-of-process iframes (each level must arm its own children;
        setAutoAttach doesn't recurse). Returns an unregister callable.'''
        self._session_hooks.append(hook)

        def remove() -> None:
            with suppress(ValueError):
                self._session_hooks.remove(hook)

        return remove

    def add_target_init_hook(
        self, hook: typing.Callable[[Session], typing.Awaitable[None]]
    ) -> typing.Callable[[], None]:
        '''Register ``hook(session)`` to run on every target that auto-attaches
        *paused* (waitForDebuggerOnStart), before it is resumed — the one moment
        a worker's globals can be patched before its own scripts run.
        ``Page.addScriptToEvaluateOnNewDocument`` does not reach workers, so
        this is how e.g. stealth patches get into worker scope. Requires
        ``resume_waiting_targets`` (the default) so the paused target is driven.
        Returns a callable that unregisters the hook.'''
        self._target_init_hooks.append(hook)

        def remove() -> None:
            with suppress(ValueError):
                self._target_init_hooks.remove(hook)

        return remove

    async def _resume_target(self, session: Session) -> None:
        from .protocol import runtime as _runtime

        for hook in list(self._target_init_hooks):
            with suppress(Exception):  # a bad hook must not strand the target
                await hook(session)
        with suppress(Exception):  # the target may die before we get there
            await session.execute(_runtime.run_if_waiting_for_debugger())

    def _abort(self, reason: str) -> None:
        if self._closed_reason is not None:
            return
        self._closed_reason = reason
        for future in list(self._futures.values()):
            if not future.done():
                future.set_exception(CDPConnectionClosed(reason))
        for session in (self.root, *self._sessions.values()):
            session._close(reason, CDPConnectionClosed(reason))
