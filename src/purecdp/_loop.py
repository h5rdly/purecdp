'''Event-loop backend: uvloop (POSIX) / winloop (Windows) when installed.

uvloop makes the asyncio plumbing ~1.4-1.7x faster — worthwhile for the reader
draining busy event streams and for command round-trip latency.

**Library boundary:** purecdp never installs a loop policy or hijacks the
caller's loop — as a library it must not. It only chooses the faster loop where
it *owns* one: the :func:`run` convenience (for scripts/drives) and the pytest
plugin's session loop. Everything else runs on whatever loop the caller
provides. A library user who wants uvloop everywhere just calls
``uvloop.install()`` (or ``asyncio.run(main(), loop_factory=...)``) themselves.
'''

from __future__ import annotations

import asyncio
import typing


def _pick_loop_factory() -> typing.Callable[[], asyncio.AbstractEventLoop]:
    try:
        import uvloop

        return uvloop.new_event_loop
    except ImportError:
        pass
    try:
        import winloop

        return winloop.new_event_loop
    except ImportError:
        pass
    return asyncio.new_event_loop


#: Event-loop factory: a fast loop if one is installed, else asyncio's.
new_event_loop = _pick_loop_factory()

#: True when a fast (uvloop/winloop) loop is in use.
FAST_LOOP = new_event_loop is not asyncio.new_event_loop

BACKEND = 'uvloop-or-winloop' if FAST_LOOP else 'asyncio'


def run(coro: typing.Coroutine, *, debug: bool | None = None) -> typing.Any:
    '''Like :func:`asyncio.run`, but on the fastest available loop.

    The entry point for standalone scripts/drives. Uses
    :class:`asyncio.Runner` so task cancellation and async-generator shutdown
    match ``asyncio.run`` exactly — only the loop factory differs.
    '''
    with asyncio.Runner(loop_factory=new_event_loop, debug=debug) as runner:
        return runner.run(coro)
