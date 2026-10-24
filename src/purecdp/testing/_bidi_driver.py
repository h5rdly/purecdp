'''The WebDriver BiDi implementation of the driver seam (:mod:`._driver`),
over one browsing context on a :class:`~purecdp.bidi.BidiConnection` —
how a ``Page`` drives Firefox.

Status (M20 complete): scripting (evaluate / call / handles), navigation and
load waits, console + exception capture, network in-flight tracking,
screenshots, PDF, dialogs, viewport, file inputs, preload scripts, trusted
input (``input.performActions``), cookies, extra headers, user-agent /
geolocation / timezone overrides, child frames (every iframe is a browsing
context — same-origin ones too), request interception
(``network.addIntercept`` → provideResponse / continueRequest / failRequest),
recorder bodies (a ``network.addDataCollector`` + ``getData``), downloads
(``browser.setDownloadBehavior`` + ``downloadWillBegin``/``downloadEnd``).
The accessibility tree comes from an in-page walker (:mod:`._ax_walker`),
so ``snapshot()`` / ``act()`` / the MCP server work; refs index a registry
the walker leaves on the page. Emulated media has no BiDi equivalent at all
(Firefox 157/158).

Request bodies are not on BiDi's events (only ``bodySize`` is) but Firefox
keeps them in a ``request`` data collector and hands them over even while
the request is blocked, so ``InterceptedRequest.post_data`` /
``Exchange.request_body`` / ``page.relay`` reach parity. One difference
remains: the CORS preflight of a request that was fulfilled at the intercept
never happens (Firefox serves the provided response straight to the page);
a *continued* cross-origin request's OPTIONS does come through blocked and is
answered by the route pump exactly as on CDP.
'''

from __future__ import annotations

import typing

from .. import _b64
from ..bidi.connection import BidiCommandError, BidiConnection, BidiEvent
from ..bidi.serde import from_remote, handle_of, to_local
from ..errors import CDPSessionClosed, NotSupported
from ._ax_walker import AX_IFRAME_WINDOWS_JS, AX_RESOLVE_JS, AX_WALKER_JS
from ._driver import (Console, Dialog, DownloadBegin, DownloadProgress,
                      Exception_, Failed, Finished, FrameScope, FrameStopped,
                      Handle, JSError, Load, Paused, Request, RequestExtra,
                      Response, ResponseExtra)

#: Cap for the per-context response-body collector (``maxEncodedDataSize``).
BODY_COLLECTOR_CAP = 20 * 1024 * 1024

BACKEND = 'bidi'

#: WebDriver key code points for the named keys ``Page.press`` knows; any
#: other key is sent as its own character.
_KEY_CODEPOINTS = {
    'Enter': '\ue007', 'Tab': '\ue004', 'Escape': '\ue00c', 'Backspace': '\ue003',
    'Delete': '\ue017', 'ArrowUp': '\ue013', 'ArrowDown': '\ue015',
    'ArrowLeft': '\ue012', 'ArrowRight': '\ue014', 'Home': '\ue011',
    'End': '\ue010', 'PageUp': '\ue00e', 'PageDown': '\ue00f', 'Shift': '\ue008',
    'Control': '\ue009', 'Alt': '\ue00a', 'Meta': '\ue03d', 'Space': ' ',
}
_BUTTONS = {'left': 0, 'middle': 1, 'right': 2, 'back': 3, 'forward': 4}
_SAME_SITE = {'strict': 'Strict', 'lax': 'Lax', 'none': 'None'}
#: BiDi error codes meaning "this browsing context is gone".
_GONE_CODES = frozenset({'no such frame', 'no such window'})


def _domain_matches(host: str | None, cookie_domain: str | None) -> bool:
    '''RFC 6265 §5.1.3 domain matching: equal, or host ends with
    ``"." + domain`` (so ``a.com`` never matches ``not-a.com``).'''
    h = (host or '').lower().rstrip('.')
    dom = (cookie_domain or '').lower().lstrip('.').rstrip('.')
    return bool(h and dom and (h == dom or h.endswith('.' + dom)))


class _End:
    '''Stream sentinel: this context was destroyed — end the stream.'''


def _header_list(headers: dict) -> list[dict]:
    return [{'name': k, 'value': {'type': 'string', 'value': str(v)}}
            for k, v in headers.items()]


