'''End-to-end tests of the testing layer against a real browser — written on
top of CDPTestCase itself, so the base class is dogfooded by every test here.
Skips cleanly when no Chromium-based browser is available.

Runnable three ways: `python -m unittest`, `python tests/test_testing_e2e.py`,
or `python -m pytest` — none require pytest (the pytest-plugin smoke test
skips when pytest is missing). Plain asserts: don't run with -O.
'''

import asyncio
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import purecdp  # noqa: E402
from purecdp.testing import CDPTestCase, FrameNotFound, JSError, Page  # noqa: E402

EXTRA = tuple(os.environ.get('PURECDP_E2E_ARGS', '').split())


async def eventually(condition, timeout=5.0, poll=0.02):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(poll)


class PageE2ETests(CDPTestCase):
    EXTRA_ARGS = EXTRA

    async def test_goto_title_content(self):
        await self.page.goto('data:text/html,<title>t5</title><h1>hi</h1>')
        assert await self.page.title() == 't5'
        assert '<h1>hi</h1>' in await self.page.content()

    async def test_evaluate_value_and_promise(self):
        assert await self.page.evaluate('6 * 7') == 42
        assert await self.page.evaluate("Promise.resolve('later')") == 'later'

    async def test_evaluate_raises_jserror(self):
        try:
            await self.page.evaluate("(() => { throw new Error('kaboom') })()")
        except JSError as exc:
            assert 'kaboom' in str(exc)
        else:
            raise AssertionError('expected JSError')
        # rejected promises surface too (await_promise=True default)
        try:
            await self.page.evaluate("Promise.reject(new Error('norej'))")
        except JSError as exc:
            assert 'norej' in str(exc)
        else:
            raise AssertionError('expected JSError from rejection')

    async def test_wait_for_selector_and_click(self):
        await self.page.goto(
            'data:text/html,<button onclick=\"window.clicked=1\">go</button>'
            '<script>setTimeout(() => {'
            "  const d = document.createElement('div'); d.id = 'late';"
            '  document.body.appendChild(d); }, 100)</script>')
        await self.page.wait_for_selector('#late')
        await self.page.click('button')
        assert await self.page.evaluate('window.clicked') == 1
        try:
            await self.page.click('#nope', timeout=0.3)
        except TimeoutError as exc:
            assert '#nope' in str(exc)
        else:
            raise AssertionError('expected TimeoutError')

    async def test_console_and_error_capture(self):
        await self.page.evaluate("console.log('hello', 42)")
        await eventually(lambda: self.page.console)
        assert self.page.console[0].kind == 'log'
        assert self.page.console[0].text == 'hello 42'

        await self.page.evaluate(
            "setTimeout(() => { throw new Error('later-crash') }, 0)")
        await eventually(lambda: self.page.js_errors)
        assert 'later-crash' in str(self.page.js_errors[0])

    async def test_screenshot_is_png(self):
        await self.page.goto('data:text/html,<h1>shot</h1>')
        data = await self.page.screenshot()
        assert data[:8] == b'\x89PNG\r\n\x1a\n'

    async def test_intercept_fulfill_and_abort(self):
        async def api(request):
            assert request.method == 'GET'
            await request.fulfill(body='{"ok":true}',
                                  content_type='application/json')

        async def blocked(request):
            await request.abort()

        await self.page.route('*good.invalid*', api)
        await self.page.route('*bad.invalid*', blocked)
        await self.page.goto('data:text/html,intercept')

        body = await self.page.evaluate(
            "fetch('https://good.invalid/api').then(r => r.text())")
        assert body == '{"ok":true}'
        outcome = await self.page.evaluate(
            "fetch('https://bad.invalid/x').then(() => 'ok')"
            ".catch(() => 'blocked')")
        assert outcome == 'blocked'

    async def test_goto_network_idle(self):
        await self.page.goto('data:text/html,<p>quiet</p>', wait='idle')
        assert await self.page.evaluate('document.readyState') == 'complete'

    async def test_second_page_same_context(self):
        other = await self.new_page()
        await other.goto('data:text/html,<title>two</title>')
        assert await other.title() == 'two'
        await other.close()


