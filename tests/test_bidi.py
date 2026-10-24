'''WebDriver BiDi layer (M20 phase 1): framing + connection, value serde, the
BiDi driver over a scripted fake, and the Firefox launch helpers — all
offline. The real-Firefox leg is tests/test_firefox_e2e.py.

Runnable via `python -m unittest`; plain asserts: don't run with -O.
'''

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import purecdp  # noqa: E402
from purecdp import NotSupported  # noqa: E402
from purecdp.bidi import BidiCommandError, BidiConnection  # noqa: E402
from purecdp.bidi.serde import from_remote, handle_of, to_local  # noqa: E402
from purecdp.errors import CDPConnectionClosed  # noqa: E402
from purecdp.testing import Handle, JSError, Page  # noqa: E402
from purecdp.testing._bidi_driver import BidiDriver  # noqa: E402
from purecdp.testing._driver import Console, Exception_, Failed, Finished, Load, Request  # noqa: E402
from support import FakeTransport, drain  # noqa: E402

#: The RemoteValue Firefox 157 answered for
#: (n) => ({ok: true, n, wd: navigator.webdriver, el: document.body}) with
#: resultOwnership "root" (from M20-bidi-probe.py, 2026-10-23).
PROBE_REMOTE = {
    'type': 'object', 'handle': '594052eb-b7d3-4aad-9842-ce6329d688ac',
    'value': [
        ['ok', {'type': 'boolean', 'value': True}],
        ['n', {'type': 'number', 'value': 2}],
        ['wd', {'type': 'boolean', 'value': True}],
        ['el', {'type': 'node', 'sharedId': '4ab29d6b-76b3-4949-92cb-852ebfc05747',
                'handle': 'h-el',
                'value': {'nodeType': 1, 'localName': 'body',
                          'namespaceURI': 'http://www.w3.org/1999/xhtml',
                          'childNodeCount': 0, 'attributes': {}}}],
    ],
}


class SerdeTests(unittest.TestCase):
    def test_python_to_local_value(self):
        assert to_local(None) == {'type': 'null'}
        assert to_local(True) == {'type': 'boolean', 'value': True}
        assert to_local(2) == {'type': 'number', 'value': 2}
        assert to_local(2.5) == {'type': 'number', 'value': 2.5}
        assert to_local('x') == {'type': 'string', 'value': 'x'}
        assert to_local([1, 'a']) == {'type': 'array', 'value': [
            {'type': 'number', 'value': 1}, {'type': 'string', 'value': 'a'}]}
        assert to_local({'k': [None]}) == {'type': 'object', 'value': [
            ['k', {'type': 'array', 'value': [{'type': 'null'}]}]]}

    def test_special_numbers_both_ways(self):
        assert to_local(float('nan')) == {'type': 'number', 'value': 'NaN'}
        assert to_local(float('inf')) == {'type': 'number', 'value': 'Infinity'}
        assert to_local(float('-inf')) == {'type': 'number', 'value': '-Infinity'}
        assert to_local(-0.0) == {'type': 'number', 'value': '-0'}
        assert from_remote({'type': 'number', 'value': 'NaN'}) != from_remote(
            {'type': 'number', 'value': 'NaN'})  # nan
        assert from_remote({'type': 'number', 'value': '-Infinity'}) == float('-inf')
        assert str(from_remote({'type': 'number', 'value': '-0'})) == '-0.0'

    def test_handles_become_remote_references(self):
        assert to_local(Handle('h1')) == {'handle': 'h1'}
        assert to_local(Handle('h1', node_id='s1')) == {'sharedId': 's1', 'handle': 'h1'}
        assert to_local(Handle('s1', node_id='s1')) == {'sharedId': 's1'}

        class ElementLike:
            _handle = Handle('h9', node_id='s9')
        assert to_local(ElementLike()) == {'sharedId': 's9', 'handle': 'h9'}

    def test_non_json_raises_type_error(self):
        try:
            to_local(object())
        except TypeError as exc:
            assert 'neither JSON-like nor a Handle' in str(exc)
        else:
            raise AssertionError('expected TypeError')
        try:
            to_local({1: 2})
        except TypeError as exc:
            assert 'keys must be str' in str(exc)
        else:
            raise AssertionError('expected TypeError')

    def test_probe_remote_value_unwraps_to_python_with_node_handles(self):
        value = from_remote(PROBE_REMOTE)
        assert value['ok'] is True and value['n'] == 2 and value['wd'] is True
        el = value['el']
        assert isinstance(el, Handle)
        assert el.id == 'h-el' and el.node_id == '4ab29d6b-76b3-4949-92cb-852ebfc05747'
        assert el.raw['value']['localName'] == 'body'
        top = handle_of(PROBE_REMOTE)
        assert top is not None and top.id.startswith('594052eb') and top.node_id is None

    def test_node_without_handle_is_keyed_by_shared_id(self):
        node = {'type': 'node', 'sharedId': 's-7', 'value': {'nodeType': 1}}
        h = from_remote(node)
        assert h == Handle('s-7', node_id='s-7')
        assert handle_of({'type': 'number', 'value': 1}) is None

    def test_other_remote_types(self):
        assert from_remote({'type': 'undefined'}) is None
        assert from_remote({'type': 'bigint', 'value': '12'}) == 12
        assert from_remote({'type': 'date', 'value': '2026-10-23T00:00:00Z'}) == '2026-10-23T00:00:00Z'
        assert from_remote({'type': 'regexp', 'value': {'pattern': 'a+', 'flags': 'i'}}) == 'a+'
        assert from_remote({'type': 'map', 'value': [['k', {'type': 'number', 'value': 1}]]}) == {'k': 1}
        assert from_remote({'type': 'set', 'value': [{'type': 'string', 'value': 'x'}]}) == ['x']
        # by reference only: a function with a handle -> Handle; without -> None
        assert from_remote({'type': 'function', 'handle': 'f1'}) == Handle('f1')
        assert from_remote({'type': 'function'}) is None
        # an array past the serialization depth has no 'value'
        assert from_remote({'type': 'array', 'handle': 'a1'}) == Handle('a1')


class FakeBidi:
    '''Scripted BiDi peer for FakeTransport: answers by method, records
    calls, can queue canned results/errors per method.'''

    def __init__(self):
        self.calls: list[dict] = []
        self.results: dict[str, list] = {}   # method -> queue of result dicts or ('error', code, msg)
        self.contexts = ['CTX-1']
        self.tree = None   # a canned browsingContext.getTree result, if set

    def queue(self, method, *results):
        self.results.setdefault(method, []).extend(results)

    def __call__(self, msg):
        self.calls.append(msg)
        method, mid, params = msg['method'], msg['id'], msg.get('params', {})
        canned = self.results.get(method)
        if canned:
            item = canned.pop(0)
            if isinstance(item, tuple) and item and item[0] == 'error':
                return [{'type': 'error', 'id': mid, 'error': item[1], 'message': item[2]}]
            return [{'type': 'success', 'id': mid, 'result': item}]
        if method == 'session.new':
            return [{'type': 'success', 'id': mid, 'result': {
                'sessionId': 'S-1', 'capabilities': {'browserName': 'firefox',
                                                      'browserVersion': '157.0.1'}}}]
        if method == 'session.subscribe':
            return [{'type': 'success', 'id': mid, 'result': {}}]
        if method == 'browsingContext.getTree':
            tree = self.tree or {'contexts': [
                {'context': c, 'url': 'about:blank', 'children': []} for c in self.contexts]}
            return [{'type': 'success', 'id': mid, 'result': tree}]
        if method == 'browsingContext.create':
            ctx = f'CTX-{len(self.contexts) + 1}'
            self.contexts.append(ctx)
            return [{'type': 'success', 'id': mid, 'result': {'context': ctx}}]
        if method in ('script.evaluate', 'script.callFunction'):
            return [{'type': 'success', 'id': mid, 'result': {
                'type': 'success', 'result': {'type': 'undefined'}, 'realm': 'R'}}]
        if method == 'network.addDataCollector':
            return [{'type': 'success', 'id': mid, 'result': {'collector': f'C-{mid}'}}]
        if method == 'network.addIntercept':
            return [{'type': 'success', 'id': mid, 'result': {'intercept': f'I-{mid}'}}]
        if method == 'network.getData':
            return [{'type': 'error', 'id': mid, 'error': 'no such network data',
                     'message': f"no data for {params.get('request')}"}]
        return [{'type': 'success', 'id': mid, 'result': {}}]


