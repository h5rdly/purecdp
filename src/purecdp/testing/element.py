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
import re
import typing

from ..errors import PureCDPError
from ._driver import Handle

if typing.TYPE_CHECKING:
    from .page import Page

#: JS that resolves a wrapper to its real control: component libraries often
#: put the click target on a wrapper whose own .click() does nothing, with a
#: visually-hidden <input> inside it (or inside its label).
_RESOLVE_AND_CLICK = (
    "(el) => { if (!el.isConnected) return 'detached';"
    ' const i = el.matches('
    "'input,button,a,select,textarea,[role=button],[role=radio],[role=checkbox]')"
    " ? el : el.querySelector('input') || el.closest('label')?.querySelector('input');"
    " (i || el).click(); return 'ok'; }"
)

#: React patches value setters; go through the native prototype setter, then
#: fire bubbling input+change so controlled components commit either way.
_SET_VALUE = (
    "(el, value) => { if (!el.isConnected) return 'detached';"
    ' const proto = el instanceof HTMLTextAreaElement'
    '   ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;'
    " Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);"
    " el.dispatchEvent(new Event('input', { bubbles: true }));"
    " el.dispatchEvent(new Event('change', { bubbles: true }));"
    " return 'ok'; }"
)

#: Focus for typing, reporting whether focus actually landed: keys sent to a
#: node that is no longer in the document, or that refused focus, vanish
#: without a trace — the classic "the app ignored my typing".
_FOCUS_FOR_TYPING = (
    "(el) => { if (!el.isConnected) return 'detached';"
    ' el.focus();'
    " return document.activeElement === el ? 'ok' : 'unfocused'; }"
)

_ACTION_FAILURES = {
    'detached': ('element is no longer in the document (the app re-rendered '
                 'or replaced it) — re-query it, or use page.live(selector), '
                 'which re-resolves on every action'),
    'unfocused': ('element did not take focus (hidden, disabled, or not '
                  'focusable) — typed keys would go nowhere'),
}

#: In-browser actionability poll: 
#: loop until the element is connected, has a visible box, is enabled, has stopped
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


class ActionabilityError(PureCDPError):
    '''A trusted action's target never became actionable (connected, visible,
    enabled, stable, and un-obscured) within the timeout. ``reason`` names the
    check that was still failing when time ran out.'''

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


class QueryTimeout(TimeoutError, PureCDPError):
    '''No element matched within the timeout — and the message says what WAS
    there, because "selector timed out" alone means a screenshot hunt. Three
    forms: the selector matches *nothing*; it matches but *none are visible*
    (SPAs hide rather than remove — a different bug than absence); or the
    ``containing`` text missed and the message lists the visible candidates'
    texts. Still a ``TimeoutError`` (existing handlers keep working) and a
    ``PureCDPError`` (the family catch is universal).'''

    def __init__(self, message: str, *, selector: str = '',
                 containing: typing.Any = None, matched: int = 0,
                 visible_count: int = 0,
                 candidates: typing.Sequence[str] = ()):
        super().__init__(message)
        self.selector = selector
        self.containing = containing
        self.matched = matched                #: bare-selector match count
        self.visible_count = visible_count    #: of those, how many rendered
        self.candidates = list(candidates)    #: visible matches' texts (capped)


#: On-failure-only diagnostic: what does the BARE selector match, how many of
#: those are rendered, and what do the first few say. Never runs on success.
_NEAR_MISS_CANDIDATES = 8
_NEAR_MISS_CHARS = 48


def _near_miss_js(selector: str) -> str:
    return (
        '(() => {'
        f' const els = [...document.querySelectorAll({json.dumps(selector)})];'
        ' const vis = els.filter(e => {'
        '   const b = e.getBoundingClientRect();'
        '   return b.width > 0 && b.height > 0 && e.getClientRects().length; });'
        ' return {total: els.length, visible: vis.length,'
        f'  texts: vis.slice(0, {_NEAR_MISS_CANDIDATES}).map(e =>'
        "   (e.textContent || '').trim().replace(/\\s+/g, ' ')"
        f'   .slice(0, {_NEAR_MISS_CHARS}))}};'
        ' })()'
    )


async def _near_miss(owner: typing.Any, selector: str
                     ) -> tuple[int, int, list[str]] | None:
    '''Run the diagnostic pass; None when the page can't answer (best-effort —
    a broken page still gets the plain timeout message).'''
    try:
        diag = await owner.evaluate(_near_miss_js(selector))
    except Exception:
        return None
    if not isinstance(diag, dict):
        return None
    return (int(diag.get('total', 0)), int(diag.get('visible', 0)),
            [str(t) for t in diag.get('texts', [])])


