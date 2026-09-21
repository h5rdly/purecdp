'''End-to-end smoke tests against a real Chromium-based browser.

Skipped cleanly when no browser binary is available (see purecdp.browser
CANDIDATES / $CDP_BROWSER). Extra launch flags can be injected with
$PURECDP_E2E_ARGS (space-separated), e.g. sandboxed CI environments may need
"--no-sandbox".

Runnable three ways: `python -m unittest`, `python tests/test_e2e.py`,
or `python -m pytest` — none require pytest. Plain asserts: don't run with -O.
'''

import asyncio
import os
import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import purecdp  # noqa: E402
from purecdp.protocol import browser as browser_domain  # noqa: E402
from purecdp.protocol import network, page, runtime, storage, target  # noqa: E402

BROWSER = purecdp.find_browser()
EXTRA_ARGS = tuple(os.environ.get('PURECDP_E2E_ARGS', '').split())

# Browser e2e sporadically flakes on the hosted runners — a dropped transport
# ("transport closed by peer") or a browser that stalls into a timeout, seen on
# the macOS/Windows legs. Retry those tests once. A genuine failure still errors on 
# the final attempt (so a real break isn't masked).
_FLAKY = (purecdp.CDPConnectionClosed, purecdp.CDPTransportError, TimeoutError)


def retry_flaky(times=2):
    '''Decorator: re-run an async browser-e2e test on transient flakiness.'''
    def decorate(fn):
        async def wrapper(self, *args, **kwargs):
            for attempt in range(times):
                try:
                    return await fn(self, *args, **kwargs)
                except _FLAKY:
                    if attempt == times - 1:
                        raise
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return decorate


