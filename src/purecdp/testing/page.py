'''The Page helper: opinionated, test-oriented operations over one Session.

Create with ``await Page.create(session)`` — it enables the Page/Runtime/
Network domains and starts background capture of console messages, uncaught
exceptions, and in-flight network requests (for idle waits).
'''

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import tempfile
import typing
import warnings
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from urllib.parse import urlparse

from ..browser import Browser, close_page, launch
from ..connection import EventStream, Session
from ..errors import CDPClosedError, PureCDPError
from .. import _b64
from ..protocol import browser as browser_proto
from ..protocol import emulation as emulation_proto
from ..protocol import fetch as fetch_proto
from ..protocol import input as input_proto
from ..protocol import network as network_proto
from ..protocol import page as page_proto
from ..protocol import runtime as runtime_proto
from . import _agent
from .element import Element, ElementQueries
from .live import LiveFactories
from .snapshot import Snapshot
from .intercept import (Handler, InterceptedRequest, Route, forward,
                        matching_routes)
from .recorder import NetworkRecorder

#: Injects a <script>/<style> by url or inline content, resolving when ready.
#: Args (printf): tag ('script'|'style'), url (JSON), content (JSON).
_ADD_TAG_JS = '''
(() => new Promise((resolve, reject) => {
  const tag = %r, url = %s, content = %s;
  let el;
  if (tag === 'style' && url) {
    el = document.createElement('link'); el.rel = 'stylesheet'; el.href = url;
  } else {
    el = document.createElement(tag); if (url) el.src = url;
  }
  if (content) el.textContent = content;
  el.onload = () => resolve(true);
  el.onerror = () => reject(new Error('failed to load ' + (url || tag)));
  document.head.appendChild(el);
  if (!url) resolve(true);  // inline content is applied synchronously
}))()
'''.strip()


#: Key descriptors for press(): (code, text, virtual key code).
KEYS = {
    'Enter': ('Enter', '\r', 13),
    'Tab': ('Tab', '\t', 9),
    'Escape': ('Escape', None, 27),
    'Backspace': ('Backspace', None, 8),
    'Delete': ('Delete', None, 46),
    'ArrowUp': ('ArrowUp', None, 38),
    'ArrowDown': ('ArrowDown', None, 40),
    'ArrowLeft': ('ArrowLeft', None, 37),
    'ArrowRight': ('ArrowRight', None, 39),
}


class NavigateError(PureCDPError):
    '''Navigation failed (net error, bad URL, ...). ``net_error`` is Chrome's
    raw error string verbatim (e.g. ``'net::ERR_CONNECTION_TIMED_OUT'`` — the
    same vocabulary as ``Exchange.failed``) when known; ``url`` is the
    navigation target.'''

    def __init__(self, message: str, *, url: str | None = None,
                 net_error: str | None = None):
        super().__init__(message)
        self.url = url
        self.net_error = net_error


class JSError(PureCDPError):
    '''A JavaScript exception, surfaced from evaluate() or captured uncaught.'''

    def __init__(self, message: str, details: typing.Any = None):
        super().__init__(message)
        self.details = details  # protocol.runtime.ExceptionDetails

    @classmethod
    def from_details(cls, details: typing.Any) -> JSError:
        exception = details.exception
        if exception is not None and exception.description:
            message = exception.description
        elif exception is not None and exception.value is not None:
            message = str(exception.value)
        else:
            message = details.text
            if details.url:
                message += f' ({details.url}:{details.line_number})'
        return cls(message, details)


async def _call_function(
    session: Session,
    function: str,
    args: tuple,
    *,
    await_promise: bool = True,
    return_by_value: bool = True,
    user_gesture: bool = False,
) -> typing.Any:
    '''``function(*args)`` in the page via Runtime.callFunctionOn — how
    evaluate() passes values without interpolating them into JS source.
    Anchored on a per-call ``globalThis`` handle: a cached one dies with
    every navigation, and evaluate is nowhere near a hot path. Element args
    become live handles (legal: element handles and the anchor are both
    main-world); everything else must be JSON-serializable.'''
    call_args = []
    for index, arg in enumerate(args):
        if isinstance(arg, Element):
            call_args.append(runtime_proto.CallArgument(
                object_id=runtime_proto.RemoteObjectId(arg._object_id)))
            continue
        try:
            json.dumps(arg)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f'evaluate() argument {index} is neither JSON-serializable '
                f'nor an Element: {arg!r}') from exc
        call_args.append(runtime_proto.CallArgument(value=arg))
    anchor, _ = await session.execute(runtime_proto.evaluate(
        expression='globalThis', return_by_value=False))
    if anchor.object_id is None:  # can't realistically happen; fail loudly
        raise JSError('could not resolve globalThis to anchor callFunctionOn')
    try:
        result, details = await session.execute(runtime_proto.call_function_on(
            function_declaration=(
                f'function(...args) {{ return ({function})(...args); }}'),
            object_id=anchor.object_id,
            arguments=call_args,
            return_by_value=return_by_value,
            await_promise=await_promise,
            user_gesture=True if user_gesture else None,
        ))
    finally:
        with suppress(Exception):
            await session.execute(
                runtime_proto.release_object(anchor.object_id))
    if details is not None:
        error = JSError.from_details(details)
        if 'is not a function' in str(error):
            error.add_note('with arguments, evaluate() takes a function '
                           "declaration like '(a, b) => ...' as its expression")
        raise error
    return result.value if return_by_value else result


@dataclass
class ConsoleMessage:
    kind: str  # log, warning, error, ...
    text: str  # arguments stringified and space-joined


class DownloadError(PureCDPError):
    '''A download was canceled, or never completed within the timeout.'''


