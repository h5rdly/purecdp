'''Optional pytest plugin — pytest is NOT a purecdp dependency.

This module is only imported when pytest loads it: automatically via the
``pytest11`` entry point once purecdp is pip-installed, or explicitly with
``pytest -p purecdp.testing.pytest_plugin`` (or
``pytest_plugins = ["purecdp.testing.pytest_plugin"]`` in conftest.py).

Fixtures:
- ``cdp_browser`` (session-scoped): one launched browser for the whole run
- ``cdp_page`` (function-scoped): a fresh Page in a fresh isolated
  BrowserContext per test — cheap isolation, no browser-per-test cost

``async def`` tests that use these fixtures run on the plugin's session
event loop; no pytest-asyncio required. Extra launch flags come from
$PURECDP_LAUNCH_ARGS.
'''

from __future__ import annotations

import asyncio
import inspect
import os

import pytest

from .. import _loop
from ..browser import find_browser, launch
from .page import Page

_CDP_FIXTURES = {'cdp_loop', 'cdp_browser', 'cdp_page'}


@pytest.fixture(scope='session')
def cdp_loop():
    # purecdp owns this loop, so it may pick the fast one (uvloop/winloop)
    loop = _loop.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope='session')
def cdp_browser(cdp_loop):
    if find_browser() is None:
        pytest.skip('no Chromium-based browser found')
    args = tuple(os.environ.get('PURECDP_LAUNCH_ARGS', '').split())
    browser = cdp_loop.run_until_complete(launch(extra_args=args))
    yield browser
    cdp_loop.run_until_complete(browser.aclose())


@pytest.fixture
def cdp_page(cdp_browser, cdp_loop):
    async def make():
        context = await cdp_browser.new_context()
        session = await cdp_browser.new_page(context=context)
        return context, await Page.create(session)

    context, page = cdp_loop.run_until_complete(make())
    yield page

    async def cleanup():
        await page.aclose()
        await context.aclose()

    cdp_loop.run_until_complete(cleanup())


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    '''Run async tests that use our fixtures on the shared session loop.'''
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    if not (_CDP_FIXTURES & set(pyfuncitem.fixturenames)):
        return None
    loop = pyfuncitem._request.getfixturevalue('cdp_loop')
    kwargs = {name: pyfuncitem.funcargs[name]
              for name in pyfuncitem._fixtureinfo.argnames}
    loop.run_until_complete(pyfuncitem.obj(**kwargs))
    return True
