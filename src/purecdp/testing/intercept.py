'''Fetch-domain request interception: stub, rewrite, or abort requests.

Wire-level plumbing lives here; the user-facing entry point is
``Page.route(pattern, handler)``.
'''

from __future__ import annotations

import fnmatch
import json as _json
import typing
from dataclasses import dataclass

from ..connection import Session
from ..protocol import fetch as fetch_proto
from ..protocol import network as network_proto
from .. import _b64

#: An async callable receiving an InterceptedRequest; it should call exactly
#: one of fulfill/continue_/abort (unhandled requests are continued).
Handler = typing.Callable[['InterceptedRequest'], typing.Awaitable[None]]


@dataclass
class Route:
    #: fnmatch glob over the full URL ('*' crosses '/'), or a
    #: ``callable(url) -> bool`` — the same predicate form record() takes.
    pattern: str | typing.Callable[[str], bool]
    handler: Handler


def match_route(routes: list[Route], url: str) -> Route | None:
    for route in routes:
        if (route.pattern(url) if callable(route.pattern)
                else fnmatch.fnmatch(url, route.pattern)):
            return route
    return None


class InterceptedRequest:
    '''One paused network request. Call exactly one of :meth:`fulfill`,
    :meth:`continue_`, or :meth:`abort`.'''

    def __init__(self, session: Session, event: fetch_proto.RequestPaused):
        self._session = session
        self._event = event
        self.handled = False

    @property
    def url(self) -> str:
        return self._event.request.url

    @property
    def method(self) -> str:
        return self._event.request.method

    @property
    def headers(self) -> dict:
        return dict(self._event.request.headers)

    @property
    def resource_type(self) -> str:
        return str(self._event.resource_type)

    def _header(self, name: str) -> str | None:
        return next((v for k, v in self._event.request.headers.items()
                     if k.lower() == name.lower()), None)

    @property
    def is_cors_preflight(self) -> bool:
        return (self.method.upper() == 'OPTIONS'
                and self._header('Access-Control-Request-Method') is not None)

    async def respond_preflight(self) -> None:
        '''Answer a CORS preflight permissively (what a stubbed API would
        allow). Page's route pump calls this automatically for matched
        preflights unless ``page.auto_preflight`` is False.'''
        await self.fulfill(
            status=204,
            cors=False,
            headers={
                'Access-Control-Allow-Origin': self._header('Origin') or '*',
                'Access-Control-Allow-Methods':
                    self._header('Access-Control-Request-Method') or '*',
                'Access-Control-Allow-Headers':
                    self._header('Access-Control-Request-Headers') or '*',
                'Access-Control-Max-Age': '600',
            },
        )

    @staticmethod
    def _header_entries(headers: dict | None) -> list[fetch_proto.HeaderEntry]:
        return [fetch_proto.HeaderEntry(name=k, value=v)
                for k, v in (headers or {}).items()]

    async def fulfill(
        self,
        *,
        status: int = 200,
        body: str | bytes = b'',
        json: typing.Any = None,
        content_type: str | None = None,
        headers: dict | None = None,
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
            entries.append(fetch_proto.HeaderEntry(name='Content-Type',
                                                   value=content_type))
        if cors and not any(e.name.lower() == 'access-control-allow-origin'
                            for e in entries):
            origin = next((v for k, v in self._event.request.headers.items()
                           if k.lower() == 'origin'), '*')
            entries.append(fetch_proto.HeaderEntry(
                name='Access-Control-Allow-Origin', value=origin))
        raw = body.encode() if isinstance(body, str) else body
        await self._session.execute(fetch_proto.fulfill_request(
            request_id=self._event.request_id,
            response_code=status,
            response_headers=entries or None,
            body=_b64.b64encode(raw).decode() if raw else None,
        ))

    async def continue_(
        self,
        *,
        url: str | None = None,
        method: str | None = None,
        post_data: str | bytes | None = None,
        headers: dict | None = None,
    ) -> None:
        '''Let the request proceed, optionally rewriting parts of it.'''
        self.handled = True
        raw = post_data.encode() if isinstance(post_data, str) else post_data
        await self._session.execute(fetch_proto.continue_request(
            request_id=self._event.request_id,
            url=url,
            method=method,
            post_data=_b64.b64encode(raw).decode() if raw else None,
            headers=self._header_entries(headers) if headers else None,
        ))

    async def abort(self, reason: str = 'Aborted') -> None:
        '''Fail the request (fetch() in the page rejects).'''
        self.handled = True
        await self._session.execute(fetch_proto.fail_request(
            request_id=self._event.request_id,
            error_reason=network_proto.ErrorReason(reason),
        ))
