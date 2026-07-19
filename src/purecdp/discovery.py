'''HTTP discovery endpoints of a debuggable browser.

A browser started with ``--remote-debugging-port`` serves JSON metadata over
plain HTTP: ``/json/version`` (browser + browser-level webSocketDebuggerUrl)
and ``/json/list`` (open targets, each with its own webSocketDebuggerUrl).
Our own launcher doesn't need this (it reads the DevToolsActivePort file),
but it's how you reach a browser someone else started, e.g.
``chromium --remote-debugging-port=9222``.
'''

from __future__ import annotations

import asyncio
import http.client
import json
import typing

from .errors import CDPTransportError


def _fetch(host: str, port: int, path: str, timeout: float) -> typing.Any:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request('GET', path)
        response = conn.getresponse()
        body = response.read()
        if response.status != 200:
            raise CDPTransportError(
                f'GET {path} -> {response.status} {response.reason}'
            )
        return json.loads(body)
    except OSError as exc:
        raise CDPTransportError(f'discovery request to {host}:{port} failed: {exc}') from exc
    finally:
        conn.close()


async def get_version(
    host: str = '127.0.0.1', port: int = 9222, *, timeout: float = 10.0
) -> dict:
    '''Fetch ``/json/version``; ``result['webSocketDebuggerUrl']`` is the
    browser-level endpoint to hand to WebSocketTransport.connect().'''
    return await asyncio.to_thread(_fetch, host, port, '/json/version', timeout)


async def list_targets(
    host: str = '127.0.0.1', port: int = 9222, *, timeout: float = 10.0
) -> list[dict]:
    '''Fetch ``/json/list``: one dict per open target (pages, workers...).'''
    return await asyncio.to_thread(_fetch, host, port, '/json/list', timeout)
