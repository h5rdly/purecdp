'''Behavioral realism (M8 Track B): human-like mouse, typing, scroll.

Stealth (Track A) changes what the browser *reports*; this changes how the
input *moves* — the more durable signal, since detectors increasingly score
cursor curvature, keystroke cadence, and scroll dynamics rather than just
querying navigator. All of it rides the **trusted** Input domain (real
`isTrusted` events), so it's indistinguishable from user input at the event
level, with curved paths / eased timing on top.

Deterministic and fast when you need it: every generator takes a seedable
``random.Random`` and the delays scale to zero, so tests assert exact paths and
run instantly.

Honest framing (as everywhere in stealth): this defeats naive
"straight-line / instant-type" heuristics, not a determined behavioral model.
'''

from __future__ import annotations

import asyncio
import math
import random
import typing

from ..protocol import input as input_proto

if typing.TYPE_CHECKING:
    from .element import Element
    from .page import Page


def _cubic_bezier(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    u = 1 - t
    return (u * u * u * p0 + 3 * u * u * t * p1
            + 3 * u * t * t * p2 + t * t * t * p3)


class HumanCursor:
    '''A cursor with position state, moving in curved, eased paths.

    Held by ``page.cursor``. Position persists across moves so successive
    actions start where the last one ended, like a real pointer.
    '''

    def __init__(
        self,
        page: Page,
        *,
        rng: random.Random | None = None,
        start: tuple[float, float] = (0.0, 0.0),
    ):
        self.page = page
        self.x, self.y = start
        self._rng = rng or random.Random()

    def _path(
        self, x1: float, y1: float, steps: int, curve: float, jitter: float
    ) -> list[tuple[float, float]]:
        x0, y0 = self.x, self.y
        dx, dy = x1 - x0, y1 - y0
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return [(x1, y1)]
        # control points offset perpendicular to the straight line, so the
        # path bows out like a hand movement rather than a ruler
        nx, ny = -dy / dist, dx / dist
        amp = curve * dist * 0.15
        o1 = self._rng.uniform(-amp, amp)
        o2 = self._rng.uniform(-amp, amp)
        cx1, cy1 = x0 + dx / 3 + nx * o1, y0 + dy / 3 + ny * o1
        cx2, cy2 = x0 + 2 * dx / 3 + nx * o2, y0 + 2 * dy / 3 + ny * o2
        pts: list[tuple[float, float]] = []
        for i in range(1, steps + 1):
            t = i / steps
            te = t * t * (3 - 2 * t)  # ease in-out: accelerate then decelerate
            px = _cubic_bezier(x0, cx1, cx2, x1, te)
            py = _cubic_bezier(y0, cy1, cy2, y1, te)
            if i < steps:  # tiny tremor, but land exactly on target
                px += self._rng.uniform(-jitter, jitter)
                py += self._rng.uniform(-jitter, jitter)
            pts.append((px, py))
        pts[-1] = (x1, y1)
        return pts

    async def move_to(
        self,
        x: float,
        y: float,
        *,
        steps: int | None = None,
        curve: float = 1.0,
        jitter: float = 1.5,
        min_delay: float = 0.004,
        max_delay: float = 0.012,
    ) -> None:
        '''Move the cursor to (x, y) along a curved, eased path of trusted
        mouseMoved events. ``steps`` defaults to a distance-based count.'''
        if steps is None:
            dist = math.hypot(x - self.x, y - self.y)
            steps = max(6, min(60, int(dist / 8)))
        for px, py in self._path(x, y, steps, curve, jitter):
            await self.page.session.execute(input_proto.dispatch_mouse_event(
                type='mouseMoved', x=px, y=py))
            if max_delay > 0:
                await asyncio.sleep(self._rng.uniform(min_delay, max_delay))
        self.x, self.y = x, y

    async def click(
        self,
        element: Element | None = None,
        *,
        x: float | None = None,
        y: float | None = None,
        **move_kwargs: typing.Any,
    ) -> None:
        '''Move to an element (or explicit x/y) along a human path, then press
        and release — all trusted Input events.'''
        if element is not None:
            box = await element.bounding_box()
            if box is None:
                raise LookupError('element has no visible box to click')
            # aim for a random point near the center, not the exact pixel
            x = box['x'] + box['width'] * self._rng.uniform(0.35, 0.65)
            y = box['y'] + box['height'] * self._rng.uniform(0.35, 0.65)
        if x is None or y is None:
            raise ValueError('click needs an element or explicit x and y')
        await self.move_to(x, y, **move_kwargs)
        for kind in ('mousePressed', 'mouseReleased'):
            await self.page.session.execute(input_proto.dispatch_mouse_event(
                type=kind, x=self.x, y=self.y,
                button=input_proto.MouseButton.LEFT, click_count=1))


async def human_type(
    page: Page,
    text: str,
    *,
    wpm: float = 220,
    variance: float = 0.45,
    mistake_pause: float = 0.05,
    rng: random.Random | None = None,
) -> None:
    '''Type ``text`` character-by-character with real key events and
    human-cadence gaps (log-ish jitter around ``wpm``, occasional longer
    pauses). Unlike ``insert_text`` this fires per-key keydown/keyup, so key
    handlers and cadence detectors see a real typist. Submit with
    ``page.press('Enter')``.'''
    rng = rng or random.Random()
    base = 60.0 / (wpm * 5)  # seconds/char at wpm (≈5 chars/word)
    for ch in text:
        await page.session.execute(input_proto.dispatch_key_event(
            type='keyDown', text=ch, key=ch))
        await page.session.execute(input_proto.dispatch_key_event(
            type='keyUp', key=ch))
        delay = base * (1 + rng.uniform(-variance, variance))
        if rng.random() < mistake_pause:  # a brief "thinking" pause
            delay += rng.uniform(0.15, 0.4)
        if delay > 0:
            await asyncio.sleep(max(0.0, delay))


async def human_scroll(
    page: Page,
    delta_y: float,
    *,
    x: float | None = None,
    y: float | None = None,
    steps: int | None = None,
    min_delay: float = 0.01,
    max_delay: float = 0.03,
    rng: random.Random | None = None,
) -> None:
    '''Scroll by ``delta_y`` pixels (positive = down) in eased wheel steps
    rather than one flat jump. Uses the cursor's current position unless x/y
    given.'''
    rng = rng or random.Random()
    cursor = page.cursor
    px = cursor.x if x is None else x
    py = cursor.y if y is None else y
    if steps is None:
        steps = max(4, min(40, int(abs(delta_y) / 40)))
    # ease-out weights: larger deltas first, tapering to a gentle stop
    weights = [math.sin((i + 1) / steps * math.pi / 2) for i in range(steps)]
    total = sum(weights)
    for w in weights:
        await page.session.execute(input_proto.dispatch_mouse_event(
            type='mouseWheel', x=px, y=py, delta_x=0, delta_y=delta_y * w / total))
        if max_delay > 0:
            await asyncio.sleep(rng.uniform(min_delay, max_delay))
