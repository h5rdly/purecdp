'''Element handles: hold a DOM node (a Runtime RemoteObject) and act on it.

Why handles matter in real-app tests: apps like React keep *stale* copies of
widgets in the DOM (earlier chat turns, virtual lists), so a selector-based
write can land in a dead control. Query once — with text filtering and
indexing — hold the element, and drive that exact node.

Handles are invalidated by navigation; re-query after goto().
'''

from __future__ import annotations

import asyncio
import json
import typing
from contextlib import suppress


from ..protocol import dom as dom_proto
from ..protocol import input as input_proto
from ..protocol import page as page_proto
from ..protocol import runtime as runtime_proto
from .. import _b64

if typing.TYPE_CHECKING:
    from .page import Page

#: JS that resolves a wrapper to its real control: component libraries often
#: put the click target on a wrapper whose own .click() does nothing, with a
#: visually-hidden <input> inside it (or inside its label).
_RESOLVE_AND_CLICK = (
    '(el) => { const i = el.matches('
    "'input,button,a,select,textarea,[role=button],[role=radio],[role=checkbox]')"
    " ? el : el.querySelector('input') || el.closest('label')?.querySelector('input');"
    ' (i || el).click(); }'
)

#: React patches value setters; go through the native prototype setter, then
#: fire bubbling input+change so controlled components commit either way.
_SET_VALUE = (
    '(el, value) => {'
    ' const proto = el instanceof HTMLTextAreaElement'
    '   ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;'
    " Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);"
    " el.dispatchEvent(new Event('input', { bubbles: true }));"
    " el.dispatchEvent(new Event('change', { bubbles: true })); }"
)

#: In-browser actionability poll (Playwright's contract, one round-trip): loop
#: until the element is connected, has a visible box, is enabled, has stopped
#: moving (bounding box unchanged across animation frames), and is the top
#: element at its own centre — or the deadline passes. Runs the whole wait in
#: the page so a flaky control costs one CDP call, not one per frame.
_ACTIONABLE_JS = '''
async (el, opts) => {
  el.scrollIntoView({block: 'center', inline: 'center'});
  const deadline = performance.now() + opts.timeout;
  const raf = () => new Promise(r => requestAnimationFrame(() => r()));
  const box = () => { const r = el.getBoundingClientRect();
    return {x: r.x, y: r.y, w: r.width, h: r.height}; };
  const same = (a, b) => a && b &&
    Math.abs(a.x - b.x) < 0.5 && Math.abs(a.y - b.y) < 0.5 &&
    Math.abs(a.w - b.w) < 0.5 && Math.abs(a.h - b.h) < 0.5;
  let last = null, reason = 'timeout';
  while (performance.now() < deadline) {
    for (const sel of (opts.dismiss || [])) {
      const d = document.querySelector(sel);
      if (d && d.getBoundingClientRect().width > 0) d.click();
    }
    if (!el.isConnected) { reason = 'detached'; last = null; await raf(); continue; }
    const b = box();
    if (opts.visible && (b.w <= 0 || b.h <= 0)) {
      reason = 'not visible'; last = b; await raf(); continue; }
    if (opts.enabled && (el.disabled || el.getAttribute('aria-disabled') === 'true')) {
      reason = 'disabled'; last = b; await raf(); continue; }
    if (opts.stable && !same(b, last)) { last = b; await raf(); continue; }
    if (opts.hit) {
      const top = document.elementFromPoint(b.x + b.w / 2, b.y + b.h / 2);
      if (!top || !(top === el || el.contains(top))) {
        reason = 'obscured'; last = b; await raf(); continue; }
    }
    return {ok: true};
  }
  return {ok: false, reason};
}
'''.strip()


class ActionabilityError(Exception):
    '''A trusted action's target never became actionable (connected, visible,
    enabled, stable, and un-obscured) within the timeout. ``reason`` names the
    check that was still failing when time ran out.'''

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


