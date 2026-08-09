'''NetworkRecorder: capture full HTTP exchanges (request body, status, mime,
response body) for URLs matching a filter — the generic tool behind
"assert on the traffic, not the DOM" testing.

Bodies are fetched with Network.getResponseBody when loading finishes, which
also works for responses fulfilled by interception and for SSE streams once
they close.
'''

from __future__ import annotations

import asyncio
import typing
from contextlib import suppress
from dataclasses import dataclass, field

from .. import _json
from .. import _b64
from ..protocol import network as network_proto

if typing.TYPE_CHECKING:
    from .page import Page


@dataclass
class Exchange:
    '''One recorded request/response pair.

    Headers are the WIRE view: the base events' headers merged with the
    ``*ExtraInfo`` events, which carry what the browser actually put on the
    wire — ``Cookie``, ``Origin``, ``Sec-*`` on the request; ``Set-Cookie``
    (which the base event redacts) on the response. ExtraInfo does not fire
    for every request (interception-fulfilled responses, data: URLs, cache
    hits), so for those the headers are the base view only.
    '''

    url: str
    method: str
    request_body: str | None = None
    status: int | None = None
    mime: str | None = None
    body: bytes | None = None
    #: The request/load itself failed (a Network.loadingFailed: net error,
    #: blocked, aborted). None when the request completed — check this, not
    #: ``body_error``, to ask "did this request fail?".
    failed: str | None = None
    #: A body was expected but could not be fetched (evicted, session gone).
    #: NOT set for a legitimately bodyless response — a 204/304, a HEAD, or a
    #: redirect has no body by design, so ``body`` is None and this stays None.
    body_error: str | None = None
    #: Bodies were deliberately NOT captured — ``record(bodies=False)`` or a
    #: ``redact=`` hit. A choice, not a failure: ``body_error`` stays None.
    #: Byte counts survive in ``request_body_size`` / ``body_size``.
    redacted: bool = False
    #: Sizes recorded when the bytes themselves were withheld (redacted):
    #: the request body's length, and the response's TOTAL wire length
    #: (headers + body as received — CDP's encodedDataLength; the split
    #: isn't observable without fetching the body we chose not to fetch).
    request_body_size: int | None = None
    body_size: int | None = None
    #: Request headers as sent (base + requestWillBeSentExtraInfo).
    request_headers: dict = field(default_factory=dict)
    #: Response headers as received (base + responseReceivedExtraInfo;
    #: multiple Set-Cookie values arrive newline-joined, as CDP sends them).
    response_headers: dict = field(default_factory=dict)
    #: Wall-clock epoch seconds when the request went out — for correlating
    #: a capture against server logs.
    timestamp: float | None = None
    #: Seconds from request start to loading finished/failed.
    duration: float | None = None
    _started: float | None = field(default=None, repr=False)  # CDP monotonic
    _finished: bool = field(default=False, repr=False)
    _masked: bool = field(default=False, repr=False)  # redact= hit: scrub headers too

    def _guard_redacted(self, side: str) -> None:
        if self.redacted:
            raise ValueError(f'{side} body not captured (redacted) for '
                             f'{self.method} {self.url}')

    @property
    def request_json(self) -> typing.Any:
        '''Request body parsed as JSON ({} when absent).'''
        self._guard_redacted('request')
        return _json.loads(self.request_body) if self.request_body else {}

    @property
    def json(self) -> typing.Any:
        '''Response body parsed as JSON (None when absent).'''
        self._guard_redacted('response')
        return _json.loads(self.body) if self.body else None

    @property
    def text(self) -> str:
        self._guard_redacted('response')
        return self.body.decode('utf-8', 'replace') if self.body else ''


def _header(headers: dict, name: str) -> str:
    '''Case-insensitive header lookup (HTTP names have no canonical case).'''
    for key, value in headers.items():
        if key.lower() == name:
            return str(value)
    return ''


#: Header values scrubbed on a ``redact=`` exchange — on such an exchange the
#: session cookie is as much a credential as the body it accompanies.
_CREDENTIAL_HEADERS = frozenset(
    {'authorization', 'proxy-authorization', 'cookie', 'set-cookie'})


