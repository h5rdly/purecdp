'''unittest integration: a base class that hands every test a live Page.

Stays true to the zero-dependency rule — this is stdlib unittest, and it is
the canonical way to write purecdp-powered tests. (The pytest plugin next
door is the optional alternative.)
'''

from __future__ import annotations

import asyncio
import os
import unittest
from contextlib import suppress

from ..browser import find_browser, find_firefox, launch
from ..errors import PureCDPError
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
    #: ``'chromium'`` (CDP, default) or ``'firefox'`` (WebDriver BiDi,
    #: experimental); None reads $PURECDP_E2E_BROWSER. On Firefox the
    #: Chromium-specific EXTRA_ARGS / $PURECDP_LAUNCH_ARGS are NOT passed —
    #: use FIREFOX_ARGS / $PURECDP_FIREFOX_ARGS.
    BROWSER_KIND: str | None = None
    FIREFOX_ARGS: tuple[str, ...] = ()
    DEFAULT_TIMEOUT = 10.0
    ARTIFACTS = True
    ARTIFACTS_DIR: str | None = None
    ARTIFACTS_NETWORK = False

    #: Bound on bringing a browser up for a test (launch + context + page) and
    #: how many extra attempts a hung or failed start gets. Hosted CI runners
    #: occasionally start a Chrome that never answers one early command; with
    #: no bound that is a watchdog-killed run, with this it is one retried
    #: setup — and, if it fails twice, an error naming the step.
    SETUP_TIMEOUT = 60.0
    SETUP_RETRIES = 1

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        last: BaseException | None = None
        for attempt in range(self.SETUP_RETRIES + 1):
            self._setup_step = 'launch'
            self._pages = []          # a page from a failed attempt is gone with its browser
            try:
                async with asyncio.timeout(self.SETUP_TIMEOUT):
                    await self._bring_up_browser()
                return
            except (TimeoutError, PureCDPError) as exc:
                last = exc
                step = self._setup_step
                browser = getattr(self, 'browser', None)
                self.browser = None
                if browser is not None:
                    with suppress(BaseException):
                        await asyncio.wait_for(browser.aclose(), 15)
                if attempt < self.SETUP_RETRIES:
                    continue
                kind = type(exc).__name__
                detail = str(exc) or f'no reply within {self.SETUP_TIMEOUT:g}s'
                raise AssertionError(
                    f'browser setup failed {attempt + 1}× at step {step!r}: '
                    f'{kind}: {detail}') from last

    async def _bring_up_browser(self) -> None:
        '''launch → isolated context → first page (``_setup_step`` tracks it).'''
        kind = self.BROWSER_KIND or os.environ.get('PURECDP_E2E_BROWSER') or 'chromium'
        if kind == 'firefox':
            path = self.BROWSER_PATH or find_firefox()
            if path is None:
                self.skipTest('no Firefox binary found')
            env_args = tuple(os.environ.get('PURECDP_FIREFOX_ARGS', '').split())
            self.browser = await launch(
                path, browser='firefox', headless=self.HEADLESS,
                extra_args=(*self.FIREFOX_ARGS, *env_args))
        else:
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
        # while setting up, every command is bounded so a browser that never
        # answers names the command instead of hanging; tests run unbounded
        link = self.browser.connection if self.browser.connection is not None else self.browser.bidi
        link.default_command_timeout = self.SETUP_TIMEOUT / 2
        try:
            self._setup_step = 'new_context'
            self.context = await self.browser.new_context()
            self._setup_step = 'new_page'
            self.page = await self.new_page()
        finally:
            link.default_command_timeout = None

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
        page = await self.context.new_page(default_timeout=self.DEFAULT_TIMEOUT)
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
