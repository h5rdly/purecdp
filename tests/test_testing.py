'''Tests for the testing layer (Page, interception) — no browser required,
driven by the stateful FakeBrowser in tests/support.py.

Runnable three ways: `python -m unittest`, `python tests/test_testing.py`,
or `python -m pytest` — none require pytest. Plain asserts: don't run with -O.
'''

import asyncio, base64, pathlib, sys, time
import unittest
from unittest import mock


_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

try:
    from .support import FakeBrowser, FakeTransport, ax, drain
except ImportError:
    from support import FakeBrowser, FakeTransport, ax, drain

import purecdp  # noqa: E402
from purecdp import Connection  # noqa: E402
from purecdp.testing import JSError, NavigateError, Page  # noqa: E402


EXCEPTION_DETAILS = {
    'exceptionId': 1, 'text': 'Uncaught', 'lineNumber': 3, 'columnNumber': 7,
    'exception': {'type': 'object', 'subtype': 'error',
                  'description': 'Error: kaboom\n    at <anonymous>:1:7'},
}


class PageTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fake = FakeBrowser()
        self.transport = FakeTransport(self.fake)
        self.conn = Connection(self.transport)
        await self.conn.open()
        session = await purecdp.new_session(self.conn, 'about:blank')
        self.page = await Page.create(session, default_timeout=5.0)

    async def asyncTearDown(self):
        await self.page.stop()
        await self.conn.aclose()
        await super().asyncTearDown()

    def sent(self, method):
        return [m for m in self.transport.sent if m['method'] == method]


class NavigationTests(PageTestBase):
    async def test_goto_waits_for_load(self):
        await self.page.goto('https://x/')
        assert self.sent('Page.navigate')[0]['params'] == {'url': 'https://x/'}

    async def test_goto_failure_raises(self):
        self.fake.navigate_error = 'net::ERR_NAME_NOT_RESOLVED'
        try:
            await self.page.goto('https://nope.invalid/')
        except NavigateError as exc:
            assert 'ERR_NAME_NOT_RESOLVED' in str(exc)
        else:
            raise AssertionError('expected NavigateError')


class EvaluateTests(PageTestBase):
    async def test_evaluate_returns_value(self):
        self.fake.evaluate_results.append(
            {'result': {'type': 'number', 'value': 5}})
        assert await self.page.evaluate('2+3') == 5

    async def test_evaluate_exception_raises_jserror(self):
        self.fake.evaluate_results.append(
            {'result': {'type': 'object'}, 'exceptionDetails': EXCEPTION_DETAILS})
        try:
            await self.page.evaluate('boom()')
        except JSError as exc:
            assert 'kaboom' in str(exc)
            assert exc.details is not None
        else:
            raise AssertionError('expected JSError')

    async def test_wait_for_function_polls_until_truthy(self):
        self.fake.evaluate_results += [
            {'result': {'type': 'boolean', 'value': False}},
            {'result': {'type': 'boolean', 'value': False}},
            {'result': {'type': 'boolean', 'value': True}},
        ]
        await self.page.wait_for_function('window.ready', poll=0.001)
        assert len(self.sent('Runtime.evaluate')) == 3

    async def test_wait_for_selector_builds_expression(self):
        self.fake.evaluate_results.append(
            {'result': {'type': 'boolean', 'value': True}})
        await self.page.wait_for_selector('#late')
        sent = self.sent('Runtime.evaluate')[-1]['params']['expression']
        assert 'document.querySelector("#late")' in sent


class CaptureTests(PageTestBase):
    async def test_console_capture(self):
        self.transport.push({
            'method': 'Runtime.consoleAPICalled', 'sessionId': self.page.session.session_id,
            'params': {'type': 'log', 'executionContextId': 1, 'timestamp': 1.0,
                       'args': [{'type': 'string', 'value': 'hello'},
                                {'type': 'number', 'value': 42}]}})
        await drain()
        assert self.page.console[0].kind == 'log'
        assert self.page.console[0].text == 'hello 42'

    async def test_uncaught_exception_capture(self):
        self.transport.push({
            'method': 'Runtime.exceptionThrown', 'sessionId': self.page.session.session_id,
            'params': {'timestamp': 1.0, 'exceptionDetails': EXCEPTION_DETAILS}})
        await drain()
        assert len(self.page.js_errors) == 1
        assert 'kaboom' in str(self.page.js_errors[0])

    async def test_network_idle_tracking(self):
        sid = self.page.session.session_id
        self.transport.push({
            'method': 'Network.requestWillBeSent', 'sessionId': sid,
            'params': {'requestId': 'R1', 'loaderId': 'L1',
                       'documentURL': 'https://x/', 'timestamp': 1.0,
                       'wallTime': 1.0, 'initiator': {'type': 'other'},
                       'redirectHasExtraInfo': False,
                       'request': {'url': 'https://x/a', 'method': 'GET',
                                   'headers': {}, 'initialPriority': 'High',
                                   'referrerPolicy': 'no-referrer'}}})
        await drain()
        assert self.page._inflight == {'R1'}
        self.transport.push({
            'method': 'Network.loadingFinished', 'sessionId': sid,
            'params': {'requestId': 'R1', 'timestamp': 2.0,
                       'encodedDataLength': 10}})
        await drain()
        assert not self.page._inflight
        await self.page.wait_for_network_idle(idle_time=0, timeout=2)


