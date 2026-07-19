'''Test utilities: in-memory transports for driving a Connection.

Stdlib only, like everything in tests/.
'''

import asyncio
import base64
import hashlib
import json
import struct
from contextlib import suppress


class FakeTransport:
    '''Scriptable in-memory transport satisfying purecdp.connection.Transport.

    ``responder`` (optional) is called with each sent message (as a dict) and
    returns an iterable of reply dicts to queue as incoming — enough to script
    a fake browser. Unsolicited traffic (events) goes in via :meth:`push`;
    :meth:`end` simulates the peer closing the connection.
    '''

    def __init__(self, responder=None):
        self.sent: list[dict] = []
        self.closed = False
        self._responder = responder
        self._incoming: asyncio.Queue = asyncio.Queue()

    async def send(self, message: str) -> None:
        msg = json.loads(message)
        self.sent.append(msg)
        if self._responder is not None:
            for reply in self._responder(msg) or ():
                self._incoming.put_nowait(json.dumps(reply))

    def push(self, message: dict) -> None:
        '''Queue an unsolicited incoming message (e.g. an event).'''
        self._incoming.put_nowait(json.dumps(message))

    def push_raw(self, raw: str) -> None:
        '''Queue a raw incoming string (for malformed-traffic tests).'''
        self._incoming.put_nowait(raw)

    def end(self) -> None:
        '''Simulate the peer closing: iteration stops after queued items.'''
        self._incoming.put_nowait(None)

    async def close(self) -> None:
        self.closed = True
        self.end()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self._incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item


async def drain(cycles: int = 20) -> None:
    '''Let the reader task process everything already queued.'''
    for _ in range(cycles):
        await asyncio.sleep(0)


def ax(node_id, *, role=None, name=None, value=None, props=None,
       backend=None, children=(), ignored=False) -> dict:
    '''Build one AXNode JSON dict for FakeBrowser.ax_nodes (test helper).'''
    def axval(v):
        t = ('boolean' if isinstance(v, bool)
             else 'integer' if isinstance(v, int) else 'string')
        return {'type': t, 'value': v}

    node = {'nodeId': str(node_id), 'ignored': ignored,
            'childIds': [str(c) for c in children]}
    if role is not None:
        node['role'] = {'type': 'role', 'value': role}
    if name is not None:
        node['name'] = {'type': 'computedString', 'value': name}
    if value is not None:
        node['value'] = {'type': 'string', 'value': value}
    if props:
        node['properties'] = [{'name': k, 'value': axval(v)}
                              for k, v in props.items()]
    if backend is not None:
        node['backendDOMNodeId'] = backend
    return node