@dataclass
class Download:
    '''A completed download. ``path`` is the file on disk — the browser names
    it by download GUID under a temp directory, so read the bytes with
    :meth:`read` or copy it out under a real name with :meth:`save_as`.'''

    url: str
    suggested_filename: str
    guid: str
    path: str

    async def read(self) -> bytes:
        '''The downloaded file's bytes.'''
        return await asyncio.to_thread(pathlib.Path(self.path).read_bytes)

    async def save_as(self, dest: str) -> str:
        '''Copy the download to ``dest`` (its GUID-named temp copy stays put).
        Returns ``dest``.'''
        await asyncio.to_thread(shutil.copyfile, self.path, dest)
        return dest


class _DownloadExpectation:
    '''Handle yielded by :meth:`Page.expect_download`; ``await .value`` resolves
    to the :class:`Download` once it finishes.'''

    def __init__(self, task: asyncio.Task):
        self._task = task

    @property
    def value(self) -> typing.Awaitable[Download]:
        return self._task


def _remote_text(obj: typing.Any) -> str:
    '''Best-effort string for a runtime.RemoteObject.'''
    if obj.value is not None:
        return str(obj.value)
    if obj.unserializable_value is not None:
        return str(obj.unserializable_value)
    if obj.description:
        return obj.description
    return str(obj.type)


