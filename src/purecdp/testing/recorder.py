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
    '''One recorded request/response pair.'''

    url: str
    method: str
    request_body: str | None = None
    status: int | None = None
    mime: str | None = None
    body: bytes | None = None
    body_error: str | None = None  # why body is None, when it failed
    _finished: bool = field(default=False, repr=False)

    @property
    def request_json(self) -> typing.Any:
        '''Request body parsed as JSON ({} when absent).'''
        return _json.loads(self.request_body) if self.request_body else {}

    @property
    def json(self) -> typing.Any:
        '''Response body parsed as JSON (None when absent).'''
        return _json.loads(self.body) if self.body else None

    @property
    def text(self) -> str:
        return self.body.decode('utf-8', 'replace') if self.body else ''


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

    async def __aenter__(self) -> _ExchangeExpectation:
        return self

    async def __aexit__(self, *exc_info: typing.Any) -> None:
        return None

    @property
    def new(self) -> list[Exchange]:
        '''Every exchange completed since arming, oldest first (no waiting).'''
        return self._recorder.exchanges[self._mark:]

    @property
    def value(self) -> typing.Awaitable[Exchange]:
        return self._recorder.wait_for_next(
            self._mark, json=self._json, timeout=self._timeout)


class NetworkRecorder:
    '''Records exchanges whose URL contains ``needle`` (minus ``exclude``
    matches), or matching a custom ``predicate(url)``. Create via
    ``page.record(...)`` so its pump is cleaned up with the page.

    ``exchanges`` holds *completed* exchanges, oldest first; ``requests`` and
    ``responses`` are the JSON-parsed conveniences. To wait for a response,
    arm ``recorder.expect()`` before the trigger and ``await .value`` after.
    '''

    def __init__(
        self,
        page: Page,
        *,
        needle: str = '',
        exclude: str | None = None,
        predicate: typing.Callable[[str], bool] | None = None,
        record_preflights: bool = False,
    ):
        self._page = page
        self._needle = needle
        self._exclude = exclude
        self._predicate = predicate
        self._record_preflights = record_preflights
        self.exchanges: list[Exchange] = []
        self._pending: dict[str, Exchange] = {}
        self._appended = asyncio.Event()

    @property
    def requests(self) -> list[typing.Any]:
        return [e.request_json for e in self.exchanges]

    @property
    def responses(self) -> list[typing.Any]:
        return [e.json for e in self.exchanges]

    def _matches(self, url: str) -> bool:
        if self._predicate is not None:
            return self._predicate(url)
        if self._needle not in url:
            return False
        return not (self._exclude and self._exclude in url)

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
        those that don't (a dev proxy's interleaved HTML error page) — the
        skipped ones still appear in ``.new`` and ``exchanges``. ``.new`` lists
        everything captured since arming, no waiting.'''
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
        :meth:`expect` wraps this with the position captured for you.'''
        index = previous_count
        async with asyncio.timeout(timeout or self._page.default_timeout):
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
                elif isinstance(event, network_proto.LoadingFinished):
                    await self._on_done(event, failed=None)
                elif isinstance(event, network_proto.LoadingFailed):
                    await self._on_done(event, failed=str(event.error_text))

    def _on_request(self, event) -> None:
        if not self._matches(event.request.url):
            return
        if (not self._record_preflights
                and event.request.method.upper() == 'OPTIONS'):
            return  # CORS preflights are noise for traffic assertions
        self._pending[str(event.request_id)] = Exchange(
            url=event.request.url,
            method=event.request.method,
            request_body=event.request.post_data,
        )

    def _on_response(self, event) -> None:
        exchange = self._pending.get(str(event.request_id))
        if exchange is not None:
            exchange.status = int(event.response.status)
            exchange.mime = str(event.response.mime_type)

    async def _on_done(self, event, failed: str | None) -> None:
        exchange = self._pending.pop(str(event.request_id), None)
        if exchange is None:
            return
        if failed is not None:
            exchange.body_error = failed
        else:
            try:
                body, is_b64 = await self._page.session.execute(
                    network_proto.get_response_body(
                        network_proto.RequestId(str(event.request_id))))
                exchange.body = (_b64.b64decode(body) if is_b64
                                 else body.encode())
            except Exception as exc:  # a lost body must not kill the pump
                exchange.body_error = repr(exc)
        exchange._finished = True
        self.exchanges.append(exchange)
        self._appended.set()
