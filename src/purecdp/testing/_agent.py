'''The agent surface (snapshot / element_for_ref / act) as free functions over
a *host*, so it works verbatim on a Page (the main frame) or a Frame (a
cross-origin child frame — see frame.py).

A host exposes ``.session`` (where DOM/AX/Runtime/Input commands go — the frame's
own session for a Frame), a mutable ``._snapshot_refs`` dict, and the rest of the
Element owner contract (``.default_timeout``, ``._dismissers``, ``.cursor``).
Both Page and Frame do; every command runs against the host's own session in its
own coordinate space, so the same code drives the top document or an OOPIF.
'''

from __future__ import annotations

import asyncio
import typing

from ..protocol import dom as dom_proto
from .element import Element
from .snapshot import Snapshot, _from_wire, build_from_wire, build_snapshot

#: AX roles for an <iframe> host node (whose subtree, for an OOPIF, lives in a
#: separate session and must be stitched in — see stitched_snapshot).
_IFRAME_ROLES = frozenset({'Iframe', 'iframe', 'IframePresentational'})


def _raw_command(method: str, params: dict | None = None):
    '''A CDP command whose result parse is the identity — yields the request
    and returns the raw result dict. Drives the normal execute path (routing,
    errors) but skips generated ``from_json``, so a caller can read the wire
    directly (see snapshot).'''
    result = yield {'method': method, 'params': params or {}}
    return result


async def snapshot(host: typing.Any, *, depth: int | None = None) -> Snapshot:
    params = {} if depth is None else {'depth': depth}
    # Read the AX nodes straight off the wire: build_from_wire needs only a few
    # fields, so parsing every node into a typed AXNode (then flattening it
    # right back) is pure overhead — sizeable on large trees.
    result = await host.session.execute(
        _raw_command('Accessibility.getFullAXTree', params))
    snap = build_from_wire(result.get('nodes', []))
    host._snapshot_refs = dict(snap.refs)
    host._snapshot_owners = {}  # single frame — every ref resolves on host
    return snap


def _iframe_sessions(connection: typing.Any) -> set:
    return {s for s in connection.sessions.values()
            if s.target_info and 'iframe' in (s.target_info.type or '')}


async def _settle_frames(connection: typing.Any, timeout: float = 0.5,
                         interval: float = 0.03) -> None:
    '''Wait until the attached-session count stops growing — nested OOPIFs
    attach a level at a time as auto-attach propagates (see Page._enable_frame_
    attach). Best-effort: a frame still in flight just misses this snapshot.'''
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    prev = -1
    while loop.time() < deadline:
        count = len(connection.sessions)
        if count == prev:
            return
        prev = count
        await asyncio.sleep(interval)


async def stitched_snapshot(page: typing.Any, *, depth: int | None = None) -> Snapshot:
    '''A cross-frame snapshot: the page's AX tree with every attached
    cross-origin iframe's tree spliced in under its <iframe> node — recursively,
    to any nesting depth. Refs are numbered across the whole thing and each
    remembers its frame, so element_for_ref/act route to the right session.'''
    from ..protocol import page as page_proto
    from .frame import Frame

    await page._enable_frame_attach()
    connection = page.session.connection
    await _settle_frames(connection)
    params = {} if depth is None else {'depth': depth}
    merged: list[dict] = []
    token_to_frame: dict[str, typing.Any] = {}
    counter = [0]
    remaining = _iframe_sessions(connection)

    async def frame_nodes(session, token):
        result = await session.execute(
            _raw_command('Accessibility.getFullAXTree', params))
        mapped, by_backend = [], {}
        for n in result.get('nodes', []):
            d = _from_wire(n)
            d['frame'] = token
            d['id'] = f'{token}:{d['id']}'
            d['children'] = [f'{token}:{c}' for c in d['children']]
            mapped.append(d)
            if d['backend'] is not None:
                by_backend[int(d['backend'])] = d
        return mapped, by_backend

    async def build(session, owner):
        token = str(counter[0])
        counter[0] += 1
        if owner is not page:
            token_to_frame[token] = owner
        mapped, by_backend = await frame_nodes(session, token)
        merged.extend(mapped)
        # claim the OOPIFs whose <iframe> host node lives in *this* frame's tree
        for s in list(remaining):
            if s not in remaining:
                continue
            try:
                backend, _ = await session.execute(dom_proto.get_frame_owner(
                    frame_id=page_proto.FrameId(str(s.target_id))))
            except Exception:
                continue  # frame isn't a descendant of this session's document
            host_node = by_backend.get(int(backend))
            if host_node is None:
                continue
            remaining.discard(s)
            child_roots = await build(s, Frame(page, s, str(s.target_id)))
            host_node['children'].extend(child_roots)
        referenced = {c for d in mapped for c in d['children']}
        roots = [d for d in mapped if d['id'] not in referenced]
        if owner is not page:  # flatten this frame's RootWebArea under its <iframe>
            for d in roots:
                d['ignored'] = True
        return [d['id'] for d in roots]

    await build(page.session, page)
    snap = build_snapshot(merged)
    page._snapshot_refs = dict(snap.refs)
    page._snapshot_owners = {ref: token_to_frame[tok]
                             for ref, tok in snap.frames.items()
                             if tok in token_to_frame}
    return snap


