'''Fetch-domain request interception: stub, rewrite, or abort requests.

Wire-level plumbing lives here; the user-facing entry point is
``Page.route(pattern, handler)``.
'''

from __future__ import annotations

import asyncio
import fnmatch
import json as _json
import typing
from dataclasses import dataclass

from .. import _fastjson
from ._driver import Paused

#: An async callable receiving an InterceptedRequest; it should call exactly
#: one of fulfill/continue_/abort (unhandled requests are continued).
Handler = typing.Callable[['InterceptedRequest'], typing.Awaitable[None]]
#: Response/request headers: a dict, or (name, value) pairs when a name
#: repeats (several Set-Cookie).
Headers = typing.Union[dict, typing.Iterable[tuple[str, str]]]


@dataclass
class Route:
    #: fnmatch glob over the full URL ('*' crosses '/'), or a
    #: ``callable(url) -> bool`` — the same predicate form record() takes.
    pattern: str | typing.Callable[[str], bool]
    handler: Handler


def matching_routes(routes: list[Route], url: str) -> list[Route]:
    '''Every route whose pattern matches ``url``, in registration order.'''
    return [route for route in routes
            if (route.pattern(url) if callable(route.pattern)
                else fnmatch.fnmatch(url, route.pattern))]


def match_route(routes: list[Route], url: str) -> Route | None:
    matches = matching_routes(routes, url)
    return matches[0] if matches else None


#: Headers that describe one hop, never forwarded by a relay (RFC 9110 §7.6.1)
#: plus the ones urllib / Chrome compute themselves.
_NOT_FORWARDED = frozenset({
    'connection', 'keep-alive', 'transfer-encoding', 'te', 'trailer',
    'upgrade', 'proxy-authorization', 'proxy-authenticate',
    'proxy-connection', 'host', 'content-length', 'accept-encoding',
})
#: ...and on the way back, the target's CORS grant is replaced by fulfill()'s
#: echo of the page's own Origin — the page asked, the page is allowed.
_NOT_RETURNED = _NOT_FORWARDED | {'content-encoding',
                                  'access-control-allow-origin'}