@unittest.skipUnless(purecdp.find_browser(), 'no Chromium-based browser found')
@unittest.skipUnless(importlib.util.find_spec('pytest'), 'pytest not installed')
class PytestPluginTests(unittest.TestCase):
    def test_plugin_fixtures_run_async_test(self):
        test_source = (
            'async def test_with_page(cdp_page):\n'
            "    await cdp_page.goto('data:text/html,<title>plug</title>')\n"
            "    assert await cdp_page.title() == 'plug'\n"
        )
        env = dict(os.environ)
        env['PYTHONPATH'] = _SRC + os.pathsep + env.get('PYTHONPATH', '')
        if EXTRA:
            env['PURECDP_LAUNCH_ARGS'] = ' '.join(EXTRA)
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / 'test_plug.py').write_text(test_source)
            proc = subprocess.run(
                [sys.executable, '-m', 'pytest', '-q',
                 '-p', 'purecdp.testing.pytest_plugin',
                 '-p', 'no:cacheprovider', 'test_plug.py'],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert '1 passed' in proc.stdout


class DriveParityE2ETests(CDPTestCase):
    '''E2E proof of the primitives real-app drives need: init scripts,
    element handles, trusted input, exchange recording.'''

    EXTRA_ARGS = EXTRA

    async def test_init_script_runs_before_app_code(self):
        await self.page.add_init_script("window.__seeded = 'early'")
        await self.page.goto(
            'data:text/html,<script>window.__seen = window.__seeded</script>')
        assert await self.page.evaluate('window.__seen') == 'early'

    async def test_query_containing_and_index(self):
        await self.page.goto(
            'data:text/html,<div class=row>alpha</div>'
            '<div class=row>the BETA row</div><div class=row>gamma</div>')
        beta = await self.page.query('.row', containing='beta')
        assert 'BETA' in await beta.text()
        last = await self.page.query('.row', index=-1)
        assert (await last.text()) == 'gamma'
        assert await self.page.query_count('.row') == 3
        assert await self.page.query('#missing', timeout=0.3,
                                     required=False) is None

    async def test_element_scoped_query_and_wrapper_click(self):
        # hidden checkbox inside a label: wrapper .click() does nothing,
        # Element.click resolves to the real control
        await self.page.goto(
            'data:text/html,<div id=card><label><span>Agree</span>'
            "<input type=checkbox style='opacity:0;width:0;height:0'>"
            '</label></div>')
        card = await self.page.query('#card')
        span = await card.query('span', containing='agree')
        await span.click()
        assert await self.page.evaluate(
            "document.querySelector('input').checked") is True

    async def test_query_all_enumerates_and_filters(self):
        await self.page.goto(
            'data:text/html,<button>one</button>'
            '<button disabled>two</button><button>the THREE</button>')
        buttons = await self.page.query_all('button')
        assert len(buttons) == 3
        # order is preserved and each is an independent, held handle
        assert [await b.text() for b in buttons] == ['one', 'two', 'the THREE']
        # filter to the enabled ones by reading a property off each handle
        enabled = [b for b in buttons if not await b.eval('(el) => el.disabled')]
        assert len(enabled) == 2
        # containing= filters case-insensitively, like query()
        (three,) = await self.page.query_all('button', containing='three')
        assert 'THREE' in await three.text()
        # no match -> empty list, not an error
        assert await self.page.query_all('.nope') == []

    async def test_element_query_all_is_scoped(self):
        await self.page.goto(
            'data:text/html,<div id=a><span>x</span><span>y</span></div>'
            '<div id=b><span>z</span></div>')
        card = await self.page.query('#a')
        spans = await card.query_all('span')
        assert [await s.text() for s in spans] == ['x', 'y']   # not z, from #b

    async def test_set_value_commits_to_controlled_inputs(self):
        # simulate React's patched-value pattern: listeners count events
        await self.page.goto(
            'data:text/html,<input id=a>'
            '<script>window.events = [];'
            "const el = document.getElementById('a');"
            "el.addEventListener('input', () => events.push('input'));"
            "el.addEventListener('change', () => events.push('change'));"
            '</script>')
        field = await self.page.query('#a')
        await field.set_value('typed-value')
        assert await self.page.evaluate("document.getElementById('a').value") \
            == 'typed-value'
        assert await self.page.evaluate('window.events') == ['input', 'change']

    async def test_insert_text_and_press_enter(self):
        await self.page.goto(
            'data:text/html,<input id=chat>'
            "<script>document.getElementById('chat').addEventListener("
            "'keydown', e => { if (e.key === 'Enter')"
            " window.submitted = document.getElementById('chat').value })"
            '</script>')
        field = await self.page.query('#chat')
        await field.focus()
        await self.page.insert_text('hello enter')
        await self.page.press('Enter')
        assert await self.page.evaluate('window.submitted') == 'hello enter'

    async def test_mouse_click_is_trusted(self):
        await self.page.goto(
            'data:text/html,<button id=b>hit</button>'
            "<script>document.getElementById('b').addEventListener('click',"
            ' e => { window.trusted = e.isTrusted })</script>')
        button = await self.page.query('#b')
        await button.mouse_click()
        assert await self.page.evaluate('window.trusted') is True

    async def test_recorder_with_stubbed_api(self):
        async def api(request):
            await request.fulfill(json={'answer': 42})

        await self.page.route('*stub.invalid*', api)
        recorder = self.page.record(needle='stub.invalid')
        await self.page.goto('data:text/html,recorder')

        previous = len(recorder.exchanges)
        result = await self.page.evaluate(
            "fetch('https://stub.invalid/query', {method: 'POST',"
            " headers: {'Content-Type': 'application/json'},"
            " body: JSON.stringify({ask: 'count'})}).then(r => r.json())")
        assert result == {'answer': 42}

        exchange = await recorder.wait_for_next(previous)
        assert exchange.method == 'POST'
        assert exchange.request_json == {'ask': 'count'}
        assert exchange.status == 200
        assert exchange.mime == 'application/json'
        assert exchange.json == {'answer': 42}
        assert recorder.responses[-1] == {'answer': 42}


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(purecdp.find_browser(), 'no Chromium-based browser found')
class LaunchedPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_launched_page_one_liner(self):
        from purecdp.testing import launched_page

        async with asyncio.timeout(60):
            async with launched_page(extra_args=EXTRA) as (browser, page):
                assert await page.evaluate('2 + 3') == 5
                assert browser.process.returncode is None
        assert browser.process.returncode is not None  # torn down


class ConvenienceE2ETests(CDPTestCase):
    EXTRA_ARGS = EXTRA

    async def test_element_type_types_for_real(self):
        await self.page.goto(
            'data:text/html,<input id=name>'
            "<script>document.getElementById('name').addEventListener("
            "'input', () => window.inputs = (window.inputs || 0) + 1)</script>")
        field = await self.page.query('#name')
        await field.type('purecdp')
        assert await self.page.evaluate(
            "document.getElementById('name').value") == 'purecdp'
        assert await self.page.evaluate('window.inputs') >= 1

    async def test_mock_api_end_to_end(self):
        await self.page.mock_api({
            '/api/items': {'items': ['a', 'b']},
            '/api/items/': lambda path: {'id': path.rsplit('/', 1)[-1]},
        })
        await self.page.goto('data:text/html,mockapi')
        listing = await self.page.evaluate(
            "fetch('https://svc.invalid/api/items').then(r => r.json())")
        assert listing == {'items': ['a', 'b']}
        single = await self.page.evaluate(
            "fetch('https://svc.invalid/api/items/42').then(r => r.json())")
        assert single == {'id': '42'}
        # unmatched paths pass through to the (nonexistent) network and fail
        outcome = await self.page.evaluate(
            "fetch('https://svc.invalid/other').then(() => 'hit')"
            ".catch(() => 'passed-through')")
        assert outcome == 'passed-through'


@unittest.skipUnless(purecdp.find_browser(), 'no Chromium-based browser found')
class CdpHygieneE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_stealth_launch_clean_page_hides_webdriver(self):
        from purecdp.testing import Page

        async with asyncio.timeout(60):
            async with await purecdp.launch(stealth=True, extra_args=EXTRA) as browser:
                session = await browser.new_page()
                # a "clean" page: no Runtime/Network domain enabled
                page = await Page.create(session, capture=False,
                                         track_network=False)
                # evaluate is a command, not a domain event — still works
                await page.goto('data:text/html,<title>clean</title>', wait='load')
                assert await page.title() == 'clean'
                # AutomationControlled disabled -> navigator.webdriver not true
                assert await page.evaluate('navigator.webdriver') in (False, None)
                # capture buffers stay empty (nothing was enabled)
                await page.evaluate("console.log('unseen')")
                assert page.console == []


class M7FeatureE2ETests(CDPTestCase):
    '''Emulation, cookies, storage, content, PDF, element actions, dialogs.'''

    EXTRA_ARGS = EXTRA

    async def test_set_viewport(self):
        await self.page.set_viewport(800, 600)
        await self.page.goto('data:text/html,<p>vp</p>')
        assert await self.page.evaluate('window.innerWidth') == 800

    async def test_set_user_agent(self):
        await self.page.set_user_agent('PureCDP-UA/9.9')
        await self.page.goto('data:text/html,<p>ua</p>')
        assert await self.page.evaluate('navigator.userAgent') == 'PureCDP-UA/9.9'

    async def test_emulate_dark_mode(self):
        await self.page.emulate_media(color_scheme='dark')
        await self.page.goto('data:text/html,<p>theme</p>')
        assert await self.page.evaluate(
            "matchMedia('(prefers-color-scheme: dark)').matches") is True

    async def test_cookies_round_trip(self):
        await self.page.set_cookies([
            {'name': 'sess', 'value': 'xyz', 'url': 'https://cookie.test/'}])
        got = await self.page.cookies(urls=['https://cookie.test/'])
        assert any(c['name'] == 'sess' and c['value'] == 'xyz' for c in got)
        await self.page.clear_cookies()
        assert await self.page.cookies(urls=['https://cookie.test/']) == []

    async def test_storage_state_round_trip(self):
        # a fulfilled document gives a real (non-opaque) origin for localStorage
        async def doc(request):
            await request.fulfill(body='<html><body>app</body></html>',
                                  content_type='text/html')
        await self.page.route('*app.test*', doc)
        await self.page.goto('https://app.test/')
        await self.page.evaluate("localStorage.setItem('token', 'secret')")
        state = await self.page.storage_state()
        assert ['token', 'secret'] in state['local_storage']

        fresh = await self.new_page()
        await fresh.route('*app.test*', doc)
        await fresh.goto('https://app.test/')
        await fresh.set_storage_state(state)
        assert await fresh.evaluate("localStorage.getItem('token')") == 'secret'
        await fresh.close()

    async def test_pdf_is_pdf(self):
        await self.page.goto('data:text/html,<h1>pdf</h1>')
        data = await self.page.pdf()
        assert data[:5] == b'%PDF-'

    async def test_set_content_and_add_tags(self):
        await self.page.goto('data:text/html,<p>start</p>')
        await self.page.set_content('<div id=x>replaced</div>')
        assert await self.page.text('#x') == 'replaced'
        await self.page.add_script_tag(content='window.__added = 7')
        assert await self.page.evaluate('window.__added') == 7
        await self.page.add_style_tag(content='#x { color: rgb(1, 2, 3) }')
        assert await self.page.evaluate(
            "getComputedStyle(document.querySelector('#x')).color") \
            == 'rgb(1, 2, 3)'

    async def test_element_box_hover_scroll(self):
        await self.page.goto(
            "data:text/html,<div id=b style='width:50px;height:30px'>b</div>"
            "<script>document.getElementById('b').addEventListener("
            "'mouseenter', () => window.hovered = 1)</script>")
        el = await self.page.query('#b')
        box = await el.bounding_box()
        assert box['width'] == 50 and box['height'] == 30
        await el.hover()
        assert await self.page.evaluate('window.hovered') == 1

    async def test_element_select_option(self):
        await self.page.goto(
            'data:text/html,<select id=s>'
            '<option value=a>A</option><option value=b>B</option></select>'
            "<script>document.getElementById('s').addEventListener("
            "'change', e => window.picked = e.target.value)</script>")
        select = await self.page.query('#s')
        hit = await select.select_option('b')
        assert hit == ['b']
        assert await self.page.evaluate('window.picked') == 'b'

    async def test_element_screenshot_is_png(self):
        await self.page.goto(
            "data:text/html,<div style='width:20px;height:20px;background:red'"
            ' id=box></div>')
        el = await self.page.query('#box')
        assert (await el.screenshot())[:8] == b'\x89PNG\r\n\x1a\n'

    async def test_file_upload(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as f:
            f.write('hello upload')
            tmp = f.name
        await self.page.goto('data:text/html,<input id=f type=file>')
        el = await self.page.query('#f')
        await el.set_input_files(tmp)
        assert await self.page.evaluate(
            "document.getElementById('f').files[0].name") \
            == tmp.rsplit('/', 1)[-1]
        os.unlink(tmp)

    async def test_dialogs_auto_handled(self):
        seen = await self.page.handle_dialogs(accept=True)
        await self.page.goto('data:text/html,<p>dlg</p>')
        result = await self.page.evaluate("confirm('proceed?')")
        assert result is True
        await eventually(lambda: seen)
        assert seen == ['proceed?']

    async def test_expect_navigation(self):
        # Chrome blocks top-frame navigation to data: URLs from a link click,
        # so route a real https destination and navigate to that.
        async def dest(request):
            await request.fulfill(body='<title>there</title>',
                                  content_type='text/html')
        await self.page.route('*nav.test*', dest)
        await self.page.goto(
            "data:text/html,<a id=go href='https://nav.test/next'>go</a>")
        async with self.page.expect_navigation():
            await self.page.click('#go')
        assert await self.page.title() == 'there'


class StealthE2ETests(CDPTestCase):
    '''Prove each evasion actually patches its signal in a real browser.'''

    EXTRA_ARGS = EXTRA

    async def _stealthed(self):
        from purecdp.testing import apply_stealth
        page = await self.new_page()
        await apply_stealth(page, webgl_vendor='Intel Inc.',
                            webgl_renderer='Intel Iris OpenGL Engine',
                            hardware_concurrency=8)
        await page.goto('data:text/html,<title>stealth</title>')
        return page

    async def test_navigator_signals(self):
        page = await self._stealthed()
        assert await page.evaluate('navigator.webdriver') is False
        assert await page.evaluate("navigator.languages.join(',')") == 'en-US,en'
        assert await page.evaluate('navigator.vendor') == 'Google Inc.'
        assert await page.evaluate('navigator.hardwareConcurrency') == 8
        assert await page.evaluate('navigator.plugins.length') > 0
        assert await page.evaluate(
            'navigator.plugins.item(0).name') is not None

    async def test_window_chrome_present(self):
        page = await self._stealthed()
        assert await page.evaluate('typeof window.chrome') == 'object'
        assert await page.evaluate('typeof window.chrome.runtime') == 'object'

    async def test_permissions_notifications_match(self):
        page = await self._stealthed()
        # the classic tell: query state must match Notification.permission
        state = await page.evaluate(
            "navigator.permissions.query({name:'notifications'})"
            '.then(r => r.state)')
        assert state == await page.evaluate('Notification.permission')

    async def test_webgl_vendor_spoofed(self):
        page = await self._stealthed()
        # Headless Chromium may have no WebGL backend at all (musl/Alpine, or
        # newer Chrome that gates SwiftShader behind --enable-unsafe-swiftshader)
        # — getContext('webgl') is null there. The spoof only applies once a
        # context exists, so skip cleanly when the environment has none.
        values = await page.evaluate(
            "(() => { const gl = document.createElement('canvas')"
            ".getContext('webgl') || document.createElement('canvas')"
            ".getContext('experimental-webgl');"
            ' return gl && [gl.getParameter(0x9245),'   # UNMASKED_VENDOR_WEBGL
            ' gl.getParameter(0x9246)]; })()')          # UNMASKED_RENDERER_WEBGL
        if not values:
            self.skipTest('no WebGL context in this headless environment')
        assert values[0] == 'Intel Inc.'
        assert values[1] == 'Intel Iris OpenGL Engine'

    async def test_media_codecs_spoofed(self):
        page = await self._stealthed()
        answer = await page.evaluate(
            "document.createElement('video')"
            ".canPlayType('video/mp4; codecs=\"avc1.42E01E\"')")
        assert answer == 'probably'

    async def test_stealth_reaches_worker_scope(self):
        # workers=True (default) injects the same patches into worker scope, so
        # a worker's navigator agrees with the page's — no page/worker mismatch
        # tell (e.g. CreepJS's hasBadWebGL cross-check). Proven here on
        # hardwareConcurrency, which exists in worker scope regardless of GPU.
        from purecdp.testing import apply_stealth
        page = await self.new_page()
        await apply_stealth(page, hardware_concurrency=7)
        await page.goto('data:text/html,<title>worker</title>')
        in_worker = await page.evaluate('''
          (() => {
            const code = "self.onmessage = () => "
              + "self.postMessage(navigator.hardwareConcurrency);";
            const url = URL.createObjectURL(
              new Blob([code], {type: 'application/javascript'}));
            const w = new Worker(url);
            return new Promise((res) => {
              w.onmessage = (e) => res(e.data);
              w.postMessage(0);
            });
          })()
        ''')
        # worker sees the spoofed value, and it agrees with the page
        assert in_worker == 7, in_worker
        assert await page.evaluate('navigator.hardwareConcurrency') == 7

    async def test_function_tostring_mask_hides_patches(self):
        # with function.toString on, a patched fn reports "[native code]"
        from purecdp.testing import apply_stealth
        page = await self.new_page()
        await apply_stealth(page, evasions=[
            'function.toString', 'navigator.webdriver', 'navigator.permissions'])
        await page.goto('data:text/html,<title>mask</title>')
        # the webdriver getter and permissions.query look native
        getter_src = await page.evaluate(
            'Object.getOwnPropertyDescriptor('
            "Navigator.prototype, 'webdriver').get.toString()")
        assert getter_src == 'function get webdriver() { [native code] }', getter_src
        query_src = await page.evaluate(
            'navigator.permissions.query.toString()')
        assert query_src == 'function query() { [native code] }', query_src
        # and toString itself doesn't betray the proxy
        assert await page.evaluate(
            'Function.prototype.toString.toString()') \
            == 'function toString() { [native code] }'

    async def test_max_touch_points_optin(self):
        from purecdp.testing import apply_stealth
        page = await self.new_page()
        await apply_stealth(page, evasions=['navigator.maxTouchPoints'],
                            max_touch_points=5)
        await page.goto('data:text/html,<title>touch</title>')
        assert await page.evaluate('navigator.maxTouchPoints') == 5

    async def test_subset_only_applies_selected(self):
        from purecdp.testing import apply_stealth
        page = await self.new_page()
        await apply_stealth(page, evasions=['navigator.vendor'], vendor='Acme')
        await page.goto('data:text/html,<title>subset</title>')
        assert await page.evaluate('navigator.vendor') == 'Acme'
        # webdriver was NOT selected, so the init script didn't touch it
        # (value depends on launch flags; just assert vendor override worked)
        assert await page.evaluate('navigator.languages.length') >= 1


class HumanBehaviorE2ETests(CDPTestCase):
    '''Prove human mouse/typing/scroll produce trusted, correct effects.'''

    EXTRA_ARGS = EXTRA

    async def test_human_click_is_trusted_and_hits(self):
        await self.page.goto(
            "data:text/html,<button id=b style='margin:80px;width:120px;"
            "height:40px'>go</button>"
            "<script>const b=document.getElementById('b');"
            "b.addEventListener('click', e => {"
            ' window.trusted = e.isTrusted; window.hits = (window.hits||0)+1; });'
            'window.moves = 0;'
            "document.addEventListener('mousemove', () => window.moves++);"
            '</script>')
        el = await self.page.query('#b')
        await el.human_click()
        assert await self.page.evaluate('window.trusted') is True
        assert await self.page.evaluate('window.hits') == 1
        # a curved path fired many intermediate mousemove events, not a jump
        assert await self.page.evaluate('window.moves') > 3

    async def test_human_type_produces_keystrokes_and_value(self):
        await self.page.goto(
            'data:text/html,<input id=t>'
            '<script>window.keys=0;'
            "document.getElementById('t').addEventListener("
            "'keydown', () => window.keys++);</script>")
        el = await self.page.query('#t')
        await el.focus()
        await self.page.human_type('hello', wpm=2000)  # fast but real per-key
        assert await self.page.evaluate("document.getElementById('t').value") \
            == 'hello'
        assert await self.page.evaluate('window.keys') == 5

    async def test_human_scroll_moves_page(self):
        await self.page.goto(
            "data:text/html,<div style='height:5000px'>tall</div>")
        await self.page.human_scroll(1200, x=50, y=50)
        assert await self.page.evaluate('window.scrollY') > 100


class AgentSurfaceE2ETests(CDPTestCase):
    '''M9: snapshot()/act() drive a real page through the accessibility tree.'''

    EXTRA_ARGS = EXTRA

    FORM = ('data:text/html,<title>login</title><form>'
            '<h2>Sign in</h2>'
            '<input aria-label=Email type=email>'
            '<input aria-label=Password type=password>'
            '<button onclick=\"window.clicked=1;return false\">Sign in</button>'
            '</form>')

    async def test_snapshot_lists_controls_with_refs(self):
        await self.page.goto(self.FORM)
        snap = await self.page.snapshot()
        text = str(snap)
        assert 'Email' in text and 'Password' in text
        assert 'button "Sign in"' in text
        assert snap.refs                                   # refs assigned
        # names are discoverable for an agent to target
        assert any(m['name'] == 'Email' for m in snap.meta.values())

    async def test_act_fill_and_click(self):
        await self.page.goto(self.FORM)
        snap = await self.page.snapshot()
        email = next(r for r, m in snap.meta.items() if m['name'] == 'Email')
        # both the <h2> and the <button> are named "Sign in" — disambiguate by
        # role (exactly what an agent does with the outline)
        button = next(r for r, m in snap.meta.items()
                      if m['role'] == 'button' and m['name'] == 'Sign in')
        await self.page.act(email, 'fill', text='x@y.com')
        assert await self.page.evaluate(
            "document.querySelector('input[type=email]').value") == 'x@y.com'
        await self.page.act(button, 'click')
        assert await self.page.evaluate('window.clicked') == 1

    async def test_password_value_never_leaks(self):
        await self.page.goto(self.FORM)
        pw = next(r for r, m in (await self.page.snapshot()).meta.items()
                  if m['name'] == 'Password')
        await self.page.act(pw, 'fill', text='s3cret')
        assert 's3cret' not in str(await self.page.snapshot())


class ActionabilityE2ETests(CDPTestCase):
    '''The Playwright-style actionability gate on real, obstructed pages.'''

    EXTRA_ARGS = EXTRA

    # a button covered by a full-viewport overlay div; no '#' (data: URLs treat
    # it as a fragment). The overlay is removed after `after` ms, or never.
    def _covered(self, after: int | None) -> str:
        drop = (f"<script>setTimeout(()=>document.querySelector('div').remove(),"
                f'{after})</script>') if after is not None else ''
        return ('data:text/html,<title>ov</title>'
                '<style>div{position:fixed;inset:0;background:red}</style>'
                '<button onclick=\"window.clicked=1\">Go</button><div></div>' + drop)

    async def _button_ref(self):
        snap = await self.page.snapshot()
        return next(r for r, m in snap.meta.items() if m['role'] == 'button')

    async def test_act_waits_for_overlay_to_clear(self):
        await self.page.goto(self._covered(after=150))
        # default stable=True: blocks until the overlay is gone, then the click
        # lands on the button (not the overlay)
        await self.page.act(await self._button_ref(), 'click')
        assert await self.page.evaluate('window.clicked') == 1

    async def test_act_raises_when_permanently_obscured(self):
        from purecdp.testing import ActionabilityError
        await self.page.goto(self._covered(after=None))
        ref = await self._button_ref()
        try:
            await self.page.act(ref, 'click', timeout=0.4)
        except ActionabilityError as exc:
            assert exc.reason == 'obscured'
        else:
            raise AssertionError('expected ActionabilityError (obscured)')
        # without the gate the click silently misses (hits the overlay)
        await self.page.act(ref, 'click', stable=False)
        assert await self.page.evaluate('window.clicked || 0') == 0

    async def test_act_raises_on_disabled(self):
        from purecdp.testing import ActionabilityError
        await self.page.goto(
            'data:text/html,<title>d</title>'
            '<button disabled onclick=\"window.clicked=1\">Go</button>')
        ref = await self._button_ref()
        try:
            await self.page.act(ref, 'click', timeout=0.4)
        except ActionabilityError as exc:
            assert exc.reason == 'disabled'
        else:
            raise AssertionError('expected ActionabilityError (disabled)')

    async def test_auto_dismiss_clears_banner_then_acts(self):
        # a consent banner covers the button; its close control ('.x') removes
        # it. Registering the dismisser lets act() clear it and click through.
        await self.page.goto(
            'data:text/html,<title>b</title>'
            '<style>.banner{position:fixed;inset:0;background:blue}</style>'
            '<button onclick=\"window.clicked=1\">Go</button>'
            '<div class=banner><button class=x '
            "onclick=\"document.querySelector('.banner').remove()\">X</button></div>")
        self.page.add_auto_dismiss('.x')
        await self.page.act(await self._button_ref(), 'click')
        assert await self.page.evaluate('window.clicked') == 1


class ConnectE2ETests(CDPTestCase):
    '''connect() attaches to an already-running browser without owning it —
    driven against the browser CDPTestCase itself launched.'''

    EXTRA_ARGS = EXTRA

    def _endpoint_parts(self) -> tuple[str, str]:
        port_file = pathlib.Path(self.browser.user_data_dir) / 'DevToolsActivePort'
        port, path = port_file.read_text().splitlines()[:2]
        return port.strip(), path.strip()

    async def test_connect_by_host_port_drives_a_page(self):
        port, _ = self._endpoint_parts()
        other = await purecdp.connect(host='127.0.0.1', port=int(port))
        try:
            session = await other.new_page()
            page = await Page.create(session)
            await page.goto('data:text/html,<title>remote</title>')
            assert await page.title() == 'remote'
        finally:
            await other.aclose()
        # closing the connected client must leave the real browser running
        assert self.browser.process.returncode is None

    async def test_connect_by_endpoint_url(self):
        port, path = self._endpoint_parts()
        other = await purecdp.connect(f'ws://127.0.0.1:{port}{path}')
        try:
            session = await other.new_page()
            page = await Page.create(session)
            assert await page.evaluate('1 + 1') == 2
        finally:
            await other.aclose()
        assert self.browser.process.returncode is None


# Parent on 127.0.0.1, child iframe on 127.0.0.2 -> cross-site -> out-of-process
# under --site-per-process. The button sits at an offset inside a child that is
# itself offset in the parent, so a landed click proves trusted input dispatched
# on the frame's own session hits the right element in the frame's coord space.
_CHILD_HTML = (
    '<title>child</title><style>body{margin:0}</style><h1>Child</h1>'
    "<button style='position:absolute;left:20px;top:30px;width:120px;height:40px'"
    " onclick='window.clicked=1'>Pay</button>"
    "<input aria-label=Card><script>window.marker='in-child'</script>")


def _parent_html(port: int) -> str:
    return ('<title>parent</title><h1>Parent</h1>'
            f"<iframe src='http://127.0.0.2:{port}/child' style='position:absolute;"
            "left:50px;top:60px;width:340px;height:260px;border:0'></iframe>")


class OOPIFE2ETests(CDPTestCase):
    '''M10 Phase 1: drive a real cross-origin (out-of-process) iframe.'''

    EXTRA_ARGS = (*EXTRA, '--site-per-process')

    @classmethod
    def setUpClass(cls):
        port_box = {}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = (_CHILD_HTML if 'child' in self.path
                        else _parent_html(port_box['port']))
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        cls.httpd = ThreadingHTTPServer(('', 0), H)
        cls.port = port_box['port'] = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    async def _frame(self, **kwargs):
        await self.page.goto(f'http://127.0.0.1:{self.port}/parent', wait='load')
        try:
            return await self.page.frame(timeout=5, **kwargs)
        except FrameNotFound:
            self.skipTest('iframe did not go out-of-process (no site isolation)')

    async def test_frame_by_selector_runs_in_child_realm(self):
        frame = await self._frame(selector='iframe')
        assert frame.session is not self.page.session      # its own OOPIF session
        assert await frame.evaluate('window.marker') == 'in-child'
        assert await frame.evaluate('location.hostname') == '127.0.0.2'

    async def test_frame_by_url(self):
        frame = await self._frame(url='*127.0.0.2*')
        assert await frame.evaluate('location.hostname') == '127.0.0.2'

    async def test_trusted_click_lands_in_frame(self):
        frame = await self._frame(selector='iframe')
        button = await frame.query('button')
        await button.mouse_click()          # trusted input on the frame session
        assert await frame.evaluate('window.clicked') == 1

    async def test_frame_snapshot_and_act(self):
        frame = await self._frame(selector='iframe')
        snap = await frame.snapshot()
        assert 'button "Pay"' in str(snap)
        ref = next(r for r, m in snap.meta.items() if m['role'] == 'button')
        await frame.act(ref, 'click')       # actionability gate, frame session
        assert await frame.evaluate('window.clicked') == 1

    async def test_cross_frame_snapshot_stitches_and_act_routes(self):
        # ensure the OOPIF is attached (and site isolation is active), then take
        # a stitched page-level snapshot that includes the child's controls
        await self._frame(selector='iframe')
        snap = await self.page.snapshot(cross_frame=True)
        text = str(snap)
        assert 'button "Pay"' in text        # child control in the PAGE outline
        assert 'Child' in text and 'Parent' in text   # both frames stitched
        ref = next(r for r, m in snap.meta.items()
                   if m['role'] == 'button' and m['name'] == 'Pay')
        # act() on a page-level ref routes into the OOPIF session automatically
        await self.page.act(ref, 'click')
        frame = await self.page.frame('iframe')
        assert await frame.evaluate('window.clicked') == 1


# Three sites chained: top(127.0.0.1) -> mid(127.0.0.2) -> deep(127.0.0.3).
# Under --site-per-process each hop is its own process => nested OOPIFs.
_DEEP_HTML = (
    '<title>deep</title><style>body{margin:0}</style><h1>Deep</h1>'
    "<button style='position:absolute;left:10px;top:20px;width:100px;height:40px'"
    " onclick='window.clicked=1'>Deep</button><script>window.deep='here'</script>")


def _mid_html(port):
    return ('<title>mid</title><h1>Mid</h1>'
            f"<iframe src='http://127.0.0.3:{port}/deep' style='position:absolute;"
            "left:30px;top:40px;width:300px;height:220px;border:0'></iframe>")


def _top_html(port):
    return ('<title>top</title><h1>Top</h1>'
            f"<iframe src='http://127.0.0.2:{port}/mid' style='position:absolute;"
            "left:50px;top:60px;width:360px;height:300px;border:0'></iframe>")


class NestedOOPIFE2ETests(CDPTestCase):
    '''M10 Phase 4: OOPIF nested inside an OOPIF (two process hops).'''

    EXTRA_ARGS = (*EXTRA, '--site-per-process')

    @classmethod
    def setUpClass(cls):
        box = {}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if 'deep' in self.path:
                    body = _DEEP_HTML
                elif 'mid' in self.path:
                    body = _mid_html(box['port'])
                else:
                    body = _top_html(box['port'])
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        cls.httpd = ThreadingHTTPServer(('', 0), H)
        cls.port = box['port'] = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    async def _chain(self):
        await self.page.goto(f'http://127.0.0.1:{self.port}/top', wait='load')
        try:
            outer = await self.page.frame('iframe', timeout=5)      # -> mid
            inner = await outer.frame('iframe', timeout=5)          # -> deep
        except FrameNotFound:
            self.skipTest('nested iframe did not go out-of-process')
        return outer, inner

    async def test_frame_frame_recurses_into_grandchild(self):
        outer, inner = await self._chain()
        assert await outer.evaluate('location.hostname') == '127.0.0.2'
        assert await inner.evaluate('location.hostname') == '127.0.0.3'
        assert await inner.evaluate('window.deep') == 'here'
        button = await inner.query('button')
        await button.mouse_click()
        assert await inner.evaluate('window.clicked') == 1

    async def test_stitched_snapshot_reaches_grandchild(self):
        _outer, inner = await self._chain()          # forces both hops attached
        snap = await self.page.snapshot(cross_frame=True)
        text = str(snap)
        assert 'Top' in text and 'Mid' in text and 'button "Deep"' in text
        # the deep frame has both an <h1> and a <button> named "Deep" — pick the
        # button (exactly what an agent does with the outline)
        ref = next(r for r, m in snap.meta.items()
                   if m['role'] == 'button' and m['name'] == 'Deep')
        await self.page.act(ref, 'click')            # routes two hops down
        assert await inner.evaluate('window.clicked') == 1


_DOWNLOAD_CSV = b'name,value\nalpha,1\nbeta,2\n'
_DOWNLOAD_PAGE = (
    '<title>dl</title>'
    "<a id=dl href='/file.csv' download='report.csv'>download</a>")


class DownloadE2ETests(CDPTestCase):
    '''expect_download(): capture a file download triggered by a click.'''

    @classmethod
    def setUpClass(cls):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if 'file.csv' in self.path:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/csv')
                    self.send_header('Content-Disposition',
                                     'attachment; filename=report.csv')
                    self.end_headers()
                    self.wfile.write(_DOWNLOAD_CSV)
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.end_headers()
                    self.wfile.write(_DOWNLOAD_PAGE.encode())

            def log_message(self, *a):
                pass

        cls.httpd = ThreadingHTTPServer(('', 0), H)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    async def test_captures_download_bytes_and_name(self):
        await self.page.goto(f'http://127.0.0.1:{self.port}/page', wait='load')
        link = await self.page.query('#dl')
        async with self.page.expect_download() as info:
            await link.mouse_click()             # trusted gesture triggers it
        download = await info.value
        assert download.suggested_filename == 'report.csv'
        assert download.url.endswith('/file.csv')
        assert await download.read() == _DOWNLOAD_CSV
        # save_as copies it out under a chosen name
        dest = os.path.join(tempfile.mkdtemp(), 'kept.csv')
        await download.save_as(dest)
        assert pathlib.Path(dest).read_bytes() == _DOWNLOAD_CSV

    async def test_timeout_raises_download_error(self):
        from purecdp.testing import DownloadError

        await self.page.goto(f'http://127.0.0.1:{self.port}/page', wait='load')
        async with self.page.expect_download(timeout=0.5) as info:
            pass                                 # nothing triggers a download
        try:
            await info.value
        except DownloadError:
            pass
        else:
            raise AssertionError('expected DownloadError on timeout')