def _cookie_out(c: dict) -> dict:
    '''BiDi cookie -> the CDP-shaped dict ``Page.cookies()`` promises.'''
    value = c.get('value')
    out = {'name': c.get('name'),
           'value': value.get('value') if isinstance(value, dict) else value,
           'domain': c.get('domain'), 'path': c.get('path', '/'),
           'secure': bool(c.get('secure')), 'httpOnly': bool(c.get('httpOnly')),
           'session': 'expiry' not in c}
    if 'expiry' in c:
        out['expires'] = c['expiry']
    if c.get('sameSite'):
        out['sameSite'] = _SAME_SITE.get(str(c['sameSite']).lower(), c['sameSite'])
    if 'size' in c:
        out['size'] = c['size']
    return out


def _cookie_in(c: dict) -> dict:
    '''CDP-shaped cookie dict (``name``/``value`` + ``domain`` or ``url``) ->
    BiDi ``PartialCookie``.'''
    from urllib.parse import urlsplit
    domain = c.get('domain')
    if not domain and c.get('url'):
        domain = urlsplit(c['url']).hostname
    if not domain:
        raise ValueError(f'cookie {c.get("name")!r} needs a domain or url')
    out: dict = {'name': c['name'],
                 'value': {'type': 'string', 'value': str(c.get('value', ''))},
                 'domain': domain}
    if c.get('path'):
        out['path'] = c['path']
    if 'secure' in c:
        out['secure'] = bool(c['secure'])
    if 'httpOnly' in c:
        out['httpOnly'] = bool(c['httpOnly'])
    if c.get('sameSite'):
        out['sameSite'] = str(c['sameSite']).lower()
    if c.get('expires') not in (None, -1):
        out['expiry'] = int(c['expires'])
    return out

#: normalized kind -> BiDi event method(s) it is read from
_EVENTS: dict[type, tuple[str, ...]] = {
    Console: ('log.entryAdded',),
    Exception_: ('log.entryAdded',),
    Load: ('browsingContext.load',),
    FrameStopped: ('browsingContext.load',),
    Dialog: ('browsingContext.userPromptOpened',),
    Request: ('network.beforeRequestSent',),
    RequestExtra: (),      # BiDi headers arrive on the base event: never emitted
    Response: ('network.responseStarted',),
    ResponseExtra: (),
    Finished: ('network.responseCompleted',),
    Failed: ('network.fetchError',),
    Paused: ('network.beforeRequestSent',),       # the isBlocked ones
    DownloadBegin: ('browsingContext.downloadWillBegin',),
    DownloadProgress: ('browsingContext.downloadEnd',),
}


def _headers(entries: typing.Iterable[dict] | None) -> dict:
    '''BiDi header list ``[{name, value: {type, value}}]`` -> dict.'''
    out: dict = {}
    for entry in entries or ():
        value = entry.get('value')
        out[entry.get('name', '')] = (value.get('value', '') if isinstance(value, dict)
                                      else value)
    return out


def _js_error(details: dict) -> JSError:
    exception = details.get('exception') or {}
    text = details.get('text') or ''
    if not text and exception.get('type') == 'string':
        text = str(exception.get('value'))
    return JSError(text or 'JavaScript exception', details)


class _Stream:
    '''A BiDi event stream whose items come out as normalized events for ONE
    browsing context (the subscription is connection-wide).'''

    def __init__(self, inner, kinds: tuple[type, ...], context: str):
        self._inner = inner
        self._kinds = kinds
        self._context = context
        self._pending: list = []   # one BiDi event can normalize to several

    def close(self) -> None:
        self._inner.close()

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> typing.Any:
        while True:
            if self._pending:
                item = self._pending.pop(0)
                if item is _End:
                    self.close()
                    raise StopAsyncIteration  # the tab is gone: pumps end, as on CDP
                return item
            event = await self._inner.__anext__()
            self._pending = [item for item in _normalize(event, self._context)
                             if item is _End or isinstance(item, self._kinds)]


