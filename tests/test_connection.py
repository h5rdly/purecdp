'''Tests for the asyncio Connection/Session layer, driven by a scripted
in-memory transport playing the browser's side of recorded-style traffic.

Runnable three ways: `python -m unittest`, `python tests/test_connection.py`,
or `python -m pytest` — none require pytest. Plain asserts: don't run with -O.
'''

import asyncio, pathlib, sys, warnings, unittest
from unittest import mock
_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

try:
    from .support import FakeTransport, drain  # package context
except ImportError:
    from support import FakeTransport, drain  # direct run

from purecdp import (  # noqa: E402
    CDPConnectionClosed,
    CDPError,
    CDPSessionClosed,
    Connection,
)
from purecdp._shared import UnknownEvent  # noqa: E402
from purecdp.protocol import page, target  # noqa: E402


def scripted_browser(msg):
    '''Responder playing a browser: attach, Page.enable, Page.navigate
    (response followed by lifecycle events, as Chrome actually interleaves).'''
    method, msg_id = msg['method'], msg['id']
    if method == 'Target.attachToTarget':
        assert msg['params'] == {'targetId': 'T1', 'flatten': True}
        return [{'id': msg_id, 'result': {'sessionId': 'SESS1'}}]
    if method == 'Page.enable':
        assert msg['sessionId'] == 'SESS1'
        return [{'id': msg_id, 'sessionId': 'SESS1', 'result': {}}]
    if method == 'Page.navigate':
        return [
            {'id': msg_id, 'sessionId': 'SESS1',
             'result': {'frameId': 'F1', 'loaderId': 'L1'}},
            {'method': 'Page.frameStartedLoading', 'sessionId': 'SESS1',
             'params': {'frameId': 'F1'}},
            {'method': 'Page.loadEventFired', 'sessionId': 'SESS1',
             'params': {'timestamp': 100.5}},
        ]
    raise AssertionError(f'unscripted method {method}')


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    '''The M2 flagship: a full attach→enable→navigate→load flow, replayed.'''

    async def test_full_navigation_flow(self):
        transport = FakeTransport(scripted_browser)
        async with Connection(transport) as conn:
            session = await conn.attach('T1')
            assert session.session_id == 'SESS1'

            await session.execute(page.enable())

            # subscribe BEFORE triggering — the documented pattern
            with session.listen(page.LoadEventFired) as stream:
                result = await session.execute(page.navigate(url='https://x'))
                assert result[0] == page.FrameId('F1')
                event = await asyncio.wait_for(anext(stream), timeout=2)
            assert isinstance(event, page.LoadEventFired)
            assert event.timestamp == 100.5

        # wire assertions: ids increment, session commands carry sessionId
        methods = [m['method'] for m in transport.sent]
        assert methods == ['Target.attachToTarget', 'Page.enable', 'Page.navigate']
        assert [m['id'] for m in transport.sent] == [1, 2, 3]
        assert 'sessionId' not in transport.sent[0]
        assert transport.sent[2]['sessionId'] == 'SESS1'
        assert transport.closed

    async def test_wait_for_started_before_trigger(self):
        async with Connection(FakeTransport(scripted_browser)) as conn:
            session = await conn.attach('T1')
            waiter = asyncio.create_task(
                session.wait_for(page.LoadEventFired, timeout=2))
            await drain()  # let wait_for subscribe
            await session.execute(page.navigate(url='https://x'))
            event = await waiter
            assert event.timestamp == 100.5


class ExecuteTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_error_raises_cdp_error(self):
        def responder(msg):
            return [{'id': msg['id'],
                     'error': {'code': -32000, 'message': 'Not allowed'}}]

        async with Connection(FakeTransport(responder)) as conn:
            try:
                await conn.execute(target.create_target(url='about:blank'))
            except CDPError as exc:
                assert exc.code == -32000
                assert exc.message == 'Not allowed'
            else:
                raise AssertionError('expected CDPError')

    async def test_execute_after_close_raises(self):
        conn = Connection(FakeTransport())
        await conn.open()
        await conn.aclose()
        try:
            await conn.execute(page.enable())
        except CDPConnectionClosed:
            pass
        else:
            raise AssertionError('expected CDPConnectionClosed')

    async def test_transport_ending_fails_pending_command(self):
        transport = FakeTransport()
        async with Connection(transport) as conn:
            task = asyncio.create_task(conn.execute(page.enable()))
            await drain()
            transport.end()  # peer closes with the command still pending
            try:
                await asyncio.wait_for(task, timeout=2)
            except CDPConnectionClosed:
                pass
            else:
                raise AssertionError('expected CDPConnectionClosed')
            assert conn.closed

    async def test_garbage_from_peer_aborts_connection(self):
        transport = FakeTransport()
        async with Connection(transport) as conn:
            transport.push_raw('not json')
            await drain()
            assert conn.closed


class EventDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_unwanted_events_are_not_parsed(self):
        # a listener wanting only LoadEventFired; push a *different* known
        # event whose payload would raise on parse. Eager parsing would emit a
        # ProtocolDriftWarning — silence proves the dispatcher skipped it.
        transport = FakeTransport(scripted_browser)
        async with Connection(transport) as conn:
            with conn.listen(page.LoadEventFired) as stream:
                with mock.patch('warnings.warn') as warn:
                    transport.push({'method': 'Page.frameStartedLoading',
                                    'params': {}})  # missing required frameId
                    await drain()
                assert warn.call_count == 0  # never parsed → no drift warning
                # the wanted event still flows and parses fine
                transport.push({'method': 'Page.loadEventFired',
                                'params': {'timestamp': 9.0}})
                got = await asyncio.wait_for(anext(stream), timeout=2)
            assert isinstance(got, page.LoadEventFired)

    async def test_events_route_to_their_session_only(self):
        transport = FakeTransport(scripted_browser)
        async with Connection(transport) as conn:
            session = await conn.attach('T1')
            with conn.listen() as root_stream, session.listen() as sess_stream:
                transport.push({'method': 'Page.loadEventFired',
                                'sessionId': 'SESS1', 'params': {'timestamp': 1.0}})
                transport.push({'method': 'Target.targetCrashed',
                                'params': {'targetId': 'T2', 'status': 'crashed',
                                           'errorCode': 1}})
                await drain()
                sess_ev = await asyncio.wait_for(anext(sess_stream), timeout=2)
                root_ev = await asyncio.wait_for(anext(root_stream), timeout=2)
            assert isinstance(sess_ev, page.LoadEventFired)
            assert isinstance(root_ev, target.TargetCrashed)

    async def test_type_filter_and_predicate(self):
        transport = FakeTransport(scripted_browser)
        async with Connection(transport) as conn:
            session = await conn.attach('T1')
            waiter = asyncio.create_task(session.wait_for(
                page.LoadEventFired,
                predicate=lambda e: e.timestamp > 5,
                timeout=2))
            await drain()
            for ts in (1.0, 3.0, 7.5):  # filtered by predicate until 7.5
                transport.push({'method': 'Page.loadEventFired',
                                'sessionId': 'SESS1', 'params': {'timestamp': ts}})
            transport.push({'method': 'Page.frameStartedLoading',  # filtered by type
                            'sessionId': 'SESS1', 'params': {'frameId': 'F1'}})
            event = await waiter
            assert event.timestamp == 7.5

    async def test_wait_for_timeout(self):
        async with Connection(FakeTransport()) as conn:
            try:
                await conn.wait_for(page.LoadEventFired, timeout=0.05)
            except TimeoutError:
                pass
            else:
                raise AssertionError('expected TimeoutError')

    async def test_unknown_event_still_delivered(self):
        transport = FakeTransport()
        async with Connection(transport) as conn:
            with conn.listen() as stream:
                transport.push({'method': 'FutureDomain.newThing',
                                'params': {'x': 1}})
                event = await asyncio.wait_for(anext(stream), timeout=2)
            assert isinstance(event, UnknownEvent)
            assert event.method == 'FutureDomain.newThing'

    async def test_buffer_overflow_drops_oldest_and_warns(self):
        transport = FakeTransport()
        async with Connection(transport) as conn:
            with conn.listen(buffer_size=2) as stream:
                # on the free-threaded build warnings state is context-scoped, so the
                # warning escapes catch_warnings. Patch warnings.warn at the call
                # site instead, which is context/thread independent.
                with mock.patch('warnings.warn') as warn:
                    for ts in (1.0, 2.0, 3.0):
                        transport.push({'method': 'Page.loadEventFired',
                                        'params': {'timestamp': ts}})
                    await drain()
                assert any('dropping oldest' in str(c.args[0]) for c in warn.call_args_list if c.args)
                first = await anext(stream)
                second = await anext(stream)
            assert (first.timestamp, second.timestamp) == (2.0, 3.0)


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_detached_from_target_closes_session(self):
        transport = FakeTransport(scripted_browser)
        async with Connection(transport) as conn:
            session = await conn.attach('T1')
            stream = session.listen()
            transport.push({'method': 'Target.detachedFromTarget',
                            'params': {'sessionId': 'SESS1', 'targetId': 'T1'}})
            await drain()
            assert session.closed
            # stream ends cleanly
            try:
                await asyncio.wait_for(anext(stream), timeout=2)
            except StopAsyncIteration:
                pass
            else:
                raise AssertionError('expected StopAsyncIteration')
            # further commands on the dead session fail fast
            try:
                await session.execute(page.enable())
            except CDPSessionClosed:
                pass
            else:
                raise AssertionError('expected CDPSessionClosed')
            # ...but the connection itself is still healthy
            assert not conn.closed
            session2 = await conn.attach('T1')
            assert session2 is not session


if __name__ == '__main__':
    unittest.main()
