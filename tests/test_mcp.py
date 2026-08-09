'''MCP server protocol + tool routing, driven over a FakeBrowser-backed page.

No stdio and no real browser: we feed JSON-RPC dicts to MCPServer.handle and
inject the page via page_factory. Plain asserts; don't run with -O.
'''

import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

try:
    from .support import FakeBrowser, FakeTransport, ax, drain
except ImportError:
    from support import FakeBrowser, FakeTransport, ax, drain

import purecdp  # noqa: E402
from purecdp import Connection  # noqa: E402
from purecdp.mcp import MCPServer, TOOLS  # noqa: E402
from purecdp.testing import Page  # noqa: E402


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeBrowser()
        self.fake.ax_nodes = [
            ax('1', role='RootWebArea', name='Home', children=['2']),
            ax('2', role='button', name='Go', backend=20),
        ]
        self.transport = FakeTransport(self.fake)
        self.conn = Connection(self.transport)
        await self.conn.open()

        async def factory():
            session = await purecdp.new_session(self.conn, 'about:blank')
            return await Page.create(session, default_timeout=5.0)

        self.server = MCPServer(page_factory=factory)

    async def asyncTearDown(self):
        await self.conn.aclose()

    async def call(self, name, **arguments):
        resp = await self.server.handle({
            'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments}})
        return resp['result']

    async def test_initialize_advertises_tools_capability(self):
        resp = await self.server.handle({
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
        assert resp['result']['serverInfo']['name'] == 'purecdp'
        assert 'tools' in resp['result']['capabilities']

    async def test_initialized_notification_no_reply(self):
        assert await self.server.handle(
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'}) is None

    async def test_tools_list_excludes_gated_evaluate(self):
        resp = await self.server.handle(
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        names = {t['name'] for t in resp['result']['tools']}
        assert 'evaluate' not in names                     # gated off by default
        assert {'navigate', 'snapshot', 'act', 'requests', 'request',
                'wait', 'seed_storage'} <= names
        assert 'evaluate' in {t['name'] for t in TOOLS}    # exists, just hidden

    async def test_evaluate_gated_off_by_default(self):
        result = await self.call('evaluate', expression='1+1')
        assert result['isError'] is True
        assert '--allow-eval' in result['content'][0]['text']

    async def test_allow_eval_exposes_and_runs_evaluate(self):
        async def factory():
            session = await purecdp.new_session(self.conn, 'about:blank')
            return await Page.create(session, default_timeout=5.0)
        server = MCPServer(page_factory=factory, allow_eval=True)
        listed = await server.handle(
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        assert 'evaluate' in {t['name'] for t in listed['result']['tools']}
        self.fake.evaluate_results = [{'result': {'type': 'number', 'value': 2}}]
        resp = await server.handle({
            'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
            'params': {'name': 'evaluate', 'arguments': {'expression': '1+1'}}})
        assert resp['result']['content'][0]['text'] == '2'
        assert not resp['result'].get('isError')

    async def test_evaluate_args_use_call_function_on(self):
        async def factory():
            session = await purecdp.new_session(self.conn, 'about:blank')
            return await Page.create(session, default_timeout=5.0)
        server = MCPServer(page_factory=factory, allow_eval=True)
        self.fake.evaluate_results = [           # the globalThis anchor
            {'result': {'type': 'object', 'objectId': 'G-1'}}]
        self.fake.call_results = [{'result': {'type': 'number', 'value': 5}}]
        resp = await server.handle({
            'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
            'params': {'name': 'evaluate', 'arguments': {
                'expression': '(a, b) => a + b', 'args': [2, 3]}}})
        assert resp['result']['content'][0]['text'] == '5'
        sent = [m for m in self.transport.sent
                if m['method'] == 'Runtime.callFunctionOn']
        assert sent[0]['params']['arguments'] == [{'value': 2}, {'value': 3}]

    async def test_snapshot_scope_dialog_passes_through(self):
        result = await self.call('snapshot', scope='dialog')
        text = result['content'][0]['text']
        assert text.startswith('(no open dialog')   # fake tree has no dialog
        assert not result.get('isError')

    async def test_navigate_returns_snapshot_text(self):
        result = await self.call('navigate', url='https://x/')
        text = result['content'][0]['text']
        assert 'button "Go"' in text and '[e' in text
        assert not result.get('isError')

    async def test_act_click_by_ref(self):
        # snapshot first so the ref exists (e1 = the button; the RootWebArea
        # has no backend node so gets no ref), then click it
        await self.call('snapshot')
        # act() defaults stable=True: first callFunctionOn is the actionability
        # probe (return ok), then mouse_click measures the element's center
        self.fake.call_results = [
            {'result': {'type': 'object', 'value': {'ok': True}}},
            {'result': {'type': 'object', 'value': {'x': 5, 'y': 6}}}]
        result = await self.call('act', ref='e1', action='click')
        assert not result.get('isError'), result
        assert any(m['method'] == 'DOM.resolveNode'
                   for m in self.transport.sent)
        assert any(m['method'] == 'Input.dispatchMouseEvent'
                   for m in self.transport.sent)

    async def test_unknown_tool_reports_inband_error(self):
        result = await self.call('nope')
        assert result['isError'] is True
        assert 'unknown tool' in result['content'][0]['text']

    async def test_unknown_method_is_jsonrpc_error(self):
        resp = await self.server.handle(
            {'jsonrpc': '2.0', 'id': 7, 'method': 'bogus/thing'})
        assert resp['error']['code'] == -32601


class MCPNetworkToolTests(unittest.IsolatedAsyncioTestCase):
    '''The M15 network/wait/seed tools, over a FakeBrowser page. Exchanges are
    injected into the server's recorder directly — the recorder wiring itself is
    covered in test_testing; here we test the MCP projection/cursor layer.'''

    async def asyncSetUp(self):
        self.fake = FakeBrowser()
        self.fake.ax_nodes = [
            ax('1', role='RootWebArea', name='Home', children=['2']),
            ax('2', role='button', name='Go', backend=20)]
        self.transport = FakeTransport(self.fake)
        self.conn = Connection(self.transport)
        await self.conn.open()

        async def factory():
            session = await purecdp.new_session(self.conn, 'about:blank')
            return await Page.create(session, default_timeout=5.0)

        self.server = MCPServer(page_factory=factory)

    async def asyncTearDown(self):
        await self.conn.aclose()

    async def call(self, _tool, **arguments):
        resp = await self.server.handle({
            'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
            'params': {'name': _tool, 'arguments': arguments}})
        return resp['result']['content'][0]['text']

    async def _seed(self):
        from purecdp.testing import Exchange
        await self.call('snapshot')          # triggers _ensure_page -> recorder
        rec = self.server._recorder
        rec.exchanges.append(Exchange(
            url='https://x/api/query', method='POST', request_body='{"ask": 1}',
            status=200, mime='application/json',
            body=b'{"selections": {"filters": {"logic": "AND"}}, "status": "ok"}',
            request_headers={'Cookie': 's=1'},
            response_headers={'Set-Cookie': 's=1'}))
        rec.exchanges.append(Exchange(
            url='https://x/page.html', method='GET', status=200,
            mime='text/html', body=b'<html>' + b'x' * 5000 + b'</html>'))
        return rec

    async def test_requests_lists_metadata_with_cursor(self):
        await self._seed()
        out = await self.call('requests')
        assert '[0] POST https://x/api/query -> 200' in out
        assert '[1] GET https://x/page.html -> 200' in out
        assert '[cursor: 2]' in out
        assert 'selections' not in out       # bodies NOT in the list view

    async def test_requests_since_and_contains_and_json_filter(self):
        await self._seed()
        assert '[0]' not in await self.call('requests', since=1)
        only = await self.call('requests', contains='query')
        assert '[0]' in only and '[1]' not in only
        js = await self.call('requests', only_json=True)
        assert 'api/query' in js and 'page.html' not in js

    async def test_request_shape_first_default(self):
        await self._seed()
        out = await self.call('request', id=0)
        assert 'shape: ["selections", "status"]' in out
        assert 'preview:' in out
        assert 'status: 200' in out

    async def test_request_select_projects_subtree(self):
        await self._seed()
        out = await self.call('request', id=0, select='selections.filters')
        assert '"logic": "AND"' in out
        bad = await self.call('request', id=0, select='selections.nope')
        assert 'error' in bad and 'nope' in bad

    async def test_request_headers_show_wire_cookies(self):
        await self._seed()
        out = await self.call('request', id=0, part='headers')
        assert 'Cookie' in out and 'Set-Cookie' in out

    async def test_request_bad_id_is_inband_error(self):
        await self._seed()
        resp = await self.server.handle({
            'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
            'params': {'name': 'request', 'arguments': {'id': 99}}})
        assert resp['result']['isError'] is True
        assert 'no exchange 99' in resp['result']['content'][0]['text']

    async def test_get_body_pages_large_raw(self):
        await self._seed()
        out = await self.call('get_body', id=1, offset=0, limit=100)
        assert '<html>' in out
        assert 'get_body(offset=100)' in out      # more remains
        assert 'of 5013' in out                   # total length noted

    async def test_record_narrows_going_forward_preserving_ids(self):
        rec = await self._seed()
        out = await self.call('record', needle='/api')
        assert rec._needle == '/api'
        assert len(rec.exchanges) == 2            # ids preserved
        assert 'ids preserved' in out

    async def test_wait_visible_passes_on_probe(self):
        await self.call('snapshot')               # ensure page
        # queue the probe reducer's observation: visible=True
        self.fake.evaluate_results.append({'result': {'type': 'object', 'value': {
            'count': 1, 'present': True, 'visible': True, 'enabled': True,
            'checked': False, 'text': 'hi', 'value': None}}})
        out = await self.call('wait', **{'for': 'visible', 'selector': '#x'})
        assert out.startswith('ok:')

    async def test_seed_storage_plants_init_script(self):
        await self.call('snapshot')
        out = await self.call('seed_storage', kind='session', key='tok', value='abc')
        sent = [m for m in self.transport.sent
                if m['method'] == 'Page.addScriptToEvaluateOnNewDocument']
        assert sent and 'tok' in sent[-1]['params']['source']
        assert 'next navigation' in out

    async def test_new_tools_are_advertised(self):
        names = {t['name'] for t in TOOLS}
        assert {'requests', 'request', 'get_body', 'record', 'wait',
                'seed_storage', 'screenshot', 'set_cookie'} <= names

    async def test_screenshot_returns_image_block(self):
        import base64
        await self.call('snapshot')
        resp = await self.server.handle({
            'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
            'params': {'name': 'screenshot', 'arguments': {}}})
        block = resp['result']['content'][0]
        assert block['type'] == 'image' and block['mimeType'] == 'image/png'
        assert base64.b64decode(block['data']).startswith(b'\x89PNG')

    async def test_set_cookie_sends_setcookies(self):
        await self.call('snapshot')
        out = await self.call('set_cookie', name='sid', value='xyz',
                              url='https://x.example/')
        sent = [m for m in self.transport.sent
                if m['method'] == 'Network.setCookies']
        assert sent and sent[-1]['params']['cookies'][0] == {
            'name': 'sid', 'value': 'xyz', 'url': 'https://x.example/'}
        assert 'sid' in out

    async def test_set_cookie_defaults_url_to_current_page(self):
        await self.call('snapshot')
        self.fake.evaluate_results.append(
            {'result': {'type': 'string', 'value': 'https://cur.example/p'}})
        await self.call('set_cookie', name='a', value='b')
        sent = [m for m in self.transport.sent
                if m['method'] == 'Network.setCookies']
        assert sent[-1]['params']['cookies'][0]['url'] == 'https://cur.example/p'

    async def test_act_by_description_requires_a_target(self):
        result = await self.call('act', action='click')   # no ref, no by
        assert 'ref=' in result and 'by=' in result        # in-band guidance

    async def test_snapshot_json_format_returns_ref_rows(self):
        import json as json_mod
        out = await self.call('snapshot', format='json')
        rows = json_mod.loads(out)
        assert isinstance(rows, list)
        assert all('ref' in r for r in rows)               # only actionable rows
        assert any(r.get('role') == 'button' for r in rows)  # the 'Go' button

    async def test_mock_gated_off_by_default(self):
        result = await self.call('mock', url='*/api/*', json={'ok': True})
        assert 'disabled' in result and '--allow-mock' in result


class MCPMockToolTests(unittest.IsolatedAsyncioTestCase):
    '''The mock tool needs allow_mock and a real interception path; drive it
    through the FakeBrowser Fetch domain like test_testing's InterceptTests.'''

    async def asyncSetUp(self):
        self.fake = FakeBrowser()
        self.fake.ax_nodes = [ax('1', role='RootWebArea', name='H')]
        self.transport = FakeTransport(self.fake)
        self.conn = Connection(self.transport)
        await self.conn.open()

        async def factory():
            session = await purecdp.new_session(self.conn, 'about:blank')
            return await Page.create(session, default_timeout=5.0)

        self.server = MCPServer(page_factory=factory, allow_mock=True)

    async def asyncTearDown(self):
        await self.conn.aclose()

    async def call(self, _tool, **arguments):
        resp = await self.server.handle({
            'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
            'params': {'name': _tool, 'arguments': arguments}})
        return resp['result']['content'][0]['text']

    async def test_mock_advertised_and_fulfills_matching_request(self):
        listed = await self.server.handle(
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        assert 'mock' in {t['name'] for t in listed['result']['tools']}

        out = await self.call('mock', url='*/api/*', json={'ok': True}, status=201)
        assert 'mocking' in out and '201' in out

        # a matching paused request gets fulfilled with the canned response
        self.transport.push({
            'method': 'Fetch.requestPaused',
            'sessionId': self.server._page.session.session_id,
            'params': {'requestId': 'R1', 'frameId': 'F-1',
                       'resourceType': 'Fetch',
                       'request': {'url': 'https://x/api/cart', 'method': 'GET',
                                   'headers': {}, 'initialPriority': 'High',
                                   'referrerPolicy': 'no-referrer'}}})
        await drain()
        fulfilled = [m for m in self.transport.sent
                     if m['method'] == 'Fetch.fulfillRequest']
        assert fulfilled and fulfilled[-1]['params']['responseCode'] == 201
        import base64 as b64
        assert b64.b64decode(fulfilled[-1]['params']['body']) == b'{"ok": true}'


class MCPStdioSmokeTest(unittest.TestCase):
    '''`python -m purecdp.mcp` really speaks JSON-RPC over stdio. initialize +
    tools/list need no browser (it launches lazily), so this stays fast.'''

    def test_stdio_handshake(self):
        import json
        import os
        import subprocess

        env = {**os.environ, 'PYTHONPATH': _SRC}
        lines = [
            json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                        'params': {}}),
            json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}),
        ]
        proc = subprocess.run(
            [sys.executable, '-m', 'purecdp.mcp'],
            input='\n'.join(lines) + '\n', capture_output=True, text=True,
            env=env, timeout=30)
        out = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
        assert out[0]['result']['serverInfo']['name'] == 'purecdp'
        advertised = {t['name'] for t in out[1]['result']['tools']}
        assert 'evaluate' not in advertised          # gated off by default
        assert {'navigate', 'act', 'requests', 'request'} <= advertised


if __name__ == '__main__':
    unittest.main()
