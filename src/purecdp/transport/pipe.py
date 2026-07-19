'''CDP over ``--remote-debugging-pipe``: NUL-separated JSON on child fds 3/4.

The browser reads commands from its fd 3 and writes messages to its fd 4 —
no port, no HTTP discovery, no websocket, best isolation for parallel test
runs. POSIX-only (fd plumbing + preexec_fn); the websocket transport is the
portable path.
'''

from __future__ import annotations

import asyncio, os, sys, typing
from contextlib import suppress

from ..errors import CDPTransportError

#: Same generous per-message budget as the websocket transport.
DEFAULT_MAX_MESSAGE_SIZE = 256 * 1024 * 1024


class PipeTransport:
    '''A pair of OS pipes satisfying purecdp.connection.Transport.'''

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        read_file: typing.BinaryIO,
    ):
        self._reader = reader
        self._writer = writer
        self._read_file = read_file  # keep alive; owns read_fd
        self._closed = False

    @classmethod
    async def open(
        cls,
        read_fd: int,
        write_fd: int,
        *,
        max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
    ) -> PipeTransport:
        '''Wrap two already-open pipe fds (our read end and our write end).'''
        loop = asyncio.get_running_loop()

        reader = asyncio.StreamReader(limit=max_message_size)
        read_file = os.fdopen(read_fd, 'rb', buffering=0)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), read_file
        )

        write_file = os.fdopen(write_fd, 'wb', buffering=0)
        w_transport, w_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, write_file
        )
        writer = asyncio.StreamWriter(w_transport, w_protocol, None, loop)

        return cls(reader, writer, read_file)

    # -- Transport interface -------------------------------------------------

    async def send(self, message: str) -> None:
        if self._closed:
            raise CDPTransportError('send on closed pipe')
        self._writer.write(message.encode() + b'\0')
        await self._writer.drain()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._writer.close()
        with suppress(Exception):
            await self._writer.wait_closed()

    def __aiter__(self) -> PipeTransport:
        return self

    async def __anext__(self) -> str:
        try:
            chunk = await self._reader.readuntil(b'\0')
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            raise StopAsyncIteration from None
        return chunk[:-1].decode('utf-8')


async def spawn_pipe_process(
    argv: list[str], **subprocess_kwargs: typing.Any
) -> tuple[asyncio.subprocess.Process, PipeTransport]:
    '''Spawn a process that speaks CDP-pipe framing on its fds 3 and 4.

    Creates the two pipes, moves our ends onto the child's fds 3/4 (via
    preexec_fn, after fork — clobbering 3/4 in the *child* is safe), and
    returns the process plus a connected :class:`PipeTransport`.
    '''
    if sys.platform == 'win32':
        raise CDPTransportError(
            'the pipe transport is POSIX-only (needs fd 3/4 plumbing via '
            'preexec_fn); use the websocket transport (launch(pipe=False)) '
            'on Windows')
    child_read, we_write = os.pipe()  # child fd 3: reads what we write
    we_read, child_write = os.pipe()  # child fd 4: writes what we read
    os.set_inheritable(child_read, True)
    os.set_inheritable(child_write, True)

    def _child_setup() -> None:
        # os.pipe() allocates ascending, so child_read < child_write and this
        # ordering never clobbers a pipe end the child still needs.
        os.dup2(child_read, 3)
        os.dup2(child_write, 4)
        for fd in (child_read, child_write):
            if fd not in (3, 4):
                os.close(fd)

    try:
        # close_fds must stay False: subprocess's fd-closing pass runs *after*
        # preexec_fn and would close the dup2'd 3/4. Only explicitly
        # inheritable fds survive exec anyway (PEP 446), so nothing leaks
        # beyond the two pipe ends.
        process = await asyncio.create_subprocess_exec(
            *argv,
            close_fds=False,
            preexec_fn=_child_setup,
            **subprocess_kwargs,
        )
    except BaseException:
        for fd in (child_read, we_write, we_read, child_write):
            with suppress(OSError):
                os.close(fd)
        raise
    # parent must drop the child's ends or EOF never arrives
    os.close(child_read)
    os.close(child_write)

    try:
        transport = await PipeTransport.open(read_fd=we_read, write_fd=we_write)
    except BaseException:
        for fd in (we_read, we_write):
            with suppress(OSError):
                os.close(fd)
        process.kill()
        raise
    return process, transport
