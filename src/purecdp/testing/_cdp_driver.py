'''The CDP implementation of the driver seam (:mod:`._driver`), over one
:class:`~purecdp.connection.Session` — a page's, or a cross-origin frame's.
Everything protocol-specific the testing layer used to do inline lives here:
RemoteObject handles, the per-call ``globalThis`` anchor for argument-passing
calls, Fetch interception, Network event shapes, OOPIF sessions.
'''

from __future__ import annotations

import typing
from contextlib import suppress

from .. import _b64
from ..connection import EventStream, Session
from ..errors import CDPCommandError
from ..protocol import browser as browser_proto
from ..protocol import dom as dom_proto
from ..protocol import emulation as emulation_proto
from ..protocol import fetch as fetch_proto
from ..protocol import input as input_proto
from ..protocol import network as network_proto
from ..protocol import page as page_proto
from ..protocol import runtime as runtime_proto
from ._ax_walker import AX_RESOLVE_JS, AX_WALKER_JS
from ._driver import (Console, Dialog, DownloadBegin, DownloadProgress,
                      Exception_, Failed, Finished, FrameScope, FrameStopped,
                      Handle, JSError, Load, Paused, Request, RequestExtra,
                      Response, ResponseExtra)


def raw_command(method: str, params: dict | None = None):
    '''A CDP command whose result parse is the identity — yields the request
    and returns the raw result dict. Drives the normal execute path (routing,
    errors) but skips generated ``from_json``, so a caller can read the wire
    directly (see ax_tree).'''
    result = yield {'method': method, 'params': params or {}}
    return result


def _remote_text(obj: typing.Any) -> str:
    '''Best-effort string for a runtime.RemoteObject.'''
    if obj.value is not None:
        return str(obj.value)
    if obj.unserializable_value is not None:
        return str(obj.unserializable_value)
    if obj.description:
        return obj.description
    return str(obj.type)


def _handle_of(obj: typing.Any) -> Handle | None:
    return Handle(str(obj.object_id), raw=obj) if obj.object_id is not None else None


def _header_entries(pairs: typing.Iterable[tuple[str, str]] | None
                    ) -> list[fetch_proto.HeaderEntry] | None:
    entries = [fetch_proto.HeaderEntry(name=k, value=v) for k, v in (pairs or ())]
    return entries or None


def _paused_body(request: typing.Any) -> bytes | None:
    '''Everything knowable is on the paused event already (no round-trip):
    Chrome puts small text bodies in ``postData`` and large or binary ones
    only in ``postDataEntries``.'''
    if request.post_data is not None:
        return request.post_data.encode()
    if request.post_data_entries:
        return b''.join(_b64.b64decode(entry.bytes)
                        for entry in request.post_data_entries if entry.bytes)
    return None


#: normalized kind -> the CDP event type(s) it is read from
_SESSION_EVENTS: dict[type, tuple[type, ...]] = {
    Console: (runtime_proto.ConsoleAPICalled,),
    Exception_: (runtime_proto.ExceptionThrown,),
    Load: (page_proto.LoadEventFired,),
    FrameStopped: (page_proto.FrameStoppedLoading,),
    Dialog: (page_proto.JavascriptDialogOpening,),
    Request: (network_proto.RequestWillBeSent,),
    RequestExtra: (network_proto.RequestWillBeSentExtraInfo,),
    Response: (network_proto.ResponseReceived,),
    ResponseExtra: (network_proto.ResponseReceivedExtraInfo,),
    Finished: (network_proto.LoadingFinished,),
    Failed: (network_proto.LoadingFailed,),
    Paused: (fetch_proto.RequestPaused,),
}
#: browser-level (connection) events
_CONNECTION_EVENTS: dict[type, tuple[type, ...]] = {
    DownloadBegin: (browser_proto.DownloadWillBegin,),
    DownloadProgress: (browser_proto.DownloadProgress,),
}


