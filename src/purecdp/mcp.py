'''Optional MCP server exposing purecdp's agent surface as tools (M9).

A minimal `Model Context Protocol <https://modelcontextprotocol.io>`_ server —
stdlib only, no ``mcp`` package — speaking newline-delimited JSON-RPC 2.0 over
stdio. It drives one browser page and offers four tools an LLM host can call:

- ``navigate`` (url) — go to a page, return its accessibility snapshot
- ``snapshot`` — return the current snapshot (ref-tagged outline)
- ``act`` (ref, action, text?, values?) — act on a ref, return the new snapshot
- ``evaluate`` (expression) — evaluate JS, return the JSON result
  (**off by default** — arbitrary page JS; enable with ``--allow-eval``)

Run it as a subprocess from an MCP host::

    python -m purecdp.mcp                 # headless
    python -m purecdp.mcp --headful       # visible window
    python -m purecdp.mcp --stealth       # low-observability launch
    python -m purecdp.mcp --allow-eval    # also expose the evaluate tool

The protocol layer (:meth:`MCPServer.handle`) is transport-free and unit-tested
by feeding it JSON-RPC dicts; :func:`serve_stdio` wires it to real stdin/stdout.
'''

from __future__ import annotations

import asyncio
import json
import sys
import typing
from contextlib import suppress

from .browser import launch
from .testing.page import Page

PROTOCOL_VERSION = '2024-11-05'

#: Tool definitions advertised via tools/list (JSON-Schema input).
TOOLS: list[dict] = [
    {
        'name': 'navigate',
        'description': "Navigate to a URL and return the page's accessibility "
                       'snapshot (a ref-tagged outline of the controls).',
        'inputSchema': {
            'type': 'object',
            'properties': {'url': {'type': 'string'}},
            'required': ['url'],
        },
    },
    {
        'name': 'snapshot',
        'description': "Return the current page's accessibility snapshot: an "
                       'indented outline where each control has a ref like e3.',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'act',
        'description': 'Act on a snapshot ref. action is one of click, hover, '
                       'focus, scroll, fill (needs text), type (needs text), '
                       'select (needs values). Returns the updated snapshot.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'ref': {'type': 'string'},
                'action': {'type': 'string', 'default': 'click'},
                'text': {'type': 'string'},
                'values': {'type': 'array', 'items': {'type': 'string'}},
            },
            'required': ['ref'],
        },
    },
    {
        'name': 'evaluate',
        'description': 'Evaluate a JavaScript expression in the page and return '
                       'its JSON value (for reading state the snapshot omits).',
        'inputSchema': {
            'type': 'object',
            'properties': {'expression': {'type': 'string'}},
            'required': ['expression'],
        },
    },
]

#: Tools advertised/callable only when the server is started with allow_eval —
#: arbitrary JS in the page is a sharp edge, so it is off by default
_GATED_TOOLS = frozenset({'evaluate'})


def _result(msg_id: typing.Any, result: dict) -> dict:
    return {'jsonrpc': '2.0', 'id': msg_id, 'result': result}


def _error(msg_id: typing.Any, code: int, message: str) -> dict:
    return {'jsonrpc': '2.0', 'id': msg_id, 'error': {'code': code,
                                                       'message': message}}


class MCPServer:
    '''MCP protocol handler over one browser page.

    ``page_factory`` (async, returns a ready :class:`~purecdp.testing.page.Page`)
    is the injection point for tests; in production the server launches its own
    browser lazily with ``launch_kwargs``.
    '''

    def __init__(
        self,
        *,
        launch_kwargs: dict | None = None,
        page_factory: typing.Callable[[], typing.Awaitable[Page]] | None = None,
        allow_eval: bool = False,
    ):
        self._launch_kwargs = launch_kwargs or {}
        self._page_factory = page_factory
        #: When False (default), the `evaluate` tool is neither advertised nor
        #: callable — arbitrary page JS is opt-in.
        self._allow_eval = allow_eval
        self._browser: typing.Any = None
        self._page: Page | None = None

    def _tools(self) -> list[dict]:
        '''Advertised tools, minus the gated ones unless allow_eval.'''
        if self._allow_eval:
            return TOOLS
        return [t for t in TOOLS if t['name'] not in _GATED_TOOLS]

    async def _ensure_page(self) -> Page:
        if self._page is None:
            if self._page_factory is not None:
                self._page = await self._page_factory()
            else:
                self._browser = await launch(**self._launch_kwargs)
                session = await self._browser.new_page()
                self._page = await Page.create(session)
        return self._page

    async def aclose(self) -> None:
        if self._browser is not None:
            with suppress(Exception):
                await self._browser.aclose()
            self._browser = None
            self._page = None

    # -- protocol ------------------------------------------------------------

    async def handle(self, msg: dict) -> dict | None:
        '''Process one JSON-RPC message; return the response dict, or None for
        notifications (no id) that need no reply.'''
        method = msg.get('method')
        msg_id = msg.get('id')
        if method == 'initialize':
            return _result(msg_id, {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'purecdp', 'version': '0'},
            })
        if method == 'tools/list':
            return _result(msg_id, {'tools': self._tools()})
        if method == 'tools/call':
            return await self._call_tool(msg_id, msg.get('params') or {})
        if method == 'ping':
            return _result(msg_id, {})
        if method is not None and method.startswith('notifications/'):
            return None
        if msg_id is not None:
            return _error(msg_id, -32601, f'method not found: {method}')
        return None

    async def _call_tool(self, msg_id: typing.Any, params: dict) -> dict:
        name = params.get('name')
        args = params.get('arguments') or {}
        try:
            text = await self._run_tool(name, args)
            return _result(msg_id, {
                'content': [{'type': 'text', 'text': text}]})
        except Exception as exc:  # tool errors are reported in-band, per MCP
            return _result(msg_id, {
                'content': [{'type': 'text', 'text': f'error: {exc}'}],
                'isError': True})

    async def _run_tool(self, name: str | None, args: dict) -> str:
        page = await self._ensure_page()
        if name == 'navigate':
            await page.goto(args['url'])
            return str(await page.snapshot())
        if name == 'snapshot':
            return str(await page.snapshot())
        if name == 'act':
            await page.act(args['ref'], args.get('action', 'click'),
                           text=args.get('text'), values=args.get('values'))
            return str(await page.snapshot())
        if name == 'evaluate':
            if not self._allow_eval:
                raise ValueError(
                    "the 'evaluate' tool is disabled; start the server with "
                    '--allow-eval to enable arbitrary page JavaScript')
            return json.dumps(await page.evaluate(args['expression']))
        raise ValueError(f'unknown tool {name!r}')


async def serve_stdio(server: MCPServer) -> None:
    '''Run ``server`` over stdin/stdout (newline-delimited JSON-RPC) '''
    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:  # EOF
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            response = await server.handle(msg)
            if response is not None:
                sys.stdout.write(json.dumps(response) + '\n')
                sys.stdout.flush()
    finally:
        await server.aclose()


def main(argv: typing.Sequence[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    launch_kwargs = {'headless': '--headful' not in argv,
                     'stealth': '--stealth' in argv}
    server = MCPServer(launch_kwargs=launch_kwargs,
                       allow_eval='--allow-eval' in argv)
    asyncio.run(serve_stdio(server))


if __name__ == '__main__':
    main()