class InterceptTests(PageTestBase):
    def paused_event(self, request_id, url):
        return {
            'method': 'Fetch.requestPaused',
            'sessionId': self.page.session.session_id,
            'params': {'requestId': request_id, 'frameId': 'F-1',
                       'resourceType': 'Fetch',
                       'request': {'url': url, 'method': 'GET', 'headers': {},
                                   'initialPriority': 'High',
                                   'referrerPolicy': 'no-referrer'}}}

    async def test_fulfill_matched_continue_unmatched(self):
        async def handler(request):
            await request.fulfill(body='stubbed', content_type='text/plain',
                                  status=201)

        await self.page.route('*api*', handler)
        assert self.sent('Fetch.enable')
        self.transport.push(self.paused_event('R1', 'https://x/api/1'))
        self.transport.push(self.paused_event('R2', 'https://x/other'))
        await drain()

        fulfilled = self.sent('Fetch.fulfillRequest')[0]['params']
        assert fulfilled['requestId'] == 'R1'
        assert fulfilled['responseCode'] == 201
        assert base64.b64decode(fulfilled['body']) == b'stubbed'
        assert {'name': 'Content-Type', 'value': 'text/plain'} \
            in fulfilled['responseHeaders']
        continued = self.sent('Fetch.continueRequest')[0]['params']
        assert continued['requestId'] == 'R2'

    async def test_abort(self):
        async def handler(request):
            await request.abort()

        await self.page.route('*blocked*', handler)
        self.transport.push(self.paused_event('R9', 'https://x/blocked'))
        await drain()
        failed = self.sent('Fetch.failRequest')[0]['params']
        assert failed == {'requestId': 'R9', 'errorReason': 'Aborted'}

    async def test_route_accepts_predicate_callable(self):
        async def handler(request):
            await request.fulfill(body='ok', content_type='text/plain')

        await self.page.route(lambda url: url.endswith('/pred'), handler)
        self.transport.push(self.paused_event('R1', 'https://x/pred'))
        self.transport.push(self.paused_event('R2', 'https://x/pred/nope'))
        await drain()
        assert self.sent('Fetch.fulfillRequest')[0]['params']['requestId'] == 'R1'
        assert self.sent('Fetch.continueRequest')[0]['params']['requestId'] == 'R2'


class HarExportTests(unittest.TestCase):
    def test_to_har_structure_bodies_and_encoding(self):
        import base64 as b64_mod
        import json as json_mod
        from purecdp.testing import Exchange, to_har

        textual = Exchange(
            url='https://x/q?a=1&b=two', method='POST',
            request_body='{"ask": 1}', status=200, mime='application/json',
            body=b'{"answer": 42}',
            request_headers={'Content-Type': 'application/json',
                             'Cookie': 's=1'},
            response_headers={'Set-Cookie': 's=1'},
            timestamp=1_700_000_000.0, duration=0.25)
        binary = Exchange(url='https://x/img', method='GET', status=200,
                          mime='image/png', body=b'\x89PNG\xff\xfe')
        har = to_har([textual, binary])
        json_mod.dumps(har)                          # fully serializable
        log = har['log']
        assert log['version'] == '1.2' and log['creator']['name'] == 'purecdp'
        first, second = log['entries']
        assert first['startedDateTime'].startswith('2023-11-14')
        assert first['time'] == 250.0
        assert first['request']['queryString'] == [
            {'name': 'a', 'value': '1'}, {'name': 'b', 'value': 'two'}]
        assert first['request']['postData'] == {
            'mimeType': 'application/json', 'text': '{"ask": 1}'}
        assert {'name': 'Cookie', 'value': 's=1'} in first['request']['headers']
        assert {'name': 'Set-Cookie', 'value': 's=1'} \
            in first['response']['headers']
        assert first['response']['content']['text'] == '{"answer": 42}'
        assert 'encoding' not in first['response']['content']
        assert second['response']['content']['encoding'] == 'base64'
        assert b64_mod.b64decode(
            second['response']['content']['text']) == b'\x89PNG\xff\xfe'
        assert second['time'] == -1                  # duration unknown


class ErrorHierarchyTests(unittest.TestCase):
    def test_every_purecdp_error_shares_the_base(self):
        import purecdp.errors as errors
        for name in ('CDPCommandError', 'CDPProtocolError', 'CDPConnectionClosed',
                     'CDPSessionClosed', 'CDPTransportError', 'BrowserLaunchError'):
            assert issubclass(getattr(errors, name), errors.PureCDPError), name
        assert errors.CDPError is errors.CDPCommandError    # compat alias
        assert purecdp.PureCDPError is errors.PureCDPError  # exported

    async def test_broken_handler_continues_and_warns(self):
        async def handler(request):
            raise RuntimeError('oops')

        await self.page.route('*', handler)
        # patch warnings.warn at the call site (context/thread independent) rather than 
        # catch_warnings, which can miss it on the free-threaded build.
        
        with mock.patch('warnings.warn') as warn:
            self.transport.push(self.paused_event('R5', 'https://x/thing'))
            await drain()
        assert any('route handler failed' in str(c.args[0]) for c in warn.call_args_list if c.args)
        assert self.sent('Fetch.continueRequest')[0]['params']['requestId'] == 'R5'


if __name__ == '__main__':
    unittest.main()


OBJ = {'result': {'type': 'object', 'subtype': 'node', 'objectId': 'OBJ-1'}}
NULL = {'result': {'type': 'object', 'subtype': 'null', 'value': None}}