def _normalize(event: BidiEvent, context: str) -> list:
    '''BiDi event -> normalized events for ``context`` (empty for another
    context's events, or anything unexpected).'''
    p = event.params
    method = event.method
    if method == 'browsingContext.contextDestroyed':
        stack = [p]
        while stack:
            node = stack.pop()
            if str(node.get('context')) == context:
                return [_End]
            stack.extend(node.get('children') or [])
        return []
    if method == 'log.entryAdded':
        source = p.get('source') or {}
        if source.get('context') not in (None, context):
            return []
        text = p.get('text') or ''
        if p.get('type') == 'javascript':
            level = p.get('level', 'error')
            out: list = [Console(kind=level, text=text)]
            if level == 'error':
                out.append(Exception_(JSError(text, p)))
            return out
        kind = p.get('method') or p.get('level') or 'log'
        return [Console(kind=str(kind), text=text)]
    if p.get('context') not in (None, context):
        return []
    if method == 'browsingContext.load':
        return [Load(), FrameStopped(frame_id=context)]
    if method == 'browsingContext.userPromptOpened':
        return [Dialog(message=p.get('message', ''), type=p.get('type', 'alert'),
                       default_prompt=p.get('defaultValue'))]
    if method == 'network.beforeRequestSent':
        req = p.get('request') or {}
        initiator = (p.get('initiator') or {}).get('type')
        seconds = float(p.get('timestamp', 0)) / 1000.0
        resource_type = 'Document' if p.get('navigation') else initiator
        headers = _headers(req.get('headers'))
        size = req.get('bodySize')
        body_size = int(size) if isinstance(size, (int, float)) else None
        out: list = [Request(
            request_id=str(req.get('request')), url=req.get('url', ''),
            method=req.get('method', 'GET'), headers=headers, post_data=None,
            resource_type=resource_type, frame_id=context, timestamp=seconds,
            wall_time=seconds, body_size=body_size)]
        if p.get('isBlocked'):
            out.append(Paused(
                request_id=str(req.get('request')), url=req.get('url', ''),
                method=req.get('method', 'GET'), headers=headers,
                post_data=None,  # only bodySize is on the wire: see request_body()
                resource_type=resource_type, frame_id=context, body_size=body_size))
        return out
    if method == 'browsingContext.downloadWillBegin':
        return [DownloadBegin(guid=str(p.get('download') or p.get('navigation')),
                              url=p.get('url', ''),
                              suggested_filename=p.get('suggestedFilename', ''))]
    if method == 'browsingContext.downloadEnd':
        return [DownloadProgress(
            guid=str(p.get('download') or p.get('navigation')),
            state='completed' if p.get('status') == 'complete' else 'canceled',
            file_path=p.get('filepath'))]
    if method == 'network.responseStarted':
        req = p.get('request') or {}
        resp = p.get('response') or {}
        return [Response(request_id=str(req.get('request')),
                         status=int(resp.get('status', 0)),
                         mime=str(resp.get('mimeType', '')),
                         headers=_headers(resp.get('headers')))]
    if method == 'network.responseCompleted':
        req = p.get('request') or {}
        resp = p.get('response') or {}
        return [Finished(request_id=str(req.get('request')),
                         timestamp=float(p.get('timestamp', 0)) / 1000.0,
                         encoded_length=int(resp.get('bytesReceived', 0)))]
    if method == 'network.fetchError':
        req = p.get('request') or {}
        text = str(p.get('errorText', ''))
        return [Failed(request_id=str(req.get('request')),
                       timestamp=float(p.get('timestamp', 0)) / 1000.0,
                       error_text=text, canceled='ABORTED' in text.upper())]
    return []


