'''Live: lazy, re-resolving locators (M11).

A ``Live`` holds a *query recipe*, never a resolved node — every action and
assertion re-runs the recipe against the current DOM in one round-trip, so it
survives re-renders (React remounts, virtual lists, chat UIs that keep stale
copies of a widget). It auto-waits (the actionability gate) and carries
auto-retrying assertions (:meth:`Live.should`).

Get one from a page/frame: ``page.live(selector)``, ``page.get_by_test_id(id)``,
``page.get_by_text(text)``, ``page.get_by_role(role, name=...)``,
``page.get_by_label(text)`` — all return a ``Live``. Refine with ``.first`` /
``.last`` / ``.nth(i)`` / ``.containing(text)`` and scope with ``.live(sub)`` or a
scoped ``.get_by_*``. ``query`` / ``query_all`` / :class:`Element` remain the
eager lower-level layer; ``await live.get()`` drops to a one-shot ``Element``.
'''

from __future__ import annotations

import asyncio
import json
import re
import typing

from ..errors import PureCDPError
from .element import (Element, QueryTimeout, _elements_from_array, _js_regex,
                      _near_miss, _near_miss_tail)

if typing.TYPE_CHECKING:
    from .page import Page
    from .frame import Frame

_CONDITIONS = frozenset(
    {'visible', 'text', 'value', 'count', 'enabled', 'checked'})

#: Approximate role -> CSS. Covers the common roles; not full ARIA computation
#: (see get_by_role docs). Unknown roles fall back to [role=<role>].
_ROLE_CSS: dict[str, str] = {
    'button': 'button, [role=button], input[type=button], input[type=submit], '
              'input[type=reset]',
    'link': 'a[href], [role=link]',
    'textbox': 'input:not([type=checkbox]):not([type=radio]):not([type=button])'
               ':not([type=submit]):not([type=reset]):not([type=hidden]), '
               'textarea, [role=textbox], [contenteditable=""], '
               '[contenteditable=true]',
    'searchbox': 'input[type=search], [role=searchbox]',
    'checkbox': 'input[type=checkbox], [role=checkbox]',
    'radio': 'input[type=radio], [role=radio]',
    'combobox': 'select, [role=combobox]',
    'option': 'option, [role=option]',
    'heading': 'h1, h2, h3, h4, h5, h6, [role=heading]',
    'list': 'ul, ol, [role=list]',
    'listitem': 'li, [role=listitem]',
    'img': 'img, [role=img]',
    'dialog': 'dialog, [role=dialog]',
    'tab': '[role=tab]',
    'menuitem': '[role=menuitem]',
}


def _role_css(role: str) -> str:
    return _ROLE_CSS.get(role.lower(), f'[role={_css_str(role)}]')


def _css_str(s: str) -> str:
    '''CSS string literal for an attribute selector value.'''
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


#: (steps, mode) -> element (mode 'one') | Element[] ('all') | probe dict
#: ('probe'). steps: list of {kind, ...}. Resolution reduces the steps over an
#: element accumulator; null accumulator means "start from document".
_LIVE_JS = r'''
(steps, mode) => {
  const lc = (s) => (s || '').toLowerCase();
  const txt = (e) => (e.textContent || '').trim();
  const match = (s, q, exact) => exact ? (s || '').trim() === q : lc(s).includes(lc(q));
  const mtext = (s, q) => (q && typeof q === 'object')
    ? new RegExp(q.re, q.flags).test((s || '').trim())
    : lc(s).includes(lc(q));
  const accName = (e) => {
    let n = e.getAttribute && e.getAttribute('aria-label');
    if (!n && e.labels && e.labels.length) n = e.labels[0].textContent;
    if (!n && e.closest) { const l = e.closest('label'); if (l) n = l.textContent; }
    if (!n && e.getAttribute) n = e.getAttribute('title') || e.getAttribute('placeholder');
    if (!n) n = e.value;
    if (!n) n = e.textContent;
    return (n || '').trim();
  };
  const LABELABLE = 'input, select, textarea, button, [role=textbox], '
    + '[role=combobox], [role=checkbox], [role=radio], [contenteditable=""], '
    + '[contenteditable=true]';
  let els = null;                       // null => start from document
  for (const s of steps) {
    if (s.kind === 'css') {
      const roots = els === null ? [document] : els;
      let out = [];
      for (const r of roots) for (const x of r.querySelectorAll(s.sel)) out.push(x);
      els = [...new Set(out)];
      if (s.containing) els = els.filter(e => mtext(txt(e), s.containing));
      if (s.name) els = els.filter(e => lc(accName(e)).includes(lc(s.name)));
    } else if (s.kind === 'text') {
      const roots = els === null ? [document.documentElement] : els;
      let pool = new Set();
      for (const r of roots) { pool.add(r); for (const x of r.querySelectorAll('*')) pool.add(x); }
      els = [...pool].filter(e => match(txt(e), s.q, s.exact)
        && ![...e.children].some(c => match(txt(c), s.q, s.exact)));
    } else if (s.kind === 'label') {
      const roots = els === null ? [document] : els;
      let pool = new Set();
      for (const r of roots) for (const x of r.querySelectorAll(LABELABLE)) pool.add(x);
      els = [...pool].filter(e => match(accName(e), s.q, s.exact));
    } else if (s.kind === 'filter') {
      els = (els || []).filter(e => mtext(txt(e), s.containing));
    } else if (s.kind === 'index') {
      const e = els === null ? null : els.at(s.n);
      els = e ? [e] : [];
    }
  }
  els = els || [];
  if (mode === 'all') return els;
  if (mode === 'probe') {
    const e = els[0];
    let box = null; try { box = e && e.getBoundingClientRect(); } catch (_) {}
    return {
      count: els.length,
      present: !!e,
      text: e ? (e.textContent || '') : null,
      value: (e && e.value != null) ? String(e.value) : null,
      visible: !!(e && box && box.width > 0 && box.height > 0 && e.getClientRects().length),
      enabled: e ? !(e.disabled || (e.getAttribute && e.getAttribute('aria-disabled') === 'true')) : null,
      checked: e ? !!e.checked : null,
    };
  }
  return els[0] || null;                // mode 'one'
}
'''.strip()