class ElementTests(PageTestBase):
    async def test_query_polls_until_found_and_holds_object(self):
        self.fake.evaluate_results += [NULL, OBJ]
        element = await self.page.query('#late', poll=0.001)
        assert element._object_id == 'OBJ-1'
        assert len(self.sent('Runtime.evaluate')) == 2

    async def test_query_timeout_names_selector(self):
        try:
            await self.page.query('#never', timeout=0.05, poll=0.01)
        except TimeoutError as exc:
            assert '#never' in str(exc)
        else:
            raise AssertionError('expected TimeoutError')
        assert await self.page.query('#never', timeout=0.05, poll=0.01,
                                     required=False) is None

    async def test_query_containing_and_index_reach_the_wire(self):
        self.fake.evaluate_results.append(OBJ)
        await self.page.query('div.row', containing='Beta', index=-1)
        expression = self.sent('Runtime.evaluate')[-1]['params']['expression']
        assert '"div.row"' in expression
        assert '"beta"' in expression  # lowercased needle
        assert '.at(-1)' in expression

    async def test_element_eval_calls_function_on_held_object(self):
        self.fake.evaluate_results.append(OBJ)
        self.fake.call_results.append({'result': {'type': 'string', 'value': 'txt'}})
        element = await self.page.query('p')
        assert await element.eval('(el) => el.textContent') == 'txt'
        call = self.sent('Runtime.callFunctionOn')[-1]['params']
        assert call['objectId'] == 'OBJ-1'
        assert 'el.textContent' in call['functionDeclaration']
        assert call['arguments'][0] == {'objectId': 'OBJ-1'}

    async def test_set_value_uses_native_setter_and_events(self):
        self.fake.evaluate_results.append(OBJ)
        element = await self.page.query('input')
        await element.set_value('hello')
        call = self.sent('Runtime.callFunctionOn')[-1]['params']
        assert 'getOwnPropertyDescriptor' in call['functionDeclaration']
        assert 'dispatchEvent' in call['functionDeclaration']
        assert call['arguments'][1] == {'value': 'hello'}

    async def test_mouse_click_dispatches_trusted_events_at_center(self):
        self.fake.evaluate_results.append(OBJ)
        self.fake.call_results.append(
            {'result': {'type': 'object',
                        'value': {'x': 10.5, 'y': 20.5}}})
        element = await self.page.query('button')
        await element.mouse_click()
        events = self.sent('Input.dispatchMouseEvent')
        assert [e['params']['type'] for e in events] == ['mousePressed',
                                                         'mouseReleased']
        assert events[0]['params']['x'] == 10.5
        assert events[0]['params']['button'] == 'left'


class KeyboardTests(PageTestBase):
    async def test_press_enter_sends_down_and_up(self):
        await self.page.press('Enter')
        events = self.sent('Input.dispatchKeyEvent')
        assert [e['params']['type'] for e in events] == ['keyDown', 'keyUp']
        assert events[0]['params']['text'] == '\r'
        assert events[0]['params']['windowsVirtualKeyCode'] == 13
        assert 'text' not in events[1]['params']

    async def test_press_unknown_key_rejected(self):
        try:
            await self.page.press('Bogus')
        except ValueError:
            pass
        else:
            raise AssertionError('expected ValueError')

    async def test_insert_text(self):
        await self.page.insert_text('hi there')
        sent = self.sent('Input.insertText')[0]['params']
        assert sent == {'text': 'hi there'}


