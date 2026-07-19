'''Stdlib websocket client — the RFC 6455 subset CDP actually needs.

We are a *client* talking to a *local* browser: handshake, client-side
masking, text frames, fragmentation/continuation, ping/pong, close. No TLS,
no compression (Chrome does not negotiate permessage-deflate on the DevTools
socket), no server role. Server→client frames arrive unmasked, so large
payloads (screenshots) cost no unmasking pass.
'''

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import struct
from contextlib import suppress
from urllib.parse import urlsplit

from ..errors import CDPTransportError

_WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

#: CDP messages can be huge (base64 screenshots/PDFs); Chrome's own limit is
#: 256 MB per message.
DEFAULT_MAX_MESSAGE_SIZE = 256 * 1024 * 1024


class _PeerClosed(Exception):
    '''Internal: the server initiated the closing handshake.'''


def _mask(payload: bytes, mask: bytes) -> bytes:
    if not payload:
        return b''
    n = len(payload)
    repeated = (mask * (n // 4 + 1))[:n]
    return (
        int.from_bytes(payload, 'big') ^ int.from_bytes(repeated, 'big')
    ).to_bytes(n, 'big')


class WebSocketTransport:
    '''A connected websocket satisfying purecdp.connection.Transport.

    Create with :meth:`connect`; iterate for incoming text messages; ping
    frames are answered automatically during iteration.
    '''

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        max_message_size: int,
    ):
        self._reader = reader
        self._writer = writer
        self._max_message_size = max_message_size
        self._closed = False

    @classmethod
    async def connect(
        cls, url: str, *, max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE
    ) -> WebSocketTransport:
        '''Open and upgrade a connection to a ``ws://`` URL.'''
        parts = urlsplit(url)
        if parts.scheme != 'ws':
            raise CDPTransportError(f'unsupported URL scheme: {url!r}')
        host = parts.hostname or '127.0.0.1'
        port = parts.port or 80
        path = parts.path or '/'
        if parts.query:
            path += '?' + parts.query

        reader, writer = await asyncio.open_connection(host, port)
        try:
            key = base64.b64encode(secrets.token_bytes(16)).decode()
            request = (
                f'GET {path} HTTP/1.1\r\n'
                f'Host: {host}:{port}\r\n'
                'Upgrade: websocket\r\n'
                'Connection: Upgrade\r\n'
                f'Sec-WebSocket-Key: {key}\r\n'
                'Sec-WebSocket-Version: 13\r\n'
                '\r\n'
            )
            writer.write(request.encode())
            await writer.drain()

            raw = await reader.readuntil(b'\r\n\r\n')
            status_line, *header_lines = raw.decode('latin-1').split('\r\n')
            status_parts = status_line.split(' ', 2)
            if len(status_parts) < 2 or status_parts[1] != '101':
                raise CDPTransportError(f'handshake rejected: {status_line!r}')
            headers = {}
            for line in header_lines:
                name, sep, value = line.partition(':')
                if sep:
                    headers[name.strip().lower()] = value.strip()
            expected = base64.b64encode(
                hashlib.sha1((key + _WS_GUID).encode()).digest()
            ).decode()
            if headers.get('sec-websocket-accept') != expected:
                raise CDPTransportError('handshake failed: bad Sec-WebSocket-Accept')
        except BaseException:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            raise
        return cls(reader, writer, max_message_size)

    # -- Transport interface -------------------------------------------------

    async def send(self, message: str) -> None:
        if self._closed:
            raise CDPTransportError('send on closed websocket')
        await self._send_frame(_OP_TEXT, message.encode())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            await self._send_frame(_OP_CLOSE, struct.pack('!H', 1000), force=True)
        self._writer.close()
        with suppress(Exception):
            await self._writer.wait_closed()

    def __aiter__(self) -> WebSocketTransport:
        return self

    async def __anext__(self) -> str:
        try:
            return (await self._recv_message()).decode('utf-8')
        except (
            _PeerClosed,
            asyncio.IncompleteReadError,
            ConnectionResetError,
            BrokenPipeError,
        ):
            raise StopAsyncIteration from None

    # -- frame layer ---------------------------------------------------------

    async def _send_frame(self, opcode: int, payload: bytes, force: bool = False) -> None:
        if self._closed and not force:
            raise CDPTransportError('send on closed websocket')
        mask = secrets.token_bytes(4)
        length = len(payload)
        if length < 126:
            header = struct.pack('!BB', 0x80 | opcode, 0x80 | length)
        elif length < 65536:
            header = struct.pack('!BBH', 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack('!BBQ', 0x80 | opcode, 0x80 | 127, length)
        # one write() per frame keeps concurrent sends from interleaving
        self._writer.write(header + mask + _mask(payload, mask))
        await self._writer.drain()

    async def _recv_frame(self) -> tuple[bool, int, bytes]:
        first, second = await self._reader.readexactly(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack('!H', await self._reader.readexactly(2))
        elif length == 127:
            (length,) = struct.unpack('!Q', await self._reader.readexactly(8))
        if length > self._max_message_size:
            raise CDPTransportError(f'frame of {length} bytes exceeds limit')
        if second & 0x80:  # RFC 6455 5.1: server frames must not be masked
            raise CDPTransportError('server sent a masked frame')
        payload = await self._reader.readexactly(length) if length else b''
        return fin, opcode, payload

    async def _recv_message(self) -> bytes:
        parts: list[bytes] = []
        total = 0
        in_message = False
        while True:
            fin, opcode, payload = await self._recv_frame()
            if opcode == _OP_PING:
                await self._send_frame(_OP_PONG, payload, force=True)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                self._closed = True
                with suppress(Exception):
                    await self._send_frame(_OP_CLOSE, payload[:2], force=True)
                self._writer.close()
                raise _PeerClosed
            if opcode in (_OP_TEXT, _OP_BINARY):
                if in_message:
                    raise CDPTransportError('new message before previous finished')
                in_message = True
            elif opcode == _OP_CONT:
                if not in_message:
                    raise CDPTransportError('continuation frame without a message')
            else:
                raise CDPTransportError(f'unsupported opcode {opcode:#x}')
            total += len(payload)
            if total > self._max_message_size:
                raise CDPTransportError(f'message of {total} bytes exceeds limit')
            parts.append(payload)
            if fin:
                return b''.join(parts)
