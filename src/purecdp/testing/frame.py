'''Frame: drive a single cross-origin (out-of-process) iframe (M10 Phase 1).

A cross-origin iframe runs in its own renderer with its own CDP session, so the
page's JS can't reach into it. ``page.frame(...)`` correlates the <iframe> to
that session and hands back a :class:`Frame` — an Element *owner* just like Page,
but whose ``session`` is the iframe's. Everything (query/evaluate/snapshot/act,
and trusted Input) therefore runs against the frame's own session in the frame's
own coordinate space, exactly as a Page drives the top document; Chromium routes
Input dispatched on an OOPIF session into that frame. No coordinate translation
is involved — which is also why nested frames will "just work" per session.

Scope (Phase 1): cross-origin iframes only. Same-origin frames are reachable
from the page's own JS already (they share the renderer) and are out of scope
here. See ROADMAP M10 for stitched cross-frame snapshots (Phase 3).
'''

from __future__ import annotations

import asyncio
import typing

from ..errors import PureCDPError
from ..protocol import runtime as runtime_proto
from . import _agent
from .element import Element, ElementQueries
from .live import LiveFactories
from .snapshot import Snapshot

if typing.TYPE_CHECKING:
    from .connection import Session
    from .page import Page


class FrameNotFound(PureCDPError):
    '''No cross-origin iframe matched, or it never attached in time.'''


class Frame(ElementQueries, LiveFactories):
    '''A cross-origin child frame, driven through its own CDP session. Created
    by :meth:`Page.frame`; exposes the same element/agent surface as Page, scoped
    to the frame. Invalidated when the iframe navigates/detaches (re-acquire via
    ``page.frame(...)``).'''

    _where = ' in frame'         # seasons ElementQueries timeout messages

    def __init__(self, page: Page, session: Session, frame_id: str):
        self.page = page                 # the Page this iframe is embedded in
        self.session = session           # the frame's own (OOPIF) CDP session
        self.frame_id = frame_id
        self.default_timeout = page.default_timeout
        self._dismissers = page._dismissers      # shared with the page
        self._snapshot_refs: dict[str, int] = {}
        self._snapshot_owners: dict[str, typing.Any] = {}

    def __repr__(self) -> str:
        return f'Frame({self.frame_id!r})'

    @property
    def cursor(self):
        raise NotImplementedError(
            "human cursor motion inside a frame isn't supported yet "
            '(M10 Phase 1); use mouse_click/act')

    # -- scripting & queries (mirror Page, scoped to this frame's session) ----

    async def evaluate(
        self,
        expression: str,
        *,
        await_promise: bool = True,
        return_by_value: bool = True,
        user_gesture: bool = False,
        timeout: float | None = None,
    ) -> typing.Any:
        '''Evaluate JS in the frame's context; raises JSError on exceptions.'''
        from .page import JSError  # local: page imports this module lazily
        async with asyncio.timeout(timeout or self.default_timeout):
            result, details = await self.session.execute(runtime_proto.evaluate(
                expression=expression,
                return_by_value=return_by_value,
                await_promise=await_promise,
                user_gesture=True if user_gesture else None,
            ))
        if details is not None:
            raise JSError.from_details(details)
        return result.value if return_by_value else result

    # query/query_count/query_all and live()/get_by_* are inherited —
    # ElementQueries (element.py) and LiveFactories (live.py), shared with Page.

    async def frame(
        self,
        selector: str | None = None,
        *,
        url: str | None = None,
        timeout: float | None = None,
    ) -> Frame:
        '''Attach to a cross-origin iframe nested *inside this frame* (M10 Phase
        4). Same contract as :meth:`Page.frame`; auto-attach has already
        propagated down to this frame's children (see Page._enable_frame_attach),
        so nesting works to any depth.'''
        return await _agent.resolve_frame(self, self.page, selector, url, timeout)

    # -- agent surface (shared with Page, scoped to this frame) ---------------

    async def snapshot(self, *, depth: int | None = None) -> Snapshot:
        '''Ref-tagged accessibility outline of *this frame* (refs are
        frame-local; a stitched page-wide snapshot is M10 Phase 3).'''
        return await _agent.snapshot(self, depth=depth)

    async def element_for_ref(self, ref: str) -> Element:
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
        '''Act on a frame-local snapshot ref (see Page.act).'''
        return await _agent.act(self, ref, action, text=text, values=values,
                                stable=stable, timeout=timeout)