class InitScriptAndRecorderTests(PageTestBase):
    async def test_add_init_script(self):
        await self.page.add_init_script("sessionStorage.setItem('k', 'v')")
        sent = self.sent('Page.addScriptToEvaluateOnNewDocument')[0]['params']
        assert sent['source'] == "sessionStorage.setItem('k', 'v')"

    async def test_fulfill_json_convenience(self):
        async def handler(request):
            await request.fulfill(json={'items': []})

        await self.page.route('*api*', handler)
        self.transport.push({
            'method': 'Fetch.requestPaused',
            'sessionId': self.page.session.session_id,
            'params': {'requestId': 'R1', 'frameId': 'F-1',
                       'resourceType': 'Fetch',
                       'request': {'url': 'https://x/api/a', 'method': 'GET',
                                   'headers': {}, 'initialPriority': 'High',
                                   'referrerPolicy': 'no-referrer'}}})
        await drain()
        fulfilled = self.sent('Fetch.fulfillRequest')[0]['params']
        assert base64.b64decode(fulfilled['body']) == b'{"items": []}'
        assert {'name': 'Content-Type', 'value': 'application/json'} \
            in fulfilled['responseHeaders']

    async def test_recorder_captures_full_exchange(self):
        recorder = self.page.record(needle='/query', exclude='/query/preview')
        sid = self.page.session.session_id
        self.fake.response_bodies['R1'] = {
            'body': '{"answer": 42}', 'base64Encoded': False}

        def req(request_id, url, post=None):
            body = {'requestId': request_id, 'loaderId': 'L1',
                    'documentURL': 'https://x/', 'timestamp': 1.0,
                    'wallTime': 1.0, 'initiator': {'type': 'other'},
                    'redirectHasExtraInfo': False,
                    'request': {'url': url, 'method': 'POST',
                                'headers': {}, 'initialPriority': 'High',
                                'referrerPolicy': 'no-referrer'}}
            if post:
                body['request']['postData'] = post
            return {'method': 'Network.requestWillBeSent', 'sessionId': sid,
                    'params': body}

        self.transport.push(req('R1', 'https://x/query', post='{"ask": 1}'))
        self.transport.push(req('R9', 'https://x/query/preview'))  # excluded
        self.transport.push({
            'method': 'Network.responseReceived', 'sessionId': sid,
            'params': {'requestId': 'R1', 'loaderId': 'L1', 'timestamp': 2.0,
                       'type': 'XHR', 'frameId': 'F-1',
                       'hasExtraInfo': False,
                       'response': {'url': 'https://x/query', 'status': 200,
                                    'statusText': 'OK', 'headers': {},
                                    'mimeType': 'application/json',
                                    'charset': 'utf-8',
                                    'connectionReused': False,
                                    'connectionId': 1,
                                    'encodedDataLength': 14,
                                    'securityState': 'secure'}}})
        self.transport.push({
            'method': 'Network.loadingFinished', 'sessionId': sid,
            'params': {'requestId': 'R1', 'timestamp': 3.0,
                       'encodedDataLength': 14}})
        exchange = await recorder.wait_for_next(0, timeout=2)
        assert exchange.url == 'https://x/query'
        assert exchange.request_json == {'ask': 1}
        assert exchange.status == 200
        assert exchange.mime == 'application/json'
        assert exchange.json == {'answer': 42}
        assert len(recorder.exchanges) == 1  # the excluded one never recorded
        assert recorder.requests == [{'ask': 1}]
        assert recorder.responses == [{'answer': 42}]

    def _push_exchange(self, request_id: str, url: str, body: str) -> None:
        '''Push a full request/response/finished series through the fake.'''
        sid = self.page.session.session_id
        self.fake.response_bodies[request_id] = {
            'body': body, 'base64Encoded': False}
        self.transport.push({
            'method': 'Network.requestWillBeSent', 'sessionId': sid,
            'params': {'requestId': request_id, 'loaderId': 'L1',
                       'documentURL': 'https://x/', 'timestamp': 1.0,
                       'wallTime': 1.0, 'initiator': {'type': 'other'},
                       'redirectHasExtraInfo': False,
                       'request': {'url': url, 'method': 'GET', 'headers': {},
                                   'initialPriority': 'High',
                                   'referrerPolicy': 'no-referrer'}}})
        self.transport.push({
            'method': 'Network.responseReceived', 'sessionId': sid,
            'params': {'requestId': request_id, 'loaderId': 'L1',
                       'timestamp': 2.0, 'type': 'XHR', 'frameId': 'F-1',
                       'hasExtraInfo': False,
                       'response': {'url': url, 'status': 200,
                                    'statusText': 'OK', 'headers': {},
                                    'mimeType': 'application/json',
                                    'charset': 'utf-8',
                                    'connectionReused': False,
                                    'connectionId': 1,
                                    'encodedDataLength': 14,
                                    'securityState': 'secure'}}})
        self.transport.push({
            'method': 'Network.loadingFinished', 'sessionId': sid,
            'params': {'requestId': request_id, 'timestamp': 3.0,
                       'encodedDataLength': 14}})

    async def test_wait_for_next_is_burst_safe_and_json_filters(self):
        recorder = self.page.record(needle='/query')
        # both exchanges complete before anyone waits — a burst
        self._push_exchange('R1', 'https://x/query', '<html>proxy err</html>')
        self._push_exchange('R2', 'https://x/query', '{"answer": 42}')

        first = await recorder.wait_for_next(0, timeout=2)
        assert first.text == '<html>proxy err</html>'   # oldest first, not newest
        second = await recorder.wait_for_next(1, timeout=2)
        assert second.json == {'answer': 42}
        # json=True skips the non-JSON body but leaves it recorded
        json_one = await recorder.wait_for_next(0, json=True, timeout=2)
        assert json_one.json == {'answer': 42}
        assert len(recorder.exchanges) == 2

    async def test_headers_merge_extra_info_any_order(self):
        recorder = self.page.record(needle='/query')
        sid = self.page.session.session_id
        self.fake.response_bodies['R1'] = {'body': '{}', 'base64Encoded': False}

        def extra(method, request_id, headers):
            self.transport.push({'method': method, 'sessionId': sid,
                                 'params': {'requestId': request_id,
                                            'headers': headers,
                                            'associatedCookies': [],
                                            'connectTiming': {'requestTime': 1.0},
                                            'blockedCookies': [],
                                            'resourceIPAddressSpace': 'Local',
                                            'statusCode': 200}})

        # request ExtraInfo arrives BEFORE the base event (CDP guarantees no
        # order) and carries the wire-only headers
        extra('Network.requestWillBeSentExtraInfo', 'R1',
              {'Cookie': 'sid=abc', 'Origin': 'https://x'})
        self.transport.push({
            'method': 'Network.requestWillBeSent', 'sessionId': sid,
            'params': {'requestId': 'R1', 'loaderId': 'L1',
                       'documentURL': 'https://x/', 'timestamp': 1.0,
                       'wallTime': 1234.5, 'initiator': {'type': 'other'},
                       'redirectHasExtraInfo': False,
                       'request': {'url': 'https://x/query', 'method': 'GET',
                                   'headers': {'Accept': '*/*'},
                                   'initialPriority': 'High',
                                   'referrerPolicy': 'no-referrer'}}})
        self.transport.push({
            'method': 'Network.responseReceived', 'sessionId': sid,
            'params': {'requestId': 'R1', 'loaderId': 'L1', 'timestamp': 2.0,
                       'type': 'XHR', 'frameId': 'F-1', 'hasExtraInfo': True,
                       'response': {'url': 'https://x/query', 'status': 200,
                                    'statusText': 'OK',
                                    'headers': {'Content-Type': 'application/json'},
                                    'mimeType': 'application/json',
                                    'charset': 'utf-8',
                                    'connectionReused': False,
                                    'connectionId': 1,
                                    'encodedDataLength': 14,
                                    'securityState': 'secure'}}})
        # response ExtraInfo AFTER the base event — Set-Cookie lives ONLY here
        extra('Network.responseReceivedExtraInfo', 'R1',
              {'Set-Cookie': 'sid=abc; HttpOnly'})
        self.transport.push({
            'method': 'Network.loadingFinished', 'sessionId': sid,
            'params': {'requestId': 'R1', 'timestamp': 3.0,
                       'encodedDataLength': 14}})

        exchange = await recorder.wait_for_next(0, timeout=2)
        assert exchange.request_headers['Accept'] == '*/*'          # base
        assert exchange.request_headers['Cookie'] == 'sid=abc'      # extra
        assert exchange.request_headers['Origin'] == 'https://x'
        assert exchange.response_headers['Content-Type'] == 'application/json'
        assert exchange.response_headers['Set-Cookie'] == 'sid=abc; HttpOnly'
        assert exchange.timestamp == 1234.5
        assert exchange.duration == 2.0     # loadingFinished(3.0) - request(1.0)

    async def test_extra_info_for_unmatched_requests_is_dropped(self):
        recorder = self.page.record(needle='/query')
        sid = self.page.session.session_id
        self.transport.push({
            'method': 'Network.requestWillBeSent', 'sessionId': sid,
            'params': {'requestId': 'R7', 'loaderId': 'L1',
                       'documentURL': 'https://x/', 'timestamp': 1.0,
                       'wallTime': 1.0, 'initiator': {'type': 'other'},
                       'redirectHasExtraInfo': False,
                       'request': {'url': 'https://x/other', 'method': 'GET',
                                   'headers': {}, 'initialPriority': 'High',
                                   'referrerPolicy': 'no-referrer'}}})
        self.transport.push({'method': 'Network.requestWillBeSentExtraInfo',
                             'sessionId': sid,
                             'params': {'requestId': 'R7',
                                        'headers': {'Cookie': 'x'},
                                        'associatedCookies': [],
                                        'connectTiming': {'requestTime': 1.0}}})
        await drain()
        assert not recorder._early_request_extra    # dropped, not buffered
        assert not recorder.exchanges

    async def test_expect_pins_position_at_arming(self):
        recorder = self.page.record(needle='/query')
        self._push_exchange('R0', 'https://x/query', '{"stale": true}')
        await recorder.wait_for_next(0, timeout=2)      # R0 recorded pre-arm

        async with recorder.expect(json=True, timeout=2) as turn:
            plain = recorder.expect(timeout=2)          # CM-less arming works too
            self._push_exchange('R1', 'https://x/query', '<html>proxy err</html>')
            self._push_exchange('R2', 'https://x/query', '{"answer": 42}')

        exchange = await turn.value
        assert exchange.json == {'answer': 42}          # R0 pre-arm, R1 non-JSON
        assert [e.text for e in turn.new] == ['<html>proxy err</html>',
                                              '{"answer": 42}']
        assert [e.text for e in turn.skipped] == ['<html>proxy err</html>']
        assert (await plain.value).text == '<html>proxy err</html>'
        assert plain.skipped == []                      # json=False skips nothing

    async def test_recorder_default_timeout_beats_page_default(self):
        # the endpoint's latency profile lives on the recorder: with nothing
        # recorded, a 0.05s recorder default must fire long before the page's
        # (multi-second) default would.
        recorder = self.page.record(needle='/nothing', default_timeout=0.05)
        start = time.monotonic()
        try:
            await recorder.wait_for_next(0)
        except TimeoutError:
            assert time.monotonic() - start < 1
        else:
            raise AssertionError('expected TimeoutError from the recorder default')
        # an explicit per-call timeout still wins over the recorder default
        start = time.monotonic()
        try:
            await recorder.expect(timeout=0.01).value
        except TimeoutError:
            assert time.monotonic() - start < 1
        else:
            raise AssertionError('expected TimeoutError from the explicit timeout')