async def _await_frame_session(connection: typing.Any, frame_id: str | None,
                               url: str | None, timeout: float):
    '''Poll attached sessions for the OOPIF: by target id (== frameId for
    out-of-process frames) or by fnmatch on the frame URL. None on timeout.'''
    from fnmatch import fnmatch
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        for s in connection.sessions.values():
            if frame_id is not None and s.target_id == frame_id:
                return s
            info = s.target_info
            if (url is not None and info is not None
                    and 'iframe' in (info.type or '')
                    and fnmatch(info.url or '', url)):
                return s
        if loop.time() > deadline:
            return None
        await asyncio.sleep(0.03)


async def resolve_frame(host: typing.Any, page: typing.Any, selector: str | None,
                        url: str | None, timeout: float | None):
    '''Resolve a cross-origin child iframe of ``host`` (a Page or a Frame) to a
    Frame. Shared by Page.frame() and Frame.frame() — the only difference is
    which session the <iframe> is queried in (the host's).'''
    from .frame import Frame, FrameNotFound
    await page._enable_frame_attach()
    to = timeout if timeout is not None else host.default_timeout
    frame_id: str | None = None
    if selector is not None:
        iframe = await host.query(selector, timeout=to)
        frame_id = await iframe.frame_id()
        if frame_id is None:
            raise FrameNotFound(f'{selector!r} is not a frame element')
    elif url is None:
        raise ValueError('frame() needs selector= or url=')
    session = await _await_frame_session(
        page.session.connection, frame_id, url, to)
    if session is None:
        raise FrameNotFound(
            f'frame {selector or url!r} did not attach within {to}s — is it '
            'actually cross-origin (out-of-process)? same-origin iframes have '
            'no separate session')
    return Frame(page, session, frame_id or str(session.target_id))


async def element_for_ref(host: typing.Any, ref: str) -> Element:
    backend = host._snapshot_refs.get(ref)
    if backend is None:
        raise LookupError(
            f'unknown ref {ref!r}; call snapshot() first (known: '
            f'{len(host._snapshot_refs)} refs)')
    # a cross-frame ref resolves on its own frame's session (see stitched_snapshot)
    owner = host._snapshot_owners.get(ref, host)
    obj = await owner.session.execute(dom_proto.resolve_node(
        backend_node_id=dom_proto.BackendNodeId(backend)))
    if obj.object_id is None:
        raise LookupError(f'ref {ref!r} no longer resolves; re-snapshot()')
    return Element(owner, str(obj.object_id))


async def act(
    host: typing.Any,
    ref: str,
    action: str = 'click',
    *,
    text: str | None = None,
    values: typing.Sequence[str] | None = None,
    stable: bool = True,
    timeout: float | None = None,
) -> typing.Any:
    # validate the verb and its required args before any browser round-trip
    if action not in ('click', 'hover', 'focus', 'scroll',
                      'fill', 'type', 'select'):
        raise ValueError(
            f'unknown action {action!r}; expected click/hover/focus/'
            'scroll/fill/type/select')
    if action in ('fill', 'type') and text is None:
        raise ValueError(f'action {action!r} needs text=')
    if action == 'select' and values is None:
        raise ValueError("action 'select' needs values=")

    element = await host.element_for_ref(ref)
    if stable and action in ('click', 'hover', 'fill', 'type', 'select'):
        # pointer actions need the hit-test; value/keyboard writes don't
        await element.wait_actionable(
            hit=action in ('click', 'hover'), timeout=timeout)

    if action == 'click':
        await element.mouse_click()
    elif action == 'hover':
        await element.hover()
    elif action == 'focus':
        await element.focus()
    elif action == 'scroll':
        await element.scroll_into_view()
    elif action == 'fill':
        await element.set_value(text)
    elif action == 'type':
        await element.type(text)
    elif action == 'select':
        return await element.select_option(*values)
    return element
