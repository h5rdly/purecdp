'''Pure-Python Chrome DevTools Protocol library.

Layers (see DESIGN.md):
- purecdp.protocol   — generated typed bindings, one module per CDP domain
- purecdp.engine     — sans-I/O protocol engine (strings in, typed happenings out)
- purecdp.connection — asyncio Connection/Session over an abstract Transport
- purecdp.transport  — stdlib websocket + pipe transports
- purecdp.browser    — find/launch/clean-up a debuggable browser
- purecdp.discovery  — /json/version, /json/list HTTP endpoints
- purecdp.errors     — exception types
- purecdp._shared    — support types shared by generated code and the engine
'''

from ._loop import run
from .browser import (
    STEALTH_ARGS,
    Browser,
    BrowserContext,
    close_page,
    connect,
    find_browser,
    launch,
    new_context,
    new_session,
)
from .connection import Connection, EventStream, Session, Transport
from .errors import (
    BrowserLaunchError,
    CDPClosedError,
    CDPConnectionClosed,
    CDPCommandError,
    CDPCommandTimeout,
    CDPError,
    PureCDPError,
    CDPProtocolError,
    CDPSessionClosed,
    CDPTransportError,
    ProtocolDriftWarning,
    TargetNotFound,
)
from .transport import PipeTransport, WebSocketTransport

__version__ = '0.8.0'

__all__ = [
    'Connection',
    'Session',
    'EventStream',
    'Transport',
    'WebSocketTransport',
    'PipeTransport',
    'Browser',
    'BrowserContext',
    'launch',
    'connect',
    'find_browser',
    'new_context',
    'new_session',
    'close_page',
    'STEALTH_ARGS',
    'run',
    'CDPCommandError',
    'CDPCommandTimeout',
    'CDPError',
    'PureCDPError',
    'CDPProtocolError',
    'CDPClosedError',
    'CDPConnectionClosed',
    'CDPSessionClosed',
    'CDPTransportError',
    'TargetNotFound',
    'BrowserLaunchError',
    'ProtocolDriftWarning',
]
