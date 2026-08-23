'''Layer 1: sans-I/O CDP engine.

A pure state machine — no sockets, no asyncio, no clocks. Outgoing commands go
in as generated command generators and come out as wire strings; incoming wire
strings come out as typed "happenings" (:class:`CommandResult`,
:class:`CommandFailure`, :class:`EventReceived`). Any transport can drive it,
which is what makes the whole protocol layer testable by replaying recorded
JSON traffic (see tests/test_engine.py).
'''

from __future__ import annotations

import typing
import warnings
from dataclasses import dataclass

from . import _fastjson, protocol
from ._shared import T_JSON_DICT, UnknownEvent, parse_event
from .errors import CDPCommandError, CDPProtocolError, ProtocolDriftWarning

#: The shape of every generated command: yields one request, receives the raw
#: result, returns the typed value.
CommandGenerator = typing.Generator[T_JSON_DICT, T_JSON_DICT, typing.Any]


@dataclass(frozen=True)
class _Pending:
    gen: CommandGenerator
    session_id: str | None
    method: str = ''


@dataclass(frozen=True)
class CommandResult:
    '''A pending command completed; ``value`` is its typed return value.'''

    id: int
    session_id: str | None
    value: typing.Any


@dataclass(frozen=True)
class CommandFailure:
    '''A pending command failed. ``error`` is a :class:`CDPCommandError` from the
    browser, or the local exception raised while parsing the result.'''

    id: int
    session_id: str | None
    error: Exception


#: Resolved-domain memo: CDP domain name -> is it in the pinned spec (and its
#: module imported)? Shared across engines; import side-effects are global and
#: idempotent, so caching the yes/no answer skips a per-event importlib call.
_DOMAIN_KNOWN: dict[str, bool] = {}


def resolve_event(method: str, params: T_JSON_DICT) -> typing.Any:
    '''Parse a CDP event to a typed dataclass, or :class:`UnknownEvent` when
    the method's domain is outside the pinned spec or the payload fails to
    parse (protocol drift). Domain lookup is memoized.'''
    known = _DOMAIN_KNOWN.get(method.partition('.')[0])
    if known is None:
        domain = method.partition('.')[0]
        try:
            protocol.import_domain(domain)
            known = True
        except KeyError:
            known = False  # outside the pinned spec — expected drift
        _DOMAIN_KNOWN[domain] = known
    if not known:
        return UnknownEvent(method=method, params=params)
    try:
        return parse_event(method, params)
    except Exception as exc:
        warnings.warn(
            f'failed to parse event {method}: {exc!r}; delivering raw payload',
            ProtocolDriftWarning,
            stacklevel=2,
        )
        return UnknownEvent(method=method, params=params)


class EventReceived:
    '''An event arrived. ``event`` is parsed **lazily on first access** — a
    generated event dataclass, or :class:`UnknownEvent` for drift — so the
    dispatcher can skip parsing events no listener wants. ``method`` and
    ``params`` are the cheap, always-available raw form.'''

    __slots__ = ('session_id', 'method', 'params', '_event', '_parsed')

    def __init__(self, session_id: str | None, method: str, params: T_JSON_DICT):
        self.session_id = session_id
        self.method = method
        self.params = params
        self._parsed = False
        self._event: typing.Any = None

    @property
    def event(self) -> typing.Any:
        if not self._parsed:
            self._event = resolve_event(self.method, self.params)
            self._parsed = True
        return self._event


Happening = CommandResult | CommandFailure | EventReceived


class Engine:
    '''Correlates command ids and parses incoming CDP messages.

    One Engine per socket-level endpoint. Session routing state (which
    session_id maps to which Session object) lives a layer up, in
    purecdp.connection; the engine only tags happenings with the session id.
    '''

    def __init__(self) -> None:
        self._next_id = 1
        self._pending: dict[int, _Pending] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def start_command(
        self, cmd: CommandGenerator, session_id: str | None = None
    ) -> tuple[int, str]:
        '''Assign an id to a generated command and serialize its request.

        Returns ``(command_id, wire_string)``; the caller must send the wire
        string and later feed the response back via :meth:`receive`.
        '''
        request = cmd.send(None)
        cmd_id = self._next_id
        self._next_id += 1
        request['id'] = cmd_id
        if session_id is not None:
            request['sessionId'] = session_id
        self._pending[cmd_id] = _Pending(
            gen=cmd, session_id=session_id, method=request.get('method', '')
        )
        return cmd_id, _fastjson.dumps(request)

    def pending_method(self, cmd_id: int) -> str:
        '''CDP method name of a still-pending command ('' once resolved or
        unknown) — lets a caller name what it was waiting on after a timeout.'''
        pending = self._pending.get(cmd_id)
        return pending.method if pending is not None else ''

    def receive(self, raw: str | bytes) -> Happening:
        '''Process one incoming wire message (CDP sends one JSON object per
        websocket/pipe message) into a happening.

        Raises :class:`CDPProtocolError` for messages that cannot belong to
        this connection at all; per-command parse problems are contained in
        :class:`CommandFailure` instead so one bad response cannot take down
        dispatch for everything else.
        '''
        try:
            msg = _fastjson.loads(raw)
        except (ValueError, TypeError) as exc:
            raise CDPProtocolError(f'unparseable message: {exc}') from exc
        if not isinstance(msg, dict):
            raise CDPProtocolError(f'message is not an object: {msg!r}')
        if 'id' in msg:
            return self._receive_response(msg)
        if 'method' in msg:
            # do NOT parse here — EventReceived.event parses lazily, so the
            # connection can drop events no listener wants without paying for
            # a dataclass it will never read.
            return EventReceived(
                session_id=msg.get('sessionId'),
                method=msg['method'],
                params=msg.get('params', {}),
            )
        raise CDPProtocolError(f'message is neither response nor event: {msg!r}')

    def _receive_response(self, msg: T_JSON_DICT) -> CommandResult | CommandFailure:
        cmd_id = msg['id']
        pending = self._pending.pop(cmd_id, None)
        if pending is None:
            raise CDPProtocolError(f'response for unknown command id {cmd_id!r}')
        if 'error' in msg:
            pending.gen.close()
            err = msg['error']
            error = CDPCommandError(
                code=err.get('code', 0),
                message=err.get('message', ''),
                data=err.get('data'),
            )
            return CommandFailure(id=cmd_id, session_id=pending.session_id, error=error)
        try:
            pending.gen.send(msg.get('result', {}))
        except StopIteration as stop:
            return CommandResult(
                id=cmd_id, session_id=pending.session_id, value=stop.value
            )
        except Exception as exc:
            return CommandFailure(id=cmd_id, session_id=pending.session_id, error=exc)
        raise RuntimeError('command generator yielded more than one request')