def event(method, params):
    return {'type': 'event', 'method': method, 'params': params}


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeBidi()
        self.transport = FakeTransport(self.fake)
        self.conn = BidiConnection(self.transport)
        await self.conn.open()

    async def asyncTearDown(self):
        await self.conn.aclose()

    async def test_call_frames_and_correlates(self):
        caps = await self.conn.new_session({'acceptInsecureCerts': True})
        sent = self.transport.sent[0]
        assert sent == {'id': 1, 'method': 'session.new',
                        'params': {'capabilities': {'alwaysMatch': {'acceptInsecureCerts': True}}}}
        assert caps['browserName'] == 'firefox'
        assert self.conn.session_id == 'S-1'
        assert self.conn.capabilities['browserVersion'] == '157.0.1'

    async def test_error_reply_raises_bidi_command_error(self):
        self.fake.queue('browsingContext.navigate',
                        ('error', 'invalid argument', 'Expected "url" to be a string'))
        try:
            await self.conn.call('browsingContext.navigate', {})
        except BidiCommandError as exc:
            assert exc.code == 'invalid argument'
            assert 'Expected' in exc.message
            assert str(exc) == '[invalid argument] Expected "url" to be a string'
            assert isinstance(exc, purecdp.PureCDPError)
        else:
            raise AssertionError('expected BidiCommandError')

    async def test_events_reach_listeners_by_method(self):
        with self.conn.listen('log.entryAdded') as stream:
            self.transport.push(event('browsingContext.load', {'context': 'CTX-1'}))
            self.transport.push(event('log.entryAdded', {'text': 'hi', 'type': 'console'}))
            got = await asyncio.wait_for(stream.__anext__(), 2)
        assert got.method == 'log.entryAdded' and got.params['text'] == 'hi'

    async def test_wait_for_with_predicate(self):
        waiter = asyncio.create_task(self.conn.wait_for(
            'browsingContext.load', predicate=lambda e: e.params['context'] == 'CTX-2',
            timeout=2))
        await asyncio.sleep(0)
        self.transport.push(event('browsingContext.load', {'context': 'CTX-1'}))
        self.transport.push(event('browsingContext.load', {'context': 'CTX-2'}))
        assert (await waiter).params['context'] == 'CTX-2'

    async def test_subscribe_is_deduplicated_globally(self):
        await self.conn.subscribe(['log.entryAdded', 'browsingContext.load'])
        await self.conn.subscribe(['log.entryAdded'])
        await self.conn.subscribe(['log.entryAdded'], contexts=['CTX-1'])
        subs = [m['params'] for m in self.transport.sent if m['method'] == 'session.subscribe']
        assert subs == [{'events': ['log.entryAdded', 'browsingContext.load']},
                        {'events': ['log.entryAdded'], 'contexts': ['CTX-1']}]

    async def test_peer_close_fails_pending_and_ends_streams(self):
        stream = self.conn.listen()
        self.fake.results['script.evaluate'] = [None]  # swallow: no reply ever

        def no_reply(msg):
            return []
        self.transport._responder = no_reply
        pending = asyncio.create_task(self.conn.call('script.evaluate', {}))
        await asyncio.sleep(0)
        self.transport.end()
        try:
            await asyncio.wait_for(pending, 2)
        except CDPConnectionClosed as exc:
            assert 'closed by peer' in str(exc)
        else:
            raise AssertionError('pending call survived the close')
        assert self.conn.closed
        try:
            await stream.__anext__()
        except StopAsyncIteration:
            pass
        else:
            raise AssertionError('stream did not end')

    async def test_timeout_names_the_method(self):
        self.transport._responder = lambda msg: []
        try:
            await self.conn.call('script.evaluate', {}, timeout=0.05)
        except purecdp.CDPCommandTimeout as exc:
            assert 'script.evaluate' in str(exc)
        else:
            raise AssertionError('expected CDPCommandTimeout')
        assert self.conn._pending_methods == {} and self.conn._futures == {}

    async def test_stream_overflow_drops_oldest_and_warns_once(self):
        # the warning fires in the connection's reader task; on free-threaded
        # 3.14 warnings.catch_warnings is context-aware and would not see it,
        # so observe the module-level warn() instead
        from unittest import mock
        with self.conn.listen('log.entryAdded', buffer_size=2) as stream:
            with mock.patch('purecdp.bidi.connection.warnings.warn') as warn:
                for i in range(4):
                    self.transport.push(event('log.entryAdded', {'text': str(i)}))
                await drain()
            assert warn.call_count == 1
            assert warn.call_args.args[1] is RuntimeWarning
            assert 'buffer full' in warn.call_args.args[0]
            got = [(await stream.__anext__()).params['text'] for _ in range(2)]
        assert got == ['2', '3']