def _near_miss_tail(selector: str, containing: typing.Any, index: int | None,
                    diag: tuple[int, int, list[str]]) -> str:
    '''The " — what was actually there" clause of a QueryTimeout message.
    ``index`` is None when the caller has no index axis (Live chains) — then
    visible-but-missed falls through to the candidate list.'''
    total, visible, texts = diag
    if total == 0:
        return ' — selector matches nothing'
    if visible == 0:
        return f' — {total} match but none are visible (hidden or zero-size)'
    if containing is None and index is not None:
        return (f' — {total} match, {visible} visible; '
                f'is index={index} out of range?')
    return (f' — {visible} visible {selector!r}: '
            + ' | '.join(repr(t) for t in texts))


async def _query_timeout(owner: typing.Any, selector: str,
                         containing: typing.Any, index: int,
                         where: str, timeout: float) -> QueryTimeout:
    detail = f' containing {containing!r}' if containing is not None else ''
    base = f'no element for {selector!r}{detail}{where} within {timeout}s'
    diag = await _near_miss(owner, selector)
    if diag is None:
        return QueryTimeout(base, selector=selector, containing=containing)
    total, visible, texts = diag
    return QueryTimeout(
        base + _near_miss_tail(selector, containing, index, diag),
        selector=selector, containing=containing, matched=total,
        visible_count=visible, candidates=texts)


class Element:
    '''A held DOM node. Created by Page.query()/query_all()/Element.query().'''

    def __init__(self, page: Page, handle: Handle | str):
        self._page = page
        self._handle = Handle.coerce(handle)

    @property
    def _object_id(self) -> str:
        '''The backend object id (pre-seam spelling, kept for callers).'''
        return self._handle.id

    def __repr__(self) -> str:
        return f'Element({self._object_id!r})'

    async def eval(self, function: str, *args: typing.Any) -> typing.Any:
        '''Call ``function`` (JS, e.g. ``'(el) => el.textContent'``) with this
        element as its first argument, plus JSON-serializable *args.'''
        return await self._page._driver.call(function, [self, *args])

    async def text(self) -> str:
        return (await self.eval('(el) => el.textContent')) or ''

    @staticmethod
    def _raise_if_failed(result: typing.Any, action: str) -> None:
        '''Turn an explicit failure token from an action script into an
        :class:`ActionabilityError`. Only the known tokens fail — anything
        else (a real page's 'ok', an odd/None result) lets the action
        stand, so a quirk can't spuriously block it.'''
        if result in _ACTION_FAILURES:
            raise ActionabilityError(
                f'{action}: {_ACTION_FAILURES[result]}', result)

    async def click(self) -> None:
        '''Synthetic click, resolving wrappers to their real control (see
        _RESOLVE_AND_CLICK). Use mouse_click() when the app needs a trusted
        event. Raises :class:`ActionabilityError` (reason ``detached``) if
        this node is no longer in the document — a click into the void is
        never what you meant.'''
        self._raise_if_failed(await self.eval(_RESOLVE_AND_CLICK), 'click')

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
        raise :class:`ActionabilityError`. The safety gate that
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
            await self._page._driver.mouse(kind, x, y, button='left',
                                           click_count=1)

    async def human_click(self, **move_kwargs: typing.Any) -> None:
        '''Move the cursor here along a human, curved path and click — trusted
        Input events with realistic motion (M8 Track B). Uses page.cursor, so
        position carries over between actions.'''
        await self._page.cursor.click(self, **move_kwargs)

    async def set_value(
        self, value: str, *, stable: bool = False, timeout: float | None = None
    ) -> None:
        '''Instant value write on this exact node: native setter + bubbling
        ``input`` and ``change`` events, so controlled inputs (React & co.)
        commit it — and so does ``type``, with real keystrokes (verified
        against React 18); pick by what the app listens to, not by
        framework. Prefer this over selector-based writes whenever several
        copies of a widget can be in the DOM.

        Raises :class:`ActionabilityError` (reason ``detached``) if the node
        is no longer in the document: a value written into a node nobody
        renders used to "succeed" silently — the app re-rendered after you
        queried, so re-query or use ``page.live(selector)``.

        ``stable=True`` first waits for the field to be visible, enabled, and
        settled (no pointer hit-test needed for a value write).'''
        if stable:
            await self.wait_actionable(hit=False, timeout=timeout)
        self._raise_if_failed(await self.eval(_SET_VALUE, value), 'set_value')

    async def focus(self) -> None:
        await self.eval('(el) => el.focus()')

    async def type(
        self, text: str, *, insert: bool = False, stable: bool = False,
        timeout: float | None = None
    ) -> None:
        '''Focus this element and type ``text`` with REAL per-key events —
        keydown/keyup for every character (at machine speed, no cadence), so
        anything listening to keys (autocomplete-on-keydown, shortcut
        handlers) fires exactly as for a human typist. Follow with
        ``page.press('Enter')`` to submit.

        ``insert=True`` pastes the whole text via one ``Input.insertText``
        instead: ONE ``input`` event, ZERO key events — fast bulk entry, but
        key listeners silently never fire while ``.value`` looks perfect
        (maddening to debug; that is why it is not the default). See also
        ``set_value`` (instant setter + synthetic input/change, no key
        events) and ``page.human_type`` (real keys + human cadence). Both
        ``type`` and ``set_value`` reach controlled (React) inputs.

        Keys go to whatever has focus, so this first checks focus landed on
        THIS node and raises :class:`ActionabilityError` otherwise — reason
        ``detached`` (the app re-rendered and this handle is stale: re-query,
        or use ``page.live(selector)``) or ``unfocused`` (hidden, disabled,
        not focusable). Before 0.9.0 both cases typed into the void.

        ``stable=True`` first waits for the field to be visible, enabled, and
        settled before focusing.'''
        if stable:
            await self.wait_actionable(hit=False, timeout=timeout)
        self._raise_if_failed(await self.eval(_FOCUS_FOR_TYPING), 'type')
        if insert:
            await self._page._driver.insert_text(text)
            return
        for ch in text:
            await self._page._driver.key('keyDown', key=ch, text=ch)
            await self._page._driver.key('keyUp', key=ch)

    async def scroll_into_view(self) -> None:
        await self.eval(
            "(el) => el.scrollIntoView({block: 'center', inline: 'center'})")

    async def frame_id(self) -> str | None:
        '''The frameId this element *owns* — set for <iframe>/<frame> host
        nodes, else None. Used by Page.frame() to correlate an iframe to its
        out-of-process CDP session.'''
        return await self._page._driver.node_frame_id(self._handle)

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
        await self._page._driver.mouse('mouseMoved', center['x'], center['y'])

    async def screenshot(self, path: str | None = None, *, format: str = 'png'
                         ) -> bytes:
        '''Screenshot just this element (clipped to its box). Returns bytes,
        optionally writing path.'''
        await self.scroll_into_view()
        box = await self.bounding_box()
        if box is None:
            raise LookupError('element has no visible box to screenshot')
        raw = await self._page._driver.screenshot(
            format=format, clip={**box, 'scale': 1}, beyond_viewport=True)
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
        await self._page._driver.set_files(self._handle, paths)

    async def query(
        self,
        selector: str,
        *,
        containing: str | re.Pattern | None = None,
        index: int = 0,
    ) -> Element | None:
        '''Single-shot scoped query inside this element (no polling); None
        when nothing matches.'''
        function = _query_js('el', selector, containing, index)
        handle = await self._page._driver.call_handle(function, [self])
        return Element(self._page, handle) if handle is not None else None

    async def query_all(
        self,
        selector: str,
        *,
        containing: str | re.Pattern | None = None,
    ) -> list[Element]:
        '''Every matching descendant inside this element, as held Elements
        (single-shot — no polling), optionally filtered by ``containing``
        (case-insensitive substring, or a ``re.Pattern`` tested against the
        trimmed text). ``[]`` when nothing matches. Use it to enumerate/filter
        a set (a node's buttons, a widget's chips); for a single node use
        :meth:`query`.'''
        function = _query_all_js('el', selector, containing)
        handle = await self._page._driver.call_handle(function, [self])
        if handle is None:
            return []
        return await _elements_from_array(self._page, handle)


