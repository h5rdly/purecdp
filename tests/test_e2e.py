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


if __name__ == '__main__':
    unittest.main()