def _mask_credentials(headers: dict) -> dict:
    return {key: ('<redacted>' if key.lower() in _CREDENTIAL_HEADERS else value)
            for key, value in headers.items()}


def _har_headers(headers: dict) -> list[dict]:
    return [{'name': str(k), 'value': str(v)} for k, v in headers.items()]


def to_har(exchanges: typing.Iterable[Exchange]) -> dict:
    '''Exchanges as a HAR 1.2 dict — ``json.dump`` it to a ``.har`` file and
    any HAR viewer (browser devtools' network-tab import included) opens it.
    Fields the recorder doesn't observe (httpVersion, statusText, per-phase
    timings) are left empty/-1 per the spec; binary bodies go base64.'''
    from datetime import datetime, timezone
    from urllib.parse import parse_qsl, urlsplit

    entries = []
    for exchange in exchanges:
        started = datetime.fromtimestamp(exchange.timestamp or 0, timezone.utc)
        request = {
            'method': exchange.method,
            'url': exchange.url,
            'httpVersion': '',
            'cookies': [],
            'headers': _har_headers(exchange.request_headers),
            'queryString': [{'name': k, 'value': v} for k, v in
                            parse_qsl(urlsplit(exchange.url).query)],
            'headersSize': -1,
            'bodySize': (len(exchange.request_body.encode())
                         if exchange.request_body
                         else exchange.request_body_size or 0),
        }
        if exchange.request_body:
            request['postData'] = {
                'mimeType': _header(exchange.request_headers, 'content-type'),
                'text': exchange.request_body,
            }
        body = exchange.body or b''
        body_size = (exchange.body_size if exchange.redacted
                     and exchange.body_size is not None else len(body))
        content = {'size': body_size, 'mimeType': exchange.mime or ''}
        if not exchange.redacted:  # a redacted body has no text, only a size
            try:
                content['text'] = body.decode('utf-8')
            except UnicodeDecodeError:
                content['text'] = _b64.b64encode(body).decode('ascii')
                content['encoding'] = 'base64'
        if exchange.failed:
            content['comment'] = f'request failed: {exchange.failed}'
        elif exchange.redacted:
            content['comment'] = 'body redacted'
        elif exchange.body_error:
            content['comment'] = f'body unavailable: {exchange.body_error}'
        time_ms = (round(exchange.duration * 1000, 3)
                   if exchange.duration is not None else -1)
        entries.append({
            'startedDateTime': started.isoformat(),
            'time': time_ms,
            'request': request,
            'response': {
                'status': exchange.status or 0,
                'statusText': '',
                'httpVersion': '',
                'cookies': [],
                'headers': _har_headers(exchange.response_headers),
                'content': content,
                'redirectURL': '',
                'headersSize': -1,
                'bodySize': body_size,
            },
            'cache': {},
            'timings': {'send': 0, 'wait': time_ms, 'receive': 0},
        })
    from .. import __version__
    return {'log': {'version': '1.2',
                    'creator': {'name': 'purecdp', 'version': __version__},
                    'entries': entries}}


def _body_expected(exchange: Exchange) -> bool:
    '''Whether a successful response should have carried a body. 204/304, a
    HEAD or OPTIONS (CORS preflight) request, and redirects (3xx) are bodyless
    by design, so a failed getResponseBody on them is "no body", not an error.'''
    if exchange.method.upper() in ('HEAD', 'OPTIONS'):
        return False
    status = exchange.status
    if status is None:
        return True  # no ResponseReceived seen — assume a body was expected
    return status not in (204, 304) and not (300 <= status < 400)


def _parses_as_json(exchange: Exchange) -> bool:
    if exchange.body is None:
        return False
    try:
        _json.loads(exchange.body)
    except Exception:
        return False
    return True