class Element:
    '''A held DOM node. Created by Page.query()/query_all()/Element.query().'''

    def __init__(self, page: Page, object_id: str):
        self._page = page
        self._object_id = object_id

    def __repr__(self) -> str:
        return f'Element({self._object_id!r})'

    async def eval(self, function: str, *args: typing.Any) -> typing.Any:
        '''Call ``function`` (JS, e.g. ``'(el) => el.textContent'``) with this
        element as its first argument, plus JSON-serializable *args.'''
        from .page import JSError  # local import: page imports this module

        call_args = [runtime_proto.CallArgument(
            object_id=runtime_proto.RemoteObjectId(self._object_id))]
        call_args += [runtime_proto.CallArgument(value=a) for a in args]
        result, details = await self._page.session.execute(
            runtime_proto.call_function_on(
                function_declaration=(
                    f'function(...args) {{ return ({function})(...args); }}'),
                object_id=runtime_proto.RemoteObjectId(self._object_id),
                arguments=call_args,
                return_by_value=True,
                await_promise=True,
            ))
        if details is not None:
            raise JSError.from_details(details)
        return result.value

    async def text(self) -> str:
        return (await self.eval('(el) => el.textContent')) or ''

    async def click(self) -> None:
        '''Synthetic click, resolving wrappers to their real control (see
        _RESOLVE_AND_CLICK). Use mouse_click() when the app needs a trusted
        event.'''
        await self.eval(_RESOLVE_AND_CLICK)

    async def wait_actionable(
        self,
        *,
        visible: bool = True,
        stable: bool = True,
        enabled: bool = True,
        hit: bool = True,
        timeout: float | None = None,
    ) -> None:
        '''Block until this node is *actionable* — connected, has a visible box,
        enabled, has stopped moving (bounding box steady across animation
        frames), and is the top element at its own centre (not covered) — or
        raise :class:`ActionabilityError`. The Playwright-style safety gate that
        trusted input needs on animated / late-mounting pages. The whole wait
        runs in the browser (one CDP round-trip). Pass ``hit=False`` for actions
        that don't dispatch a pointer (a value write, focus). ``timeout``
        defaults to the page's. Any selectors registered via
        :meth:`Page.add_auto_dismiss` are clicked away while we wait (cookie /
        consent banners that would otherwise block the action).'''
        to = self._page.default_timeout if timeout is None else timeout
        res = await self.eval(_ACTIONABLE_JS, {
            'timeout': to * 1000, 'visible': visible, 'stable': stable,
            'enabled': enabled, 'hit': hit,
            'dismiss': list(self._page._dismissers)})
        # a real page always returns {ok, reason}; only fail on an explicit
        # ok:false so an odd/None result can't spuriously block an action
        if isinstance(res, dict) and not res.get('ok'):
            reason = res.get('reason', 'timeout')
            raise ActionabilityError(
                f'element not actionable within {to}s: {reason}', reason)

    async def mouse_click(
        self, *, stable: bool = False, timeout: float | None = None
    ) -> None:
        '''A real (trusted) mouse press+release at the element's center, via
        the Input domain — for widgets that ignore synthetic .click().

        ``stable=True`` first waits until the element is actionable (see
        :meth:`wait_actionable`) — the safety gate for animated / late-mounting
        UIs. Off by default to keep the fast path fast; the agent
        :meth:`Page.act` turns it on.'''
        if stable:
            await self.wait_actionable(timeout=timeout)
        center = await self.eval(
            "(el) => { el.scrollIntoView({block: 'center'});"
            ' const r = el.getBoundingClientRect();'
            ' return {x: r.x + r.width / 2, y: r.y + r.height / 2}; }')
        x, y = center['x'], center['y']
        for kind in ('mousePressed', 'mouseReleased'):
            await self._page.session.execute(input_proto.dispatch_mouse_event(
                type=kind, x=x, y=y,
                button=input_proto.MouseButton.LEFT, click_count=1))

    async def human_click(self, **move_kwargs: typing.Any) -> None:
        '''Move the cursor here along a human, curved path and click — trusted
        Input events with realistic motion (M8 Track B). Uses page.cursor, so
        position carries over between actions.'''
        await self._page.cursor.click(self, **move_kwargs)

    async def set_value(
        self, value: str, *, stable: bool = False, timeout: float | None = None
    ) -> None:
        '''React-safe value write on this exact node (native setter + input
        and change events). Prefer this over selector-based writes whenever
        several copies of a widget can be in the DOM.

        ``stable=True`` first waits for the field to be visible, enabled, and
        settled (no pointer hit-test needed for a value write).'''
        if stable:
            await self.wait_actionable(hit=False, timeout=timeout)
        await self.eval(_SET_VALUE, value)

    async def focus(self) -> None:
        await self.eval('(el) => el.focus()')

    async def type(
        self, text: str, *, stable: bool = False, timeout: float | None = None
    ) -> None:
        '''Focus this element and type with real input events
        (Input.insertText) — what forms and controlled inputs expect from a
        user; follow with ``page.press('Enter')`` to submit.

        ``stable=True`` first waits for the field to be visible, enabled, and
        settled before focusing.'''
        if stable:
            await self.wait_actionable(hit=False, timeout=timeout)
        await self.focus()
        await self._page.session.execute(input_proto.insert_text(text))

    async def scroll_into_view(self) -> None:
        await self.eval(
            "(el) => el.scrollIntoView({block: 'center', inline: 'center'})")

    async def frame_id(self) -> str | None:
        '''The frameId this element *owns* — set for <iframe>/<frame> host
        nodes, else None. Used by Page.frame() to correlate an iframe to its
        out-of-process CDP session.'''
        node = await self._page.session.execute(dom_proto.describe_node(
            object_id=runtime_proto.RemoteObjectId(self._object_id)))
        return str(node.frame_id) if node.frame_id is not None else None

    async def bounding_box(self) -> dict | None:
        '''The element's viewport rect as {x, y, width, height} (from
        getBoundingClientRect), or None if it has no layout box.'''
        box = await self.eval(
            '(el) => { const r = el.getBoundingClientRect();'
            ' return r.width || r.height ?'
            ' {x: r.x, y: r.y, width: r.width, height: r.height} : null; }')
        return box

    async def _center(self) -> dict:
        await self.scroll_into_view()
        box = await self.bounding_box()
        if box is None:
            raise LookupError('element has no visible box (not rendered?)')
        return {'x': box['x'] + box['width'] / 2,
                'y': box['y'] + box['height'] / 2}

    async def hover(self) -> None:
        '''Move the mouse to the element's center (Input.dispatchMouseEvent).'''
        center = await self._center()
        await self._page.session.execute(input_proto.dispatch_mouse_event(
            type='mouseMoved', x=center['x'], y=center['y']))

    async def screenshot(self, path: str | None = None, *, format: str = 'png'
                         ) -> bytes:
        '''Screenshot just this element (clipped to its box). Returns bytes,
        optionally writing path.'''
        await self.scroll_into_view()
        box = await self.bounding_box()
        if box is None:
            raise LookupError('element has no visible box to screenshot')
        data = await self._page.session.execute(
            page_proto.capture_screenshot(
                format=format,
                clip=page_proto.Viewport(
                    x=box['x'], y=box['y'], width=box['width'],
                    height=box['height'], scale=1),
                capture_beyond_viewport=True))
        raw = _b64.b64decode(data)
        if path is not None:
            with open(path, 'wb') as f:
                f.write(raw)
        return raw

    async def select_option(self, *values: str) -> list[str]:
        '''Select <option>s in a <select> by value; fires input+change like a
        real user. Returns the values that matched.'''
        selected = await self.eval(
            '(el, wanted) => { const set = new Set(wanted);'
            ' for (const opt of el.options) opt.selected = set.has(opt.value);'
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            # read AFTER the full loop: a single-select settles its invariant
            # (exactly one selected) only once every option is set
            ' return [...el.options].filter(o => o.selected).map(o => o.value); }',
            list(values))
        return selected

    async def set_input_files(self, *paths: str) -> None:
        '''Set the files on a <input type=file>
        (DOM.setFileInputFiles), the way to drive uploads.'''
        await self._page.session.execute(dom_proto.set_file_input_files(
            files=list(paths),
            object_id=runtime_proto.RemoteObjectId(self._object_id)))

    async def query(
        self,
        selector: str,
        *,
        containing: str | None = None,
        index: int = 0,
    ) -> Element | None:
        '''Single-shot scoped query inside this element (no polling); None
        when nothing matches.'''
        function = _query_js('el', selector, containing, index)
        result, details = await self._page.session.execute(
            runtime_proto.call_function_on(
                function_declaration=f'function() {{ return ({function})(this); }}',
                object_id=runtime_proto.RemoteObjectId(self._object_id),
                return_by_value=False,
            ))
        if details is not None:
            from .page import JSError

            raise JSError.from_details(details)
        if result.object_id is None:
            return None
        return Element(self._page, str(result.object_id))

    async def query_all(
        self,
        selector: str,
        *,
        containing: str | None = None,
    ) -> list[Element]:
        '''Every matching descendant inside this element, as held Elements
        (single-shot — no polling), optionally filtered to those whose
        textContent contains ``containing`` (case-insensitive). ``[]`` when
        nothing matches. Use it to enumerate/filter a set (a node's buttons, a
        widget's chips); for a single node use :meth:`query`.'''
        function = _query_all_js('el', selector, containing)
        result, details = await self._page.session.execute(
            runtime_proto.call_function_on(
                function_declaration=f'function() {{ return ({function})(this); }}',
                object_id=runtime_proto.RemoteObjectId(self._object_id),
                return_by_value=False,
            ))
        if details is not None:
            from .page import JSError

            raise JSError.from_details(details)
        if result.object_id is None:
            return []
        return await _elements_from_array(self._page, str(result.object_id))


