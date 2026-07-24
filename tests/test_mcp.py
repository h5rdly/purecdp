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
    from .support import FakeBrowser, FakeTransport, ax
except ImportError:
    from support import FakeBrowser, FakeTransport, ax

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
        assert names == {'navigate', 'snapshot', 'act'}   # evaluate gated off
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
        assert {t['name'] for t in out[1]['result']['tools']} == \
            {'navigate', 'snapshot', 'act'}   # evaluate gated off by default


if __name__ == '__main__':
    unittest.main()
