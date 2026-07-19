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


class NetworkRecorder:
    '''Records exchanges whose URL contains ``needle`` (minus ``exclude``
    matches), or matching a custom ``predicate(url)``. Create via
    ``page.record(...)`` so its pump is cleaned up with the page.

    ``exchanges`` holds *completed* exchanges, oldest first; ``requests`` and
    ``responses`` are the JSON-parsed conveniences. Capture the length before
    triggering, then ``await recorder.wait_for_next(previous_len)``.
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

    async def wait_for_next(
        self, previous_count: int, *, timeout: float | None = None
    ) -> Exchange:
        '''Return the newest exchange once more than previous_count exist.'''
        async with asyncio.timeout(timeout or self._page.default_timeout):
            while len(self.exchanges) <= previous_count:
                self._appended.clear()
                await self._appended.wait()
        return self.exchanges[-1]

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