class BidiDriverTests(unittest.IsolatedAsyncioTestCase):
    '''The seam over BiDi, driven through a real Page (arm + pumps) against
    the scripted fake.'''

    async def asyncSetUp(self):
        self.fake = FakeBidi()
        self.transport = FakeTransport(self.fake)
        self.conn = BidiConnection(self.transport)
        await self.conn.open()
        await self.conn.new_session()
        self.driver = BidiDriver(self.conn, 'CTX-1')
        self.page = await Page._create_with(self.driver, default_timeout=2.0)

    async def asyncTearDown(self):
        await self.page.stop()
        await self.conn.aclose()

    def sent(self, method):
        return [m for m in self.transport.sent if m['method'] == method]

    def script_result(self, remote):
        return {'type': 'success', 'result': remote, 'realm': 'R'}

    async def test_page_wiring_and_arm_subscribes(self):
        assert self.page.backend == 'bidi' and self.page.session is None
        assert self.page.bidi is self.conn and self.page.alive
        subs = self.sent('session.subscribe')[0]['params']['events']
        assert 'log.entryAdded' in subs and 'browsingContext.load' in subs
        assert 'network.beforeRequestSent' in subs

    async def test_evaluate_by_value_and_by_handle(self):
        self.fake.queue('script.evaluate', self.script_result({'type': 'number', 'value': 2}))
        assert await self.page.evaluate('1 + 1') == 2
        params = self.sent('script.evaluate')[-1]['params']
        assert params['target'] == {'context': 'CTX-1'}
        assert params['awaitPromise'] is True and params['resultOwnership'] == 'none'
        self.fake.queue('script.evaluate', self.script_result(
            {'type': 'node', 'sharedId': 's-body', 'handle': 'h-body', 'value': {}}))
        handle = await self.page.evaluate('document.body', return_by_value=False)
        assert handle == Handle('h-body', node_id='s-body')
        assert self.sent('script.evaluate')[-1]['params']['resultOwnership'] == 'root'

    async def test_call_with_args_encodes_local_values_and_references(self):
        from purecdp.testing import Element
        self.fake.queue('script.callFunction', self.script_result({'type': 'string', 'value': 'ok'}))
        el = Element(self.page, Handle('h-el', node_id='s-el'))
        assert await self.page.evaluate('(a, el, o) => "ok"', 2, el, {'k': [1]}) == 'ok'
        params = self.sent('script.callFunction')[-1]['params']
        assert params['functionDeclaration'] == '(a, el, o) => "ok"'
        assert params['arguments'] == [
            {'type': 'number', 'value': 2},
            {'sharedId': 's-el', 'handle': 'h-el'},
            {'type': 'object', 'value': [['k', {'type': 'array', 'value': [
                {'type': 'number', 'value': 1}]}]]}]

    async def test_element_eval_goes_through_call_function(self):
        from purecdp.testing import Element
        self.fake.queue('script.callFunction', self.script_result({'type': 'string', 'value': 'DIV'}))
        el = Element(self.page, Handle('h-el', node_id='s-el'))
        assert await el.eval('(el) => el.tagName') == 'DIV'
        params = self.sent('script.callFunction')[-1]['params']
        assert params['arguments'] == [{'sharedId': 's-el', 'handle': 'h-el'}]

    async def test_js_exception_becomes_jserror(self):
        self.fake.queue('script.evaluate', {
            'type': 'exception', 'realm': 'R',
            'exceptionDetails': {'text': 'ReferenceError: nope is not defined',
                                 'lineNumber': 1, 'columnNumber': 1,
                                 'exception': {'type': 'error'}, 'stackTrace': {}}})
        try:
            await self.page.evaluate('nope')
        except JSError as exc:
            assert 'nope is not defined' in str(exc)
            assert exc.details['lineNumber'] == 1
        else:
            raise AssertionError('expected JSError')

    async def test_non_serializable_arg_raises_typeerror(self):
        try:
            await self.page.evaluate('(x) => x', object())
        except TypeError as exc:
            assert 'argument 0' in str(exc)
        else:
            raise AssertionError('expected TypeError')

    async def test_release_disowns_only_real_handles(self):
        await self.driver.release(Handle('s-1', node_id='s-1'))
        assert self.sent('script.disown') == []
        await self.driver.release(Handle('h-1', node_id='s-1'))
        assert self.sent('script.disown')[-1]['params'] == {
            'handles': ['h-1'], 'target': {'context': 'CTX-1'}}

    async def test_query_all_unwraps_an_array_of_nodes(self):
        self.fake.queue('script.evaluate', self.script_result(
            {'type': 'array', 'handle': 'h-arr', 'value': []}))
        self.fake.queue('script.callFunction', self.script_result(
            {'type': 'array', 'handle': 'h-arr2', 'value': [
                {'type': 'node', 'sharedId': 's-a', 'value': {}},
                {'type': 'node', 'sharedId': 's-b', 'value': {}}]}))
        elements = await self.page.query_all('li')
        assert [e._handle.node_id for e in elements] == ['s-a', 's-b']
        assert self.sent('script.disown')[-1]['params']['handles'] == ['h-arr2']

    async def test_goto_navigates_and_waits_for_load(self):
        self.fake.queue('browsingContext.navigate', {'navigation': 'N-1', 'url': 'http://x/'})

        async def load_later():
            await asyncio.sleep(0.02)
            self.transport.push(event('browsingContext.load', {'context': 'CTX-9'}))  # other tab
            self.transport.push(event('browsingContext.load', {'context': 'CTX-1', 'url': 'http://x/'}))
        asyncio.create_task(load_later())
        await self.page.goto('http://x/')
        params = self.sent('browsingContext.navigate')[-1]['params']
        assert params == {'context': 'CTX-1', 'url': 'http://x/', 'wait': 'none'}

    async def test_navigate_error_becomes_navigate_error(self):
        from purecdp.testing import NavigateError
        self.fake.queue('browsingContext.navigate',
                        ('error', 'unknown error', 'NS_ERROR_CONNECTION_REFUSED'))
        try:
            await self.page.goto('http://127.0.0.1:1/', wait='none')
        except NavigateError as exc:
            assert 'NS_ERROR_CONNECTION_REFUSED' in exc.net_error
        else:
            raise AssertionError('expected NavigateError')

    async def test_console_and_exception_capture(self):
        self.transport.push(event('log.entryAdded', {
            'type': 'console', 'level': 'info', 'method': 'log', 'text': 'hello 1',
            'source': {'realm': 'R', 'context': 'CTX-1'}, 'args': []}))
        self.transport.push(event('log.entryAdded', {
            'type': 'javascript', 'level': 'error', 'text': 'Error: kaboom',
            'source': {'realm': 'R', 'context': 'CTX-1'}}))
        self.transport.push(event('log.entryAdded', {   # another tab's console
            'type': 'console', 'level': 'info', 'method': 'log', 'text': 'not mine',
            'source': {'realm': 'R2', 'context': 'CTX-2'}, 'args': []}))
        await drain()
        assert [(m.kind, m.text) for m in self.page.console] == [
            ('log', 'hello 1'), ('error', 'Error: kaboom')]
        assert len(self.page.js_errors) == 1 and 'kaboom' in str(self.page.js_errors[0])

    async def test_network_tracking_for_idle_waits(self):
        req = {'request': 'R-1', 'url': 'http://x/api', 'method': 'GET',
               'headers': [{'name': 'Accept', 'value': {'type': 'string', 'value': '*/*'}}]}
        self.transport.push(event('network.beforeRequestSent', {
            'context': 'CTX-1', 'navigation': None, 'request': req,
            'timestamp': 1000, 'initiator': {'type': 'fetch'}}))
        await drain()
        assert self.page._inflight == {'R-1'}
        self.transport.push(event('network.responseCompleted', {
            'context': 'CTX-1', 'request': req, 'timestamp': 1200,
            'response': {'status': 200, 'bytesReceived': 12}}))
        await drain()
        assert self.page._inflight == set()
        # a document request that fails surfaces as the frame's net error
        self.transport.push(event('network.beforeRequestSent', {
            'context': 'CTX-1', 'navigation': 'N-2', 'request': {**req, 'request': 'R-2'},
            'timestamp': 1300, 'initiator': {'type': 'other'}}))
        self.transport.push(event('network.fetchError', {
            'context': 'CTX-1', 'request': {**req, 'request': 'R-2'},
            'timestamp': 1400, 'errorText': 'NS_ERROR_UNKNOWN_HOST'}))
        await drain()
        assert self.page._doc_failures == {'CTX-1': 'NS_ERROR_UNKNOWN_HOST'}

    async def test_unsupported_features_name_themselves(self):
        for coro, feature in (
                (self.page.emulate_media(color_scheme='dark'), 'media emulation'),):
            try:
                await coro
            except NotSupported as exc:
                assert exc.feature == feature and exc.backend == 'bidi', (feature, str(exc))
            else:
                raise AssertionError(f'{feature}: expected NotSupported')

    async def test_screenshot_pdf_dialog_viewport_files_init_script(self):
        from purecdp.testing import Element
        import base64
        self.fake.queue('browsingContext.captureScreenshot', {'data': base64.b64encode(b'PNG').decode()})
        assert await self.page.screenshot(full_page=True) == b'PNG'
        p = self.sent('browsingContext.captureScreenshot')[-1]['params']
        assert p['origin'] == 'document' and p['format'] == {'type': 'image/png'}
        self.fake.queue('browsingContext.print', {'data': base64.b64encode(b'%PDF').decode()})
        assert await self.page.pdf(landscape=True, paper_width=8.5) == b'%PDF'
        p = self.sent('browsingContext.print')[-1]['params']
        assert p['orientation'] == 'landscape' and abs(p['page']['width'] - 21.59) < 0.01
        await self.page.set_viewport(800, 600, device_scale_factor=2)
        assert self.sent('browsingContext.setViewport')[-1]['params'] == {
            'context': 'CTX-1', 'viewport': {'width': 800, 'height': 600},
            'devicePixelRatio': 2}
        await Element(self.page, Handle('h', node_id='s-file')).set_input_files('/tmp/a.txt')
        assert self.sent('input.setFiles')[-1]['params'] == {
            'context': 'CTX-1', 'element': {'sharedId': 's-file'}, 'files': ['/tmp/a.txt']}
        await self.page.add_init_script('window.seeded = 1')
        p = self.sent('script.addPreloadScript')[-1]['params']
        assert 'window.seeded = 1' in p['functionDeclaration'] and p['contexts'] == ['CTX-1']
        seen = await self.page.handle_dialogs(accept=False)
        self.transport.push(event('browsingContext.userPromptOpened', {
            'context': 'CTX-1', 'type': 'confirm', 'message': 'sure?'}))
        await drain()
        assert seen == ['sure?']
        assert self.sent('browsingContext.handleUserPrompt')[-1]['params'] == {
            'context': 'CTX-1', 'accept': False}

    async def test_close_closes_the_context(self):
        await self.page.close()
        assert self.sent('browsingContext.close')[-1]['params'] == {'context': 'CTX-1'}
        assert not self.page.alive

    # -- phase 2: input, cookies, emulation, frames -------------------------

    async def test_press_and_type_become_perform_actions(self):
        from purecdp.testing import Element
        await self.page.press('Enter')
        calls = [m['params'] for m in self.sent('input.performActions')]
        assert calls[-2]['actions'] == [{'type': 'key', 'id': 'purecdp-keyboard',
                                          'actions': [{'type': 'keyDown', 'value': '\ue007'}]}]
        assert calls[-1]['actions'][0]['actions'] == [{'type': 'keyUp', 'value': '\ue007'}]
        await self.page.insert_text('ab')
        assert self.sent('input.performActions')[-1]['params']['actions'][0]['actions'] == [
            {'type': 'keyDown', 'value': 'a'}, {'type': 'keyUp', 'value': 'a'},
            {'type': 'keyDown', 'value': 'b'}, {'type': 'keyUp', 'value': 'b'}]
        # Element.type: focus check in JS, then per-char keys
        self.fake.queue('script.callFunction', self.script_result({'type': 'string', 'value': 'ok'}))
        before = len(self.sent('input.performActions'))
        await Element(self.page, Handle('h', node_id='s')).type('é')
        assert len(self.sent('input.performActions')) == before + 2
        try:
            await self.page.press('F13')
        except ValueError as exc:
            assert 'F13' in str(exc)
        else:
            raise AssertionError('unknown multi-char key must not be sent')

    async def test_mouse_click_wheel_and_human_motion(self):
        from purecdp.testing import Element
        self.fake.queue('script.callFunction', self.script_result(
            {'type': 'object', 'value': [['x', {'type': 'number', 'value': 10.5}],
                                         ['y', {'type': 'number', 'value': 20}]]}))
        await Element(self.page, Handle('h', node_id='s')).mouse_click()
        pressed, released = [m['params']['actions'][0] for m in self.sent('input.performActions')[-2:]]
        assert pressed == {'type': 'pointer', 'id': 'purecdp-mouse', 'actions': [
            {'type': 'pointerMove', 'x': 10.5, 'y': 20}, {'type': 'pointerDown', 'button': 0}]}
        assert released['actions'][-1] == {'type': 'pointerUp', 'button': 0}
        await self.driver.mouse('mouseMoved', -0.3, -0.7)   # a curve dipping past the origin
        last = self.sent('input.performActions')[-1]['params']['actions'][0]['actions'][0]
        assert (last['x'], last['y']) == (0.0, 0.0)
        await self.page.human_scroll(100, x=5, y=5, steps=2, max_delay=0)
        wheel = self.sent('input.performActions')[-1]['params']['actions'][0]
        assert wheel['type'] == 'wheel' and wheel['actions'][0]['type'] == 'scroll'
        assert wheel['actions'][0]['x'] == 5
        await self.page.cursor.move_to(50, 60, steps=3, max_delay=0)
        moves = [m['params']['actions'][0]['actions'][0]
                 for m in self.sent('input.performActions')[-3:]]
        assert all(a['type'] == 'pointerMove' for a in moves)
        assert (moves[-1]['x'], moves[-1]['y']) == (50, 60)

    async def test_cookies_map_both_ways(self):
        self.fake.queue('storage.getCookies', {'cookies': [
            {'name': 'sid', 'value': {'type': 'string', 'value': 'abc'}, 'domain': 'app.test',
             'path': '/', 'size': 6, 'httpOnly': True, 'secure': False, 'sameSite': 'lax',
             'expiry': 1800000000}], 'partitionKey': {}})
        got = await self.page.cookies()
        assert got == [{'name': 'sid', 'value': 'abc', 'domain': 'app.test', 'path': '/',
                        'secure': False, 'httpOnly': True, 'session': False,
                        'expires': 1800000000, 'sameSite': 'Lax', 'size': 6}]
        assert self.sent('storage.getCookies')[-1]['params'] == {
            'partition': {'type': 'context', 'context': 'CTX-1'}}
        await self.page.set_cookies([
            {'name': 'a', 'value': '1', 'url': 'https://app.test/x', 'secure': True,
             'sameSite': 'Strict', 'expires': 1800000000.7},
            {'name': 'b', 'value': '2', 'domain': '.app.test', 'path': '/p'}])
        sent = [m['params']['cookie'] for m in self.sent('storage.setCookie')]
        assert sent[0] == {'name': 'a', 'value': {'type': 'string', 'value': '1'},
                           'domain': 'app.test', 'secure': True, 'sameSite': 'strict',
                           'expiry': 1800000000}
        assert sent[1] == {'name': 'b', 'value': {'type': 'string', 'value': '2'},
                           'domain': '.app.test', 'path': '/p'}
        await self.page.clear_cookies()
        assert self.sent('storage.deleteCookies')[-1]['params'] == {
            'partition': {'type': 'context', 'context': 'CTX-1'}}

    async def test_emulation_and_extra_headers(self):
        await self.page.set_user_agent('UA-X', accept_language='bg')
        assert self.sent('emulation.setUserAgentOverride')[-1]['params'] == {
            'userAgent': 'UA-X', 'contexts': ['CTX-1']}
        assert self.sent('network.setExtraHeaders')[-1]['params'] == {
            'headers': [{'name': 'Accept-Language', 'value': {'type': 'string', 'value': 'bg'}}],
            'contexts': ['CTX-1']}
        await self.page.set_geolocation(1.5, 2.5, accuracy=3)
        assert self.sent('emulation.setGeolocationOverride')[-1]['params'] == {
            'coordinates': {'latitude': 1.5, 'longitude': 2.5, 'accuracy': 3},
            'contexts': ['CTX-1']}
        await self.page.set_timezone('Asia/Tokyo')
        assert self.sent('emulation.setTimezoneOverride')[-1]['params'] == {
            'timezone': 'Asia/Tokyo', 'contexts': ['CTX-1']}
        await self.page.set_extra_headers({'X-A': '1'})
        names = [h['name'] for h in self.sent('network.setExtraHeaders')[-1]['params']['headers']]
        assert names == ['Accept-Language', 'X-A']   # merged, not replaced

    async def test_frames_come_from_the_context_tree(self):
        from purecdp.testing import Element
        self.fake.tree = {'contexts': [
            {'context': 'CTX-1', 'url': 'http://top/', 'children': [
                {'context': 'CTX-child', 'url': 'http://other/frame', 'children': [
                    {'context': 'CTX-grand', 'url': 'http://other/inner', 'children': []}]}]}]}
        frame = await self.page.frame(url='*other/frame*')
        assert frame._driver.context == 'CTX-child' and frame.frame_id == 'CTX-child'
        assert self.sent('browsingContext.getTree')[-1]['params'] == {'root': 'CTX-1'}
        scopes = self.driver.frame_scopes()
        assert [s.frame_id for s in scopes] == ['CTX-child', 'CTX-grand']
        assert scopes[0].driver is self.driver.frame_scopes()[0].driver  # cached
        # an <iframe> element maps to its context via contentWindow
        self.fake.queue('script.callFunction', self.script_result(
            {'type': 'window', 'value': {'context': 'CTX-child'}}))
        assert await Element(self.page, Handle('h', node_id='s')).frame_id() == 'CTX-child'
        self.fake.queue('script.callFunction', self.script_result({'type': 'null'}))
        assert await Element(self.page, Handle('h', node_id='s')).frame_id() is None

    async def test_stealth_applies_the_gecko_subset(self):
        from purecdp.testing import GECKO_EVASIONS, apply_stealth
        await apply_stealth(self.page)
        script = self.sent('script.addPreloadScript')[-1]['params']['functionDeclaration']
        assert "'webdriver'" in script and 'window.chrome' not in script
        assert 'Google Inc.' not in script
        assert GECKO_EVASIONS == ('navigator.webdriver', 'function.toString')
        assert self.sent('session.subscribe')  # no auto-attach / worker dance on BiDi

    async def test_recorder_records_metadata_without_bodies(self):
        rec = self.page.record(bodies=False)
        req = {'request': 'R-9', 'url': 'http://x/api', 'method': 'POST',
               'headers': [{'name': 'Content-Type', 'value': {'type': 'string', 'value': 'application/json'}}]}
        self.transport.push(event('network.beforeRequestSent', {
            'context': 'CTX-1', 'navigation': None, 'request': req, 'timestamp': 1000,
            'initiator': {'type': 'fetch'}}))
        self.transport.push(event('network.responseStarted', {
            'context': 'CTX-1', 'request': req, 'timestamp': 1100,
            'response': {'status': 201, 'mimeType': 'application/json',
                         'headers': [{'name': 'X-R', 'value': {'type': 'string', 'value': 'y'}}]}}))
        self.transport.push(event('network.responseCompleted', {
            'context': 'CTX-1', 'request': req, 'timestamp': 1200,
            'response': {'status': 201, 'bytesReceived': 40}}))
        await drain()
        assert len(rec.exchanges) == 1
        ex = rec.exchanges[0]
        assert ex.status == 201 and ex.request_headers == {'Content-Type': 'application/json'}
        assert ex.response_headers == {'X-R': 'y'} and ex.body is None
        assert ex.redacted and ex.body_error is None and ex.body_size == 40
        assert self.sent('network.addDataCollector') == []   # bodies=False: no collector

    # -- review fixes (2026-10-24) -------------------------------------------

    async def test_tab_destroyed_elsewhere_flips_alive_and_ends_pumps(self):
        '''The 0.8.0 watcher contract on Firefox: a tab closed by the user /
        window.close() / anyone's browsingContext.close turns page.alive
        False, ends the capture pumps, and makes commands raise a
        CDPClosedError — never a bare "no such frame".'''
        from purecdp import CDPClosedError
        from purecdp.errors import CDPSessionClosed
        assert self.page.alive
        subs = [e for m in self.sent('session.subscribe') for e in m['params']['events']]
        assert 'browsingContext.contextDestroyed' in subs
        pump_tasks = list(self.page._tasks)
        self.transport.push(event('browsingContext.contextDestroyed', {
            'context': 'CTX-1', 'url': 'about:blank', 'children': [],
            'userContext': 'default'}))
        await drain()
        assert not self.page.alive
        assert all(t.done() for t in pump_tasks), 'pumps must end with the tab'
        try:
            await self.page.evaluate('1')
        except CDPSessionClosed as exc:
            assert isinstance(exc, CDPClosedError) and 'CTX-1' in str(exc)
        else:
            raise AssertionError('command on a dead tab must raise CDPSessionClosed')
        assert not any(m['method'] == 'script.evaluate' for m in self.transport.sent[-1:])

    async def test_child_context_destroyed_with_its_parent(self):
        other = BidiDriver(self.conn, 'CTX-child')
        assert other.alive
        self.transport.push(event('browsingContext.contextDestroyed', {
            'context': 'CTX-parent', 'url': 'x', 'children': [
                {'context': 'CTX-child', 'url': 'y', 'children': []}]}))
        await drain()
        assert not other.alive and self.page.alive

    async def test_no_such_frame_becomes_closed_error(self):
        from purecdp.errors import CDPSessionClosed
        self.fake.queue('script.evaluate', ('error', 'no such frame', 'Browsing Context with id CTX-1 not found'))
        try:
            await self.page.evaluate('1')
        except CDPSessionClosed as exc:
            assert 'no such frame' in str(exc)
        else:
            raise AssertionError('expected CDPSessionClosed')
        assert not self.page.alive   # remembered, so the next call is local
        # other command errors are untouched
        self.conn.destroyed_contexts.discard('CTX-1')
        self.fake.queue('script.evaluate', ('error', 'invalid argument', 'nope'))
        try:
            await self.page.evaluate('1')
        except BidiCommandError as exc:
            assert exc.code == 'invalid argument'
        else:
            raise AssertionError('expected BidiCommandError')

    async def test_cookie_url_filter_uses_domain_matching(self):
        from purecdp.testing._bidi_driver import _domain_matches
        assert _domain_matches('a.com', 'a.com') and _domain_matches('www.a.com', '.a.com')
        assert _domain_matches('x.y.a.com', 'a.com')
        assert not _domain_matches('not-a.com', 'a.com')
        assert not _domain_matches('a.com.evil.org', 'a.com')
        assert not _domain_matches('badexample.com', 'example.com')
        assert not _domain_matches('a.com', 'www.a.com')
        assert not _domain_matches(None, 'a.com') and not _domain_matches('a.com', '')
        jar = [{'name': n, 'value': {'type': 'string', 'value': 'v'}, 'domain': d, 'path': '/'}
               for n, d in (('ok', 'a.com'), ('sub', '.a.com'), ('evil', 'not-a.com'))]
        self.fake.queue('storage.getCookies', {'cookies': jar, 'partitionKey': {}})
        got = await self.page.cookies(['https://www.a.com/x'])
        assert [c['name'] for c in got] == ['ok', 'sub']

    async def test_extra_headers_merge_instead_of_replace(self):
        await self.page.set_extra_headers({'X-Test': '1'})
        await self.page.set_user_agent('UA', accept_language='fr')
        sent = self.sent('network.setExtraHeaders')[-1]['params']['headers']
        assert {h['name']: h['value']['value'] for h in sent} == {
            'X-Test': '1', 'Accept-Language': 'fr'}
        await self.page.set_extra_headers({'X-Test': '2', 'X-More': '3'})
        sent = self.sent('network.setExtraHeaders')[-1]['params']['headers']
        assert {h['name']: h['value']['value'] for h in sent} == {
            'X-Test': '2', 'Accept-Language': 'fr', 'X-More': '3'}

    # -- phase 3: interception, bodies, downloads ----------------------------

    def blocked(self, request_id, url, method='GET', ctx='CTX-1'):
        return event('network.beforeRequestSent', {
            'context': ctx, 'isBlocked': True, 'navigation': None, 'redirectCount': 0,
            'intercepts': ['I-1'], 'initiator': {'type': 'fetch'}, 'timestamp': 1000,
            'request': {'request': request_id, 'url': url, 'method': method, 'bodySize': 0,
                        'headers': [{'name': 'Origin', 'value': {'type': 'string', 'value': 'http://app.test'}},
                                    {'name': 'Accept', 'value': {'type': 'string', 'value': '*/*'}}]}})

    async def test_route_fulfill_continue_and_abort(self):
        self.fake.queue('network.addIntercept', {'intercept': 'I-1'})
        seen = []

        async def handler(request):
            seen.append(request)
            if request.url.endswith('/stub'):
                await request.fulfill(json={'ok': 1})
            elif request.url.endswith('/rewrite'):
                await request.continue_(url='http://real/x', headers={'X-A': '1'}, post_data='body')
            elif request.url.endswith('/kill'):
                await request.abort()
            # else: unhandled -> continued untouched

        await self.page.route('*', handler)
        assert self.sent('network.addIntercept')[-1]['params'] == {
            'phases': ['beforeRequestSent'], 'contexts': ['CTX-1']}
        for rid, path in (('R-1', '/stub'), ('R-2', '/rewrite'), ('R-3', '/kill'), ('R-4', '/plain')):
            self.transport.push(self.blocked(rid, 'http://app.test' + path))
        await drain()
        assert [r.url.rsplit('/', 1)[1] for r in seen] == ['stub', 'rewrite', 'kill', 'plain']
        assert seen[0].header('origin') == 'http://app.test' and seen[0].post_data is None
        # a blocked request WITH a body: fetched from the collector before the handler
        self.fake.queue('network.getData', {'bytes': {'type': 'string', 'value': '{"ask": 1}'}})
        posted = self.blocked('R-5', 'http://app.test/plain', method='POST')
        posted['params']['request']['bodySize'] = 10
        self.transport.push(posted)
        await drain()
        assert seen[-1].post_data == '{"ask": 1}' and seen[-1].json == {'ask': 1}
        assert self.sent('network.getData')[-1]['params']['dataType'] == 'request'
        provided = self.sent('network.provideResponse')[-1]['params']
        assert provided['request'] == 'R-1' and provided['statusCode'] == 200
        names = {h['name']: h['value']['value'] for h in provided['headers']}
        assert names == {'Content-Type': 'application/json',
                         'Access-Control-Allow-Origin': 'http://app.test'}
        import base64
        assert base64.b64decode(provided['body']['value']) == b'{"ok": 1}'
        cont = [m['params'] for m in self.sent('network.continueRequest')]
        assert cont[0] == {'request': 'R-2', 'url': 'http://real/x',
                           'body': {'type': 'base64', 'value': base64.b64encode(b'body').decode()},
                           'headers': [{'name': 'X-A', 'value': {'type': 'string', 'value': '1'}}]}
        assert cont[1] == {'request': 'R-4'}   # unhandled: continued untouched
        assert self.sent('network.failRequest')[-1]['params'] == {'request': 'R-3'}
        # the blocked event ALSO counts as in-flight traffic for idle waits
        assert 'R-1' in self.page._inflight
        # route() twice arms the intercept once
        await self.page.route('*/other', handler)
        assert len(self.sent('network.addIntercept')) == 1

    async def test_mock_api_and_preflight_on_bidi(self):
        self.fake.queue('network.addIntercept', {'intercept': 'I-1'})
        await self.page.mock_api({'/api/items': {'items': [1]}})
        pre = self.blocked('R-9', 'http://api.test/api/items', method='OPTIONS')
        pre['params']['request']['headers'].append(
            {'name': 'Access-Control-Request-Method', 'value': {'type': 'string', 'value': 'POST'}})
        self.transport.push(pre)
        self.transport.push(self.blocked('R-10', 'http://api.test/api/items', method='POST'))
        await drain()
        answers = {m['params']['request']: m['params'] for m in self.sent('network.provideResponse')}
        assert answers['R-9']['statusCode'] == 204 and 'body' not in answers['R-9']
        assert answers['R-10']['statusCode'] == 200

    async def test_recorder_collects_bodies_via_data_collector(self):
        self.fake.queue('network.addDataCollector', {'collector': 'C-1'})
        rec = self.page.record()
        await drain()
        assert self.sent('network.addDataCollector')[-1]['params'] == {
            'dataTypes': ['request', 'response'], 'maxEncodedDataSize': 20 * 1024 * 1024,
            'contexts': ['CTX-1']}
        assert 'network.responseStarted' in [
            e for m in self.sent('session.subscribe') for e in m['params']['events']]
        req = {'request': 'R-7', 'url': 'http://x/api', 'method': 'POST', 'headers': [],
               'bodySize': 9}
        # getData answers in call order: the request body first, then the response
        self.fake.queue('network.getData', {'bytes': {'type': 'string', 'value': '{"q": 2}'}})
        self.fake.queue('network.getData', {'bytes': {'type': 'string', 'value': '{"a": 1}'}})
        self.transport.push(event('network.beforeRequestSent', {
            'context': 'CTX-1', 'navigation': None, 'request': req, 'timestamp': 1000,
            'initiator': {'type': 'fetch'}}))
        self.transport.push(event('network.responseStarted', {
            'context': 'CTX-1', 'request': req, 'timestamp': 1100,
            'response': {'status': 200, 'mimeType': 'application/json', 'headers': []}}))
        self.transport.push(event('network.responseCompleted', {
            'context': 'CTX-1', 'request': req, 'timestamp': 1200,
            'response': {'status': 200, 'bytesReceived': 8}}))
        await drain()
        assert len(rec.exchanges) == 1 and rec.exchanges[0].json == {'a': 1}
        assert rec.exchanges[0].request_json == {'q': 2}
        assert [m['params']['dataType'] for m in self.sent('network.getData')] == ['request', 'response']
        assert self.sent('network.getData')[-1]['params'] == {
            'dataType': 'response', 'request': 'R-7', 'collector': 'C-1'}
        assert self.sent('network.disownData')[-1]['params']['request'] == 'R-7'
        # base64 payloads decode too; a second recorder reuses the collector
        self.fake.queue('network.getData', {'bytes': {'type': 'base64', 'value': 'AAEC'}})
        assert await self.driver.response_body('R-8') == b'\x00\x01\x02'
        self.page.record(); await drain()
        assert len(self.sent('network.addDataCollector')) == 1

    async def test_expect_download_round_trip(self):
        from purecdp.testing import Download
        self.fake.tree = {'contexts': [{'context': 'CTX-1', 'url': 'about:blank',
                                        'children': [], 'userContext': 'UC-7'}]}
        async with self.page.expect_download(timeout=2) as info:
            params = self.sent('browser.setDownloadBehavior')[-1]['params']
            assert params['downloadBehavior']['type'] == 'allowed'
            assert params['userContexts'] == ['UC-7']
            directory = params['downloadBehavior']['destinationFolder']
            self.transport.push(event('browsingContext.downloadWillBegin', {
                'context': 'CTX-1', 'download': 'D-1', 'navigation': 'N-1',
                'suggestedFilename': 'report.bin', 'url': 'http://x/dl', 'timestamp': 1}))
            self.transport.push(event('browsingContext.downloadEnd', {
                'context': 'CTX-1', 'download': 'D-1', 'navigation': 'N-1', 'status': 'complete',
                'filepath': directory + '/report.bin', 'url': 'http://x/dl', 'timestamp': 2}))
        download = await info.value
        assert isinstance(download, Download)
        assert download.suggested_filename == 'report.bin' and download.guid == 'D-1'
        assert download.path == directory + '/report.bin'

    # -- phase 4: accessibility via the in-page walker ------------------------

    @staticmethod
    def _remote(value):
        '''Python -> RemoteValue (the fake has to answer in wire shape).'''
        if value is None:
            return {'type': 'null'}
        if isinstance(value, bool):
            return {'type': 'boolean', 'value': value}
        if isinstance(value, (int, float)):
            return {'type': 'number', 'value': value}
        if isinstance(value, str):
            return {'type': 'string', 'value': value}
        if isinstance(value, list):
            return {'type': 'array', 'value': [BidiDriverTests._remote(v) for v in value]}
        return {'type': 'object', 'value': [[k, BidiDriverTests._remote(v)] for k, v in value.items()]}

    WIRE = [
        {'nodeId': 'ax0', 'ignored': False, 'role': {'value': 'RootWebArea'},
         'name': {'value': 't'}, 'backendDOMNodeId': 0, 'childIds': ['ax1', 'ax2', 'ax3']},
        {'nodeId': 'ax1', 'ignored': False, 'role': {'value': 'textbox'},
         'name': {'value': 'Email'}, 'value': {'value': 'a@b'}, 'backendDOMNodeId': 1,
         'properties': [{'name': 'required', 'value': {'value': True}}], 'childIds': []},
        {'nodeId': 'ax2', 'ignored': False, 'role': {'value': 'button'},
         'name': {'value': 'Go'}, 'backendDOMNodeId': 2, 'childIds': ['ax4']},
        {'nodeId': 'ax4', 'ignored': False, 'role': {'value': 'StaticText'},
         'name': {'value': 'Go'}, 'childIds': []},
        {'nodeId': 'ax3', 'ignored': False, 'role': {'value': 'Iframe'},
         'name': {'value': 'Fr'}, 'backendDOMNodeId': 3, 'childIds': []},
    ]

    async def test_snapshot_comes_from_the_walker(self):
        from purecdp.testing._ax_walker import AX_WALKER_JS
        self.fake.queue('script.callFunction', self.script_result(self._remote(self.WIRE)))
        self.fake.queue('script.callFunction', self.script_result(
            {'type': 'array', 'value': [{'type': 'array', 'value': [
                {'type': 'number', 'value': 3}, {'type': 'window', 'value': {'context': 'CTX-child'}}]}]}))
        snap = await self.page.snapshot()
        assert snap.text.splitlines() == [
            '- RootWebArea "t" [e1]',
            '  - textbox "Email" = "a@b" (required) [e2]',
            '  - button "Go" [e3]',
            '    - StaticText "Go"',
            '  - Iframe "Fr" [e4]']
        calls = self.sent('script.callFunction')[-2:]
        assert calls[0]['params']['functionDeclaration'] == AX_WALKER_JS
        assert calls[0]['params']['arguments'] == [{'type': 'null'}]
        assert snap.refs == {'e1': 0, 'e2': 1, 'e3': 2, 'e4': 3}
        assert await self.driver.frame_owner('CTX-child') == 3
        assert await self.driver.frame_owner('CTX-other') is None
        # depth travels to the walker; a tree without an Iframe node skips the
        # second (iframe → context) round-trip entirely
        before = len(self.sent('script.callFunction'))
        self.fake.queue('script.callFunction', self.script_result(self._remote(self.WIRE[:1])))
        await self.page.snapshot(depth=2)
        calls = self.sent('script.callFunction')[before:]
        assert len(calls) == 1
        assert calls[0]['params']['arguments'] == [{'type': 'number', 'value': 2}]
        assert await self.driver.frame_owner('CTX-child') is None   # map reset with the tree

    async def test_element_for_ref_indexes_the_registry(self):
        from purecdp.testing._ax_walker import AX_RESOLVE_JS
        self.fake.queue('script.callFunction', self.script_result(self._remote(self.WIRE)))
        self.fake.queue('script.callFunction', self.script_result({'type': 'array', 'value': []}))
        snap = await self.page.snapshot()
        ref = next(r for r, m in snap.meta.items() if m['name'] == 'Go')
        self.fake.queue('script.callFunction', self.script_result(
            {'type': 'node', 'sharedId': 's-go', 'handle': 'h-go', 'value': {'localName': 'button'}}))
        el = await self.page.element_for_ref(ref)
        assert el._handle == Handle('h-go', node_id='s-go')
        call = self.sent('script.callFunction')[-1]['params']
        assert call['functionDeclaration'] == AX_RESOLVE_JS
        assert call['arguments'] == [{'type': 'number', 'value': 2}]
        # a navigation emptied the registry: null -> "re-snapshot" error
        self.fake.queue('script.callFunction', self.script_result({'type': 'null'}))
        try:
            await self.page.element_for_ref(ref)
        except LookupError as exc:
            assert 're-snapshot' in str(exc)
        else:
            raise AssertionError('stale ref must raise LookupError')