class ConvenienceTests(PageTestBase):
    def paused_event(self, request_id, url):
        return {
            'method': 'Fetch.requestPaused',
            'sessionId': self.page.session.session_id,
            'params': {'requestId': request_id, 'frameId': 'F-1',
                       'resourceType': 'Fetch',
                       'request': {'url': url, 'method': 'GET', 'headers': {},
                                   'initialPriority': 'High',
                                   'referrerPolicy': 'no-referrer'}}}

    async def test_seed_storage_becomes_init_script(self):
        await self.page.seed_session_storage('oidc.user', '{"tok": 1}')
        await self.page.seed_local_storage('theme', 'dark')
        sources = [m['params']['source'] for m in
                   self.sent('Page.addScriptToEvaluateOnNewDocument')]
        assert 'sessionStorage.setItem("oidc.user", "{\\"tok\\": 1}");' in sources[0]
        assert 'localStorage.setItem("theme", "dark");' in sources[1]

    async def test_mock_api_exact_prefix_callable_and_passthrough(self):
        await self.page.mock_api({
            '/api/v1/studies': {'items': [1, 2]},
            '/api/v1/studies/': lambda path: {'id': path.rsplit('/', 1)[-1]},
        })
        self.transport.push(self.paused_event('R1', 'https://x/api/v1/studies'))
        self.transport.push(self.paused_event('R2', 'https://x/api/v1/studies/77'))
        self.transport.push(self.paused_event('R3', 'https://x/api/v1/other'))
        await drain()
        fulfilled = {m['params']['requestId']:
                     base64.b64decode(m['params']['body'])
                     for m in self.sent('Fetch.fulfillRequest')}
        assert fulfilled['R1'] == b'{"items": [1, 2]}'
        assert fulfilled['R2'] == b'{"id": "77"}'
        continued = [m['params']['requestId']
                     for m in self.sent('Fetch.continueRequest')]
        assert continued == ['R3']

    async def test_element_type_focuses_then_inserts(self):
        self.fake.evaluate_results.append(OBJ)
        element = await self.page.query('input')
        await element.type('hello')
        focus_call = self.sent('Runtime.callFunctionOn')[-1]['params']
        assert 'el.focus()' in focus_call['functionDeclaration']
        assert self.sent('Input.insertText')[0]['params'] == {'text': 'hello'}


