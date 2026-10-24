'''End-to-end against a real Firefox over WebDriver BiDi (M20 phase 1).

Skipped cleanly when no Firefox binary is available (find_firefox /
$FIREFOX_BROWSER). Plain asserts: don't run with -O.
'''

import asyncio
import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import purecdp  # noqa: E402
from purecdp.testing import Handle, JSError, NotSupported  # noqa: E402

FIREFOX = purecdp.find_firefox()


@unittest.skipUnless(FIREFOX, 'no Firefox binary found')
class FirefoxE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_launch_evaluate_navigate_close(self):
        async with asyncio.timeout(90):
            browser = await purecdp.launch(browser='firefox')
            try:
                assert browser.kind == 'firefox'
                assert browser.capabilities['browserName'] == 'firefox'
                page = await browser.new_page()
                assert page.backend == 'bidi' and page.session is None
                # scripting: by value, with args, by handle, exceptions
                assert await page.evaluate('6 * 7') == 42
                assert await page.evaluate('(a, b) => a + b', 2, 3) == 5
                assert await page.evaluate('({a: [1, "x", null], b: true})') == {
                    'a': [1, 'x', None], 'b': True}
                body = await page.evaluate('document.body', return_by_value=False)
                assert isinstance(body, Handle) and body.node_id is not None
                assert await page.evaluate('(el) => el.tagName', body) == 'BODY'
                try:
                    await page.evaluate('nope.nope')
                except JSError as exc:
                    assert 'nope' in str(exc)
                else:
                    raise AssertionError('expected JSError')
                # navigation + load wait + console capture
                await page.goto('data:text/html,<title>ff</title>'
                                '<script>console.log("hi", 1); throw new Error("boom")</script>'
                                '<ul><li>a</li><li>b</li></ul>')
                assert await page.title() == 'ff'
                items = await page.query_all('li')
                assert [await i.text() for i in items] == ['a', 'b']
                el = await page.query('li', containing='b')
                assert await el.text() == 'b'
                for _ in range(40):
                    if page.console and page.js_errors:
                        break
                    await asyncio.sleep(0.05)
                assert any('hi 1' in m.text for m in page.console), page.console
                assert any('boom' in str(e) for e in page.js_errors), page.js_errors
                # screenshot is real PNG bytes
                png = await page.screenshot()
                assert png[:8] == b'\x89PNG\r\n\x1a\n'
                # a feature without a mapping says so instead of misbehaving
                try:
                    await page.emulate_media(color_scheme='dark')
                except NotSupported as exc:
                    assert exc.backend == 'bidi' and exc.feature == 'media emulation'
                else:
                    raise AssertionError('expected NotSupported')
                await page.close()
            finally:
                await browser.aclose()
            assert browser.process.returncode == 0

    async def test_user_context_and_attach(self):
        async with asyncio.timeout(60):
            async with await purecdp.launch(browser='firefox') as browser:
                async with await browser.new_context() as ctx:
                    page = await ctx.new_page('data:text/html,<title>ctx</title>')
                    await page.goto('data:text/html,<title>ctx</title>')
                    assert await page.title() == 'ctx'
                    again = await browser.attach(url_contains='title>ctx')
                    assert again._driver.context == page._driver.context
                    await again.stop()
                    await page.close()


if __name__ == '__main__':
    unittest.main()


# -- the Chromium e2e classes, re-run on Firefox (M20 phase 2) ---------------
# Each subclass names the tests that need a CDP-only feature and why; anything
# else must pass unchanged. The base classes are reached through the module
# object so unittest discovery here never re-runs the Chromium versions.

import importlib  # noqa: E402

try:
    _e2e = importlib.import_module('tests.test_testing_e2e')
except ImportError:  # run as a script / from inside tests/
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    _e2e = importlib.import_module('test_testing_e2e')


