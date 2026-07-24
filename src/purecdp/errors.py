'''Exception and warning types for the CDP engine and connection layers.

All purecdp exceptions inherit :class:`PureCDPError`, so ``except
PureCDPError:`` catches anything this library raises.
'''

from __future__ import annotations


class PureCDPError(Exception):
    '''Base class for every exception purecdp raises.'''


class CDPCommandError(PureCDPError):
    '''A command error reported by the browser ({code, message, data}).'''

    def __init__(self, code: int, message: str, data: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def __str__(self) -> str:
        s = f'[{self.code}] {self.message}'
        if self.data:
            s += f': {self.data}'
        return s


#: Backward-compat alias: CDPCommandError was named CDPError before 0.4 —
#: the old name read like the package base class, which it never was.
CDPError = CDPCommandError


class CDPProtocolError(PureCDPError):
    '''The remote endpoint sent something that is not a valid CDP message
    (unparseable, not an object, unknown command id, neither response nor
    event). Indicates a broken peer or transport; the connection aborts.'''


class CDPConnectionClosed(PureCDPError):
    '''The connection is closed; pending and future operations fail with this.'''


class CDPSessionClosed(PureCDPError):
    '''The session detached (target closed/crashed) or its connection closed.'''


class CDPTransportError(PureCDPError):
    '''Transport-level failure: websocket handshake rejected, oversized or
    malformed frame, write on a closed transport, discovery endpoint error.'''


class BrowserLaunchError(PureCDPError):
    '''The browser binary could not be found, failed to start, or never
    published its debugging endpoint.'''


class ProtocolDriftWarning(UserWarning):
    '''The browser sent an event the pinned spec can parse only partially;
    the payload was preserved raw instead of crashing dispatch.'''