class CaptureOptInTests(unittest.IsolatedAsyncioTestCase):
    async def _make(self, **create_kw):
        fake = FakeBrowser()
        transport = FakeTransport(fake)
        conn = Connection(transport)
        await conn.open()
        session = await purecdp.new_session(conn, 'about:blank')
        page = await Page.create(session, default_timeout=5.0, **create_kw)
        self.addAsyncCleanup(conn.aclose)
        self.addAsyncCleanup(page.stop)
        methods = lambda: [m['method'] for m in transport.sent]
        return fake, transport, page, methods

    async def test_default_enables_runtime_and_network(self):
        _, _, _, methods = await self._make()
        assert 'Runtime.enable' in methods()
        assert 'Network.enable' in methods()
        assert 'Page.enable' in methods()

    async def test_clean_page_skips_runtime_the_stealth_leak(self):
        _, _, page, methods = await self._make(capture=False, track_network=False)
        assert 'Runtime.enable' not in methods()   # the isAutomatedWithCDP leak
        assert 'Network.enable' not in methods()
        assert 'Page.enable' in methods()           # nav waits still work

    async def test_capture_only_leaves_network_off(self):
        _, _, _, methods = await self._make(capture=True, track_network=False)
        assert 'Runtime.enable' in methods()
        assert 'Network.enable' not in methods()

    async def test_network_idle_without_tracking_raises(self):
        _, _, page, _ = await self._make(capture=False, track_network=False)
        try:
            await page.wait_for_network_idle(timeout=1)
        except RuntimeError as exc:
            assert 'track_network' in str(exc)
        else:
            raise AssertionError('expected RuntimeError')


class EmulationWireTests(PageTestBase):
    async def test_set_viewport(self):
        await self.page.set_viewport(1280, 720, device_scale_factor=2.0)
        p = self.sent('Emulation.setDeviceMetricsOverride')[0]['params']
        assert p['width'] == 1280 and p['height'] == 720
        assert p['deviceScaleFactor'] == 2.0 and p['mobile'] is False

    async def test_set_user_agent_with_metadata(self):
        await self.page.set_user_agent('UA/1.0', accept_language='en-GB',
                                       metadata={'platform': 'Linux',
                                                 'platformVersion': '6',
                                                 'architecture': 'x86',
                                                 'model': '', 'mobile': False})
        p = self.sent('Emulation.setUserAgentOverride')[0]['params']
        assert p['userAgent'] == 'UA/1.0'
        assert p['acceptLanguage'] == 'en-GB'
        assert p['userAgentMetadata']['platform'] == 'Linux'

    async def test_emulate_media_color_scheme(self):
        await self.page.emulate_media(color_scheme='dark', media='print')
        p = self.sent('Emulation.setEmulatedMedia')[0]['params']
        assert p['media'] == 'print'
        assert {'name': 'prefers-color-scheme', 'value': 'dark'} in p['features']

    async def test_geolocation_and_timezone(self):
        await self.page.set_geolocation(48.85, 2.35, accuracy=5.0)
        await self.page.set_timezone('Europe/Paris')
        geo = self.sent('Emulation.setGeolocationOverride')[0]['params']
        assert geo['latitude'] == 48.85 and geo['longitude'] == 2.35
        assert self.sent('Emulation.setTimezoneOverride')[0]['params'] == {
            'timezoneId': 'Europe/Paris'}


class CookieWireTests(PageTestBase):
    async def test_set_cookies_builds_params(self):
        await self.page.set_cookies([
            {'name': 'sid', 'value': 'abc', 'url': 'https://x/'},
            {'name': 't', 'value': '1', 'domain': 'x', 'path': '/',
             'httpOnly': True},
        ])
        p = self.sent('Network.setCookies')[0]['params']['cookies']
        assert p[0] == {'name': 'sid', 'value': 'abc', 'url': 'https://x/'}
        assert p[1]['httpOnly'] is True

    async def test_clear_cookies(self):
        await self.page.clear_cookies()
        sent = self.sent('Network.clearBrowserCookies')[0]
        assert sent['method'] == 'Network.clearBrowserCookies'
        assert 'params' not in sent

    async def test_set_extra_headers(self):
        await self.page.set_extra_headers({'X-Test': '1'})
        assert self.sent('Network.setExtraHTTPHeaders')[0]['params'] == {
            'headers': {'X-Test': '1'}}


class ElementActionWireTests(PageTestBase):
    async def _element(self):
        self.fake.evaluate_results.append(OBJ)
        return await self.page.query('input')

    async def test_set_input_files(self):
        el = await self._element()
        await el.set_input_files('/tmp/a.txt', '/tmp/b.txt')
        p = self.sent('DOM.setFileInputFiles')[0]['params']
        assert p['files'] == ['/tmp/a.txt', '/tmp/b.txt']
        assert p['objectId'] == 'OBJ-1'

    async def test_select_option_passes_values_and_returns_hits(self):
        el = await self._element()
        self.fake.call_results.append(
            {'result': {'type': 'object', 'value': ['b']}})
        hit = await el.select_option('a', 'b')
        call = self.sent('Runtime.callFunctionOn')[-1]['params']
        assert call['arguments'][1] == {'value': ['a', 'b']}
        assert hit == ['b']

    async def test_hover_moves_mouse_to_center(self):
        el = await self._element()
        # scroll_into_view (call 1), bounding_box (call 2)
        self.fake.call_results += [
            {'result': {'type': 'undefined'}},
            {'result': {'type': 'object',
                        'value': {'x': 10, 'y': 20, 'width': 40, 'height': 60}}},
        ]
        await el.hover()
        move = self.sent('Input.dispatchMouseEvent')[0]['params']
        assert move['type'] == 'mouseMoved'
        assert move['x'] == 30 and move['y'] == 50  # center