@unittest.skipUnless(BROWSER, 'no Chromium-based browser found')
class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    @retry_flaky()
    async def test_websocket_evaluate_and_navigate(self):
        async with asyncio.timeout(60):
            async with await purecdp.launch(extra_args=EXTRA_ARGS) as browser:
                session = await browser.new_session()
                assert session.target_id is not None

                # evaluate
                result, exception_details = await session.execute(
                    runtime.evaluate(expression='6 * 7', return_by_value=True))
                assert exception_details is None
                assert result.value == 42

                # navigate + load event, subscribe-before-trigger
                await session.execute(page.enable())
                waiter = asyncio.create_task(
                    session.wait_for(page.LoadEventFired, timeout=30))
                await asyncio.sleep(0)
                await session.execute(page.navigate(
                    url='data:text/html,<title>purecdp</title>ok'))
                await waiter
                title, *_ = await session.execute(
                    runtime.evaluate(expression='document.title',
                                     return_by_value=True))
                assert title.value == 'purecdp'

    @unittest.skipIf(sys.platform == 'win32', 'pipe transport is POSIX-only')
    @retry_flaky()
    async def test_pipe_evaluate(self):
        async with asyncio.timeout(60):
            async with await purecdp.launch(pipe=True, extra_args=EXTRA_ARGS) as browser:
                session = await browser.new_session()
                result, exception_details = await session.execute(
                    runtime.evaluate(expression='1 + 1', return_by_value=True))
                assert exception_details is None
                assert result.value == 2

    @retry_flaky()
    async def test_contexts_isolation_and_close(self):
        async with asyncio.timeout(60):
            async with await purecdp.launch(extra_args=EXTRA_ARGS) as browser:
                conn = browser.connection
                async with await browser.new_context() as ctx_a:
                    async with await browser.new_context() as ctx_b:
                        page_a = await browser.new_session(context=ctx_a)
                        page_b = await browser.new_session(context=ctx_b)
                        # cookie isolation between contexts (Storage domain is
                        # browser-level and context-scoped)
                        await conn.execute(storage.set_cookies(
                            [network.CookieParam(name='iso', value='A',
                                                 url='https://example.com')],
                            browser_context_id=browser_domain.BrowserContextID(
                                ctx_a.context_id)))
                        in_a = await conn.execute(storage.get_cookies(
                            browser_context_id=browser_domain.BrowserContextID(
                                ctx_a.context_id)))
                        in_b = await conn.execute(storage.get_cookies(
                            browser_context_id=browser_domain.BrowserContextID(
                                ctx_b.context_id)))
                        assert any(c.name == 'iso' for c in in_a)
                        assert not any(c.name == 'iso' for c in in_b)
                        # closing one page leaves the rest healthy
                        await browser.close_page(page_a)
                        while not page_a.closed:
                            await asyncio.sleep(0.01)
                        ok, *_ = await page_b.execute(runtime.evaluate(
                            expression='2 + 2', return_by_value=True))
                        assert ok.value == 4

    # window.open under auto-attach fails ONLY on the GitHub-HOSTED runners:
    # Windows drops the connection ("transport closed by peer"), macOS times out
    # (AttachedToTarget never arrives). This is NOT a purecdp bug and NOT
    # Windows/Chrome-for-Testing in general — reproduced as fully WORKING on a
    # real Win10 VM with the same CfT browser (tests/windebug/windows_gauntlet.py
    # → RESULT: SUCCESS), so it's specific to the hosted-runner environment. The
    # mechanism is also gated on Linux/FreeBSD + the FakeBrowser test_lifecycle
    # tests. Skip on the two hosted runners; Linux is the real gate.
    @unittest.skipIf(sys.platform in ('win32', 'darwin'),
                     'window.open popup fails only on hosted Windows/macOS runners (works on a real VM)')
    @retry_flaky()
    async def test_auto_attach_resumes_popup(self):
        async with asyncio.timeout(60):
            async with await purecdp.launch(extra_args=EXTRA_ARGS) as browser:
                conn = browser.connection
                opener = await browser.new_session('data:text/html,opener')
                await conn.set_auto_attach(wait_for_debugger=True)

                waiter = asyncio.create_task(conn.wait_for(
                    target.AttachedToTarget,
                    predicate=lambda e: str(e.target_info.opener_id or '')
                    == opener.target_id,
                    timeout=30))
                await asyncio.sleep(0)
                await opener.execute(runtime.evaluate(
                    expression="window.open('about:blank')", user_gesture=True))
                attached = await waiter

                popup = conn.sessions[str(attached.session_id)]
                assert popup.target_id == str(attached.target_info.target_id)
                # the popup attached paused; auto-resume must have run it,
                # or this evaluate would hang
                answer, *_ = await popup.execute(runtime.evaluate(
                    expression='7 * 6', return_by_value=True))
                assert answer.value == 42

    @retry_flaky()
    async def test_locked_profile_diagnosed_then_reusable(self):
        '''A second launch into a live profile must say "in use by PID N" —
        not die with ConnectionRefused on a stale DevToolsActivePort — and
        after the first browser closes, the same profile must launch again.'''
        import tempfile

        async with asyncio.timeout(120):
            with tempfile.TemporaryDirectory() as profile:
                async with await purecdp.launch(
                        user_data_dir=profile, extra_args=EXTRA_ARGS) as first:
                    assert first.process is not None
                    try:
                        await purecdp.launch(user_data_dir=profile,
                                             extra_args=EXTRA_ARGS)
                    except purecdp.BrowserLaunchError as exc:
                        if sys.platform != 'win32':  # no SingletonLock there
                            assert (f'in use by PID {first.process.pid}'
                                    in str(exc))
                    else:
                        raise AssertionError(
                            'second launch into a locked profile succeeded')
                # the dir still holds last run's DevToolsActivePort remnants;
                # relaunching proves the stale-port clearing works
                async with await purecdp.launch(
                        user_data_dir=profile, extra_args=EXTRA_ARGS) as again:
                    session = await again.new_session()
                    assert session.target_id is not None

    @retry_flaky()
    async def test_aclose_is_graceful_and_releases_the_profile(self):
        '''aclose() must end with the browser exiting by itself on
        Browser.close — exit code 0, SingletonLock gone — not by SIGTERM.'''
        import tempfile
        from purecdp.browser import _profile_lock_holder

        async with asyncio.timeout(60):
            with tempfile.TemporaryDirectory() as profile:
                browser = await purecdp.launch(user_data_dir=profile,
                                               extra_args=EXTRA_ARGS)
                await browser.new_session()
                await browser.aclose()
                assert browser.process.returncode == 0
                assert _profile_lock_holder(profile) is None

    @retry_flaky()
    async def test_no_arg_attach_finds_the_realized_tab(self):
        '''attach() with no selector = "the realized page tab".'''
        from purecdp.protocol import target as _target

        async with asyncio.timeout(60):
            async with await purecdp.launch(extra_args=EXTRA_ARGS) as browser:
                # Make ours the only page target. Create it FIRST: the CI
                # runners' Chrome opens a fresh about:blank whenever a
                # createTarget happens with no window alive, so closing
                # everything and then creating leaves two tabs. And
                # closeTarget acknowledges before the tab leaves getTargets,
                # so wait until the listing agrees.
                session = await browser.new_session(
                    'data:text/html,<title>only-me</title>')

                async def page_targets():
                    infos = await browser.connection.execute(
                        _target.get_targets())
                    return [str(i.target_id) for i in infos if i.type == 'page']

                for target_id in await page_targets():
                    if target_id != session.target_id:
                        await browser.close_target(target_id)
                while await page_targets() != [session.target_id]:
                    await asyncio.sleep(0.05)
                page = await browser.attach()
                assert page.session.target_id == session.target_id
                await page.stop()


if __name__ == '__main__':
    unittest.main()