async def _elements_from_array(owner: typing.Any, array_object_id: str
                               ) -> list[Element]:
    '''Resolve a JS array RemoteObject (of DOM nodes) into held Elements in
    index order, then release the array wrapper. The element handles are
    independent object ids and outlive the array.'''
    props, *_rest = await owner.session.execute(runtime_proto.get_properties(
        runtime_proto.RemoteObjectId(array_object_id), own_properties=True))
    indexed: list[tuple[int, Element]] = []
    for prop in props:
        if (prop.name.isdigit() and prop.value is not None
                and prop.value.object_id is not None):
            indexed.append(
                (int(prop.name), Element(owner, str(prop.value.object_id))))
    indexed.sort(key=lambda pair: pair[0])
    with suppress(Exception):  # freeing the wrapper is best-effort
        await owner.session.execute(runtime_proto.release_object(
            runtime_proto.RemoteObjectId(array_object_id)))
    return [element for _, element in indexed]


def _query_js(root: str, selector: str, containing: str | None, index: int) -> str:
    '''Build '(root) => element-or-null' JS for a filtered, indexed query.'''
    parts = [f'(({root}) => {{',
             f' let els = [...{root}.querySelectorAll({json.dumps(selector)})];']
    if containing is not None:
        parts.append(
            f' const needle = {json.dumps(containing.lower())};'
            ' els = els.filter(e =>'
            " (e.textContent || '').toLowerCase().includes(needle));")
    parts.append(f' return els.at({index}) ?? null; }})')
    return ''.join(parts)