class StealthWireTests(PageTestBase):
    async def test_apply_stealth_installs_one_init_script(self):
        from purecdp.testing import apply_stealth
        await apply_stealth(self.page)
        scripts = self.sent('Page.addScriptToEvaluateOnNewDocument')
        assert len(scripts) == 1
        src = scripts[0]['params']['source']
        from purecdp.testing import DEFAULT_EVASIONS
        # markers proving each evasion's JS is present, each wrapped defensively
        assert "'webdriver'" in src and 'Navigator.prototype' in src
        assert 'window.chrome' in src
        assert 'UNMASKED_VENDOR_WEBGL' in src
        assert 'navigator.permissions.query' in src
        assert src.count('try {') == len(DEFAULT_EVASIONS)

    async def test_subset_and_overrides(self):
        from purecdp.testing import apply_stealth
        await apply_stealth(self.page, evasions=['webgl.vendor'],
                            webgl_vendor='ATI Technologies Inc.')
        src = self.sent('Page.addScriptToEvaluateOnNewDocument')[0]['params']['source']
        assert 'ATI Technologies Inc.' in src
        assert 'navigator.webdriver' not in src  # not selected

    async def test_unknown_evasion_rejected(self):
        from purecdp.testing import apply_stealth
        try:
            await apply_stealth(self.page, evasions=['nope'])
        except ValueError:
            pass
        else:
            raise AssertionError('expected ValueError')

    def _attach_target(self, sid, ttype):
        self.transport.push({
            'method': 'Target.attachedToTarget',
            'params': {
                'sessionId': sid, 'waitingForDebugger': True,
                'targetInfo': {
                    'targetId': 'T-' + sid, 'type': ttype, 'title': '',
                    'url': 'about:blank', 'attached': True,
                    'canAccessOpener': False},
            }})

    async def test_workers_true_injects_into_worker_before_resume(self):
        from purecdp.testing import apply_stealth
        await apply_stealth(self.page)  # workers=True default
        # auto-attach was armed to pause children on start
        auto = self.sent('Target.setAutoAttach')
        assert auto and auto[-1]['params']['waitForDebuggerOnStart'] is True

        self._attach_target('WSESS', 'worker')
        await drain()

        # the stealth script was Runtime.evaluate'd in the worker's session ...
        evals = [m for m in self.transport.sent
                 if m['method'] == 'Runtime.evaluate'
                 and m.get('sessionId') == 'WSESS']
        assert len(evals) == 1
        assert 'UNMASKED_VENDOR_WEBGL' in evals[0]['params']['expression']
        # ... strictly before the worker was resumed
        order = [m['method'] for m in self.transport.sent
                 if m.get('sessionId') == 'WSESS']
        assert order.index('Runtime.evaluate') < \
            order.index('Runtime.runIfWaitingForDebugger')

    async def test_workers_true_ignores_non_worker_targets(self):
        from purecdp.testing import apply_stealth
        await apply_stealth(self.page)
        self._attach_target('FSESS', 'iframe')
        await drain()
        assert not [m for m in self.transport.sent
                    if m['method'] == 'Runtime.evaluate'
                    and m.get('sessionId') == 'FSESS']

    async def test_workers_false_skips_auto_attach(self):
        from purecdp.testing import apply_stealth
        await apply_stealth(self.page, workers=False)
        assert not self.sent('Target.setAutoAttach')
        self._attach_target('WSESS', 'worker')
        await drain()
        assert not [m for m in self.transport.sent
                    if m['method'] == 'Runtime.evaluate'
                    and m.get('sessionId') == 'WSESS']

    async def test_optin_evasions_excluded_from_default(self):
        from purecdp.testing import (DEFAULT_EVASIONS, EVASIONS,
                                     OPT_IN_EVASIONS, apply_stealth)
        for name in OPT_IN_EVASIONS:
            assert name in EVASIONS
            assert name not in DEFAULT_EVASIONS
        await apply_stealth(self.page)
        src = self.sent('Page.addScriptToEvaluateOnNewDocument')[0]['params']['source']
        assert 'Function.prototype.toString' not in src   # opt-in, not default
        assert 'maxTouchPoints' not in src

    async def test_function_tostring_mask_hoisted_and_installed(self):
        from purecdp.testing import build_stealth_script
        src = build_stealth_script(
            evasions=['navigator.webdriver', 'function.toString'])
        # the mask reassigns the shared __pcMask helper and proxies toString
        assert 'Function.prototype.toString = proxy' in src
        assert '__pcMask = function' in src
        # hoisted: the mask install precedes the webdriver getter that uses it
        assert src.index('Function.prototype.toString = proxy') \
            < src.index('get webdriver')

    async def test_extra_signal_evasions(self):
        from purecdp.testing import build_stealth_script
        src = build_stealth_script(
            evasions=['navigator.maxTouchPoints', 'navigator.connection'],
            max_touch_points=1, connection_rtt=100)
        assert 'maxTouchPoints' in src and 'return 1' in src
        assert 'navigator.connection' in src and 'return 100' in src


