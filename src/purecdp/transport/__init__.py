'''Layer 2: transports — stdlib implementations of purecdp.connection.Transport.

- WebSocketTransport: RFC 6455 client subset over asyncio streams
- PipeTransport: ``--remote-debugging-pipe`` framing (NUL-separated JSON)
'''

from .pipe import PipeTransport, spawn_pipe_process
from .websocket import WebSocketTransport

__all__ = ['WebSocketTransport', 'PipeTransport', 'spawn_pipe_process']
