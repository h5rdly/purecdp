'''unittest integration: a base class that hands every test a live Page.

Stays true to the zero-dependency rule — this is stdlib unittest, and it is
the canonical way to write purecdp-powered tests. (The pytest plugin next
door is the optional alternative.)
'''

from __future__ import annotations

import os
import unittest
from contextlib import suppress

from ..browser import find_browser, launch
from .artifacts import _sanitize, artifacts_dir, dump_artifacts
from .page import Page


class CDPTestCase(unittest.IsolatedAsyncioTestCase):
    '''Each test gets a fresh browser, an isolated context, and self.page.

    Skips (not fails) when no Chromium-based browser binary is found.
    Class-level knobs: HEADLESS, PIPE, BROWSER_PATH, EXTRA_ARGS,
    DEFAULT_TIMEOUT; extra launch flags also come from $PURECDP_LAUNCH_ARGS.

    On failure/error the test's pages are dumped as diagnostic artifacts
    (screenshot, HTML, console, JS errors, recorded traffic — see
    :func:`~purecdp.testing.artifacts.dump_artifacts`) into
    ``ARTIFACTS_DIR/<test id>/`` before the browser closes. Green tests
    write nothing. Knobs: ARTIFACTS (default on; $PURECDP_NO_ARTIFACTS=1
    forces off), ARTIFACTS_DIR (default $PURECDP_ARTIFACTS_DIR or
    ./purecdp-artifacts), ARTIFACTS_NETWORK (also arm a NetworkRecorder on
    every page so failures include traffic; off by default — but traffic
    your test already records via ``page.record()`` is dumped regardless).
    '''

    HEADLESS = True
    PIPE = False
    BROWSER_PATH: str | None = None
    EXTRA_ARGS: tuple[str, ...] = ()
    DEFAULT_TIMEOUT = 10.0
    ARTIFACTS = True
    ARTIFACTS_DIR: str | None = None
    ARTIFACTS_NETWORK = False

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        path = self.BROWSER_PATH or find_browser()
        if path is None:
            self.skipTest('no Chromium-based browser found')
        env_args = tuple(os.environ.get('PURECDP_LAUNCH_ARGS', '').split())
        self.browser = await launch(
            path,
            headless=self.HEADLESS,
            pipe=self.PIPE,
            extra_args=(*self.EXTRA_ARGS, *env_args),
        )
        self.context = await self.browser.new_context()
        self.page = await self.new_page()

    async def asyncTearDown(self) -> None:
        exc = getattr(self, '_artifact_exc', None)
        if exc is not None and self._artifacts_on():
            # capture must never mask the real failure with a teardown error
            with suppress(BaseException):
                await dump_artifacts(
                    getattr(self, '_pages', ()),
                    os.path.join(artifacts_dir(self.ARTIFACTS_DIR),
                                 _sanitize(self.id())),
                    test_id=self.id(), exc=exc)
        browser = getattr(self, 'browser', None)
        if browser is not None:
            await browser.aclose()
        await super().asyncTearDown()

    async def new_page(self) -> Page:
        '''An additional Page in the same isolated context.'''
        session = await self.browser.new_page(context=self.context)
        page = await Page.create(session, default_timeout=self.DEFAULT_TIMEOUT)
        if self.ARTIFACTS_NETWORK and self._artifacts_on():
            page.record()
        if not hasattr(self, '_pages'):
            self._pages: list[Page] = []
        self._pages.append(page)
        return page

    def _artifacts_on(self) -> bool:
        return self.ARTIFACTS and not os.environ.get('PURECDP_NO_ARTIFACTS')

    def _callTestMethod(self, method) -> None:
        # remember the test body's exception so asyncTearDown can dump
        # artifacts while the browser is still alive; unittest's own outcome
        # bookkeeping isn't readable from inside tearDown (and differs under
        # pytest), so capture at the source and re-raise unchanged.
        try:
            super()._callTestMethod(method)
        except (unittest.SkipTest, KeyboardInterrupt):
            raise
        except BaseException as exc:
            if not getattr(getattr(self, '_outcome', None),
                           'expecting_failure', False):
                self._artifact_exc = exc
            raise