class ExpectationError(AssertionError, PureCDPError):
    '''A :meth:`Live.should` condition never held within the timeout. Subclasses
    AssertionError so it registers as a test *failure*, not an error.'''


def _text_arg(q: str | re.Pattern | None) -> typing.Any:
    '''Encode a text filter for the JSON steps recipe: a str passes through,
    a ``re.Pattern`` becomes the ``{'re': source, 'flags': ...}`` form the
    reducer turns back into a RegExp (tested against the trimmed text).'''
    if isinstance(q, re.Pattern):
        source, flags = _js_regex(q)
        return {'re': source, 'flags': flags}
    return q


def _fmt_text_arg(v: typing.Any) -> str:
    if isinstance(v, dict):
        return f'/{v["re"]}/{v["flags"]}'
    return repr(v)


def _repr_steps(steps: list[dict]) -> str:
    parts = []
    for s in steps:
        k = s['kind']
        if k == 'css':
            p = repr(s['sel'])
            if s.get('containing'):
                p += f' containing {_fmt_text_arg(s["containing"])}'
            if s.get('name'):
                p += f' name {s["name"]!r}'
            parts.append(p)
        elif k == 'text':
            parts.append(f'text {s["q"]!r}')
        elif k == 'label':
            parts.append(f'label {s["q"]!r}')
        elif k == 'filter':
            parts.append(f'containing {_fmt_text_arg(s["containing"])}')
        elif k == 'index':
            parts.append(f'[{s["n"]}]')
    return ' > '.join(parts)


class LiveFactories:
    '''Mixin: the ``live()`` / ``get_by_*`` locator constructors, defined once.
    Page and Frame inherit them starting a fresh recipe from the document;
    :class:`Live` inherits them too but overrides :meth:`_live` so the same
    constructors *scope* inside the current matches.'''

    def _live(self, step: dict) -> Live:
        return Live(self, [step])

    def live(self, selector: str, *,
             containing: str | re.Pattern | None = None) -> Live:
        '''A lazy, re-resolving locator for element(s) matching ``selector`` —
        the ergonomic default for tests. It re-runs the query on every action
        and assertion (so it survives re-renders), auto-waits, and carries
        :meth:`Live.should` assertions. ``containing`` filters by text:
        case-insensitive substring, or a ``re.Pattern`` tested against the
        trimmed text (anchors give exact match). On a Page/Frame the query
        starts at the document; on a ``Live`` it scopes inside the current
        matches. ``query`` / ``query_all`` stay as the eager lower-level
        layer.'''
        return self._live({'kind': 'css', 'sel': selector,
                           'containing': _text_arg(containing)})

    def get_by_test_id(self, test_id: str) -> Live:
        '''Locator for ``[data-testid="..."]``.'''
        return self._live({'kind': 'css', 'sel': f'[data-testid={_css_str(test_id)}]'})

    def get_by_text(self, text: str, *, exact: bool = False) -> Live:
        '''Locator for the innermost element whose text contains ``text``
        (case-insensitive; ``exact=True`` for a trimmed exact match).'''
        return self._live({'kind': 'text', 'q': text, 'exact': exact})

    def get_by_role(self, role: str, *, name: str | None = None) -> Live:
        '''Locator by (approximate) ARIA role, optionally filtered by accessible
        name. Roles map to a curated tag+ARIA CSS set (the common roles), not
        full ARIA computation — accurate enough for tests.'''
        return self._live({'kind': 'css', 'sel': _role_css(role), 'name': name})

    def get_by_label(self, text: str, *, exact: bool = False) -> Live:
        '''Locator for a form control by its label (``<label>``, ``aria-label``,
        ``title``, or ``placeholder``).'''
        return self._live({'kind': 'label', 'q': text, 'exact': exact})


