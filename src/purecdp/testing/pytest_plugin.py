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
import contextlib
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
def cdp_page(cdp_browser, cdp_loop, request):
    async def make():
        context = await cdp_browser.new_context()
        session = await cdp_browser.new_page(context=context)
        return context, await Page.create(session)

    context, page = cdp_loop.run_until_complete(make())
    artifacts_on = not os.environ.get('PURECDP_NO_ARTIFACTS')
    if artifacts_on and os.environ.get('PURECDP_ARTIFACTS_NETWORK'):
        page.record()
    yield page

    # dump artifacts while the page is still alive if the test failed
    # (pytest_runtest_makereport below stashed the call-phase report)
    report = getattr(request.node, '_purecdp_report_call', None)
    if artifacts_on and report is not None and report.failed:
        from .artifacts import _sanitize, artifacts_dir, dump_artifacts
        dest = os.path.join(artifacts_dir(), _sanitize(request.node.nodeid))
        with contextlib.suppress(BaseException):
            cdp_loop.run_until_complete(dump_artifacts(
                [page], dest, test_id=request.node.nodeid,
                exc=report.longreprtext))

    async def cleanup():
        await page.aclose()
        await context.aclose()

    cdp_loop.run_until_complete(cleanup())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    '''Stash each phase's report on the item so fixtures can see the test's
    outcome during their teardown (used for artifacts-on-failure).'''
    outcome = yield
    report = outcome.get_result()
    setattr(item, '_purecdp_report_' + report.when, report)


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