class Page(ElementQueries, LiveFactories):
    '''High-level page driver for tests. See Page.create().'''

    def __init__(self, session: Session, default_timeout: float):
        self.session = session
        self.default_timeout = default_timeout
        #: Console messages captured since create(), oldest first.
        self.console: list[ConsoleMessage] = []
        #: Uncaught page exceptions captured since create().
        self.js_errors: list[JSError] = []
        self._capture = False
        self._track_network = False
        self._inflight: set[str] = set()
        self._last_network_activity = 0.0
        self._doc_requests: dict[str, str] = {}  # Document request_id -> frame_id
        self._doc_failures: dict[str, str] = {}  # frame_id -> net error string
        self._tasks: list[asyncio.Task] = []
        self._streams: list[EventStream] = []
        #: NetworkRecorders started via record(); artifacts-on-failure dumps them.
        self._recorders: list[NetworkRecorder] = []
        self._routes: list[Route] = []
        self._intercepting = False
        self._cursor: typing.Any = None
        #: ref (e.g. "e3") -> backend DOM node id, from the latest snapshot().
        self._snapshot_refs: dict[str, int] = {}
        #: ref -> the Frame that owns it, for cross-frame (stitched) snapshots;
        #: absent => the ref resolves on this page's own session.
        self._snapshot_owners: dict[str, typing.Any] = {}
        #: CSS selectors clicked away during an actionability wait — cookie /
        #: consent banners that block actions (see add_auto_dismiss).
        self._dismissers: list[str] = []
        #: Armed on first frame() call — flat auto-attach for cross-origin
        #: iframes (see _enable_frame_attach).
        self._frame_attach_enabled = False
        #: Auto-answer CORS preflights for matched routes (see
        #: InterceptedRequest.respond_preflight). Disable for tests that are
        #: about preflight behavior itself.
        self.auto_preflight = True

    @property
    def alive(self) -> bool:
        '''True while this page can still be driven: its session is attached
        and the connection is up. Purely local state — no round-trip. Turns
        False when the tab closes, the target crashes, or the browser goes
        away (a connection abort closes every session). The watcher-loop
        front door: ``while page.alive: ...`` cannot spin on a dead tab —
        pair with ``except CDPClosedError: break`` for the give-up decision.
        '''
        return not self.session.closed

    @property
    def cursor(self):
        '''A stateful HumanCursor for curved, trusted mouse movement (M8
        Track B). Lazily created; position persists across moves.'''
        if self._cursor is None:
            from .human import HumanCursor
            self._cursor = HumanCursor(self)
        return self._cursor

    async def human_type(self, text: str, **kwargs: typing.Any) -> None:
        '''Type with real per-key events and human cadence (see
        human.human_type). Prefer over insert_text when a site scores typing
        rhythm.'''
        from .human import human_type
        await human_type(self, text, **kwargs)

    async def human_scroll(self, delta_y: float, **kwargs: typing.Any) -> None:
        '''Eased wheel scroll rather than one flat jump (see human.human_scroll).'''
        from .human import human_scroll
        await human_scroll(self, delta_y, **kwargs)

    @classmethod
    async def create(
        cls,
        session: Session,
        *,
        default_timeout: float = 10.0,
        capture: bool = True,
        track_network: bool = True,
    ) -> Page:
        '''Arm a Page over a session.

        ``capture`` (console messages + uncaught exceptions) enables the
        Runtime domain, and ``track_network`` (in-flight tracking for
        ``goto(wait='idle')`` / ``wait_for_network_idle``) enables the
        Network domain.

        Both default on for testing. **For stealth/low-observability set
        both False**: enabling the Runtime domain is itself detectable
        (`isAutomatedWithCDP`), so a "clean" page must skip it. Interception
        (``route``) and ``record`` enable their own domains on demand and are
        unaffected by these flags. The Page domain is always enabled (needed
        for navigation waits, low-risk).
        '''
        page = cls(session, default_timeout)
        page._capture = capture
        page._track_network = track_network
        event_types: list[type] = []
        if capture:
            event_types += [runtime_proto.ConsoleAPICalled,
                            runtime_proto.ExceptionThrown]
        if track_network:
            event_types += [network_proto.RequestWillBeSent,
                            network_proto.LoadingFinished,
                            network_proto.LoadingFailed]
        if event_types:
            # subscribe BEFORE enabling the domains, or early events are lost
            stream = session.listen(*event_types, buffer_size=4096)
            page._streams.append(stream)
            page._tasks.append(asyncio.create_task(page._capture_pump(stream)))
        await session.execute(page_proto.enable())
        if capture:
            await session.execute(runtime_proto.enable())
        if track_network:
            await session.execute(network_proto.enable())
        return page

    # -- capture -------------------------------------------------------------

    async def _capture_pump(self, stream: EventStream) -> None:
        with suppress(Exception):  # session teardown ends the pump
            async for event in stream:
                if isinstance(event, runtime_proto.ConsoleAPICalled):
                    self.console.append(ConsoleMessage(
                        kind=str(event.type),
                        text=' '.join(_remote_text(a) for a in event.args)))
                elif isinstance(event, runtime_proto.ExceptionThrown):
                    self.js_errors.append(
                        JSError.from_details(event.exception_details))
                elif isinstance(event, network_proto.RequestWillBeSent):
                    self._inflight.add(str(event.request_id))
                    if (event.type is not None and str(event.type) == 'Document'
                            and event.frame_id is not None):
                        self._doc_requests[str(event.request_id)] = \
                            str(event.frame_id)
                    self._touch_network()
                elif isinstance(event, (network_proto.LoadingFinished,
                                        network_proto.LoadingFailed)):
                    request_id = str(event.request_id)
                    self._inflight.discard(request_id)
                    frame_id = self._doc_requests.pop(request_id, None)
                    if (frame_id is not None
                            and isinstance(event, network_proto.LoadingFailed)
                            and not event.canceled):
                        # a document's load failed — goto() checks this to turn
                        # a silently-committed chrome-error:// page into a
                        # NavigateError that carries the net error. Canceled
                        # loads (ERR_ABORTED: a replaced/interrupted navigation)
                        # are not failures.
                        self._doc_failures[frame_id] = str(event.error_text)
                    self._touch_network()

    def _touch_network(self) -> None:
        self._last_network_activity = asyncio.get_running_loop().time()

    # -- navigation ----------------------------------------------------------

    async def goto(
        self, url: str, *, wait: str = 'load', timeout: float | None = None
    ) -> None:
        '''Navigate and wait: 'load' (Page.loadEventFired), 'idle' (load +
        network quiet), or 'none' (return as soon as navigation starts).

        'idle' is for apps you control: a third-party-heavy public page
        (ads, analytics, long-polls) may NEVER go network-quiet — use 'load'
        there.'''
        if wait not in ('load', 'idle', 'none'):
            raise ValueError(f'wait must be load/idle/none, not {wait!r}')
        to = timeout or self.default_timeout
        frame_id = None
        try:
            async with asyncio.timeout(to):
                waiter = None
                if wait != 'none':
                    waiter = asyncio.create_task(
                        self.session.wait_for(page_proto.LoadEventFired))
                    await asyncio.sleep(0)
                try:
                    # clear BEFORE the browser can even receive the navigate:
                    # any document failure recorded after this belongs to it
                    # (clearing after would race the pump and could eat it)
                    self._doc_failures.clear()
                    result = await self.session.execute(page_proto.navigate(url=url))
                    frame_id = str(result[0])
                    error_text = result[2]
                    if error_text:
                        raise NavigateError(
                            f'navigation to {url!r} failed: {error_text}',
                            url=url, net_error=error_text)
                    if waiter is not None:
                        await waiter
                        waiter = None
                finally:
                    if waiter is not None:
                        waiter.cancel()
                if wait == 'idle':
                    await self.wait_for_network_idle()
        except TimeoutError:
            hint = (" — 'idle' may never arrive on third-party-heavy pages; "
                    "try wait='load'") if wait == 'idle' else ''
            raise TimeoutError(
                f'goto({url!r}) timed out after {to}s waiting for '
                f'{wait!r}{hint}') from None
        if wait != 'none' and frame_id is not None:
            # navigate can commit to chrome-error://chromewebdata/ WITHOUT
            # returning errorText (e.g. a server that accepts, then stalls) —
            # the load event fires on Chrome's error page and the failure
            # would pass silently. The capture pump recorded the document's
            # loadingFailed; two ticks let it drain events already queued.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            net_error = self._doc_failures.pop(frame_id, None)
            if net_error:
                raise NavigateError(
                    f'navigation to {url!r} failed: {net_error} '
                    '(the browser rendered its error page)',
                    url=url, net_error=net_error)

    async def wait_for_network_idle(
        self, *, idle_time: float = 0.5, timeout: float | None = None
    ) -> None:
        '''Wait until no request is in flight and none has been for idle_time.'''
        if not self._track_network:
            raise RuntimeError(
                'network-idle waiting needs track_network=True on Page.create()')

        async def quiet() -> None:
            loop = asyncio.get_running_loop()
            while True:
                if (not self._inflight
                        and loop.time() - self._last_network_activity >= idle_time):
                    return
                await asyncio.sleep(0.05)

        if timeout is None:
            await quiet()  # caller (e.g. goto) already holds a timeout
        else:
            async with asyncio.timeout(timeout):
                await quiet()

    @asynccontextmanager
    async def expect_navigation(
        self, *, wait: str = 'load', timeout: float | None = None
    ) -> typing.AsyncIterator[None]:
        '''Await a navigation triggered by code inside the ``async with`` body
        (e.g. a click that submits a form). Subscribes before the trigger, so
        the load event can't be missed::

            async with page.expect_navigation():
                await page.click("button[type=submit]")
        '''
        waiter = asyncio.create_task(
            self.session.wait_for(page_proto.FrameStoppedLoading
                                  if wait == 'idle' else
                                  page_proto.LoadEventFired))
        await asyncio.sleep(0)
        try:
            async with asyncio.timeout(timeout or self.default_timeout):
                yield
                await waiter
                if wait == 'idle':
                    await self.wait_for_network_idle()
        finally:
            if not waiter.done():
                waiter.cancel()

    # -- dialogs -------------------------------------------------------------

    async def handle_dialogs(
        self, *, accept: bool = True, prompt_text: str | None = None
    ) -> list[str]:
        '''Auto-handle JS dialogs (alert/confirm/prompt/beforeunload) as they
        open — otherwise they block the page. Returns a live list that each
        handled dialog's message is appended to. Enables the Page domain
        events. Call once, before the code that triggers dialogs.'''
        seen: list[str] = []
        stream = self.session.listen(page_proto.JavascriptDialogOpening,
                                     buffer_size=64)
        self._streams.append(stream)

        async def pump() -> None:
            with suppress(Exception):
                async for event in stream:
                    seen.append(event.message)
                    with suppress(Exception):
                        await self.session.execute(
                            page_proto.handle_java_script_dialog(
                                accept=accept, prompt_text=prompt_text))

        self._tasks.append(asyncio.create_task(pump()))
        return seen

    # -- scripting -----------------------------------------------------------

    async def evaluate(
        self,
        expression: str,
        *args: typing.Any,
        await_promise: bool = True,
        return_by_value: bool = True,
        user_gesture: bool = False,
        timeout: float | None = None,
    ) -> typing.Any:
        '''Evaluate JS and return its value; raises JSError on exceptions
        (including rejected promises, which are awaited by default).

        With positional ``*args``, ``expression`` must be a FUNCTION
        DECLARATION (``'(a, b) => a + b'``) and is called with the args via
        Runtime.callFunctionOn — the injection-safe way to pass values in
        (never interpolate them into the JS source). Args must be
        JSON-serializable, or ``Element`` handles (which arrive as live DOM
        nodes).'''
        async with asyncio.timeout(timeout or self.default_timeout):
            if args:
                return await _call_function(
                    self.session, expression, args,
                    await_promise=await_promise,
                    return_by_value=return_by_value,
                    user_gesture=user_gesture)
            result, details = await self.session.execute(runtime_proto.evaluate(
                expression=expression,
                return_by_value=return_by_value,
                await_promise=await_promise,
                user_gesture=True if user_gesture else None,
            ))
        if details is not None:
            raise JSError.from_details(details)
        return result.value if return_by_value else result

    async def wait_for_function(
        self, expression: str, *, timeout: float | None = None, poll: float = 0.05
    ) -> None:
        '''Poll until the expression is truthy.'''
        async with asyncio.timeout(timeout or self.default_timeout):
            while True:
                if await self.evaluate(f'!!({expression})'):
                    return
                await asyncio.sleep(poll)

    # wait_for_selector was REMOVED in 0.7.0 — it was existence-only, i.e.
    # wait_for(selector). wait_for() adds the rendered axis (visible=) and
    # every other should() condition; see ElementQueries.wait_for.

    # query/query_count/query_all/wait_for and live()/get_by_* are inherited —
    # ElementQueries (element.py) and LiveFactories (live.py), shared with Frame.

    # -- cross-origin frames (M10 Phase 1) -----------------------------------

    async def _enable_frame_attach(self) -> None:
        '''Arm flat auto-attach (once) so cross-origin iframes get their own CDP
        sessions. ``wait_for_debugger=False`` — subframes attach *live*, never
        paused, so there are no resume round-trips and no deadlock. Only called
        from frame(), so pages that never touch OOPIFs are unaffected.'''
        if self._frame_attach_enabled:
            return
        self._frame_attach_enabled = True
        conn = self.session.connection

        async def _arm(session):  # propagate auto-attach into nested OOPIFs
            info = session.target_info
            if info is not None and 'iframe' in (info.type or ''):
                await session.set_auto_attach(wait_for_debugger=False)

        conn.add_session_hook(_arm)
        await self.session.set_auto_attach(wait_for_debugger=False)
        await asyncio.sleep(0.05)  # let already-loaded OOPIFs attach (one-time)

    async def frame(
        self,
        selector: str | None = None,
        *,
        url: str | None = None,
        timeout: float | None = None,
    ):
        '''Attach to a cross-origin (out-of-process) iframe and return a
        :class:`~purecdp.testing.frame.Frame` scoped to it — same
        query/evaluate/snapshot/act surface as the page, but inside the frame.

        Identify it by ``selector`` (a CSS selector for the <iframe> — the
        robust choice) or ``url`` (fnmatch against the frame's URL). Raises
        :class:`~purecdp.testing.frame.FrameNotFound` when nothing matches or the
        frame never attaches — it must be genuinely cross-origin
        (out-of-process); same-origin iframes stay reachable from the page's own
        JS and don't need this. Nest with ``frame.frame(...)``. First use arms
        flat auto-attach on this page (see _enable_frame_attach); pages that
        never call frame() pay nothing.'''
        return await _agent.resolve_frame(self, self, selector, url, timeout)

    # -- agent surface (M9) --------------------------------------------------

    def add_auto_dismiss(self, *selectors: str) -> None:
        '''Register CSS selectors for overlays to click away automatically
        while :meth:`act` (and any ``stable=True`` action) waits for its target
        — cookie/consent banners, "continue" interstitials. The selector should
        point at the *dismiss control* (the close/accept button); it's clicked
        whenever it's visible during an actionability wait, then the real action
        proceeds. Covers the common banner case; for arbitrary logic, drive the
        dismissal yourself before acting.'''
        self._dismissers.extend(selectors)

    async def snapshot(
        self, *, depth: int | None = None, cross_frame: bool = False,
        scope: str | None = None,
    ) -> Snapshot:
        '''A compact, ref-tagged accessibility outline for an LLM to act on —
        ``str(snapshot)`` is the text to show the model, and every actionable
        node carries a ref (``e1``, ``e2``, …). The refs are session-scoped and
        replaced on each call; resolve them with :meth:`act` or
        :meth:`element_for_ref`. Password fields are masked. ``depth`` limits
        how deep the tree is fetched.

        ``cross_frame=True`` splices in cross-origin (out-of-process) iframes,
        so a consent/payment iframe's controls appear in the same outline and
        ``act(ref)`` reaches into them transparently. It arms auto-attach on
        this page (see frame()); the default (False) is the plain single-frame
        snapshot with zero extra cost.

        ``scope='dialog'`` narrows to the DEEPEST open ``dialog``/
        ``alertdialog`` subtree — "what's fillable/clickable in the modal
        that's actually open". No dialog open -> the full page, with a
        leading note line saying so.

        Prefer this over feeding raw HTML/screenshots to a model: it's small,
        stable, and names controls the way a human sees them.'''
        if cross_frame:
            if scope is not None:
                raise ValueError(
                    'scope= is not supported with cross_frame=True yet')
            return await _agent.stitched_snapshot(self, depth=depth)
        return await _agent.snapshot(self, depth=depth, scope=scope)

    async def element_for_ref(self, ref: str) -> Element:
        '''Resolve a snapshot ref (``e3``) to a live :class:`Element` — the
        escape hatch to the full element API. Raises LookupError for an unknown
        ref (call :meth:`snapshot` first) and CDPCommandError if the node is stale
        (the DOM changed since the snapshot; re-snapshot).'''
        return await _agent.element_for_ref(self, ref)

    async def act(
        self,
        ref: str,
        action: str = 'click',
        *,
        text: str | None = None,
        values: typing.Sequence[str] | None = None,
        stable: bool = True,
        timeout: float | None = None,
    ) -> typing.Any:
        '''Perform one action on a snapshot ref — the verb an agent calls.

        ``action`` is one of: ``click`` (trusted mouse press/release),
        ``hover``, ``focus``, ``fill`` (React-safe value write, needs ``text``),
        ``type`` (focus + real keystrokes, needs ``text``), ``select`` (choose
        ``values`` in a <select>), ``scroll`` (scroll into view). Returns the
        :class:`Element` (or, for ``select``, the matched values).

        ``stable=True`` (default) waits for the target to be actionable
        (visible, enabled, settled, un-obscured) before an interaction —
        the safety gate a model needs on real, animated pages; raises
        :class:`~purecdp.testing.element.ActionabilityError` on timeout. Pass
        ``stable=False`` to fire immediately.'''
        return await _agent.act(self, ref, action, text=text, values=values,
                                stable=stable, timeout=timeout)

    async def click(
        self,
        selector: str,
        *,
        containing: str | None = None,
        index: int = 0,
        timeout: float | None = None,
    ) -> None:
        '''Wait for the element and click it (synthetic, wrapper-resolving —
        see Element.click; use query().mouse_click() for trusted events).'''
        element = await self.query(selector, containing=containing,
                                   index=index, timeout=timeout)
        await element.click()

    async def text(
        self,
        selector: str,
        *,
        containing: str | None = None,
        index: int = 0,
        timeout: float | None = None,
    ) -> str:
        element = await self.query(selector, containing=containing,
                                   index=index, timeout=timeout)
        return await element.text()

    async def set_value(
        self,
        selector: str,
        value: str,
        *,
        containing: str | None = None,
        index: int = 0,
        timeout: float | None = None,
    ) -> None:
        '''React-safe input write (see Element.set_value). ``index=-1``
        targets the newest copy when stale widgets linger in the DOM.'''
        element = await self.query(selector, containing=containing,
                                   index=index, timeout=timeout)
        await element.set_value(value)

    # -- keyboard ------------------------------------------------------------

    async def press(self, key: str) -> None:
        '''A real key press (Input domain down+up) on the focused element —
        the way to submit forms; synthetic setters never type Enter.'''
        try:
            code, text_, vk = KEYS[key]
        except KeyError:
            raise ValueError(
                f'unknown key {key!r}; known: {', '.join(KEYS)}') from None
        await self.session.execute(input_proto.dispatch_key_event(
            type='keyDown', key=key, code=code, text=text_,
            windows_virtual_key_code=vk, native_virtual_key_code=vk))
        await self.session.execute(input_proto.dispatch_key_event(
            type='keyUp', key=key, code=code,
            windows_virtual_key_code=vk, native_virtual_key_code=vk))

    async def insert_text(self, text: str) -> None:
        '''Type text into the focused element (Input.insertText — fires real
        input events, unlike JS value writes).'''
        await self.session.execute(input_proto.insert_text(text))

    # -- page-level scripting ------------------------------------------------

    async def add_init_script(self, source: str) -> None:
        '''Run ``source`` in every new document before the app's own scripts
        (Page.addScriptToEvaluateOnNewDocument) — the way to seed
        sessionStorage/localStorage/globals before a SPA boots. Call before
        goto().'''
        await self.session.execute(
            page_proto.add_script_to_evaluate_on_new_document(source=source))

    async def seed_session_storage(self, key: str, value: str) -> None:
        '''Plant a sessionStorage entry into every new document before the
        app boots (the standard way to fabricate an auth/OIDC session).
        Call before goto(); needs a real origin (opaque origins like data:
        URLs have no storage).'''
        await self.add_init_script(
            f'sessionStorage.setItem({json.dumps(key)}, {json.dumps(value)});')

    async def seed_local_storage(self, key: str, value: str) -> None:
        '''localStorage counterpart of seed_session_storage().'''
        await self.add_init_script(
            f'localStorage.setItem({json.dumps(key)}, {json.dumps(value)});')

    async def mock_api(
        self,
        mocks: dict[str, typing.Any],
        *,
        pattern: str = '*',
    ) -> None:
        '''Mock APIs at the browser edge from a path-keyed dict; anything
        unmatched passes through to the real network.

        Keys are URL *paths*: exact matches, or prefixes when they end with
        ``/``. Values are the JSON payload to fulfill with, or a
        ``callable(path) -> payload`` for parameterised routes (return None
        to pass that request through). Stateful callables make replay mocks:
        ``{'/api/query': lambda _: next(turns)}``.
        '''

        def payload_for(path: str) -> typing.Any:
            for key, payload in mocks.items():
                hit = path.startswith(key) if key.endswith('/') else path == key
                if hit:
                    return payload(path) if callable(payload) else payload
            return None

        async def handler(request: InterceptedRequest) -> None:
            payload = payload_for(urlparse(request.url).path)
            if payload is not None:
                await request.fulfill(json=payload)
            # unhandled -> the route pump continues the request untouched

        await self.route(pattern, handler)

    async def relay(
        self,
        prefix: str,
        target_base: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        '''Forward requests whose URL *path* starts with ``prefix`` to
        ``target_base`` (``scheme://host[:port]``) and answer the page with
        what the target returned — the dev proxy the frontend expects,
        without running one. The sibling of ``mock_api``: stub or forward::

            await page.relay('/api/', 'http://127.0.0.1:8000')

        ``headers`` are added to every forwarded request (an Authorization
        for the real backend); ``timeout`` bounds each upstream call.
        Transparent otherwise — method, query, body, ``Cookie``/``Origin``
        go through unchanged, ``Set-Cookie`` and 4xx/5xx come back as-is;
        an unreachable target answers 502 with the reason in the body.
        Routes are tried in registration order until one handles the
        request, so a ``mock_api`` for one path and a relay for the rest
        compose either way round. Limits: whole-body (no streaming, so no
        SSE through a relay); redirects are followed upstream; meant for
        dev/test origins, not a general proxy.'''
        def under_prefix(url: str) -> bool:
            return urlparse(url).path.startswith(prefix)

        async def handler(request: InterceptedRequest) -> None:
            await forward(request, target_base, headers=headers,
                          timeout=timeout)

        await self.route(under_prefix, handler)

    def record(
        self,
        *,
        needle: str = '',
        exclude: str | None = None,
        predicate: typing.Callable[[str], bool] | None = None,
        record_preflights: bool = False,
        default_timeout: float | None = None,
        bodies: bool = True,
        redact: typing.Callable[[str], bool] | None = None,
    ) -> NetworkRecorder:
        '''Start recording matching HTTP exchanges (see NetworkRecorder).
        Subscribe before triggering; cleaned up with the page.

        ``default_timeout`` is the recorder's own wait default for
        ``expect()`` / ``wait_for_next()`` — an endpoint's latency profile has
        nothing to do with the page's DOM default, so declare it once where
        the endpoint is named (an LLM route might need 120, a preview
        sub-second) instead of remembering ``timeout=`` at every call site.
        Falls back to the page default when not set.

        ``bodies=False`` records metadata only; ``redact=`` makes matching
        exchanges fully private (no bodies, credential headers masked) — the
        secrets are withheld at capture and never enter the process. Keeps
        traffic visibility on e.g. a card-entry flow without the PAN ever
        landing in an artifact.'''
        recorder = NetworkRecorder(self, needle=needle, exclude=exclude,
                                   predicate=predicate,
                                   record_preflights=record_preflights,
                                   default_timeout=default_timeout,
                                   bodies=bodies, redact=redact)
        stream = self.session.listen(
            network_proto.RequestWillBeSent,
            network_proto.ResponseReceived,
            network_proto.RequestWillBeSentExtraInfo,
            network_proto.ResponseReceivedExtraInfo,
            network_proto.LoadingFinished,
            network_proto.LoadingFailed,
            buffer_size=4096,
        )
        self._streams.append(stream)
        self._tasks.append(asyncio.create_task(recorder._pump(stream)))
        self._recorders.append(recorder)
        return recorder

    @asynccontextmanager
    async def expect_download(
        self, *, timeout: float | None = None
    ) -> typing.AsyncIterator[_DownloadExpectation]:
        '''Capture a file download triggered inside the ``async with`` body::

            async with page.expect_download() as info:
                await page.act("e5", "click")     # the trigger
            download = await info.value           # a Download
            data = await download.read()          # bytes on disk
            await download.save_as("report.csv")  # or keep it under a name

        Arms the browser to allow downloads into a temp directory and emit
        download events (``Browser.setDownloadBehavior``). This enables
        downloads for the whole **connection** (not just this page), so use it
        with a browser/context you own — the norm in tests. Subscribes before
        the body runs, so a download that starts immediately is not missed.
        Raises :class:`DownloadError` if the download is canceled or no
        completion arrives within ``timeout`` (the page default).'''
        conn = self.session.connection
        directory = tempfile.mkdtemp(prefix='purecdp-download-')
        # scope the behavior to THIS page's browser context — an isolated
        # context (the CDPTestCase default) doesn't inherit the default one's,
        # so a browser-wide call would leave the download denied → "canceled".
        info = self.session.target_info
        context_id = info.browser_context_id if info is not None else None
        await conn.execute(browser_proto.set_download_behavior(
            behavior='allowAndName', download_path=directory,
            browser_context_id=context_id, events_enabled=True))
        stream = conn.listen(browser_proto.DownloadWillBegin,
                             browser_proto.DownloadProgress, buffer_size=256)
        self._streams.append(stream)

        async def collect() -> Download:
            info: typing.Any = None
            guid: str | None = None
            try:
                async with asyncio.timeout(timeout or self.default_timeout):
                    async for event in stream:
                        if isinstance(event, browser_proto.DownloadWillBegin):
                            info, guid = event, event.guid
                        elif (isinstance(event, browser_proto.DownloadProgress)
                              and event.guid == guid):
                            if event.state == 'completed':
                                path = (event.file_path
                                        or os.path.join(directory, guid))
                                return Download(
                                    url=info.url,
                                    suggested_filename=info.suggested_filename,
                                    guid=guid, path=path)
                            if event.state == 'canceled':
                                raise DownloadError(
                                    'download canceled: '
                                    f'{info.url if info else guid}')
            except TimeoutError:
                raise DownloadError(
                    'no download completed within '
                    f'{timeout or self.default_timeout}s') from None
            finally:
                stream.close()
            raise DownloadError('download stream ended before completion')

        task = asyncio.create_task(collect())
        self._tasks.append(task)
        yield _DownloadExpectation(task)

    async def title(self) -> str:
        return await self.evaluate('document.title')

    async def content(self) -> str:
        return await self.evaluate('document.documentElement.outerHTML')

    async def screenshot(
        self,
        path: str | None = None,
        *,
        format: str = 'png',
        quality: int | None = None,
        full_page: bool = False,
    ) -> bytes:
        '''Capture a screenshot; returns the bytes, optionally writing path.'''
        data = await self.session.execute(page_proto.capture_screenshot(
            format=format, quality=quality,
            capture_beyond_viewport=True if full_page else None))
        raw = _b64.b64decode(data)
        if path is not None:
            with open(path, 'wb') as f:
                f.write(raw)
        return raw

    async def pdf(self, path: str | None = None, **options: typing.Any) -> bytes:
        '''Render the page to PDF (headless Chromium only). Extra keyword
        options pass straight to Page.printToPDF (landscape, print_background,
        paper_width, ...). Returns the bytes, optionally writing path.'''
        data, _stream = await self.session.execute(
            page_proto.print_to_pdf(**options))
        raw = _b64.b64decode(data)
        if path is not None:
            with open(path, 'wb') as f:
                f.write(raw)
        return raw

    # -- emulation -----------------------------------------------------------

    async def set_viewport(
        self,
        width: int,
        height: int,
        *,
        device_scale_factor: float = 1.0,
        mobile: bool = False,
    ) -> None:
        '''Override the layout viewport (Emulation.setDeviceMetricsOverride).'''
        await self.session.execute(emulation_proto.set_device_metrics_override(
            width=width, height=height,
            device_scale_factor=device_scale_factor, mobile=mobile))

    async def set_user_agent(
        self,
        user_agent: str,
        *,
        accept_language: str | None = None,
        platform: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        '''Override the user agent (Emulation.setUserAgentOverride). ``metadata``
        is a User-Agent Client Hints dict — set it, or the UA string and the
        UA-CH headers disagree, which is itself a fingerprint tell.

        Coherence warning: the browser's **TLS/HTTP2 fingerprint** (JA3/JA4,
        HTTP-2 SETTINGS) reveals its *real* engine version and is the one thing
        CDP cannot alter. Claiming a different Chrome version here than the
        binary actually is makes the UA and the network fingerprint disagree —
        a strong bot signal. Keep the version matching the real browser (use
        ``strip_headless_ua()`` to drop only the ``Headless`` token); to truly
        present a different fingerprint you need an external TLS-rewriting proxy,
        not CDP.'''
        ua_metadata = (emulation_proto.UserAgentMetadata.from_json(metadata)
                       if metadata is not None else None)
        await self.session.execute(emulation_proto.set_user_agent_override(
            user_agent=user_agent, accept_language=accept_language,
            platform=platform, user_agent_metadata=ua_metadata))

    async def strip_headless_ua(self) -> str:
        '''Rewrite ``HeadlessChrome/`` → ``Chrome/`` in the user agent (the
        old-headless giveaway). Returns the applied UA. CDP-hygiene helper.

        Prefer ``launch(stealth=True)`` (``--headless=new``), whose UA carries no
        ``Headless`` token to begin with: this override applies via
        ``setUserAgentOverride`` and a worker created before it lands can still
        report the headless UA in its own scope (``workerScope.userAgent``, a
        signal CreepJS checks), whereas the new-headless binary never emits it.
        Version-preserving in both cases — only the ``Headless`` token changes.'''
        current = await self.evaluate('navigator.userAgent')
        fixed = current.replace('HeadlessChrome/', 'Chrome/')
        await self.set_user_agent(fixed)
        return fixed

    async def emulate_media(
        self,
        *,
        media: str | None = None,
        color_scheme: str | None = None,
        reduced_motion: str | None = None,
    ) -> None:
        '''Emulate CSS media (Emulation.setEmulatedMedia): media type (e.g.
        'print', or '' to reset), and/or prefers-color-scheme
        ('light'/'dark') and prefers-reduced-motion ('reduce'/'no-preference').'''
        features = []
        if color_scheme is not None:
            features.append(emulation_proto.MediaFeature(
                name='prefers-color-scheme', value=color_scheme))
        if reduced_motion is not None:
            features.append(emulation_proto.MediaFeature(
                name='prefers-reduced-motion', value=reduced_motion))
        await self.session.execute(emulation_proto.set_emulated_media(
            media=media, features=features or None))

    async def set_geolocation(
        self, latitude: float, longitude: float, *, accuracy: float = 1.0
    ) -> None:
        '''Override geolocation (Emulation.setGeolocationOverride).'''
        await self.session.execute(emulation_proto.set_geolocation_override(
            latitude=latitude, longitude=longitude, accuracy=accuracy))

    async def set_timezone(self, timezone_id: str) -> None:
        '''Override the timezone, e.g. 'Europe/Paris'
        (Emulation.setTimezoneOverride).'''
        await self.session.execute(
            emulation_proto.set_timezone_override(timezone_id=timezone_id))

    async def set_extra_headers(self, headers: dict[str, str]) -> None:
        '''Send these extra HTTP headers on every request
        (Network.setExtraHTTPHeaders); enables the Network domain.'''
        if not self._track_network:
            await self.session.execute(network_proto.enable())
            self._track_network = True
        await self.session.execute(network_proto.set_extra_http_headers(
            headers=network_proto.Headers(headers)))

    # -- cookies & storage ---------------------------------------------------

    async def cookies(self, urls: list[str] | None = None) -> list[dict]:
        '''Return cookies (Network.getCookies) as CDP dicts (name, value,
        domain, path, ...).'''
        got = await self.session.execute(network_proto.get_cookies(urls=urls))
        return [c.to_json() for c in got]

    async def set_cookies(self, cookies: list[dict]) -> None:
        '''Set cookies from CDP-shaped dicts (name + value required, plus url
        or domain/path; optional secure, httpOnly, sameSite, expires).'''
        params = [network_proto.CookieParam.from_json(c) for c in cookies]
        await self.session.execute(network_proto.set_cookies(cookies=params))

    async def clear_cookies(self) -> None:
        '''Clear all browser cookies (Network.clearBrowserCookies).'''
        await self.session.execute(network_proto.clear_browser_cookies())

    async def storage_state(self) -> dict:
        '''Snapshot cookies + localStorage for the current origin, in a shape
        set_storage_state() restores — reuse a logged-in session across runs
        without re-authenticating.'''
        cookies = await self.cookies()
        local = await self.evaluate(
            'JSON.stringify(Object.entries(localStorage))')
        return {'cookies': cookies, 'local_storage': json.loads(local)}

    async def set_storage_state(self, state: dict) -> None:
        '''Restore a storage_state() snapshot. Cookies apply immediately;
        localStorage is set on the current document, so call after navigating
        to (or seeding) the matching origin.'''
        if state.get('cookies'):
            await self.set_cookies(state['cookies'])
        entries = state.get('local_storage') or []
        if entries:
            await self.evaluate(
                f'for (const [k, v] of {json.dumps(entries)}) '
                'localStorage.setItem(k, v)')

    # -- content injection ---------------------------------------------------

    async def set_content(self, html: str) -> None:
        '''Replace the document's HTML (Page.setDocumentContent on the main
        frame).'''
        tree = await self.session.execute(page_proto.get_frame_tree())
        await self.session.execute(page_proto.set_document_content(
            frame_id=tree.frame.id, html=html))

    async def add_script_tag(
        self, *, url: str | None = None, content: str | None = None
    ) -> None:
        '''Inject a <script> (by src url or inline content) and wait for it to
        load; raises JSError on load failure.'''
        await self.evaluate(_ADD_TAG_JS % ('script', json.dumps(url),
                                           json.dumps(content)))

    async def add_style_tag(
        self, *, url: str | None = None, content: str | None = None
    ) -> None:
        '''Inject a <link rel=stylesheet> (url) or <style> (content).'''
        await self.evaluate(_ADD_TAG_JS % ('style', json.dumps(url),
                                           json.dumps(content)))

    # -- interception --------------------------------------------------------

    async def route(self, pattern: str | typing.Callable[[str], bool],
                    handler: Handler) -> None:
        '''Intercept requests whose URL matches ``pattern`` — an fnmatch glob
        (note: '*' crosses '/') or a ``callable(url) -> bool`` predicate, the
        same form ``record()`` takes. Matching routes run in registration
        order until one handles the request (fulfill/continue_/abort) — a
        handler that returns without handling passes it on. Unmatched or
        unhandled requests are continued untouched.'''
        self._routes.append(Route(pattern, handler))
        if not self._intercepting:
            self._intercepting = True
            stream = self.session.listen(fetch_proto.RequestPaused,
                                         buffer_size=4096)
            self._streams.append(stream)
            self._tasks.append(asyncio.create_task(self._route_pump(stream)))
            await self.session.execute(fetch_proto.enable())

    def clear_routes(self) -> None:
        '''Drop all routes (interception stays enabled; requests continue).'''
        self._routes.clear()

    async def _route_pump(self, stream: EventStream) -> None:
        with suppress(Exception):  # session teardown ends the pump
            async for event in stream:
                request = InterceptedRequest(self.session, event)
                routes = matching_routes(self._routes, request.url)
                try:
                    if routes and self.auto_preflight and request.is_cors_preflight:
                        await request.respond_preflight()
                    for route in routes:  # first handler that HANDLES wins
                        if request.handled:
                            break
                        await route.handler(request)
                    if not request.handled:
                        await request.continue_()
                except CDPClosedError:
                    return  # teardown mid-handle: not the handler's fault
                except Exception as exc:  # a broken handler must not wedge the page
                    warnings.warn(
                        f'route handler failed for {request.url}: {exc!r}',
                        RuntimeWarning, stacklevel=2)
                    if not request.handled:
                        with suppress(Exception):
                            await request.continue_()

    # -- lifecycle -----------------------------------------------------------

    async def stop(self) -> None:
        '''Stop captures and interception pumps; leaves the page OPEN.
        (Renamed from ``aclose`` — that name read as "close the page",
        which this never did; use :meth:`close` for that.)'''
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(BaseException):
                await task
        self._tasks.clear()
        for stream in self._streams:
            stream.close()
        self._streams.clear()

    async def close(self) -> None:
        '''aclose() and close the underlying page target.'''
        await self.stop()
        with suppress(Exception):
            await close_page(self.session.connection, self.session)

    async def __aenter__(self) -> Page:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


@asynccontextmanager
async def launched_page(
    browser_path: str | None = None,
    *,
    headless: bool = True,
    pipe: bool = False,
    extra_args: typing.Sequence[str] = (),
    default_timeout: float = 10.0,
) -> typing.AsyncIterator[tuple[Browser, Page]]:
    '''The one-liner for scripts and drives: launch a browser, yield
    ``(browser, page)`` with the Page fully armed (domains enabled,
    console/network capture running), and always tear everything down.

    For test suites prefer CDPTestCase or the pytest fixtures instead.
    '''
    browser = await launch(browser_path, headless=headless, pipe=pipe,
                           extra_args=extra_args)
    try:
        session = await browser.new_session()
        page = await Page.create(session, default_timeout=default_timeout)
        yield browser, page
    finally:
        await browser.aclose()