class Live(LiveFactories):
    '''A lazy, re-resolving handle for element(s) matching a query. Immutable —
    refinements return a new Live. Created by ``page.live()`` / ``page.get_by_*``
    (see the module docstring).'''

    def __init__(self, owner: Page | Frame, steps: list[dict]):
        self._owner = owner
        self._steps = steps

    def __repr__(self) -> str:
        return f'Live({_repr_steps(self._steps)})'

    def _live(self, step: dict) -> Live:
        return Live(self._owner, self._steps + [step])

    # -- refinement (each returns a new Live; live()/get_by_* scope too) ------

    @property
    def first(self) -> Live:
        return self._live({'kind': 'index', 'n': 0})

    @property
    def last(self) -> Live:
        return self._live({'kind': 'index', 'n': -1})

    def nth(self, i: int) -> Live:
        return self._live({'kind': 'index', 'n': i})

    def containing(self, text: str | re.Pattern) -> Live:
        '''Filter the current matches by text: case-insensitive substring, or
        a ``re.Pattern`` tested against the trimmed textContent (anchors give
        exact match: ``re.compile(r'^AND$')``).'''
        return self._live({'kind': 'filter', 'containing': _text_arg(text)})

    # -- resolution ----------------------------------------------------------

    def _call(self, mode: str) -> str:
        return f'({_LIVE_JS})({json.dumps(self._steps)}, {json.dumps(mode)})'

    async def _resolve_one(self) -> Element | None:
        result = await self._owner.evaluate(self._call('one'), return_by_value=False)
        if result.object_id is None:
            return None
        return Element(self._owner, str(result.object_id))

    async def _resolve_all(self) -> list[Element]:
        result = await self._owner.evaluate(self._call('all'), return_by_value=False)
        if result.object_id is None:
            return []
        return await _elements_from_array(self._owner, str(result.object_id))

    async def _wait_one(self, timeout: float | None = None) -> Element:
        '''Poll until the query matches at least one element (auto-wait).'''
        to = self._owner.default_timeout if timeout is None else timeout
        try:
            async with asyncio.timeout(to):
                while True:
                    el = await self._resolve_one()
                    if el is not None:
                        return el
                    await asyncio.sleep(0.05)
        except TimeoutError:
            raise (await self._query_timeout(to)) from None

    async def _query_timeout(self, to: float) -> QueryTimeout:
        '''The near-miss-enriched failure — when the base step is a CSS query
        (live()/get_by_test_id/get_by_role), say what the bare selector DID
        match; text/label bases keep the plain message.'''
        base = f'no element for {self!r} within {to}s'
        step = self._steps[0] if self._steps else {}
        if step.get('kind') != 'css':
            return QueryTimeout(base)
        selector = step['sel']
        containing = step.get('name') or step.get('containing')
        diag = await _near_miss(self._owner, selector)
        if diag is None:
            return QueryTimeout(base, selector=selector, containing=containing)
        total, visible, texts = diag
        return QueryTimeout(
            base + _near_miss_tail(selector, containing, None, diag),
            selector=selector, containing=containing, matched=total,
            visible_count=visible, candidates=texts)

    async def get(self, *, timeout: float | None = None) -> Element:
        '''Resolve NOW to a one-shot :class:`Element` — the escape hatch to the
        full element API. Auto-waits for the element to appear.'''
        return await self._wait_one(timeout)

    # -- actions (re-resolve the first match, actionability-gated) -----------

    async def click(self, *, trusted: bool = True, stable: bool = True,
                    timeout: float | None = None) -> None:
        '''Click the first match. ``trusted`` (default) is a real Input mouse
        press+release at the centre with the actionability gate; ``trusted=False``
        is the synthetic wrapper-resolving click (for a hidden <input> inside a
        label). ``stable`` waits for the element to be actionable first.'''
        el = await self._wait_one(timeout)
        if trusted:
            await el.mouse_click(stable=stable, timeout=timeout)
        else:
            await el.click()

    async def fill(self, text: str, *, stable: bool = True,
                   timeout: float | None = None) -> None:
        '''React-safe value write on the first match (native setter + input/
        change events).'''
        el = await self._wait_one(timeout)
        await el.set_value(text, stable=stable, timeout=timeout)

    async def type(self, text: str, *, stable: bool = True,
                   timeout: float | None = None) -> None:
        '''Focus the first match and type with real keystrokes.'''
        el = await self._wait_one(timeout)
        await el.type(text, stable=stable, timeout=timeout)

    async def hover(self, *, timeout: float | None = None) -> None:
        el = await self._wait_one(timeout)
        await el.hover()

    async def select(self, *values: str, timeout: float | None = None) -> list[str]:
        el = await self._wait_one(timeout)
        return await el.select_option(*values)

    async def text(self, *, timeout: float | None = None) -> str:
        el = await self._wait_one(timeout)
        return await el.text()

    async def value(self, *, timeout: float | None = None) -> str:
        el = await self._wait_one(timeout)
        return await el.eval('(e) => e.value == null ? "" : String(e.value)')

    async def probe(self) -> dict:
        '''One-round-trip observation of the current matches — no waiting, no
        raising: the soft-assertion counterpart of :meth:`should` for suites
        that accumulate PASS/FAIL and keep going. Keys: ``count``, ``present``,
        ``visible``, ``enabled``, ``checked``, ``text``, ``value`` (per-element
        keys describe the first match; None/False-ish when there is none).'''
        return await self._owner.evaluate(self._call('probe'), return_by_value=True)

    async def count(self) -> int:
        '''Number of matches right now (no waiting — can be 0).'''
        return int((await self.probe())['count'])

    # -- assertion -----------------------------------------------------------

    async def should(self, *, timeout: float | None = None,
                     **conditions: typing.Any) -> None:
        '''Poll until EVERY condition holds, else raise :class:`ExpectationError`
        (a test failure) naming what missed and the last value seen.

        Conditions: ``visible`` (bool), ``text`` (substring or ``re.Pattern``),
        ``value`` (substring or ``re.Pattern``), ``count`` (int), ``enabled``
        (bool), ``checked`` (bool). Per-element conditions check the FIRST match
        (narrow with ``.first`` / ``.nth``); ``count`` checks the whole set.
        Booleans negate naturally (``visible=False``); ``count=0`` asserts
        absence.'''
        unknown = set(conditions) - _CONDITIONS
        if unknown:
            raise ValueError(
                f'unknown should() condition(s): {sorted(unknown)}; '
                f'known: {sorted(_CONDITIONS)}')
        to = self._owner.default_timeout if timeout is None else timeout
        obs: dict = {}
        try:
            async with asyncio.timeout(to):
                while True:
                    # a probe can come back empty (page mid-navigation) —
                    # that's "not met yet", never a crash
                    obs = await self.probe() or {}
                    if not _failing(conditions, obs):
                        return
                    await asyncio.sleep(0.05)
        except TimeoutError:
            failed = _failing(conditions, obs)
            raise ExpectationError(
                f'{self!r}.should({_fmt_conditions(conditions)}) not met within '
                f'{to}s: {_fmt_failures(failed, obs)}') from None


def _one_ok(name: str, expected: typing.Any, obs: dict) -> bool:
    if name == 'visible':
        return bool(obs.get('visible')) == bool(expected)
    if name == 'count':
        return obs.get('count') == expected
    if name in ('enabled', 'checked'):
        return bool(obs.get(name)) == bool(expected)
    if name in ('text', 'value'):
        got = obs.get(name) or ''
        if isinstance(expected, re.Pattern):
            return bool(expected.search(got))
        return str(expected).lower() in got.lower()
    return False


def _failing(conditions: dict, obs: dict) -> list[str]:
    return [k for k, v in conditions.items() if not _one_ok(k, v, obs)]


def _fmt_conditions(conditions: dict) -> str:
    return ', '.join(f'{k}={v!r}' for k, v in conditions.items())


def _fmt_failures(failed: list[str], obs: dict) -> str:
    bits = []
    for k in failed:
        seen = obs.get('count') if k == 'count' else obs.get(k)
        bits.append(f'{k} (saw {seen!r})')
    return '; '.join(bits) or '(no observation)'