class FirefoxBrowserTests(unittest.IsolatedAsyncioTestCase):
    '''FirefoxBrowser over the fake connection (no process).'''

    async def asyncSetUp(self):
        from purecdp.browser import FirefoxBrowser
        self.fake = FakeBidi()
        self.transport = FakeTransport(self.fake)
        self.conn = BidiConnection(self.transport)
        await self.conn.open()
        await self.conn.new_session()
        self.browser = FirefoxBrowser(None, self.conn, '', owns_profile=False)

    async def asyncTearDown(self):
        await self.browser.aclose()

    def sent(self, method):
        return [m for m in self.transport.sent if m['method'] == method]

    async def test_new_page_creates_a_tab_and_navigates(self):
        page = await self.browser.new_page('http://x/', default_timeout=1.0)
        assert page.backend == 'bidi' and self.browser.kind == 'firefox'
        assert self.sent('browsingContext.create')[-1]['params'] == {'type': 'tab'}
        assert self.sent('browsingContext.navigate')[-1]['params']['url'] == 'http://x/'
        assert self.browser.capabilities['browserName'] == 'firefox'
        await page.stop()

    async def test_user_context_scopes_new_tabs(self):
        self.fake.queue('browser.createUserContext', {'userContext': 'UC-1'})
        async with await self.browser.new_context() as ctx:
            page = await ctx.new_page()
            assert self.sent('browsingContext.create')[-1]['params'] == {
                'type': 'tab', 'userContext': 'UC-1'}
            await page.stop()
        assert self.sent('browser.removeUserContext')[-1]['params'] == {'userContext': 'UC-1'}

    async def test_attach_by_url_and_errors(self):
        self.fake.contexts = ['CTX-1', 'CTX-2']
        self.fake.queue('browsingContext.getTree', {'contexts': [
            {'context': 'CTX-1', 'url': 'about:blank', 'children': []},
            {'context': 'CTX-2', 'url': 'https://app.test/login', 'children': []}]})
        page = await self.browser.attach(url_contains='app.test')
        assert page._driver.context == 'CTX-2'
        await page.stop()
        self.fake.queue('browsingContext.getTree', {'contexts': [
            {'context': 'CTX-1', 'url': 'about:blank', 'children': []},
            {'context': 'CTX-2', 'url': 'https://app.test/login', 'children': []}]})
        try:
            await self.browser.attach()
        except purecdp.TargetNotFound as exc:
            assert '2 page targets' in str(exc)
        else:
            raise AssertionError('ambiguous attach did not raise')

    async def test_cdp_only_entry_points_say_so(self):
        for coro in (self.browser.new_session(), self.browser.close_page(None)):
            try:
                await coro
            except NotSupported as exc:
                assert exc.feature == 'CDP sessions'
            else:
                raise AssertionError('expected NotSupported')
        assert self.browser.connection is None


class FirefoxLaunchHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_browser_kind_from_binary_name(self):
        from purecdp import browser_kind
        assert browser_kind('/usr/bin/firefox') == 'firefox'
        assert browser_kind('C:\\\\Program Files\\\\Mozilla Firefox\\\\firefox.exe') == 'firefox'
        assert browser_kind('/usr/bin/librewolf') == 'firefox'
        assert browser_kind('/usr/bin/chromium') == 'chromium'
        assert browser_kind('/opt/brave.com/brave/brave') == 'chromium'

    def test_user_js_writer_is_idempotent_and_keeps_user_lines(self):
        from purecdp.browser import _write_user_js
        with tempfile.TemporaryDirectory() as profile:
            pathlib.Path(profile, 'user.js').write_text('user_pref("mine", 1);\n')
            _write_user_js(profile, {'a.b': True, 'c': 'x"y', 'd': 3})
            _write_user_js(profile, {'a.b': True, 'e': False})   # a second launch
            text = pathlib.Path(profile, 'user.js').read_text()
        assert text == ('user_pref("mine", 1);\n'
                        '// purecdp:begin (rewritten on every launch; edit outside this block)\n'
                        'user_pref("a.b", true);\nuser_pref("e", false);\n'
                        '// purecdp:end\n')
        assert text.count('purecdp:begin') == 1

    @unittest.skipIf(sys.platform == 'win32',
                     "Firefox's Windows lock is parent.lock, not a symlink (and the "
                     'helper returns None there by design)')
    def test_lock_holder_live_dead_and_absent(self):
        from purecdp.browser import _firefox_lock_holder, _prepare_reused_firefox_profile
        with tempfile.TemporaryDirectory() as profile:
            assert _firefox_lock_holder(profile) is None
            os.symlink(f'127.0.0.1:+{os.getpid()}', os.path.join(profile, 'lock'))
            assert _firefox_lock_holder(profile) == os.getpid()
            try:
                _prepare_reused_firefox_profile(profile)
            except purecdp.BrowserLaunchError as exc:
                assert f'in use by PID {os.getpid()}' in str(exc)
            else:
                raise AssertionError('live lock did not block')
            os.remove(os.path.join(profile, 'lock'))
            os.symlink('127.0.0.1:+999999', os.path.join(profile, 'lock'))
            assert _firefox_lock_holder(profile) is None
            with open(os.path.join(profile, 'WebDriverBiDiServer.json'), 'w') as f:
                f.write('{"ws_host": "127.0.0.1", "ws_port": 1}')
            _prepare_reused_firefox_profile(profile)   # stale lock + stale file: fine
            assert not os.path.exists(os.path.join(profile, 'WebDriverBiDiServer.json'))

    async def test_endpoint_discovery_reads_the_json_file(self):
        from purecdp.browser import _StderrTail, _wait_for_bidi_endpoint

        class Proc:
            returncode = None

        with tempfile.TemporaryDirectory() as profile:
            async def publish():
                await asyncio.sleep(0.1)
                with open(os.path.join(profile, 'WebDriverBiDiServer.json'), 'w') as f:
                    json.dump({'ws_host': '127.0.0.1', 'ws_port': 35009}, f)
            asyncio.create_task(publish())
            url = await _wait_for_bidi_endpoint(Proc(), profile, 2.0, _StderrTail(None))
        assert url == 'ws://127.0.0.1:35009/session'

    async def test_endpoint_discovery_reports_an_early_exit(self):
        from purecdp.browser import _StderrTail, _wait_for_bidi_endpoint

        class Proc:
            returncode = 1

        with tempfile.TemporaryDirectory() as profile:
            try:
                await _wait_for_bidi_endpoint(Proc(), profile, 1.0, _StderrTail(None))
            except purecdp.BrowserLaunchError as exc:
                assert 'exited with code 1' in str(exc)
            else:
                raise AssertionError('expected BrowserLaunchError')

    async def test_launch_argument_validation(self):
        try:
            await purecdp.launch(browser='safari')
        except ValueError as exc:
            assert 'safari' in str(exc)
        else:
            raise AssertionError('expected ValueError')
        try:
            await purecdp.launch('/usr/bin/firefox', pipe=True)
        except purecdp.BrowserLaunchError as exc:
            assert 'no pipe transport' in str(exc)
        else:
            raise AssertionError('expected BrowserLaunchError')

    async def test_launch_firefox_without_binary_says_which_env_var(self):
        from unittest import mock
        with mock.patch('purecdp.browser.find_firefox', return_value=None):
            try:
                await purecdp.launch(browser='firefox')
            except purecdp.BrowserLaunchError as exc:
                assert 'FIREFOX_BROWSER' in str(exc)
            else:
                raise AssertionError('expected BrowserLaunchError')


if __name__ == '__main__':
    unittest.main()
