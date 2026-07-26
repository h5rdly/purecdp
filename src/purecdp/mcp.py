'''Optional MCP server exposing purecdp's agent surface as tools (M9).

A minimal `Model Context Protocol <https://modelcontextprotocol.io>`_ server —
stdlib only, no ``mcp`` package — speaking newline-delimited JSON-RPC 2.0 over
stdio. It drives one browser page and offers these tools an LLM host can call:

- ``navigate`` (url) — go to a page, return its accessibility snapshot
- ``snapshot`` (format?) — the snapshot as a ref-tagged outline or JSON rows
- ``mock`` (url, json?/body?, status?) — stub an endpoint (``--allow-mock``)
- ``act`` (ref | by=role/text/testid/label, action, text?) — act on an element
  by snapshot ref or by description; return the new snapshot
- ``screenshot`` (full_page?) — the page as a PNG image block (visual state)
- ``set_cookie`` (name, value, url?/domain?) — plant a cookie (e.g. auth)
- ``requests`` (since?, contains?) — list captured exchanges as compact metadata
- ``request`` (id, select?, max_bytes?) — one exchange's detail; ``select`` a
  dotted path / ``"keys"`` to read a big JSON body without dumping it all
- ``get_body`` (id, offset?, limit?) — page a large raw body
- ``record`` (needle?) — narrow capture going forward (on broad by default)
- ``wait`` (for, selector?/text?) — poll until visible/hidden/count/text holds
- ``seed_storage`` (kind, key, value) — plant session/localStorage before nav
- ``evaluate`` (expression) — evaluate JS, return the JSON result
  (**off by default** — arbitrary page JS; enable with ``--allow-eval``)

The network tools make "assert on the wire, not the DOM" reachable natively:
after ``act``, the returned cursor feeds ``requests(since=cursor)`` to see what
the action fired, then ``request(id, select=...)`` reads the response body.

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
import base64
import json
import sys
import typing
from contextlib import suppress

from .browser import launch
from .testing.page import Page

PROTOCOL_VERSION = '2024-11-05'

#: Default byte budget for a body preview / max_bytes fallback (agent context,
#: not the browser, is the bottleneck — see M15-spec "projection, not truncation").
_MAX_PREVIEW = 2048

_MISSING = object()


def _try_json(raw: str | None):
    '''Parse ``raw`` as JSON, or ``_MISSING`` if it is absent/not JSON.'''
    if not raw:
        return _MISSING
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return _MISSING


def _clip_body(head: str, raw: str, offset: int, limit: int) -> str:
    '''``head`` plus a ``[offset:offset+limit]`` slice of ``raw``, with a note
    on how much was withheld and how to page the rest.'''
    total = len(raw)
    chunk = raw[offset:offset + limit]
    note = f'bytes {offset}-{offset + len(chunk)} of {total}'
    if offset + len(chunk) < total:
        note += f'; get_body(offset={offset + len(chunk)}) for more'
    return f'{head}\n\n[{note}]\n{chunk}'


def _project(value: typing.Any, select: str | None):
    '''Semantic projection over a parsed body — the MCP equivalent of a Python
    caller drilling ``resp['selections']['filters']`` instead of dumping it all.

    ``select`` is a dotted path (``"a.b.0.c"``; integer segments index lists) or
    the literal ``"keys"`` (top-level keys of a dict / length of a list). Returns
    the projected value, or a ``{'error': ...}`` marker naming the missing/!bad
    segment so the agent learns the shape. Deliberately not full JSONPath.'''
    if select == 'keys':
        if isinstance(value, dict):
            return list(value)
        if isinstance(value, list):
            return {'type': 'list', 'length': len(value)}
        return {'type': type(value).__name__}
    node = value
    for seg in select.split('.'):
        if isinstance(node, dict):
            node = node.get(seg, _MISSING)
        elif isinstance(node, list) and seg.lstrip('-').isdigit():
            idx = int(seg)
            node = node[idx] if -len(node) <= idx < len(node) else _MISSING
        else:
            return {'error': f'cannot descend into {type(node).__name__} at {seg!r}'}
        if node is _MISSING:
            return {'error': f'no key {seg!r} (path {select!r})'}
    return node

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
        'description': "Return the current page's accessibility snapshot. "
                       'format="text" (default) is an indented outline where each '
                       'control has a ref like e3; format="json" is a flat list of '
                       '{ref, role, name, ...} for programmatic reasoning.',
        'inputSchema': {
            'type': 'object',
            'properties': {'format': {'type': 'string', 'default': 'text'}},
        },
    },
    {
        'name': 'act',
        'description': 'Act on an element. Target it EITHER by ref (from a '
                       'snapshot, e.g. "e3") OR by description without a snapshot '
                       'round-trip: by=role|text|testid|label with name (the '
                       'accessible name / text / id). action is one of click, '
                       'hover, focus, scroll, fill (needs text), type (needs '
                       'text), select (needs values). Returns the updated snapshot.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'ref': {'type': 'string'},
                'by': {'type': 'string'},
                'role': {'type': 'string', 'default': 'button'},
                'name': {'type': 'string'},
                'action': {'type': 'string', 'default': 'click'},
                'text': {'type': 'string'},
                'values': {'type': 'array', 'items': {'type': 'string'}},
            },
        },
    },
    {
        'name': 'screenshot',
        'description': 'Capture the page as a PNG image (returned as an image '
                       'block) — for layout or visual state a text snapshot cannot '
                       'show. full_page=true captures beyond the viewport.',
        'inputSchema': {
            'type': 'object',
            'properties': {'full_page': {'type': 'boolean', 'default': False}},
        },
    },
    {
        'name': 'set_cookie',
        'description': 'Set a cookie for the current site (or url/domain if '
                       'given) before/for the next requests — e.g. to plant an '
                       'auth cookie. Defaults url to the current page.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string'},
                'value': {'type': 'string'},
                'url': {'type': 'string'},
                'domain': {'type': 'string'},
                'path': {'type': 'string'},
            },
            'required': ['name', 'value'],
        },
    },
    {
        'name': 'requests',
        'description': 'List captured network exchanges as compact metadata '
                       '(no bodies): id, method, url, status, mime, sizes. Pass '
                       'since=<cursor> (from a prior call, or the cursor act/'
                       'navigate return) to get only new ones; contains= filters '
                       'by URL substring; only_json= keeps JSON responses.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'since': {'type': 'integer', 'default': 0},
                'contains': {'type': 'string'},
                'only_json': {'type': 'boolean', 'default': False},
            },
        },
    },
    {
        'name': 'request',
        'description': "One exchange's detail by id (from `requests`). select= "
                       'projects the JSON body to a dotted path (e.g. '
                       '"selections.filters") or "keys" — the way to read a big '
                       'body without spending context on all of it. max_bytes= '
                       'caps a non-JSON body. With neither, returns the body\'s '
                       'shape plus a short preview. part= is response|request|'
                       'headers.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer'},
                'part': {'type': 'string', 'default': 'response'},
                'select': {'type': 'string'},
                'max_bytes': {'type': 'integer'},
            },
            'required': ['id'],
        },
    },
    {
        'name': 'get_body',
        'description': 'A byte slice of one exchange\'s raw body — for paging a '
                       'large non-JSON body (HTML/CSV/stream) that select= does '
                       'not fit. part= is response|request.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer'},
                'part': {'type': 'string', 'default': 'response'},
                'offset': {'type': 'integer', 'default': 0},
                'limit': {'type': 'integer', 'default': _MAX_PREVIEW},
            },
            'required': ['id'],
        },
    },
    {
        'name': 'record',
        'description': 'Narrow what the recorder captures GOING FORWARD to URLs '
                       'containing needle (already-captured exchanges keep their '
                       'ids). Optional to call — capture is on by default (broad); '
                       'use this to cut asset noise on a heavy page.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'needle': {'type': 'string', 'default': ''},
                'exclude': {'type': 'string'},
            },
        },
    },
    {
        'name': 'wait',
        'description': "Poll until a condition holds, then return ok — the way to "
                       'wait for a spinner to vanish or a result to appear without '
                       're-snapshotting in a loop. for= is visible|hidden|count|'
                       'text; give selector (CSS) and/or text and (for count) n.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'for': {'type': 'string', 'default': 'visible'},
                'selector': {'type': 'string'},
                'text': {'type': 'string'},
                'n': {'type': 'integer'},
                'timeout': {'type': 'number'},
            },
        },
    },
    {
        'name': 'seed_storage',
        'description': 'Seed sessionStorage/localStorage before the NEXT '
                       'navigation (the way to plant an auth session a SPA reads '
                       'on boot). kind= is session|local. Call before navigate.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'kind': {'type': 'string', 'default': 'session'},
                'key': {'type': 'string'},
                'value': {'type': 'string'},
            },
            'required': ['key', 'value'],
        },
    },
    {
        'name': 'mock',
        'description': 'Stub a network endpoint: requests whose URL matches url '
                       '(fnmatch glob, or a plain substring) are fulfilled with '
                       'your response instead of hitting the network — to drive an '
                       'app whose backend is down, or force an error path. Give '
                       'json (an object) or body (a string) and optional status. '
                       'Stacks: call repeatedly, first match wins. Off by default '
                       '(start with --allow-mock).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'url': {'type': 'string'},
                'json': {'type': 'object'},
                'body': {'type': 'string'},
                'status': {'type': 'integer', 'default': 200},
                'content_type': {'type': 'string'},
            },
            'required': ['url'],
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

#: Tool name -> the ``allow_*`` flag that must be set for it to be advertised
#: and callable. Sharp edges (arbitrary JS; injecting fake responses) are opt-in.
_GATED_TOOLS = {'evaluate': 'allow_eval', 'mock': 'allow_mock'}


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
        allow_mock: bool = False,
    ):
        self._launch_kwargs = launch_kwargs or {}
        self._page_factory = page_factory
        #: Opt-in gates (default False): `evaluate` (arbitrary page JS) and
        #: `mock` (inject fake responses) are neither advertised nor callable.
        self._flags = {'allow_eval': allow_eval, 'allow_mock': allow_mock}
        self._browser: typing.Any = None
        self._page: Page | None = None
        #: One broad recorder armed at page creation so no traffic is missed;
        #: the `record` tool narrows it, `requests`/`request` read it.
        self._recorder: typing.Any = None

    def _tools(self) -> list[dict]:
        '''Advertised tools, minus any gated tool whose flag is off.'''
        return [t for t in TOOLS
                if self._flags.get(_GATED_TOOLS.get(t['name']), True)]

    async def _ensure_page(self) -> Page:
        if self._page is None:
            if self._page_factory is not None:
                self._page = await self._page_factory()
            else:
                self._browser = await launch(**self._launch_kwargs)
                session = await self._browser.new_session()
                self._page = await Page.create(session)
            # broad by default so early traffic is never missed; `record`
            # narrows it. Reuse an existing recorder if the factory made one.
            self._recorder = (self._page._recorders[0]
                              if self._page._recorders else self._page.record())
        return self._page

    def _exchange(self, exchange_id: typing.Any):
        '''The captured exchange at index ``exchange_id`` (ids are stable list
        positions), or a ValueError the tool layer reports in-band.'''
        exchanges = self._recorder.exchanges if self._recorder else []
        try:
            index = int(exchange_id)
        except (TypeError, ValueError):
            raise ValueError(f'bad exchange id {exchange_id!r}')
        if not 0 <= index < len(exchanges):
            raise ValueError(
                f'no exchange {index} (have {len(exchanges)}; cursor is that count)')
        return exchanges[index]

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
            out = await self._run_tool(name, args)
            # a tool returns a str (wrapped as one text block) or a ready list
            # of MCP content blocks (e.g. an image block — M15.1 screenshot).
            content = ([{'type': 'text', 'text': out}]
                       if isinstance(out, str) else out)
            return _result(msg_id, {'content': content})
        except Exception as exc:  # tool errors are reported in-band, per MCP
            return _result(msg_id, {
                'content': [{'type': 'text', 'text': f'error: {exc}'}],
                'isError': True})

    def _with_cursor(self, snapshot: typing.Any) -> str:
        '''Snapshot text plus the current exchange cursor, so the agent can
        follow up with requests(since=<cursor>) to see what the action fired.'''
        cursor = len(self._recorder.exchanges) if self._recorder else 0
        return f'{snapshot}\n\n[network cursor: {cursor}]'

    async def _run_tool(self, name: str | None, args: dict) -> str | list:
        page = await self._ensure_page()
        if name == 'navigate':
            await page.goto(args['url'])
            return self._with_cursor(await page.snapshot())
        if name == 'snapshot':
            snap = await page.snapshot()
            if args.get('format') == 'json':
                rows = [{k: v for k, v in
                         {'ref': n.get('ref'), 'role': n.get('role'),
                          'name': n.get('name'), 'value': n.get('value'),
                          'states': n.get('states')}.items() if v}
                        for n in snap.nodes if n.get('ref')]
                return json.dumps(rows, indent=2)
            return str(snap)
        if name == 'act':
            await self._do_act(page, args)
            return self._with_cursor(await page.snapshot())
        if name == 'screenshot':
            png = await page.screenshot(full_page=bool(args.get('full_page')))
            return [{'type': 'image',
                     'data': base64.b64encode(png).decode('ascii'),
                     'mimeType': 'image/png'}]
        if name == 'set_cookie':
            url = args.get('url')
            if not url and not args.get('domain'):
                url = await page.evaluate('location.href')  # internal, not the gated tool
            cookie = {'name': args['name'], 'value': args['value']}
            for k in ('url', 'domain', 'path'):
                v = url if k == 'url' else args.get(k)
                if v:
                    cookie[k] = v
            await page.set_cookies([cookie])
            return f'set cookie {args["name"]!r}'
        if name == 'requests':
            return self._tool_requests(args)
        if name == 'request':
            return self._tool_request(args)
        if name == 'get_body':
            return self._tool_get_body(args)
        if name == 'record':
            self._recorder.set_filter(needle=args.get('needle', ''),
                                      exclude=args.get('exclude'))
            return (f'recording narrowed to URLs containing '
                    f'{args.get("needle", "")!r}; ids preserved')
        if name == 'wait':
            return await self._tool_wait(page, args)
        if name == 'seed_storage':
            kind = args.get('kind', 'session')
            seed = (page.seed_local_storage if kind == 'local'
                    else page.seed_session_storage)
            await seed(args['key'], args['value'])
            return f'seeded {kind}Storage[{args["key"]!r}] for the next navigation'
        if name == 'mock':
            if not self._flags['allow_mock']:
                raise ValueError(
                    "the 'mock' tool is disabled; start the server with "
                    '--allow-mock to enable response stubbing')
            await self._add_mock(page, args)
            return f'mocking {args["url"]!r} -> {args.get("status", 200)}'
        if name == 'evaluate':
            if not self._flags['allow_eval']:
                raise ValueError(
                    "the 'evaluate' tool is disabled; start the server with "
                    '--allow-eval to enable arbitrary page JavaScript')
            return json.dumps(await page.evaluate(args['expression']))
        raise ValueError(f'unknown tool {name!r}')

    async def _add_mock(self, page: Page, args: dict) -> None:
        '''Register a route that fulfils matching requests with a canned
        response. Stacks with earlier mocks (first matching route wins).'''
        url = args['url']
        pattern = url if '*' in url else (lambda u, needle=url: needle in u)
        status = int(args.get('status', 200))
        payload = args.get('json')
        body = args.get('body')
        content_type = args.get('content_type')

        async def handler(request: typing.Any) -> None:
            if payload is not None:
                await request.fulfill(status=status, json=payload)
            elif body is not None:
                await request.fulfill(status=status, body=body,
                                      content_type=content_type or 'text/plain')
            else:
                await request.fulfill(status=status, body='')

        await page.route(pattern, handler)

    async def _do_act(self, page: Page, args: dict) -> None:
        '''Act by ref (snapshot-based) or by description (a get_by_* locator).'''
        action = args.get('action', 'click')
        ref = args.get('ref')
        if ref is not None:
            await page.act(ref, action, text=args.get('text'),
                           values=args.get('values'))
            return
        by = args.get('by')
        name = args.get('name') or ''
        locators = {
            'role': lambda: page.get_by_role(args.get('role', 'button'),
                                             name=name or None),
            'text': lambda: page.get_by_text(name),
            'testid': lambda: page.get_by_test_id(name),
            'label': lambda: page.get_by_label(name),
        }
        if by not in locators:
            raise ValueError(
                'act needs ref=<snapshot ref> or by=role|text|testid|label')
        loc = locators[by]()
        if action == 'click':
            await loc.click()
        elif action == 'hover':
            await loc.hover()
        elif action == 'fill':
            await loc.fill(args['text'])
        elif action == 'type':
            await loc.type(args['text'])
        elif action == 'select':
            await loc.select(*(args.get('values') or []))
        else:
            raise ValueError(f'action {action!r} not supported for by= targeting')

    # -- network tools -------------------------------------------------------

    def _tool_requests(self, args: dict) -> str:
        since = int(args.get('since') or 0)
        contains = args.get('contains')
        only_json = bool(args.get('only_json'))
        exchanges = self._recorder.exchanges if self._recorder else []
        lines = []
        for i in range(since, len(exchanges)):
            e = exchanges[i]
            if contains and contains not in e.url:
                continue
            if only_json and (e.mime or '').find('json') < 0:
                continue
            status = 'FAILED' if e.failed else (e.status if e.status is not None else '?')
            lines.append(
                f'[{i}] {e.method} {e.url} -> {status} {e.mime or ""}'.rstrip())
        cursor = len(exchanges)
        body = '\n'.join(lines) if lines else '(no matching exchanges)'
        return f'{body}\n\n[cursor: {cursor}]'

    def _tool_request(self, args: dict) -> str:
        e = self._exchange(args.get('id'))
        part = args.get('part', 'response')
        if part == 'headers':
            return json.dumps({'request': dict(e.request_headers),
                               'response': dict(e.response_headers)}, indent=2)
        head = [f'{e.method} {e.url}',
                f'status: {"FAILED: " + e.failed if e.failed else e.status}',
                f'mime: {e.mime}']
        raw = e.request_body if part == 'request' else e.text
        if e.body_error and part == 'response':
            head.append(f'body: <unavailable: {e.body_error}>')
            return '\n'.join(head)
        parsed = _try_json(raw)
        select = args.get('select')
        if select is not None:
            projected = _project(parsed, select) if parsed is not _MISSING \
                else {'error': 'body is not JSON; use max_bytes/get_body'}
            return '\n'.join(head) + '\n\n' + json.dumps(projected, indent=2)
        if parsed is not _MISSING:
            # shape-first default: keys + a short preview, never the whole body
            shape = _project(parsed, 'keys')
            preview = json.dumps(parsed)[:_MAX_PREVIEW]
            return ('\n'.join(head) + f'\n\nshape: {json.dumps(shape)}'
                    f'\npreview: {preview}'
                    '\n(use select="<path>" for a subtree, or get_body for raw)')
        cap = int(args.get('max_bytes') or _MAX_PREVIEW)
        return _clip_body('\n'.join(head), raw, 0, cap)

    def _tool_get_body(self, args: dict) -> str:
        e = self._exchange(args.get('id'))
        part = args.get('part', 'response')
        raw = e.request_body if part == 'request' else e.text
        offset = int(args.get('offset') or 0)
        limit = int(args.get('limit') or _MAX_PREVIEW)
        return _clip_body(f'{e.method} {e.url} [{part} body]', raw or '',
                          offset, limit)

    async def _tool_wait(self, page: Page, args: dict) -> str:
        from .testing.live import ExpectationError
        cond = args.get('for', 'visible')
        selector = args.get('selector')
        text = args.get('text')
        timeout = args.get('timeout')
        loc = (page.get_by_text(text) if selector is None and text
               else page.live(selector or '*',
                              containing=text if text else None))
        checks = {'visible': {'visible': True}, 'hidden': {'visible': False},
                  'count': {'count': int(args.get('n') or 0)},
                  'text': {'text': text or ''}}
        if cond not in checks:
            raise ValueError(f'for must be visible|hidden|count|text, not {cond!r}')
        try:
            await loc.should(timeout=timeout, **checks[cond])
            return f'ok: {cond} met'
        except ExpectationError as exc:
            return f'not met within timeout: {exc}'


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
                       allow_eval='--allow-eval' in argv,
                       allow_mock='--allow-mock' in argv)
    asyncio.run(serve_stdio(server))


if __name__ == '__main__':
    main()