def on_firefox(base, skip: dict | None = None):
    skip = skip or {}

    class OnFirefox(base):
        BROWSER_KIND = 'firefox'
        CDP_ONLY = skip

        async def asyncSetUp(self):
            reason = self.CDP_ONLY.get(self._testMethodName)
            if reason:
                self.skipTest(f'CDP only: {reason}')
            await super().asyncSetUp()

    OnFirefox.__name__ = OnFirefox.__qualname__ = 'Firefox' + base.__name__
    OnFirefox.__module__ = __name__
    return OnFirefox


SESSION = 'uses page.session / CDP target ids'
CHROME_ERROR = "Chrome's net:: error vocabulary"

INSERT_TEXT = 'insert=True has no BiDi twin: typed as key events, so key listeners DO fire'

FirefoxPageE2ETests = on_firefox(_e2e.PageE2ETests, {
    'test_goto_failure_carries_net_error': CHROME_ERROR,
    'test_type_fires_real_key_events': INSERT_TEXT,
})
FirefoxDriveParityE2ETests = on_firefox(_e2e.DriveParityE2ETests, {
    'test_close_target_by_id': SESSION,
    'test_alive_flips_when_the_tab_closes': SESSION,
    'test_attach_to_an_existing_tab': SESSION,
})
FirefoxConvenienceE2ETests = on_firefox(_e2e.ConvenienceE2ETests, {
    'test_evaluate_args_survive_navigation': 'inspects the CDP globalThis anchor cache (BiDi calls take a context directly)',
})
FirefoxLiveE2ETests = on_firefox(_e2e.LiveE2ETests)
FirefoxM7FeatureE2ETests = on_firefox(_e2e.M7FeatureE2ETests, {
    'test_emulate_dark_mode': 'media emulation has no BiDi equivalent',
})
FirefoxRelayE2ETests = on_firefox(_e2e.RelayE2ETests)
FirefoxDownloadE2ETests = on_firefox(_e2e.DownloadE2ETests)
FirefoxHumanBehaviorE2ETests = on_firefox(_e2e.HumanBehaviorE2ETests)
FirefoxDetachedNodeE2ETests = on_firefox(_e2e.DetachedNodeE2ETests)
FirefoxActionabilityE2ETests = on_firefox(_e2e.ActionabilityE2ETests)
FirefoxAgentSurfaceE2ETests = on_firefox(_e2e.AgentSurfaceE2ETests)
FirefoxAXWalkerE2ETests = on_firefox(_e2e.AXWalkerParityE2ETests, {
    'test_walker_matches_chrome_on_every_control': "needs Chrome's native tree as the oracle",
})
FirefoxOOPIFE2ETests = on_firefox(_e2e.OOPIFE2ETests, {
    'test_frame_by_selector_runs_in_child_realm': SESSION,
})
FirefoxNestedOOPIFE2ETests = on_firefox(_e2e.NestedOOPIFE2ETests)