class FakeBrowser:
    '''Stateful scripted browser for target-lifecycle tests: browser
    contexts, targets, (auto-)attach with optional pause-on-start, plus
    helpers to fabricate crash / info-changed events for FakeTransport.push.
    Use as the responder of a FakeTransport.'''

    def __init__(self):
        self._n = 0
        self.contexts: set[str] = set()
        self.targets: dict[str, dict] = {}  # tid -> {'url', 'ctx'}
        self.sessions: dict[str, str] = {}  # sid -> tid
        self.auto_attach = False
        self.wait_on_start = False
        self.resumed: list[str] = []  # sids that got Runtime.runIfWaitingForDebugger
        #: Pop-from-front canned results for Runtime.evaluate (raw result
        #: dicts, e.g. {'result': {'type': 'number', 'value': 5}}); empty ->
        #: {'result': {'type': 'undefined'}}.
        self.evaluate_results: list[dict] = []
        #: Same, for Runtime.callFunctionOn.
        self.call_results: list[dict] = []
        #: requestId -> {'body': ..., 'base64Encoded': bool} for
        #: Network.getResponseBody.
        self.response_bodies: dict[str, dict] = {}
        #: If set, Page.navigate responds with this errorText.
        self.navigate_error: str | None = None
        #: Canned AXNode JSON dicts returned by Accessibility.getFullAXTree.
        self.ax_nodes: list[dict] = []

    def _target_info(self, tid: str) -> dict:
        t = self.targets[tid]
        info = {'targetId': tid, 'type': 'page', 'title': '', 'url': t['url'],
                'attached': True, 'canAccessOpener': False}
        if t['ctx']:
            info['browserContextId'] = t['ctx']
        return info

    def _attach(self, tid: str, waiting: bool) -> tuple[str, dict]:
        sid = f'SESS-{tid}'
        self.sessions[sid] = tid
        event = {'method': 'Target.attachedToTarget', 'params': {
            'sessionId': sid, 'targetInfo': self._target_info(tid),
            'waitingForDebugger': waiting}}
        return sid, event

    def _detach(self, sid: str) -> dict:
        tid = self.sessions.pop(sid)
        return {'method': 'Target.detachedFromTarget',
                'params': {'sessionId': sid, 'targetId': tid}}

    def crash(self, tid: str) -> dict:
        '''Event dict for FakeTransport.push simulating a renderer crash.'''
        return {'method': 'Target.targetCrashed',
                'params': {'targetId': tid, 'status': 'crashed', 'errorCode': 139}}

    def info_changed(self, tid: str, url: str) -> dict:
        self.targets[tid]['url'] = url
        return {'method': 'Target.targetInfoChanged',
                'params': {'targetInfo': self._target_info(tid)}}

    def __call__(self, msg: dict) -> list[dict]:
        method, msg_id = msg['method'], msg['id']
        params = msg.get('params', {})
        sid = msg.get('sessionId')

        def ok(result: dict | None = None) -> dict:
            reply = {'id': msg_id, 'result': result or {}}
            if sid:
                reply['sessionId'] = sid
            return reply

        if method == 'Target.createBrowserContext':
            self._n += 1
            ctx = f'CTX-{self._n}'
            self.contexts.add(ctx)
            return [ok({'browserContextId': ctx})]
        if method == 'Target.createTarget':
            self._n += 1
            tid = f'T-{self._n}'
            self.targets[tid] = {'url': params['url'],
                                 'ctx': params.get('browserContextId')}
            replies = [ok({'targetId': tid})]
            if self.auto_attach:
                _, event = self._attach(tid, self.wait_on_start)
                replies.append(event)
            return replies
        if method == 'Target.attachToTarget':
            new_sid, event = self._attach(params['targetId'], False)
            return [event, ok({'sessionId': new_sid})]  # event first, like Chrome
        if method == 'Target.setAutoAttach':
            self.auto_attach = params['autoAttach']
            self.wait_on_start = params['waitForDebuggerOnStart']
            return [ok()]
        if method == 'Target.closeTarget':
            tid = params['targetId']
            replies = [ok({'success': True})]
            replies += [self._detach(s) for s, t in list(self.sessions.items())
                        if t == tid]
            del self.targets[tid]
            return replies
        if method == 'Target.disposeBrowserContext':
            ctx = params['browserContextId']
            self.contexts.discard(ctx)
            replies = [ok()]
            for tid in [t for t, info in list(self.targets.items())
                        if info['ctx'] == ctx]:
                replies += [self._detach(s) for s, t in list(self.sessions.items())
                            if t == tid]
                del self.targets[tid]
            return replies
        if method == 'Target.detachFromTarget':
            return [ok(), self._detach(params['sessionId'])]
        if method == 'Runtime.runIfWaitingForDebugger':
            self.resumed.append(sid)
            return [ok()]
        if method == 'Page.navigate':
            self._n += 1
            result = {'frameId': f'F-{self._n}', 'loaderId': f'L-{self._n}'}
            if self.navigate_error:
                result['errorText'] = self.navigate_error
                return [ok(result)]
            event = {'method': 'Page.loadEventFired',
                     'params': {'timestamp': float(self._n)}}
            if sid:
                event['sessionId'] = sid
            return [ok(result), event]
        if method == 'Runtime.evaluate':
            result = (self.evaluate_results.pop(0) if self.evaluate_results
                      else {'result': {'type': 'undefined'}})
            return [ok(result)]
        if method == 'Page.addScriptToEvaluateOnNewDocument':
            return [ok({'identifier': '1'})]
        if method == 'Runtime.callFunctionOn':
            result = (self.call_results.pop(0) if self.call_results
                      else {'result': {'type': 'undefined'}})
            return [ok(result)]
        if method == 'Network.getResponseBody':
            body = self.response_bodies.get(
                params['requestId'], {'body': '', 'base64Encoded': False})
            return [ok(body)]
        if method == 'Accessibility.getFullAXTree':
            return [ok({'nodes': self.ax_nodes})]
        if method == 'DOM.resolveNode':
            bid = params.get('backendNodeId')
            return [ok({'object': {'type': 'object', 'subtype': 'node',
                                   'objectId': f'OBJ-{bid}'}})]
        return [ok()]  # generic OK for Page.enable etc.


_WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

_OP_CONT, _OP_TEXT, _OP_CLOSE, _OP_PING, _OP_PONG = 0x0, 0x1, 0x8, 0x9, 0xA


class WsTestServer:
    '''Minimal RFC 6455 *server* — just enough to exercise the client
    transport without a browser: handshake, unmasking client frames,
    scripted sends (plain / fragmented / ping / close).'''

    def __init__(self, reject: bool = False):
        self.reject = reject
        self.received: list[str] = []
        self.pongs: list[bytes] = []
        self.url = ''
        self._writer: asyncio.StreamWriter | None = None
        self._connected = asyncio.Event()

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._on_client, '127.0.0.1', 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f'ws://127.0.0.1:{port}/devtools/test'
        return self

    async def __aexit__(self, *exc_info):
        if self._writer is not None:
            self._writer.close()
            with suppress(Exception):
                await self._writer.wait_closed()
        self._server.close()
        await self._server.wait_closed()

    async def _on_client(self, reader, writer):
        raw = await reader.readuntil(b'\r\n\r\n')
        if self.reject:
            writer.write(b'HTTP/1.1 403 Forbidden\r\n\r\n')
            await writer.drain()
            writer.close()
            return
        key = ''
        for line in raw.decode('latin-1').split('\r\n'):
            if line.lower().startswith('sec-websocket-key:'):
                key = line.split(':', 1)[1].strip()
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()
        ).decode()
        writer.write(
            b'HTTP/1.1 101 Switching Protocols\r\n'
            b'Upgrade: websocket\r\nConnection: Upgrade\r\n'
            b'Sec-WebSocket-Accept: ' + accept.encode() + b'\r\n\r\n'
        )
        await writer.drain()
        self._writer = writer
        self._connected.set()
        with suppress(asyncio.IncompleteReadError, ConnectionResetError):
            await self._read_client_frames(reader, writer)

    async def _read_client_frames(self, reader, writer):
        while True:
            first, second = await reader.readexactly(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                (length,) = struct.unpack('!H', await reader.readexactly(2))
            elif length == 127:
                (length,) = struct.unpack('!Q', await reader.readexactly(8))
            mask = await reader.readexactly(4) if second & 0x80 else b''
            payload = await reader.readexactly(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == _OP_TEXT:
                self.received.append(payload.decode())
            elif opcode == _OP_PONG:
                self.pongs.append(payload)
            elif opcode == _OP_CLOSE:
                with suppress(Exception):
                    writer.write(self._frame(_OP_CLOSE, payload[:2]))
                    await writer.drain()
                return

    @staticmethod
    def _frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
        first = (0x80 if fin else 0) | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack('!BB', first, length)
        elif length < 65536:
            header = struct.pack('!BBH', first, 126, length)
        else:
            header = struct.pack('!BBQ', first, 127, length)
        return header + payload

    async def _send(self, data: bytes) -> None:
        await self._connected.wait()
        self._writer.write(data)
        await self._writer.drain()

    async def send_text(self, text: str, fragment_size: int | None = None) -> None:
        payload = text.encode()
        if fragment_size is None:
            await self._send(self._frame(_OP_TEXT, payload))
            return
        chunks = [payload[i:i + fragment_size]
                  for i in range(0, len(payload), fragment_size)] or [b'']
        frames = []
        for i, chunk in enumerate(chunks):
            opcode = _OP_TEXT if i == 0 else _OP_CONT
            frames.append(self._frame(opcode, chunk, fin=(i == len(chunks) - 1)))
        await self._send(b''.join(frames))

    async def send_ping(self, payload: bytes = b'hb') -> None:
        await self._send(self._frame(_OP_PING, payload))

    async def send_close(self, code: int = 1000) -> None:
        await self._send(self._frame(_OP_CLOSE, struct.pack('!H', code)))

    async def wait_until(self, condition, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError('condition not met within timeout')
            await asyncio.sleep(0.005)


#: A child program speaking pipe framing on fds 3/4: echoes every message.
PIPE_ECHO_CHILD = r'''
import os
data = b""
while True:
    chunk = os.read(3, 65536)
    if not chunk:
        break
    data += chunk
    while b"\0" in data:
        msg, _, data = data.partition(b"\0")
        os.write(4, msg + b"\0")
'''