async def _elements_from_array(owner: typing.Any, array: Handle | str
                               ) -> list[Element]:
    '''Resolve a JS array handle (of DOM nodes) into held Elements in index
    order; the array wrapper is released. The element handles are
    independent and outlive the array.'''
    handles = await owner._driver.array_handles(Handle.coerce(array))
    return [Element(owner, h) for h in handles]


def _js_regex(pattern: re.Pattern) -> tuple[str, str]:
    '''(source, flags) for ``new RegExp(...)`` from a Python pattern. Python
    and JS regex syntax overlap on the subset tests use; the i/m/s flags
    carry over, anything fancier is on the caller to keep JS-compatible.'''
    flags = ''
    if pattern.flags & re.IGNORECASE:
        flags += 'i'
    if pattern.flags & re.MULTILINE:
        flags += 'm'
    if pattern.flags & re.DOTALL:
        flags += 's'
    return pattern.pattern, flags


def _containing_filter_js(containing: str | re.Pattern) -> str:
    '''JS statement filtering ``els`` by text: case-insensitive substring for a
    str, a RegExp test on the trimmed text for a ``re.Pattern`` (anchors work:
    ``^AND$`` matches an element whose whole trimmed text is AND).'''
    if isinstance(containing, re.Pattern):
        source, flags = _js_regex(containing)
        return (f' const rx = new RegExp({json.dumps(source)}, {json.dumps(flags)});'
                ' els = els.filter(e =>'
                " rx.test((e.textContent || '').trim()));")
    return (f' const needle = {json.dumps(containing.lower())};'
            ' els = els.filter(e =>'
            " (e.textContent || '').toLowerCase().includes(needle));")