@unittest.skipUnless(FIREFOX, 'no Firefox binary found')
class FirefoxPhase2E2ETests(unittest.IsolatedAsyncioTestCase):
    '''What phase 2 added, on the real browser.'''

    async def test_stealth_hides_webdriver_via_preload(self):
        from purecdp.testing import apply_stealth
        async with asyncio.timeout(60):
            async with await purecdp.launch(browser='firefox') as browser:
                page = await browser.new_page()
                assert await page.evaluate('navigator.webdriver') is True
                await apply_stealth(page)
                await page.goto('data:text/html,<title>s</title>')
                assert await page.evaluate('navigator.webdriver') is False
                shown = await page.evaluate(
                    "Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver').get.toString()")
                assert shown.startswith('function get webdriver() { [native code] }'), shown
                assert 'Headless' not in await page.evaluate('navigator.userAgent')
                await page.close()

    async def test_same_origin_iframe_is_a_frame_too(self):
        '''On BiDi every iframe is a browsing context, so page.frame() reaches
        same-origin frames as well (CDP needs them cross-origin).'''
        async with asyncio.timeout(60):
            async with await purecdp.launch(browser='firefox') as browser:
                page = await browser.new_page()
                await page.goto('data:text/html,<iframe id=fr srcdoc="<button id=b '
                                'onclick=&quot;window.hit=1&quot;>in</button>"></iframe>')
                frame = await page.frame('#fr')
                assert frame.frame_id != page._driver.context
                assert await frame.evaluate('document.body.textContent') == 'in'
                button = await frame.query('#b')
                await button.mouse_click()
                assert await frame.evaluate('window.hit') == 1
                await page.close()

    async def test_tab_closed_elsewhere_ends_the_watcher_loop(self):
        '''The 0.8.0 contract (`while page.alive … except CDPClosedError:
        break`) on Firefox: a tab closed from outside the Page.'''
        from purecdp import CDPClosedError
        async with asyncio.timeout(60):
            async with await purecdp.launch(browser='firefox') as browser:
                keeper = await browser.new_page()
                page = await browser.new_page('data:text/html,<title>bye</title>')
                assert page.alive and keeper.alive
                await browser.close_target(page._driver.context)   # not page.close()
                for _ in range(100):
                    if not page.alive:
                        break
                    await asyncio.sleep(0.02)
                assert not page.alive and keeper.alive
                try:
                    await page.evaluate('1')
                except CDPClosedError:
                    pass
                else:
                    raise AssertionError('a dead tab must raise a CDPClosedError')
                assert await keeper.evaluate('2') == 2
                await page.stop()

    async def test_launch_stealth_applies_to_every_tab(self):
        async with asyncio.timeout(60):
            async with await purecdp.launch(browser='firefox', stealth=True) as browser:
                first = await browser.new_page('data:text/html,<title>a</title>')
                await first.goto('data:text/html,<title>a</title>')
                assert await first.evaluate('navigator.webdriver') is False
                async with await browser.new_context() as ctx:
                    second = await ctx.new_page()
                    await second.goto('data:text/html,<title>b</title>')
                    assert await second.evaluate('navigator.webdriver') is False
                    await second.close()
                await first.close()

    async def test_mcp_server_drives_firefox(self):
        '''The MCP agent loop on Firefox: navigate returns the walker's
        outline, act clicks a ref, snapshot shows the result.'''
        from purecdp.mcp import MCPServer
        server = MCPServer(launch_kwargs={'headless': True, 'browser': 'firefox'})

        async def rpc(tool, **args):
            resp = await server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                        'params': {'name': tool, 'arguments': args}})
            return resp['result']['content'][0]['text']

        try:
            async with asyncio.timeout(60):
                outline = await rpc('navigate', url=(
                    'data:text/html,<title>mcp</title><button onclick="'
                    'document.body.insertAdjacentHTML(\'beforeend\', \'<p>done</p>\')">Go</button>'))
                assert 'button "Go"' in outline, outline
                ref = next(line.rsplit('[', 1)[1].rstrip(']')
                           for line in outline.splitlines() if 'button "Go"' in line)
                await rpc('act', ref=ref, action='click')
                after = await rpc('snapshot')
                assert 'done' in after, after
        finally:
            await server.aclose()

    async def test_trusted_input_and_emulation_round_trip(self):
        async with asyncio.timeout(60):
            async with await purecdp.launch(browser='firefox') as browser:
                page = await browser.new_page()
                await page.set_timezone('Asia/Tokyo')
                await page.set_user_agent('purecdp-UA')
                await page.goto('data:text/html,<input id=i><form onsubmit="window.sub=1;return false"><input id=f></form>')
                field = await page.query('#i')
                await field.type('héllo')
                assert await field.eval('(el) => el.value') == 'héllo'
                await (await page.query('#f')).type('x')
                await page.press('Enter')
                assert await page.evaluate('window.sub') == 1
                assert await page.evaluate('Intl.DateTimeFormat().resolvedOptions().timeZone') == 'Asia/Tokyo'
                assert await page.evaluate('navigator.userAgent') == 'purecdp-UA'
                await page.close()