class _ExchangeExpectation:
    '''Handle from :meth:`NetworkRecorder.expect`. ``await .value`` resolves to
    the first exchange completed after arming; ``.new`` is every exchange
    completed since arming (no waiting). Arming happens at creation, so it
    works held as a plain object or as an ``async with`` around the trigger.'''

    def __init__(self, recorder: NetworkRecorder, mark: int, *,
                 json: bool, timeout: float | None):
        self._recorder = recorder
        self._mark = mark
        self._json = json
        self._timeout = timeout
        self._resolved: Exchange | None = None

    async def __aenter__(self) -> _ExchangeExpectation:
        return self

    async def __aexit__(self, *exc_info: typing.Any) -> None:
        return None

    @property
    def new(self) -> list[Exchange]:
        '''Every exchange completed since arming, oldest first (no waiting).'''
        return self._recorder.exchanges[self._mark:]

    @property
    def skipped(self) -> list[Exchange]:
        '''The exchanges a ``json=True`` filter passed over before the one
        ``.value`` resolved to — what a proxy error page looks like when you
        want to log it. Empty until ``.value`` has resolved (and always empty
        without ``json=True``, which skips nothing).'''
        out: list[Exchange] = []
        if self._resolved is not None:
            for exchange in self.new:
                if exchange is self._resolved:
                    break
                out.append(exchange)
        return out

    @property
    def value(self) -> typing.Awaitable[Exchange]:
        return self._resolve()

    async def _resolve(self) -> Exchange:
        if self._resolved is None:
            self._resolved = await self._recorder.wait_for_next(
                self._mark, json=self._json, timeout=self._timeout)
        return self._resolved