def _fetch_sync(url: str, method: str, body: bytes | None,
                headers: dict[str, str], timeout: float
                ) -> tuple[int, list[tuple[str, str]], bytes]:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, list(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as err:  # 4xx/5xx IS a response
        with err:
            return err.code, list(err.headers.items()), err.read()


async def forward(request: InterceptedRequest, target_base: str, *,
                  headers: dict[str, str] | None = None,
                  timeout: float = 10.0) -> None:
    '''Forward ``request`` to ``target_base`` (scheme://host[:port]) — same
    path, query, method, body and headers — and fulfill it with whatever
    came back: status, headers and body, 4xx/5xx included. Transparent:
    ``Origin`` / ``Cookie`` go out unchanged and ``Set-Cookie`` comes back.
    Stdlib ``urllib`` on a worker thread; an unreachable or timing-out
    target becomes a 502 whose body names the target (visible in a recorder
    and in artifacts, unlike an aborted request). Whole-body only: no
    streaming, so nothing server-sent survives a relay.'''
    from urllib.parse import urlsplit

    parts = urlsplit(request.url)
    url = (target_base.rstrip('/') + parts.path
           + (f'?{parts.query}' if parts.query else ''))
    out = {k: v for k, v in request.headers.items()
           if k.lower() not in _NOT_FORWARDED}
    out['Accept-Encoding'] = 'identity'  # urllib does not decompress
    out.update(headers or {})
    try:
        status, resp_headers, body = await asyncio.to_thread(
            _fetch_sync, url, request.method, request.post_data_bytes, out,
            timeout)
    except OSError as err:  # URLError, refused, timeout — all OSError
        await request.fulfill(
            status=502, content_type='text/plain',
            body=f'relay: {request.method} {url}: {err}')
        return
    await request.fulfill(
        status=status, body=body,
        headers=[(k, v) for k, v in resp_headers
                 if k.lower() not in _NOT_RETURNED])


class InterceptedRequest:
    '''One paused network request. Call exactly one of :meth:`fulfill`,
    :meth:`continue_`, or :meth:`abort`.'''

    def __init__(self, driver: typing.Any, event: Paused):
        self._driver = driver
        self._event = event
        self.handled = False

    @property
    def url(self) -> str:
        return self._event.url

    @property
    def method(self) -> str:
        return self._event.method

    @property
    def headers(self) -> dict:
        return dict(self._event.headers)

    @property
    def resource_type(self) -> str:
        return self._event.resource_type or ''

    @property
    def post_data_bytes(self) -> bytes | None:
        '''The request body as sent, or None when the request has none.
        Everything knowable is on the paused event already (no round-trip).'''
        return self._event.post_data

    @property
    def post_data(self) -> str | None:
        '''The request body as text (the name ``fulfill``/``continue_``
        already use), or None when the request has none.'''
        raw = self.post_data_bytes
        return raw.decode('utf-8', 'replace') if raw is not None else None

    @property
    def json(self) -> typing.Any:
        '''Request body parsed as JSON — None when there is no body.'''
        raw = self.post_data_bytes
        return _fastjson.loads(raw) if raw else None

    def header(self, name: str) -> str | None:
        '''Case-insensitive request-header lookup (None when absent).'''
        return next((v for k, v in self._event.headers.items()
                     if k.lower() == name.lower()), None)

    _header = header  # pre-0.9.0 private spelling, kept for one release

    @property
    def is_cors_preflight(self) -> bool:
        return (self.method.upper() == 'OPTIONS'
                and self.header('Access-Control-Request-Method') is not None)

    async def respond_preflight(self) -> None:
        '''Answer a CORS preflight permissively (what a stubbed API would
        allow). Page's route pump calls this automatically for matched
        preflights unless ``page.auto_preflight`` is False.'''
        await self.fulfill(
            status=204,
            cors=False,
            headers={
                'Access-Control-Allow-Origin': self.header('Origin') or '*',
                'Access-Control-Allow-Methods':
                    self.header('Access-Control-Request-Method') or '*',
                'Access-Control-Allow-Headers':
                    self.header('Access-Control-Request-Headers') or '*',
                'Access-Control-Max-Age': '600',
            },
        )

    @staticmethod
    def _header_entries(headers: Headers | None) -> list[tuple[str, str]]:
        pairs = headers.items() if isinstance(headers, dict) else (headers or ())
        return [(k, v) for k, v in pairs]

    async def fulfill(
        self,
        *,
        status: int = 200,
        body: str | bytes = b'',
        json: typing.Any = None,
        content_type: str | None = None,
        headers: Headers | None = None,
        cors: bool = True,
    ) -> None:
        '''Answer the request with a stubbed response (never hits network).

        ``json=payload`` serializes the payload and defaults the content type
        to application/json — the common case for mocking APIs at the
        browser edge.

        By default a permissive Access-Control-Allow-Origin header is added
        (echoing the request's Origin) so cross-origin fetch() stubs just
        work; pass ``cors=False`` — or your own ACAO header — when the test
        is *about* CORS behavior.
        '''
        if json is not None:
            body = _json.dumps(json)
            if content_type is None:
                content_type = 'application/json'
        self.handled = True
        entries = self._header_entries(headers)
        if content_type is not None:
            entries.append(('Content-Type', content_type))
        if cors and not any(k.lower() == 'access-control-allow-origin'
                            for k, _ in entries):
            entries.append(('Access-Control-Allow-Origin',
                            self.header('Origin') or '*'))
        raw = body.encode() if isinstance(body, str) else body
        await self._driver.fulfill(self._event.request_id, status=status,
                                   headers=entries, body=raw)

    async def continue_(
        self,
        *,
        url: str | None = None,
        method: str | None = None,
        post_data: str | bytes | None = None,
        headers: Headers | None = None,
    ) -> None:
        '''Let the request proceed, optionally rewriting parts of it.'''
        self.handled = True
        raw = post_data.encode() if isinstance(post_data, str) else post_data
        await self._driver.continue_(
            self._event.request_id, url=url, method=method, post_data=raw,
            headers=self._header_entries(headers) if headers else None)

    async def abort(self, reason: str = 'Aborted') -> None:
        '''Fail the request (fetch() in the page rejects).'''
        self.handled = True
        await self._driver.fail(self._event.request_id, reason)