def _normalize(event: typing.Any) -> typing.Any:
    '''CDP event -> normalized event (None for anything unexpected).'''
    if isinstance(event, runtime_proto.ConsoleAPICalled):
        return Console(kind=str(event.type),
                       text=' '.join(_remote_text(a) for a in event.args))
    if isinstance(event, runtime_proto.ExceptionThrown):
        return Exception_(JSError.from_details(event.exception_details))
    if isinstance(event, page_proto.LoadEventFired):
        return Load()
    if isinstance(event, page_proto.FrameStoppedLoading):
        return FrameStopped(frame_id=str(event.frame_id))
    if isinstance(event, page_proto.JavascriptDialogOpening):
        return Dialog(message=event.message, type=str(event.type),
                      default_prompt=event.default_prompt)
    if isinstance(event, network_proto.RequestWillBeSent):
        return Request(
            request_id=str(event.request_id), url=event.request.url,
            method=event.request.method,
            headers=dict(event.request.headers or {}),
            post_data=event.request.post_data,
            resource_type=str(event.type) if event.type is not None else None,
            frame_id=str(event.frame_id) if event.frame_id is not None else None,
            timestamp=float(event.timestamp), wall_time=float(event.wall_time))
    if isinstance(event, network_proto.RequestWillBeSentExtraInfo):
        return RequestExtra(request_id=str(event.request_id),
                            headers=dict(event.headers or {}))
    if isinstance(event, network_proto.ResponseReceived):
        return Response(request_id=str(event.request_id),
                        status=int(event.response.status),
                        mime=str(event.response.mime_type),
                        headers=dict(event.response.headers or {}))
    if isinstance(event, network_proto.ResponseReceivedExtraInfo):
        return ResponseExtra(request_id=str(event.request_id),
                             headers=dict(event.headers or {}))
    if isinstance(event, network_proto.LoadingFinished):
        return Finished(request_id=str(event.request_id),
                        timestamp=float(event.timestamp),
                        encoded_length=int(event.encoded_data_length))
    if isinstance(event, network_proto.LoadingFailed):
        return Failed(request_id=str(event.request_id),
                      timestamp=float(event.timestamp),
                      error_text=str(event.error_text),
                      canceled=bool(event.canceled))
    if isinstance(event, fetch_proto.RequestPaused):
        request = event.request
        return Paused(
            request_id=str(event.request_id), url=request.url,
            method=request.method, headers=dict(request.headers),
            post_data=_paused_body(request),
            resource_type=str(event.resource_type),
            frame_id=str(event.frame_id) if event.frame_id is not None else None)
    if isinstance(event, browser_proto.DownloadWillBegin):
        return DownloadBegin(guid=event.guid, url=event.url,
                             suggested_filename=event.suggested_filename)
    if isinstance(event, browser_proto.DownloadProgress):
        return DownloadProgress(guid=event.guid, state=str(event.state),
                                file_path=event.file_path)
    return None


class _NormalizedStream:
    '''An EventStream whose items come out normalized.'''

    def __init__(self, inner: EventStream):
        self._inner = inner

    def close(self) -> None:
        self._inner.close()

    def __enter__(self) -> _NormalizedStream:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __aiter__(self) -> _NormalizedStream:
        return self

    async def __anext__(self) -> typing.Any:
        while True:
            event = _normalize(await self._inner.__anext__())
            if event is not None:
                return event