class NetworkRecorder:
    '''Records exchanges whose URL contains ``needle`` (minus ``exclude``
    matches), or matching a custom ``predicate(url)``. Create via
    ``page.record(...)`` so its pump is cleaned up with the page.

    ``exchanges`` holds *completed* exchanges, oldest first; ``requests`` and
    ``responses`` are the JSON-parsed conveniences. To wait for a response,
    arm ``recorder.expect()`` before the trigger and ``await .value`` after.

    Privacy levers — both withhold bodies AT CAPTURE, the secret never enters
    the process (see :attr:`Exchange.redacted`):

    - ``bodies=False`` — metadata-only recording, recorder-wide: URL, method,
      status, headers, timing and byte counts, no bodies on either side (and
      no getResponseBody round-trip per exchange).
    - ``redact=lambda url: '/payment' in url`` — matching exchanges are fully
      private: no bodies AND credential header values (Authorization, Cookie,
      Set-Cookie) masked. On a secret-bearing exchange the session cookie is
      as much a credential as the body.
    '''

    def __init__(
        self,
        page: Page,
        *,
        needle: str = '',
        exclude: str | None = None,
        predicate: typing.Callable[[str], bool] | None = None,
        record_preflights: bool = False,
        default_timeout: float | None = None,
        bodies: bool = True,
        redact: typing.Callable[[str], bool] | None = None,
    ):
        self._page = page
        self._needle = needle
        self._exclude = exclude
        self._predicate = predicate
        self._record_preflights = record_preflights
        self._bodies = bodies
        self._redact = redact
        #: This recorder's own wait default (None -> the page default). The
        #: endpoint's latency profile, declared once at creation.
        self.default_timeout = default_timeout
        self.exchanges: list[Exchange] = []
        self._pending: dict[str, Exchange] = {}
        # request_id -> Exchange for the whole recorder lifetime: ExtraInfo
        # events carry no URL and CDP guarantees no arrival order, so late
        # ones (even after loadingFinished) still find their exchange here.
        self._by_id: dict[str, Exchange] = {}
        # ExtraInfo that arrived BEFORE its base event, buffered per request.
        self._early_request_extra: dict[str, dict] = {}
        self._early_response_extra: dict[str, dict] = {}
        # request_ids whose base event didn't match the filter — their
        # ExtraInfo is dropped instead of buffered forever.
        self._unmatched: set[str] = set()
        self._appended = asyncio.Event()

    def __await__(self):
        '''``await page.record()`` works (and is a no-op returning the
        recorder) — record() is sync on an otherwise all-async Page, and this
        kills the first-contact TypeError instead of teaching a lesson.'''
        return self
        yield  # pragma: no cover — unreachable; makes this a generator

    @property
    def requests(self) -> list[typing.Any]:
        return [e.request_json for e in self.exchanges]

    @property
    def responses(self) -> list[typing.Any]:
        return [e.json for e in self.exchanges]

    def set_filter(self, *, needle: str = '', exclude: str | None = None,
                   predicate: typing.Callable[[str], bool] | None = None) -> None:
        '''Change what this recorder captures GOING FORWARD (already-captured
        exchanges and their indices are untouched). Lets a long-lived recorder
        be narrowed after the fact — e.g. an MCP agent scoping to '/api' once it
        sees asset noise.'''
        self._needle = needle
        self._exclude = exclude
        self._predicate = predicate

    def _matches(self, url: str) -> bool:
        if self._predicate is not None:
            return self._predicate(url)
        if self._needle not in url:
            return False
        return not (self._exclude and self._exclude in url)

    def har(self) -> dict:
        '''The capture as a HAR 1.2 dict (see :func:`to_har`) — shareable,
        diffable, and openable in browser devtools.'''
        return to_har(self.exchanges)

    def save_har(self, path: str) -> int:
        '''Write the capture as a ``.har`` file; returns the entry count.'''
        import json as json_mod

        har = self.har()
        with open(path, 'w', encoding='utf-8') as fh:
            json_mod.dump(har, fh, indent=2)
        return len(har['log']['entries'])

    def expect(self, *, json: bool = False, timeout: float | None = None
               ) -> _ExchangeExpectation:
        '''Arm a wait for the next exchange BEFORE triggering it::

            turn = recorder.expect(json=True)
            await page.press('Enter')             # the trigger
            resp = (await turn.value).json

        (``async with recorder.expect() as turn:`` works too.) Arming pins the
        current position, so the length-bookkeeping ``wait_for_next`` needs is
        done for you and nothing that lands in between is missed. ``json=True``
        resolves to the first new exchange whose body parses as JSON, skipping
        those that don't (a dev proxy's interleaved HTML error page) — after
        ``.value`` resolves, ``.skipped`` lists exactly what was passed over,
        and everything stays in ``.new`` / ``exchanges``. Timeout: the
        ``timeout`` argument, else the recorder's ``default_timeout``, else
        the page default.'''
        return _ExchangeExpectation(self, len(self.exchanges),
                                    json=json, timeout=timeout)

    async def wait_for_next(
        self, previous_count: int, *,
        json: bool = False, timeout: float | None = None,
    ) -> Exchange:
        '''The first exchange past ``previous_count``, waiting for it if
        needed. Burst-safe: exchanges landing together are returned one per
        call, oldest first. ``json=True`` returns the first whose body parses
        as JSON, skipping those that don't (they stay in ``exchanges``).
        :meth:`expect` wraps this with the position captured for you. Timeout
        resolution: the ``timeout`` argument, else the recorder's
        ``default_timeout``, else the page default.'''
        index = previous_count
        async with asyncio.timeout(timeout or self.default_timeout
                                   or self._page.default_timeout):
            while True:
                while index < len(self.exchanges):
                    exchange = self.exchanges[index]
                    index += 1
                    if not json or _parses_as_json(exchange):
                        return exchange
                self._appended.clear()
                await self._appended.wait()

    # -- pump (driven by page.record()) --------------------------------------

    async def _pump(self, stream) -> None:
        with suppress(Exception):  # session teardown ends the pump
            async for event in stream:
                if isinstance(event, network_proto.RequestWillBeSent):
                    self._on_request(event)
                elif isinstance(event, network_proto.ResponseReceived):
                    self._on_response(event)
                elif isinstance(event, network_proto.RequestWillBeSentExtraInfo):
                    self._on_request_extra(event)
                elif isinstance(event, network_proto.ResponseReceivedExtraInfo):
                    self._on_response_extra(event)
                elif isinstance(event, network_proto.LoadingFinished):
                    await self._on_done(event, failed=None)
                elif isinstance(event, network_proto.LoadingFailed):
                    await self._on_done(event, failed=str(event.error_text))

    def _on_request(self, event) -> None:
        request_id = str(event.request_id)
        if (not self._matches(event.request.url)
                or (not self._record_preflights
                    and event.request.method.upper() == 'OPTIONS')):
            # remember the miss so this request's ExtraInfo is dropped, and
            # release anything already buffered for it
            self._unmatched.add(request_id)
            self._early_request_extra.pop(request_id, None)
            self._early_response_extra.pop(request_id, None)
            return
        # privacy decision happens HERE, before the secret is ever stored:
        # postData arrives unconditionally on the base event, so a redacted
        # request body is dropped at capture, not scrubbed after the fact
        masked = self._redact is not None and self._redact(event.request.url)
        private = masked or not self._bodies
        post_data = event.request.post_data
        exchange = Exchange(
            url=event.request.url,
            method=event.request.method,
            request_body=None if private else post_data,
            request_headers=dict(event.request.headers or {}),
            timestamp=float(event.wall_time),
            _started=float(event.timestamp),
            redacted=private,
            _masked=masked,
        )
        if private and post_data is not None:
            exchange.request_body_size = len(post_data.encode())
        # ExtraInfo may already have arrived (CDP guarantees no order);
        # its headers are the wire truth, so they win on key collisions.
        exchange.request_headers.update(
            self._early_request_extra.pop(request_id, {}))
        exchange.response_headers.update(
            self._early_response_extra.pop(request_id, {}))
        if masked:
            exchange.request_headers = _mask_credentials(exchange.request_headers)
            exchange.response_headers = _mask_credentials(exchange.response_headers)
        self._pending[request_id] = exchange
        self._by_id[request_id] = exchange

    def _on_response(self, event) -> None:
        exchange = self._by_id.get(str(event.request_id))
        if exchange is not None:
            exchange.status = int(event.response.status)
            exchange.mime = str(event.response.mime_type)
            # anything already present came from ExtraInfo — keep it on top
            exchange.response_headers = {
                **dict(event.response.headers or {}),
                **exchange.response_headers}
            if exchange._masked:
                exchange.response_headers = _mask_credentials(
                    exchange.response_headers)

    def _on_request_extra(self, event) -> None:
        request_id = str(event.request_id)
        if request_id in self._unmatched:
            return
        exchange = self._by_id.get(request_id)
        if exchange is not None:
            exchange.request_headers.update(dict(event.headers or {}))
            if exchange._masked:  # ExtraInfo is where Cookie actually arrives
                exchange.request_headers = _mask_credentials(
                    exchange.request_headers)
        else:  # ExtraInfo beat the base event — buffer until it arrives
            self._early_request_extra.setdefault(request_id, {}).update(
                dict(event.headers or {}))

    def _on_response_extra(self, event) -> None:
        request_id = str(event.request_id)
        if request_id in self._unmatched:
            return
        exchange = self._by_id.get(request_id)
        if exchange is not None:
            exchange.response_headers.update(dict(event.headers or {}))
            if exchange._masked:  # Set-Cookie only ever arrives via ExtraInfo
                exchange.response_headers = _mask_credentials(
                    exchange.response_headers)
        else:
            self._early_response_extra.setdefault(request_id, {}).update(
                dict(event.headers or {}))

    async def _on_done(self, event, failed: str | None) -> None:
        exchange = self._pending.pop(str(event.request_id), None)
        if exchange is None:
            return
        if exchange._started is not None:  # same monotonic clock as the start
            exchange.duration = max(0.0, float(event.timestamp) - exchange._started)
        if failed is not None:
            exchange.failed = failed          # the request itself failed
        elif exchange.redacted:
            # deliberately no getResponseBody: the bytes never enter the
            # process. The wire length still lands — metadata is the point.
            exchange.body_size = int(event.encoded_data_length)
        else:
            try:
                body, is_b64 = await self._page.session.execute(
                    network_proto.get_response_body(
                        network_proto.RequestId(str(event.request_id))))
                exchange.body = (_b64.b64decode(body) if is_b64
                                 else body.encode())
            except Exception as exc:  # a lost body must not kill the pump
                # a bodyless response (204/304, HEAD, redirect) has nothing to
                # fetch — getResponseBody reports "No resource ..." and that is
                # NOT an error; only flag it when a body was actually expected.
                if _body_expected(exchange):
                    exchange.body_error = repr(exc)
        exchange._finished = True
        self.exchanges.append(exchange)
        self._appended.set()