def _query_js(root: str, selector: str, containing: str | re.Pattern | None,
              index: int) -> str:
    '''Build '(root) => element-or-null' JS for a filtered, indexed query.'''
    parts = [f'(({root}) => {{',
             f' let els = [...{root}.querySelectorAll({json.dumps(selector)})];']
    if containing is not None:
        parts.append(_containing_filter_js(containing))
    parts.append(f' return els.at({index}) ?? null; }})')
    return ''.join(parts)


def _query_all_js(root: str, selector: str,
                  containing: str | re.Pattern | None) -> str:
    '''Build '(root) => Element[]' JS for a filtered query returning all
    matches (the array counterpart of :func:`_query_js`).'''
    parts = [f'(({root}) => {{',
             f' let els = [...{root}.querySelectorAll({json.dumps(selector)})];']
    if containing is not None:
        parts.append(_containing_filter_js(containing))
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
        containing: str | re.Pattern | None = None,
        index: int = 0,
        timeout: float | None = None,
        poll: float = 0.05,
        required: bool = True,
    ) -> Element | None:
        '''Wait for and hold an element: CSS selector, optionally filtered by
        ``containing`` — a case-insensitive substring of the textContent (the
        :has-text() CDP never had) or a ``re.Pattern`` tested against the
        trimmed text (anchors give exact match: ``re.compile(r'^AND$')``;
        keep patterns to the Python/JS-shared syntax) — picked by ``index``
        (-1 = newest/last, for apps that keep stale copies of widgets in the
        DOM). Polling rides out framework re-renders; raises
        :class:`QueryTimeout` — whose message says what WAS there (near-miss
        candidates, hidden matches) — unless ``required=False`` (presence
        probes).'''
        js = _query_js('document', selector, containing, index)
        try:
            async with asyncio.timeout(timeout or self.default_timeout):
                while True:
                    handle = await self._driver.evaluate_handle(
                        f'({js})(document)')
                    if handle is not None:
                        return Element(self, handle)
                    await asyncio.sleep(poll)
        except TimeoutError:
            if required:
                raise (await _query_timeout(
                    self, selector, containing, index, self._where,
                    timeout or self.default_timeout)) from None
            return None

    async def wait_for(self, selector: str, *,
                       containing: str | re.Pattern | None = None,
                       timeout: float | None = None,
                       **conditions: typing.Any) -> None:
        '''Wait until ``selector`` satisfies ``conditions`` — the same
        vocabulary as :meth:`Live.should` (``visible`` / ``text`` / ``value``
        / ``count`` / ``enabled`` / ``checked``); with none, waits for
        presence (at least one match, hidden counts).

        The two axes: **``count`` is the exists axis, ``visible`` the
        rendered axis.** SPAs hide rather than remove, so existence checks
        lie about what a user can see::

            await page.wait_for('.results')                 # present
            await page.wait_for('.spinner', visible=False)  # gone OR hidden
            await page.wait_for('.spinner', count=0)        # strictly absent
            await page.wait_for('.modal', visible=True)     # actually rendered

        Presence failures raise :class:`QueryTimeout` (with the near-miss
        diagnosis); condition failures raise ``ExpectationError`` with the
        observed values.'''
        if not conditions:
            await self.query(selector, containing=containing, timeout=timeout)
            return
        await self.live(selector, containing=containing).should(
            timeout=timeout, **conditions)

    async def query_count(self, selector: str) -> int:
        return int(await self.evaluate(
            f'document.querySelectorAll({json.dumps(selector)}).length'))

    async def query_all(
        self,
        selector: str,
        *,
        containing: str | re.Pattern | None = None,
    ) -> list[Element]:
        '''Every element matching ``selector`` as a held :class:`Element`
        (single-shot — no polling), optionally filtered by ``containing``
        (case-insensitive substring, or a ``re.Pattern`` tested against the
        trimmed text). ``[]`` when nothing matches.

        Use it to enumerate or filter a *set* — chips, rows, a node's buttons —
        e.g. ``[c for c in await page.query_all('button') if not await
        c.eval('(el)=>el.disabled')]``. Holding each node dodges the stale-copy
        trap that re-running a first-match selector would hit. For a single
        element *with* waiting, use :meth:`query`; to just count, use
        :meth:`query_count`.'''
        function = _query_all_js('document', selector, containing)
        async with asyncio.timeout(self.default_timeout):
            handle = await self._driver.evaluate_handle(f'({function})(document)')
        if handle is None:
            return []
        return await _elements_from_array(self, handle)
