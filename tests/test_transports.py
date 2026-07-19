'''Tests for the websocket and pipe transports — no browser required.

The websocket client is exercised against a minimal in-process RFC 6455
server (tests/support.py); the pipe transport against a spawned Python child
speaking the NUL-framing on fds 3/4.

Runnable three ways: `python -m unittest`, `python tests/test_transports.py`,
or `python -m pytest` — none require pytest. Plain asserts: don't run with -O.
'''

import asyncio
import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

try:
    from .support import PIPE_ECHO_CHILD, WsTestServer
except ImportError:
    from support import PIPE_ECHO_CHILD, WsTestServer

from purecdp.errors import CDPTransportError  # noqa: E402
from purecdp.transport import PipeTransport, WebSocketTransport, spawn_pipe_process  # noqa: E402


class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_and_roundtrip(self):
        async with WsTestServer() as server:
            ws = await WebSocketTransport.connect(server.url)
            await ws.send('{"id":1}')
            await server.wait_until(lambda: server.received)
            assert server.received == ['{"id":1}']
            await server.send_text('{"result":{}}')
            assert await asyncio.wait_for(anext(ws), 2) == '{"result":{}}'
            await ws.close()

    async def test_fragmented_message_reassembled(self):
        async with WsTestServer() as server:
            ws = await WebSocketTransport.connect(server.url)
            await server.send_text('hello fragmented world', fragment_size=5)
            assert await asyncio.wait_for(anext(ws), 2) == 'hello fragmented world'
            await ws.close()

    async def test_large_messages_both_directions(self):
        # >65535 bytes exercises the 64-bit length path both ways
        big = 'x' * 200_000
        async with WsTestServer() as server:
            ws = await WebSocketTransport.connect(server.url)
            await ws.send(big)
            await server.wait_until(lambda: server.received)
            assert server.received[0] == big
            await server.send_text(big)
            assert await asyncio.wait_for(anext(ws), 5) == big
            await ws.close()

    async def test_ping_answered_with_pong(self):
        async with WsTestServer() as server:
            ws = await WebSocketTransport.connect(server.url)
            await server.send_ping(b'hb')
            # pong is sent during iteration; also prove data flows after it
            await server.send_text('after-ping')
            assert await asyncio.wait_for(anext(ws), 2) == 'after-ping'
            await server.wait_until(lambda: server.pongs)
            assert server.pongs == [b'hb']
            await ws.close()

    async def test_server_close_ends_iteration(self):
        async with WsTestServer() as server:
            ws = await WebSocketTransport.connect(server.url)
            await server.send_close()
            try:
                await asyncio.wait_for(anext(ws), 2)
            except StopAsyncIteration:
                pass
            else:
                raise AssertionError('expected StopAsyncIteration')
            try:
                await ws.send('late')
            except CDPTransportError:
                pass
            else:
                raise AssertionError('expected CDPTransportError after close')

    async def test_rejected_handshake_raises(self):
        async with WsTestServer(reject=True) as server:
            try:
                await WebSocketTransport.connect(server.url)
            except CDPTransportError as exc:
                assert '403' in str(exc)
            else:
                raise AssertionError('expected CDPTransportError')

    async def test_oversized_message_raises(self):
        async with WsTestServer() as server:
            ws = await WebSocketTransport.connect(server.url, max_message_size=10)
            await server.send_text('this exceeds ten bytes')
            try:
                await asyncio.wait_for(anext(ws), 2)
            except CDPTransportError:
                pass
            else:
                raise AssertionError('expected CDPTransportError')
            await ws.close()

    async def test_bad_scheme_rejected(self):
        try:
            await WebSocketTransport.connect('http://127.0.0.1:1/')
        except CDPTransportError:
            pass
        else:
            raise AssertionError('expected CDPTransportError')


@unittest.skipIf(sys.platform == 'win32', 'pipe transport is POSIX-only')
class PipeTests(unittest.IsolatedAsyncioTestCase):
    async def _spawn_echo(self):
        return await spawn_pipe_process([sys.executable, '-c', PIPE_ECHO_CHILD])

    async def test_echo_roundtrip(self):
        process, pipe = await self._spawn_echo()
        try:
            for message in ('{"id":1,"method":"Page.enable"}', 'with\nnewline', 'ünïcødé'):
                await pipe.send(message)
                assert await asyncio.wait_for(anext(pipe), 5) == message
        finally:
            await pipe.close()
            await process.wait()

    async def test_large_message(self):
        process, pipe = await self._spawn_echo()
        try:
            big = '{"data":"' + 'y' * 1_000_000 + '"}'
            await pipe.send(big)
            assert await asyncio.wait_for(anext(pipe), 10) == big
        finally:
            await pipe.close()
            await process.wait()

    async def test_close_ends_child_and_iteration(self):
        process, pipe = await self._spawn_echo()
        await pipe.close()  # child sees EOF on its fd 3 and exits
        try:
            await asyncio.wait_for(anext(pipe), 5)
        except StopAsyncIteration:
            pass
        else:
            raise AssertionError('expected StopAsyncIteration')
        assert await asyncio.wait_for(process.wait(), 5) == 0
        try:
            await pipe.send('late')
        except CDPTransportError:
            pass
        else:
            raise AssertionError('expected CDPTransportError after close')


if __name__ == '__main__':
    unittest.main()