class CdpDriver:
    '''Drive one CDP session (a page or an out-of-process frame).'''

    backend = 'cdp'

    def __init__(self, session: Session):
        self.session = session
        self._frame_drivers: dict[Session, CdpDriver] = {}
        self._frame_attach_enabled = False
        #: engine of the last ax_tree() on this scope + the walker's iframe
        #: map (registry index -> hosted frame id) for frame_owner()
        self._ax_engine = 'native'
        self._iframe_owners: dict[str, int] = {}
        self._global_anchor: Handle | None = None  # see _anchor()

    def __repr__(self) -> str:
        return f'CdpDriver({self.session.target_id!r})'

    @property
    def alive(self) -> bool:
        return not self.session.closed

    @property
    def connection(self):
        return self.session.connection

    async def arm(self, *, capture: bool, track_network: bool) -> None:
        '''Enable the domains a Page needs: Page always (navigation waits,
        low-risk), Runtime for console/exception capture (detectable —
        ``isAutomatedWithCDP``), Network for in-flight tracking. Subscribe
        BEFORE calling this, or early events are lost.'''
        await self.session.execute(page_proto.enable())
        if capture:
            await self.session.execute(runtime_proto.enable())
        if track_network:
            await self.session.execute(network_proto.enable())

    # -- scripting -----------------------------------------------------------

    async def _evaluate(self, expression: str, *, await_promise: bool,
                        user_gesture: bool, by_value: bool):
        result, details = await self.session.execute(runtime_proto.evaluate(
            expression=expression, return_by_value=by_value,
            await_promise=await_promise,
            user_gesture=True if user_gesture else None))
        if details is not None:
            raise JSError.from_details(details)
        return result

    async def evaluate(self, expression: str, *, await_promise: bool = True,
                       user_gesture: bool = False) -> typing.Any:
        result = await self._evaluate(expression, await_promise=await_promise,
                                      user_gesture=user_gesture, by_value=True)
        return result.value

    async def evaluate_handle(self, expression: str, *, await_promise: bool = True,
                              user_gesture: bool = False) -> Handle | None:
        result = await self._evaluate(expression, await_promise=await_promise,
                                      user_gesture=user_gesture, by_value=False)
        return _handle_of(result)

    @staticmethod
    def _call_args(args: typing.Sequence[typing.Any]
                   ) -> tuple[list[runtime_proto.CallArgument], Handle | None]:
        '''CallArguments for ``args`` plus the first Handle among them (the
        natural anchor for callFunctionOn — an element's own handle).'''
        import json
        call_args, anchor = [], None
        for index, arg in enumerate(args):
            handle = arg if isinstance(arg, Handle) else getattr(arg, '_handle', None)
            if isinstance(handle, Handle):
                if anchor is None:
                    anchor = handle
                call_args.append(runtime_proto.CallArgument(
                    object_id=runtime_proto.RemoteObjectId(handle.id)))
                continue
            try:
                json.dumps(arg)
            except (TypeError, ValueError) as exc:
                raise TypeError(
                    f'evaluate() argument {index} is neither JSON-serializable '
                    f'nor an Element: {arg!r}') from exc
            call_args.append(runtime_proto.CallArgument(value=arg))
        return call_args, anchor

    async def _anchor(self) -> Handle:
        '''This session's cached ``globalThis`` handle, resolving it on a
        miss — so an argument-passing call is ONE round-trip after the first
        (it was three: resolve, call, release — which a remote ``connect()``
        at tens of ms RTT paid on every ``evaluate(fn, arg)``). The handle
        dies with every navigation; rather than track that, ``_call`` drops a
        cached anchor that CDP reports gone (-32000) and retries once — the
        retry IS the invalidation.'''
        if self._global_anchor is None:
            anchor, _ = await self.session.execute(runtime_proto.evaluate(
                expression='globalThis', return_by_value=False))
            if anchor.object_id is None:  # can't realistically happen; fail loudly
                raise JSError('could not resolve globalThis to anchor callFunctionOn')
            self._global_anchor = Handle(str(anchor.object_id))
        return self._global_anchor

    async def _call(self, function: str, args: typing.Sequence[typing.Any], *,
                    await_promise: bool, user_gesture: bool, by_value: bool):
        '''``function(*args)`` via Runtime.callFunctionOn, anchored on the
        first Handle argument when there is one, else on the cached
        ``globalThis`` anchor (see _anchor).'''
        call_args, anchor = self._call_args(args)
        cached = anchor is None and self._global_anchor is not None
        while True:
            target = anchor if anchor is not None else await self._anchor()
            try:
                result, details = await self.session.execute(
                    runtime_proto.call_function_on(
                        function_declaration=(
                            f'function(...args) {{ return ({function})(...args); }}'),
                        object_id=runtime_proto.RemoteObjectId(target.id),
                        arguments=call_args,
                        return_by_value=by_value,
                        await_promise=await_promise,
                        user_gesture=True if user_gesture else None))
            except CDPCommandError as exc:
                # -32000 = "Cannot find context with specified id" / "Could not
                # find object with given id": the page navigated since the
                # anchor was taken. Only a CACHED anchor earns the retry — a
                # fresh one (or an Element arg) failing the same way is a real
                # error and must not loop.
                self._global_anchor = None
                if exc.code != -32000 or not cached:
                    raise
                cached = False
                continue
            break
        if details is not None:
            error = JSError.from_details(details)
            if 'is not a function' in str(error):
                error.add_note('with arguments, evaluate() takes a function '
                               "declaration like '(a, b) => ...' as its expression")
            raise error
        return result

    async def call(self, function: str, args: typing.Sequence[typing.Any], *,
                   await_promise: bool = True, user_gesture: bool = False
                   ) -> typing.Any:
        result = await self._call(function, args, await_promise=await_promise,
                                  user_gesture=user_gesture, by_value=True)
        return result.value

    async def call_handle(self, function: str, args: typing.Sequence[typing.Any],
                          *, await_promise: bool = True,
                          user_gesture: bool = False) -> Handle | None:
        result = await self._call(function, args, await_promise=await_promise,
                                  user_gesture=user_gesture, by_value=False)
        return _handle_of(result)

    async def release(self, handle: Handle) -> None:
        await self.session.execute(runtime_proto.release_object(
            runtime_proto.RemoteObjectId(handle.id)))

    async def array_handles(self, handle: Handle) -> list[Handle]:
        '''The object handles of a JS array's elements, in index order; the
        array wrapper itself is released (best-effort) afterwards.'''
        props, *_rest = await self.session.execute(runtime_proto.get_properties(
            runtime_proto.RemoteObjectId(handle.id), own_properties=True))
        indexed: list[tuple[int, Handle]] = []
        for prop in props:
            if (prop.name.isdigit() and prop.value is not None
                    and prop.value.object_id is not None):
                indexed.append((int(prop.name), Handle(str(prop.value.object_id))))
        indexed.sort(key=lambda pair: pair[0])
        with suppress(Exception):
            await self.release(handle)
        return [h for _, h in indexed]

    async def add_init_script(self, source: str) -> None:
        await self.session.execute(
            page_proto.add_script_to_evaluate_on_new_document(source=source))

    # -- nodes ---------------------------------------------------------------

    async def node_frame_id(self, handle: Handle) -> str | None:
        node = await self.session.execute(dom_proto.describe_node(
            object_id=runtime_proto.RemoteObjectId(handle.id)))
        return str(node.frame_id) if node.frame_id is not None else None

    async def set_files(self, handle: Handle, paths: typing.Sequence[str]) -> None:
        await self.session.execute(dom_proto.set_file_input_files(
            files=list(paths), object_id=runtime_proto.RemoteObjectId(handle.id)))

    # -- navigation / content ------------------------------------------------

    async def navigate(self, url: str) -> tuple[str | None, str | None]:
        result = await self.session.execute(page_proto.navigate(url=url))
        return str(result[0]), result[2]

    async def main_frame_id(self) -> str:
        tree = await self.session.execute(page_proto.get_frame_tree())
        return str(tree.frame.id)

    async def set_content(self, html: str) -> None:
        tree = await self.session.execute(page_proto.get_frame_tree())
        await self.session.execute(page_proto.set_document_content(
            frame_id=tree.frame.id, html=html))

    # -- capture -------------------------------------------------------------

    async def screenshot(self, *, format: str = 'png', quality: int | None = None,
                         clip: dict | None = None,
                         beyond_viewport: bool = False) -> bytes:
        viewport = None
        if clip is not None:
            viewport = page_proto.Viewport(
                x=clip['x'], y=clip['y'], width=clip['width'],
                height=clip['height'], scale=clip.get('scale', 1))
        data = await self.session.execute(page_proto.capture_screenshot(
            format=format, quality=quality, clip=viewport,
            capture_beyond_viewport=True if beyond_viewport else None))
        return _b64.b64decode(data)

    async def pdf(self, **options: typing.Any) -> bytes:
        data, _stream = await self.session.execute(
            page_proto.print_to_pdf(**options))
        return _b64.b64decode(data)

    # -- input ---------------------------------------------------------------

    async def mouse(self, kind: str, x: float, y: float, *,
                    button: str | None = None, click_count: int | None = None,
                    delta_x: float | None = None, delta_y: float | None = None
                    ) -> None:
        await self.session.execute(input_proto.dispatch_mouse_event(
            type=kind, x=x, y=y,
            button=input_proto.MouseButton(button) if button else None,
            click_count=click_count, delta_x=delta_x, delta_y=delta_y))

    async def key(self, kind: str, *, key: str, code: str | None = None,
                  text: str | None = None, virtual_key: int | None = None
                  ) -> None:
        await self.session.execute(input_proto.dispatch_key_event(
            type=kind, key=key, code=code, text=text,
            windows_virtual_key_code=virtual_key,
            native_virtual_key_code=virtual_key))

    async def insert_text(self, text: str) -> None:
        await self.session.execute(input_proto.insert_text(text))

    # -- dialogs -------------------------------------------------------------

    async def handle_dialog(self, *, accept: bool,
                            prompt_text: str | None = None) -> None:
        await self.session.execute(page_proto.handle_java_script_dialog(
            accept=accept, prompt_text=prompt_text))

    # -- network -------------------------------------------------------------

    async def enable_network(self) -> None:
        await self.session.execute(network_proto.enable())

    async def collect_bodies(self) -> None:
        pass  # CDP keeps bodies for Network.getResponseBody by itself

    async def request_body(self, request_id: str) -> bytes | None:
        return None  # CDP puts postData on the events themselves

    async def response_body(self, request_id: str) -> bytes:
        body, is_b64 = await self.session.execute(
            network_proto.get_response_body(network_proto.RequestId(request_id)))
        return _b64.b64decode(body) if is_b64 else body.encode()

    async def cookies(self, urls: typing.Sequence[str] | None = None
                      ) -> list[dict]:
        got = await self.session.execute(network_proto.get_cookies(
            urls=list(urls) if urls is not None else None))
        return [c.to_json() for c in got]

    async def set_cookies(self, cookies: typing.Sequence[dict]) -> None:
        params = [network_proto.CookieParam.from_json(c) for c in cookies]
        await self.session.execute(network_proto.set_cookies(cookies=params))

    async def clear_cookies(self) -> None:
        await self.session.execute(network_proto.clear_browser_cookies())

    async def extra_headers(self, headers: dict[str, str]) -> None:
        await self.session.execute(network_proto.set_extra_http_headers(
            headers=network_proto.Headers(headers)))

    # -- interception --------------------------------------------------------

    async def enable_intercept(self) -> None:
        await self.session.execute(fetch_proto.enable())

    async def fulfill(self, request_id: str, *, status: int,
                      headers: typing.Sequence[tuple[str, str]],
                      body: bytes) -> None:
        await self.session.execute(fetch_proto.fulfill_request(
            request_id=fetch_proto.RequestId(request_id),
            response_code=status,
            response_headers=_header_entries(headers),
            body=_b64.b64encode(body).decode() if body else None))

    async def continue_(self, request_id: str, *, url: str | None = None,
                        method: str | None = None,
                        post_data: bytes | None = None,
                        headers: typing.Sequence[tuple[str, str]] | None = None
                        ) -> None:
        await self.session.execute(fetch_proto.continue_request(
            request_id=fetch_proto.RequestId(request_id), url=url, method=method,
            post_data=_b64.b64encode(post_data).decode() if post_data else None,
            headers=_header_entries(headers) if headers else None))

    async def fail(self, request_id: str, reason: str) -> None:
        await self.session.execute(fetch_proto.fail_request(
            request_id=fetch_proto.RequestId(request_id),
            error_reason=network_proto.ErrorReason(reason)))

    # -- emulation -----------------------------------------------------------

    async def viewport(self, width: int, height: int, *, scale: float = 1.0,
                       mobile: bool = False) -> None:
        await self.session.execute(emulation_proto.set_device_metrics_override(
            width=width, height=height, device_scale_factor=scale, mobile=mobile))

    async def user_agent(self, user_agent: str, *,
                         accept_language: str | None = None,
                         platform: str | None = None,
                         metadata: dict | None = None) -> None:
        ua_metadata = (emulation_proto.UserAgentMetadata.from_json(metadata)
                       if metadata is not None else None)
        await self.session.execute(emulation_proto.set_user_agent_override(
            user_agent=user_agent, accept_language=accept_language,
            platform=platform, user_agent_metadata=ua_metadata))

    async def media(self, *, media: str | None = None,
                    features: typing.Sequence[tuple[str, str]] = ()) -> None:
        feats = [emulation_proto.MediaFeature(name=k, value=v) for k, v in features]
        await self.session.execute(emulation_proto.set_emulated_media(
            media=media, features=feats or None))

    async def geolocation(self, latitude: float, longitude: float, *,
                          accuracy: float = 1.0) -> None:
        await self.session.execute(emulation_proto.set_geolocation_override(
            latitude=latitude, longitude=longitude, accuracy=accuracy))

    async def timezone(self, timezone_id: str) -> None:
        await self.session.execute(
            emulation_proto.set_timezone_override(timezone_id=timezone_id))

    # -- downloads -----------------------------------------------------------

    async def download_behavior(self, directory: str) -> None:
        '''Allow downloads into ``directory`` with events, scoped to THIS
        page's browser context — an isolated context (the CDPTestCase
        default) doesn't inherit the default one's, so a browser-wide call
        would leave the download denied → "canceled".'''
        info = self.session.target_info
        context_id = info.browser_context_id if info is not None else None
        await self.connection.execute(browser_proto.set_download_behavior(
            behavior='allowAndName', download_path=directory,
            browser_context_id=context_id, events_enabled=True))

    # -- accessibility -------------------------------------------------------

    async def ax_tree(self, *, depth: int | None = None,
                      engine: str = 'native') -> list[dict]:
        '''``engine='native'``: raw ``Accessibility.getFullAXTree`` nodes
        (camelCase wire dicts — build_from_wire needs only a few fields, so
        parsing every node into a typed AXNode just to flatten it back is pure
        overhead, sizeable on large trees). ``engine='walker'``: the in-page
        walker (one evaluate, ~3× faster on big pages, fewer wrapper nodes;
        refs index its registry).'''
        self._ax_engine = engine
        if engine == 'walker':
            nodes = list(await self.call(AX_WALKER_JS, [depth], await_promise=False) or [])
            owners: dict[str, int] = {}
            for node in nodes:  # map hosted frames for stitched snapshots
                if (node.get('role') or {}).get('value') == 'Iframe':
                    index = node.get('backendDOMNodeId')
                    handle = await self.call_handle(AX_RESOLVE_JS, [index], await_promise=False)
                    if handle is not None:
                        frame_id = await self.node_frame_id(handle)
                        if frame_id:
                            owners[frame_id] = int(index)
            self._iframe_owners = owners
            return nodes
        params = {} if depth is None else {'depth': depth}
        result = await self.session.execute(
            raw_command('Accessibility.getFullAXTree', params))
        return result.get('nodes', [])

    async def resolve_ax_node(self, backend_id: int, *,
                              engine: str = 'native') -> Handle | None:
        if engine == 'walker':
            return await self.call_handle(AX_RESOLVE_JS, [int(backend_id)],
                                          await_promise=False)
        obj = await self.session.execute(dom_proto.resolve_node(
            backend_node_id=dom_proto.BackendNodeId(backend_id)))
        return _handle_of(obj)

    # -- cross-origin frames (OOPIFs get their own sessions) -----------------

    async def enable_frame_attach(self) -> None:
        '''Arm flat auto-attach (once) so cross-origin iframes get their own
        CDP sessions. ``wait_for_debugger=False`` — subframes attach *live*,
        never paused, so there are no resume round-trips and no deadlock.'''
        if self._frame_attach_enabled:
            return
        self._frame_attach_enabled = True
        conn = self.connection

        async def _arm(session):  # propagate auto-attach into nested OOPIFs
            info = session.target_info
            if info is not None and 'iframe' in (info.type or ''):
                await session.set_auto_attach(wait_for_debugger=False)

        conn.add_session_hook(_arm)
        await self.session.set_auto_attach(wait_for_debugger=False)
        import asyncio
        await asyncio.sleep(0.05)  # let already-loaded OOPIFs attach (one-time)

    def _driver_for(self, session: Session) -> CdpDriver:
        driver = self._frame_drivers.get(session)
        if driver is None:
            driver = self._frame_drivers[session] = CdpDriver(session)
            driver._frame_drivers = self._frame_drivers  # one cache per page
        return driver

    def frame_scopes(self) -> list[FrameScope]:
        '''Every attached out-of-process iframe on this connection. The
        target id doubles as the frame id for OOPIFs.'''
        scopes = []
        for s in self.connection.sessions.values():
            info = s.target_info
            if info is not None and 'iframe' in (info.type or ''):
                scopes.append(FrameScope(frame_id=str(s.target_id),
                                         url=info.url or '',
                                         driver=self._driver_for(s)))
        return scopes

    async def frame_owner(self, frame_id: str) -> int | None:
        '''The backend node id of the <iframe> hosting ``frame_id`` in THIS
        scope's document, or None when the frame isn't a descendant of it.
        After a walker-engine ax_tree() the ids are registry indices.'''
        if self._ax_engine == 'walker':
            return self._iframe_owners.get(frame_id)
        try:
            backend, _ = await self.session.execute(dom_proto.get_frame_owner(
                frame_id=page_proto.FrameId(frame_id)))
        except Exception:
            return None
        return int(backend)

    # -- events --------------------------------------------------------------

    def listen(self, *kinds: type, buffer_size: int = 256) -> _NormalizedStream:
        '''A stream of normalized events of the given kinds. Download kinds
        are browser-level (the connection); everything else is this session's.
        One stream cannot mix the two.'''
        if any(k in _CONNECTION_EVENTS for k in kinds):
            types = tuple(t for k in kinds for t in _CONNECTION_EVENTS[k])
            return _NormalizedStream(
                self.connection.listen(*types, buffer_size=buffer_size))
        types = tuple(t for k in kinds for t in _SESSION_EVENTS[k])
        return _NormalizedStream(
            self.session.listen(*types, buffer_size=buffer_size))

    async def wait_for(self, *kinds: type) -> typing.Any:
        '''The next event of one of ``kinds``. Subscribes immediately — but
        only to events from now on, so create the task before triggering.'''
        with self.listen(*kinds) as stream:
            async for event in stream:
                return event
        raise self.session._end_error('event stream ended while waiting')

    # -- lifecycle -----------------------------------------------------------

    async def close_target(self) -> None:
        from ..browser import close_page
        await close_page(self.connection, self.session)