def _query_all_js(root: str, selector: str, containing: str | None) -> str:
    '''Build '(root) => Element[]' JS for a filtered query returning all
    matches (the array counterpart of :func:`_query_js`).'''
    parts = [f'(({root}) => {{',
             f' let els = [...{root}.querySelectorAll({json.dumps(selector)})];']
    if containing is not None:
        parts.append(
            f' const needle = {json.dumps(containing.lower())};'
            ' els = els.filter(e =>'
            " (e.textContent || '').toLowerCase().includes(needle));")
    parts.append(' return els; })')
    return ''.join(parts)


class ElementQueries:
    '''Mixin: the eager query surface shared by every Element owner (Page and
    Frame define it once here). Owners provide ``evaluate`` and
    ``default_timeout``; ``_where`` seasons timeout messages ('' / ' in frame').
    '''

    _where = ''

    async def query(
        self,
        selector: str,
        *,
        containing: str | None = None,
        index: int = 0,
        timeout: float | None = None,
        poll: float = 0.05,
        required: bool = True,
    ) -> Element | None:
        '''Wait for and hold an element: CSS selector, optionally filtered to
        those whose textContent contains ``containing`` (case-insensitive —
        the :has-text() CDP never had), picked by ``index`` (-1 = newest/last,
        for apps that keep stale copies of widgets in the DOM). Polling rides
        out framework re-renders; raises TimeoutError with the selector in
        the message unless ``required=False`` (presence probes).'''
        js = _query_js('document', selector, containing, index)
        try:
            async with asyncio.timeout(timeout or self.default_timeout):
                while True:
                    result = await self.evaluate(
                        f'({js})(document)', return_by_value=False)
                    if result.object_id is not None:
                        return Element(self, str(result.object_id))
                    await asyncio.sleep(poll)
        except TimeoutError:
            if required:
                detail = f' containing {containing!r}' if containing else ''
                raise TimeoutError(
                    f'no element for {selector!r}{detail}{self._where} within '
                    f'{timeout or self.default_timeout}s') from None
            return None

    async def query_count(self, selector: str) -> int:
        return int(await self.evaluate(
            f'document.querySelectorAll({json.dumps(selector)}).length'))

    async def query_all(
        self,
        selector: str,
        *,
        containing: str | None = None,
    ) -> list[Element]:
        '''Every element matching ``selector`` as a held :class:`Element`
        (single-shot — no polling), optionally filtered to those whose
        textContent contains ``containing`` (case-insensitive). ``[]`` when
        nothing matches.

        Use it to enumerate or filter a *set* — chips, rows, a node's buttons —
        e.g. ``[c for c in await page.query_all('button') if not await
        c.eval('(el)=>el.disabled')]``. Holding each node dodges the stale-copy
        trap that re-running a first-match selector would hit. For a single
        element *with* waiting, use :meth:`query`; to just count, use
        :meth:`query_count`.'''
        function = _query_all_js('document', selector, containing)
        result = await self.evaluate(
            f'({function})(document)', return_by_value=False)
        if result.object_id is None:
            return []
        return await _elements_from_array(self, str(result.object_id))
