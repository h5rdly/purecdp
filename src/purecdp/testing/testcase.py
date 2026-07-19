'''unittest integration: a base class that hands every test a live Page.

Stays true to the zero-dependency rule — this is stdlib unittest, and it is
the canonical way to write purecdp-powered tests. (The pytest plugin next
door is the optional alternative.)
'''

from __future__ import annotations

import os
import unittest

from ..browser import find_browser, launch
from .page import Page


class CDPTestCase(unittest.IsolatedAsyncioTestCase):
    '''Each test gets a fresh browser, an isolated context, and self.page.

    Skips (not fails) when no Chromium-based browser binary is found.
    Class-level knobs: HEADLESS, PIPE, BROWSER_PATH, EXTRA_ARGS,
    DEFAULT_TIMEOUT; extra launch flags also come from $PURECDP_LAUNCH_ARGS.
    '''

    HEADLESS = True
    PIPE = False
    BROWSER_PATH: str | None = None
    EXTRA_ARGS: tuple[str, ...] = ()
    DEFAULT_TIMEOUT = 10.0

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
        browser = getattr(self, 'browser', None)
        if browser is not None:
            await browser.aclose()
        await super().asyncTearDown()

    async def new_page(self) -> Page:
        '''An additional Page in the same isolated context.'''
        session = await self.browser.new_page(context=self.context)
        return await Page.create(session, default_timeout=self.DEFAULT_TIMEOUT)