class AgentSurfaceTests(PageTestBase):
    '''M9: snapshot()/act()/element_for_ref() over a canned AX tree.'''

    def _login_tree(self):
        self.fake.ax_nodes = [
            ax('1', role='RootWebArea', name='Login', children=['2']),
            ax('2', role='form', backend=10, children=['3', '4', '5', '6']),
            ax('3', role='textbox', name='Email', value='a@b.com',
               props={'required': True}, backend=13),
            ax('4', role='textbox', name='Password', value='hunter2',
               backend=14),
            # ignored wrapper: its checkbox child promotes to the form's level
            ax('5', role='generic', ignored=True, backend=15, children=['5c']),
            ax('5c', role='checkbox', name='Remember', props={'checked': True},
               backend=16),
            ax('6', role='button', name='Sign in', props={'disabled': True},
               backend=17),
        ]

    async def test_snapshot_text_refs_and_masking(self):
        self._login_tree()
        snap = await self.page.snapshot()
        text = str(snap)
        assert 'textbox "Email" = "a@b.com" (required) [e' in text
        assert 'textbox "Password" = "••••••"' in text   # masked, not hunter2
        assert 'hunter2' not in text
        assert 'checkbox "Remember" (checked)' in text    # promoted past ignored
        assert 'button "Sign in" (disabled)' in text
        # refs map to backend ids and are stored on the page
        assert set(snap.refs.values()) == {10, 13, 14, 16, 17}
        assert self.page._snapshot_refs == snap.refs

    async def test_element_for_ref_resolves_backend_node(self):
        self._login_tree()
        snap = await self.page.snapshot()
        email_ref = next(r for r, m in snap.meta.items()
                         if m['name'] == 'Email')
        el = await self.page.element_for_ref(email_ref)
        # resolved via DOM.resolveNode -> objectId "OBJ-<backend>"
        assert repr(el) == "Element('OBJ-13')"
        resolve = [m for m in self.transport.sent
                   if m['method'] == 'DOM.resolveNode']
        assert resolve[-1]['params']['backendNodeId'] == 13

    async def test_unknown_ref_raises(self):
        try:
            await self.page.element_for_ref('e99')
        except LookupError:
            pass
        else:
            raise AssertionError('expected LookupError')

    async def test_act_focus_calls_on_resolved_node(self):
        self._login_tree()
        snap = await self.page.snapshot()
        button_ref = next(r for r, m in snap.meta.items()
                          if m['name'] == 'Sign in')
        await self.page.act(button_ref, 'focus')
        calls = [m for m in self.transport.sent
                 if m['method'] == 'Runtime.callFunctionOn']
        assert calls and calls[-1]['params']['objectId'] == 'OBJ-17'

    async def test_act_bad_action_and_missing_args(self):
        self._login_tree()
        snap = await self.page.snapshot()
        ref = next(iter(snap.refs))
        for kwargs, action in (({}, 'nope'), ({}, 'fill'), ({}, 'select')):
            try:
                await self.page.act(ref, action, **kwargs)
            except ValueError:
                pass
            else:
                raise AssertionError(f'expected ValueError for {action}')


class SnapshotBuilderTests(unittest.TestCase):
    '''Pure build_snapshot behavior the cross-frame stitch relies on.'''

    def test_frame_tokens_are_tracked_per_ref(self):
        from purecdp.testing.snapshot import build_snapshot
        nodes = [
            {'id': '1', 'ignored': False, 'role': 'RootWebArea', 'name': '',
             'value': None, 'props': {}, 'backend': 1, 'children': ['2'],
             'frame': '0'},
            {'id': '2', 'ignored': False, 'role': 'button', 'name': 'Pay',
             'value': None, 'props': {}, 'backend': 9, 'children': [],
             'frame': '1'},   # a spliced cross-frame node
        ]
        snap = build_snapshot(nodes)
        assert set(snap.refs) == set(snap.frames)      # every ref tagged
        pay = next(r for r, m in snap.meta.items() if m['name'] == 'Pay')
        assert snap.frames[pay] == '1'                 # routed to the child frame
        assert snap.frames[next(r for r, m in snap.meta.items()
                                if m['role'] == 'RootWebArea')] == '0'

    def test_frame_tag_absent_defaults_to_none(self):
        from purecdp.testing.snapshot import build_snapshot
        snap = build_snapshot([
            {'id': '1', 'ignored': False, 'role': 'button', 'name': 'x',
             'value': None, 'props': {}, 'backend': 5, 'children': []}])
        assert all(v is None for v in snap.frames.values())  # single-frame


class HumanBehaviorWireTests(PageTestBase):
    async def test_cursor_move_emits_curved_trusted_path_landing_on_target(self):
        import random
        from purecdp.testing import HumanCursor
        cur = HumanCursor(self.page, rng=random.Random(1), start=(0, 0))
        await cur.move_to(200, 120, min_delay=0, max_delay=0)
        moves = self.sent('Input.dispatchMouseEvent')
        assert len(moves) > 3                       # multi-step, not a teleport
        assert all(m['params']['type'] == 'mouseMoved' for m in moves)
        last = moves[-1]['params']
        assert (last['x'], last['y']) == (200, 120)  # lands exactly
        assert (cur.x, cur.y) == (200, 120)          # position persists

    async def test_human_click_moves_then_presses_at_box_center(self):
        import random
        from purecdp.testing import HumanCursor
        self.fake.evaluate_results.append(OBJ)
        el = await self.page.query('button')
        # bounding_box eval → box; then move path + press/release
        self.fake.call_results.append(
            {'result': {'type': 'object',
                        'value': {'x': 100, 'y': 100, 'width': 40, 'height': 20}}})
        self.page._cursor = HumanCursor(self.page, rng=random.Random(2))
        await el.human_click(min_delay=0, max_delay=0)
        events = self.sent('Input.dispatchMouseEvent')
        kinds = [e['params']['type'] for e in events]
        assert kinds[-2:] == ['mousePressed', 'mouseReleased']
        press = events[-1]['params']
        # released within the button box (aimed near center, not exact pixel)
        assert 100 <= press['x'] <= 140 and 100 <= press['y'] <= 120
        assert press['button'] == 'left'

    async def test_human_type_fires_per_key_events(self):
        import random
        await self.page.human_type('hi!', wpm=9999, variance=0,
                                   mistake_pause=0, rng=random.Random(0))
        keys = self.sent('Input.dispatchKeyEvent')
        # 3 chars × (keyDown + keyUp)
        assert [k['params']['type'] for k in keys] == \
            ['keyDown', 'keyUp'] * 3
        downs = [k['params'] for k in keys if k['params']['type'] == 'keyDown']
        assert [d['text'] for d in downs] == ['h', 'i', '!']

    async def test_human_scroll_eases_in_steps_summing_to_delta(self):
        import random
        self.page._cursor  # touch to create cursor
        await self.page.human_scroll(300, steps=6, min_delay=0, max_delay=0,
                                     rng=random.Random(3))
        wheels = self.sent('Input.dispatchMouseEvent')
        assert all(w['params']['type'] == 'mouseWheel' for w in wheels)
        assert len(wheels) == 6
        total = sum(w['params']['deltaY'] for w in wheels)
        assert abs(total - 300) < 1e-6           # steps sum to the requested delta