class BidiDriver:
    '''Drive one browsing context over BiDi.'''

    backend = BACKEND

    def __init__(self, connection: BidiConnection, context: str):
        self.connection = connection
        self.context = context
        self._closed = False
        self._children: dict[str, BidiDriver] = {}
        self._scopes: list[FrameScope] = []
        #: network.setExtraHeaders REPLACES the context's set: keep the merge.
        self._extra_headers: dict[str, str] = {}
        self._intercept: str | None = None    # network.addIntercept id
        self._collector: str | None = None    # network.addDataCollector id
        #: Request bodies already pulled from the collector (the route pump
        #: fetches at intercept time, a recorder at completion — the data is
        #: disowned on first read, so the second reader is served from here).
        self._request_bodies: dict[str, bytes] = {}
        #: child browsing context id -> registry index of its <iframe>, from
        #: the last ax_tree() (stitched snapshots ask via frame_owner).
        self._iframe_owners: dict[str, int] = {}

    def __repr__(self) -> str:
        return f'BidiDriver({self.context!r})'

    def _unsupported(self, feature: str) -> NotSupported:
        return NotSupported(feature, self.backend)

    @property
    def alive(self) -> bool:
        '''False once the tab is gone — closed by us, by the user, by
        ``window.close()`` or by anyone's ``browsingContext.close`` — or the
        connection is. Purely local: ``contextDestroyed`` is tracked on the
        connection (subscribed by :meth:`arm`).'''
        return (not self._closed and not self.connection.closed
                and self.context not in self.connection.destroyed_contexts)

    async def _cmd(self, method: str, params: dict) -> dict:
        '''A command on this context; the 0.8.0 watcher contract holds:
        a dead tab raises :class:`CDPSessionClosed` (a ``CDPClosedError``),
        never a bare "no such frame" command error.'''
        if not self.alive:
            raise CDPSessionClosed(
                f'browsing context {self.context} is gone'
                + ('' if not self.connection.closed else ' (connection closed)'))
        try:
            return await self.connection.call(method, params)
        except BidiCommandError as exc:
            if exc.code in _GONE_CODES:
                self.connection.destroyed_contexts.add(self.context)
                raise CDPSessionClosed(
                    f'browsing context {self.context} is gone ({exc.code})') from exc
            raise

    _NETWORK_EVENTS = ('network.beforeRequestSent', 'network.responseStarted',
                       'network.responseCompleted', 'network.fetchError')

    async def arm(self, *, capture: bool, track_network: bool) -> None:
        events = ['browsingContext.load', 'browsingContext.userPromptOpened',
                  'browsingContext.contextDestroyed']
        if capture:
            events.append('log.entryAdded')
        if track_network:
            events += self._NETWORK_EVENTS
        await self.connection.subscribe(events)

    # -- scripting -----------------------------------------------------------

    def _target(self) -> dict:
        return {'context': self.context}

    async def _script(self, method: str, params: dict, *, await_promise: bool,
                      user_gesture: bool, ownership: str) -> dict:
        params = {**params, 'target': self._target(), 'awaitPromise': await_promise,
                  'resultOwnership': ownership}
        if user_gesture:
            params['userActivation'] = True
        result = await self._cmd(method, params)
        if result.get('type') == 'exception':
            raise _js_error(result.get('exceptionDetails') or {})
        return result.get('result') or {'type': 'undefined'}

    @staticmethod
    def _arguments(args: typing.Sequence[typing.Any]) -> list[dict]:
        out = []
        for index, arg in enumerate(args):
            try:
                out.append(to_local(arg))
            except TypeError as exc:
                raise TypeError(
                    f'evaluate() argument {index} is neither JSON-serializable '
                    f'nor an Element: {arg!r}') from exc
        return out

    async def evaluate(self, expression: str, *, await_promise: bool = True,
                       user_gesture: bool = False) -> typing.Any:
        remote = await self._script('script.evaluate', {'expression': expression},
                                    await_promise=await_promise,
                                    user_gesture=user_gesture, ownership='none')
        return from_remote(remote)

    async def evaluate_handle(self, expression: str, *, await_promise: bool = True,
                              user_gesture: bool = False) -> Handle | None:
        remote = await self._script('script.evaluate', {'expression': expression},
                                    await_promise=await_promise,
                                    user_gesture=user_gesture, ownership='root')
        return handle_of(remote)

    async def _call(self, function: str, args: typing.Sequence[typing.Any], *,
                    await_promise: bool, user_gesture: bool, ownership: str) -> dict:
        params = {'functionDeclaration': function, 'arguments': self._arguments(args)}
        try:
            return await self._script('script.callFunction', params,
                                      await_promise=await_promise,
                                      user_gesture=user_gesture, ownership=ownership)
        except BidiCommandError as exc:
            if 'function' in exc.message.lower():
                exc.add_note('with arguments, evaluate() takes a function '
                             "declaration like '(a, b) => ...' as its expression")
            raise

    async def call(self, function: str, args: typing.Sequence[typing.Any], *,
                   await_promise: bool = True, user_gesture: bool = False
                   ) -> typing.Any:
        return from_remote(await self._call(function, args, await_promise=await_promise,
                                            user_gesture=user_gesture, ownership='none'))

    async def call_handle(self, function: str, args: typing.Sequence[typing.Any],
                          *, await_promise: bool = True,
                          user_gesture: bool = False) -> Handle | None:
        return handle_of(await self._call(function, args, await_promise=await_promise,
                                          user_gesture=user_gesture, ownership='root'))

    async def release(self, handle: Handle) -> None:
        if handle.id == handle.node_id:
            return  # a bare sharedId holds nothing to disown
        await self._cmd('script.disown', {
            'handles': [handle.id], 'target': self._target()})

    async def array_handles(self, handle: Handle) -> list[Handle]:
        remote = await self._call('(a) => a', [handle], await_promise=False,
                                  user_gesture=False, ownership='root')
        items = remote.get('value') if remote.get('type') == 'array' else None
        handles = [h for h in (handle_of(v) for v in items or []) if h is not None]
        owner = handle_of(remote)
        if owner is not None:
            try:
                await self.release(owner)
            except Exception:
                pass
        return handles

    async def add_init_script(self, source: str) -> None:
        await self._cmd('script.addPreloadScript', {
            'functionDeclaration': f'() => {{\n{source}\n}}',
            'contexts': [self.context]})

    # -- nodes ---------------------------------------------------------------

    async def node_frame_id(self, handle: Handle) -> str | None:
        '''The browsing context an <iframe> hosts: its ``contentWindow``
        serializes as a ``window`` RemoteValue carrying the context id.'''
        remote = await self._call('(el) => el.contentWindow || null', [handle],
                                  await_promise=False, user_gesture=False,
                                  ownership='none')
        if remote.get('type') != 'window':
            return None
        return str((remote.get('value') or {}).get('context')) or None

    async def set_files(self, handle: Handle, paths: typing.Sequence[str]) -> None:
        if handle.node_id is None:
            raise ValueError('set_files needs a DOM node handle')
        await self._cmd('input.setFiles', {
            'context': self.context, 'element': {'sharedId': handle.node_id},
            'files': list(paths)})

    # -- navigation / content ------------------------------------------------

    async def navigate(self, url: str) -> tuple[str | None, str | None]:
        try:
            await self._cmd('browsingContext.navigate', {
                'context': self.context, 'url': url, 'wait': 'none'})
        except BidiCommandError as exc:
            return self.context, exc.message or exc.code
        return self.context, None

    async def main_frame_id(self) -> str:
        return self.context

    async def set_content(self, html: str) -> None:
        await self.call('(html) => { document.open(); document.write(html); '
                        'document.close(); }', [html], await_promise=False)

    # -- capture -------------------------------------------------------------

    async def screenshot(self, *, format: str = 'png', quality: int | None = None,
                         clip: dict | None = None,
                         beyond_viewport: bool = False) -> bytes:
        params: dict = {'context': self.context,
                        'origin': 'document' if beyond_viewport else 'viewport',
                        'format': {'type': f'image/{"jpeg" if format == "jpeg" else format}'}}
        if quality is not None and format in ('jpeg', 'webp'):
            params['format']['quality'] = max(0.0, min(1.0, quality / 100.0))
        if clip is not None:
            params['clip'] = {'type': 'box', 'x': clip['x'], 'y': clip['y'],
                              'width': clip['width'], 'height': clip['height']}
        result = await self._cmd('browsingContext.captureScreenshot', params)
        return _b64.b64decode(result['data'])

    async def pdf(self, **options: typing.Any) -> bytes:
        params: dict = {'context': self.context}
        if options.get('landscape'):
            params['orientation'] = 'landscape'
        if 'print_background' in options:
            params['background'] = bool(options['print_background'])
        if 'scale' in options:
            params['scale'] = options['scale']
        page: dict = {}
        if 'paper_width' in options:   # CDP inches -> BiDi centimetres
            page['width'] = options['paper_width'] * 2.54
        if 'paper_height' in options:
            page['height'] = options['paper_height'] * 2.54
        if page:
            params['page'] = page
        result = await self._cmd('browsingContext.print', params)
        return _b64.b64decode(result['data'])

    # -- input (input.performActions; sources persist per context by id) ----

    async def _perform(self, source: dict) -> None:
        await self._cmd('input.performActions', {
            'context': self.context, 'actions': [source]})

    async def mouse(self, kind: str, x: float, y: float, *,
                    button: str | None = None, click_count: int | None = None,
                    delta_x: float | None = None, delta_y: float | None = None
                    ) -> None:
        if kind == 'mouseWheel':
            await self._perform({'type': 'wheel', 'id': 'purecdp-wheel', 'actions': [
                {'type': 'scroll', 'x': int(x), 'y': int(y),
                 'deltaX': int(delta_x or 0), 'deltaY': int(delta_y or 0)}]})
            return
        # BiDi rejects a pointer move outside the viewport ("move target out
        # of bounds"), where CDP just clamps; a human-motion curve can dip a
        # fraction below zero near the origin, so clamp the low side here.
        actions: list[dict] = [{'type': 'pointerMove', 'x': max(0.0, x), 'y': max(0.0, y)}]
        idx = _BUTTONS.get(button or 'left', 0)
        if kind == 'mousePressed':
            actions.append({'type': 'pointerDown', 'button': idx})
        elif kind == 'mouseReleased':
            actions.append({'type': 'pointerUp', 'button': idx})
        elif kind != 'mouseMoved':
            raise ValueError(f'unknown mouse event kind {kind!r}')
        await self._perform({'type': 'pointer', 'id': 'purecdp-mouse',
                             'actions': actions})

    async def key(self, kind: str, *, key: str, code: str | None = None,
                  text: str | None = None, virtual_key: int | None = None
                  ) -> None:
        value = _KEY_CODEPOINTS.get(key, key)
        if len(value) != 1:
            raise ValueError(f'cannot type key {key!r} on BiDi: not a single '
                             'character and not a known named key')
        step = {'keyDown': 'keyDown', 'keyUp': 'keyUp', 'rawKeyDown': 'keyDown',
                'char': None}.get(kind, kind)
        if step is None:
            return  # CDP's 'char' has no BiDi twin; keyDown already typed it
        await self._perform({'type': 'key', 'id': 'purecdp-keyboard',
                             'actions': [{'type': step, 'value': value}]})

    async def insert_text(self, text: str) -> None:
        '''No BiDi twin of Input.insertText: typed as key events, one
        performActions call for the whole string (so key listeners DO fire,
        unlike on CDP — document this difference).'''
        actions = []
        for ch in text:
            actions += [{'type': 'keyDown', 'value': ch}, {'type': 'keyUp', 'value': ch}]
        if actions:
            await self._perform({'type': 'key', 'id': 'purecdp-keyboard',
                                 'actions': actions})

    # -- dialogs -------------------------------------------------------------

    async def handle_dialog(self, *, accept: bool,
                            prompt_text: str | None = None) -> None:
        params: dict = {'context': self.context, 'accept': accept}
        if prompt_text is not None:
            params['userText'] = prompt_text
        await self._cmd('browsingContext.handleUserPrompt', params)

    # -- network -------------------------------------------------------------

    async def enable_network(self) -> None:
        await self.connection.subscribe(self._NETWORK_EVENTS)

    async def collect_bodies(self) -> None:
        '''Keep request and response bodies for this context (BiDi stores
        none unless a data collector asks — and the events carry only
        ``bodySize``); idempotent. Also makes sure the network events are
        subscribed, so a recorder on a ``track_network=False`` page works.'''
        await self.enable_network()
        if self._collector is None:
            result = await self._cmd('network.addDataCollector', {
                'dataTypes': ['request', 'response'],
                'maxEncodedDataSize': BODY_COLLECTOR_CAP,
                'contexts': [self.context]})
            self._collector = str(result['collector'])

    async def _get_data(self, data_type: str, request_id: str) -> bytes:
        params: dict = {'dataType': data_type, 'request': request_id}
        if self._collector is not None:
            params['collector'] = self._collector
        result = await self._cmd('network.getData', params)
        payload = result.get('bytes') or {}
        value = payload.get('value', '')
        data = (_b64.b64decode(value) if payload.get('type') == 'base64'
                else str(value).encode())
        if self._collector is not None:
            try:
                await self._cmd('network.disownData', {
                    'dataType': data_type, 'request': request_id,
                    'collector': self._collector})
            except Exception:
                pass
        return data

    async def request_body(self, request_id: str) -> bytes | None:
        '''A collected request body — available while the request is still
        blocked at the intercept, which is how ``InterceptedRequest.post_data``
        and ``page.relay`` get it on Firefox. None when nothing was collected.'''
        cached = self._request_bodies.get(request_id)
        if cached is not None:
            return cached
        try:
            data = await self._get_data('request', request_id)
        except BidiCommandError as exc:
            if exc.code == 'no such network data':
                return None
            raise
        if len(self._request_bodies) >= 256:   # bounded: oldest first
            self._request_bodies.pop(next(iter(self._request_bodies)))
        self._request_bodies[request_id] = data
        return data

    async def response_body(self, request_id: str) -> bytes:
        '''A collected response body (``network.getData``), released from the
        collector afterwards. Raises when nothing was collected for the
        request (no collector, evicted, or a bodyless response) — the
        recorder decides whether that was a body it expected.'''
        return await self._get_data('response', request_id)

    def _partition(self) -> dict:
        return {'type': 'context', 'context': self.context}

    async def cookies(self, urls: typing.Sequence[str] | None = None
                      ) -> list[dict]:
        '''Cookies in this context's storage partition (``urls`` narrows
        by host, as CDP does).'''
        from urllib.parse import urlsplit
        result = await self._cmd('storage.getCookies',
                                            {'partition': self._partition()})
        cookies = [_cookie_out(c) for c in result.get('cookies', [])]
        if urls:
            hosts = {urlsplit(u).hostname for u in urls}
            cookies = [c for c in cookies
                       if any(_domain_matches(h, c['domain']) for h in hosts)]
        return cookies

    async def set_cookies(self, cookies: typing.Sequence[dict]) -> None:
        for cookie in cookies:
            await self._cmd('storage.setCookie', {
                'cookie': _cookie_in(cookie), 'partition': self._partition()})

    async def clear_cookies(self) -> None:
        await self._cmd('storage.deleteCookies',
                                   {'partition': self._partition()})

    async def extra_headers(self, headers: dict[str, str]) -> None:
        '''Merge into this context's extra headers (the BiDi command sets
        the whole list, so a UA/Accept-Language call must not wipe an earlier
        ``set_extra_headers``, nor the reverse).'''
        self._extra_headers.update(headers)
        await self._cmd('network.setExtraHeaders', {
            'headers': _header_list(self._extra_headers),
            'contexts': [self.context]})

    # -- interception (network.addIntercept at beforeRequestSent) ------------

    async def enable_intercept(self) -> None:
        if self._intercept is not None:
            return
        await self.collect_bodies()   # blocked requests' bodies come from the collector
        result = await self._cmd('network.addIntercept', {
            'phases': ['beforeRequestSent'], 'contexts': [self.context]})
        self._intercept = str(result['intercept'])

    @staticmethod
    def _body_param(data: bytes | None) -> dict | None:
        return ({'type': 'base64', 'value': _b64.b64encode(data).decode()}
                if data else None)

    async def fulfill(self, request_id: str, *, status: int,
                      headers: typing.Sequence[tuple[str, str]],
                      body: bytes) -> None:
        params: dict = {'request': request_id, 'statusCode': status,
                        'headers': [{'name': k, 'value': {'type': 'string', 'value': v}}
                                    for k, v in headers]}
        if body:
            params['body'] = self._body_param(body)
        await self._cmd('network.provideResponse', params)

    async def continue_(self, request_id: str, *, url: str | None = None,
                        method: str | None = None,
                        post_data: bytes | None = None,
                        headers: typing.Sequence[tuple[str, str]] | None = None
                        ) -> None:
        params: dict = {'request': request_id}
        if url is not None:
            params['url'] = url
        if method is not None:
            params['method'] = method
        if post_data:
            params['body'] = self._body_param(post_data)
        if headers:
            params['headers'] = [{'name': k, 'value': {'type': 'string', 'value': v}}
                                 for k, v in headers]
        try:
            await self._cmd('network.continueRequest', params)
        except BidiCommandError as exc:
            # Firefox reports some requests blocked that it never holds (a
            # data: navigation): continuing one it already let through is a
            # no-op, not a handler failure.
            if exc.code != 'no such request' or (url or method or post_data or headers):
                raise

    async def fail(self, request_id: str, reason: str) -> None:
        await self._cmd('network.failRequest', {'request': request_id})  # no reason on BiDi

    # -- emulation -----------------------------------------------------------

    async def viewport(self, width: int, height: int, *, scale: float = 1.0,
                       mobile: bool = False) -> None:
        await self._cmd('browsingContext.setViewport', {
            'context': self.context, 'viewport': {'width': width, 'height': height},
            'devicePixelRatio': scale})

    async def user_agent(self, user_agent: str, *,
                         accept_language: str | None = None,
                         platform: str | None = None,
                         metadata: dict | None = None) -> None:
        '''``emulation.setUserAgentOverride``; UA-CH metadata and platform
        have no BiDi knob (Gecko sends no client hints), accept-language is
        applied as an extra header.'''
        await self._cmd('emulation.setUserAgentOverride', {
            'userAgent': user_agent, 'contexts': [self.context]})
        if accept_language:
            await self.extra_headers({'Accept-Language': accept_language})

    async def media(self, **_: typing.Any) -> None:
        raise self._unsupported('media emulation')

    async def geolocation(self, latitude: float, longitude: float, *,
                          accuracy: float = 1.0) -> None:
        await self._cmd('emulation.setGeolocationOverride', {
            'coordinates': {'latitude': latitude, 'longitude': longitude,
                            'accuracy': accuracy},
            'contexts': [self.context]})

    async def timezone(self, timezone_id: str) -> None:
        await self._cmd('emulation.setTimezoneOverride', {
            'timezone': timezone_id, 'contexts': [self.context]})

    # -- downloads -----------------------------------------------------------

    async def download_behavior(self, directory: str) -> None:
        '''Allow downloads into ``directory`` for THIS page's user context
        and emit the download events.'''
        await self.connection.subscribe(['browsingContext.downloadWillBegin',
                                         'browsingContext.downloadEnd'])
        tree = await self._cmd('browsingContext.getTree', {'root': self.context})
        contexts = tree.get('contexts') or [{}]
        user_context = contexts[0].get('userContext')
        params: dict = {'downloadBehavior': {'type': 'allowed',
                                             'destinationFolder': directory}}
        if user_context:
            params['userContexts'] = [user_context]
        await self._cmd('browser.setDownloadBehavior', params)

    # -- accessibility -------------------------------------------------------

    async def ax_tree(self, *, depth: int | None = None,
                      engine: str = 'walker') -> list[dict]:
        '''Wire-shaped AX nodes from the in-page walker (see _ax_walker) —
        BiDi has no tree command, so ``engine`` is accepted and ignored.
        Also notes which registry index hosts which child browsing context,
        for :meth:`frame_owner`.'''
        nodes = list(await self.call(AX_WALKER_JS, [depth], await_promise=False) or [])
        owners: dict[str, int] = {}
        if not any((n.get('role') or {}).get('value') == 'Iframe' for n in nodes):
            self._iframe_owners = owners
            return nodes                      # one round-trip on iframe-less pages
        remote = await self._call(AX_IFRAME_WINDOWS_JS, [], await_promise=False,
                                  user_gesture=False, ownership='none')
        for pair in (remote.get('value') or []) if remote.get('type') == 'array' else []:
            items = pair.get('value') or []
            if len(items) == 2 and items[1].get('type') == 'window':
                ctx = (items[1].get('value') or {}).get('context')
                if ctx is not None:
                    owners[str(ctx)] = int(items[0].get('value', 0))
        self._iframe_owners = owners
        return nodes

    async def resolve_ax_node(self, backend_id: int, *,
                              engine: str = 'walker') -> Handle | None:
        return await self.call_handle(AX_RESOLVE_JS, [int(backend_id)],
                                      await_promise=False)

    # -- child frames (every iframe is a browsing context, no attach dance) --

    async def enable_frame_attach(self) -> None:
        '''Refresh the child-context tree; ``frame_scopes()`` reads the
        cached copy, so callers that poll (resolve_frame) re-arm each loop.'''
        tree = await self._cmd('browsingContext.getTree',
                                          {'root': self.context})
        scopes: list[FrameScope] = []

        def walk(node: dict) -> None:
            for child in node.get('children') or []:
                ctx = str(child['context'])
                driver = self._children.get(ctx)
                if driver is None:
                    driver = self._children[ctx] = BidiDriver(self.connection, ctx)
                    driver._children = self._children  # one cache per page
                scopes.append(FrameScope(frame_id=ctx, url=child.get('url') or '',
                                         driver=driver))
                walk(child)

        for top in tree.get('contexts', []):
            walk(top)
        self._scopes = scopes

    def frame_scopes(self) -> list[FrameScope]:
        return list(self._scopes)

    async def frame_owner(self, frame_id: str) -> int | None:
        '''The registry index of the <iframe> hosting ``frame_id`` in this
        document — known after :meth:`ax_tree` ran on this scope.'''
        return self._iframe_owners.get(frame_id)

    # -- events --------------------------------------------------------------

    def listen(self, *kinds: type, buffer_size: int = 256) -> _Stream:
        unsupported = [k.__name__ for k in kinds if k not in _EVENTS]
        if unsupported:
            raise self._unsupported(f'{", ".join(unsupported)} events')
        methods = tuple(dict.fromkeys(
            [m for k in kinds for m in _EVENTS[k]] + ['browsingContext.contextDestroyed']))
        return _Stream(self.connection.listen(*methods, buffer_size=buffer_size),
                       kinds, self.context)

    async def wait_for(self, *kinds: type) -> typing.Any:
        with self.listen(*kinds) as stream:
            async for event in stream:
                return event
        raise CDPSessionClosed(
            f'browsing context {self.context} is gone (event stream ended)')

    # -- lifecycle -----------------------------------------------------------

    async def close_target(self) -> None:
        try:
            await self._cmd('browsingContext.close', {'context': self.context})
        finally:
            self._closed = True
