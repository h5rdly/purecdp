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


class CDPClosedError(PureCDPError):
    '''The session or connection is gone for good — retrying cannot help.

    Common base of :class:`CDPSessionClosed` and :class:`CDPConnectionClosed`,
    so a watcher loop can separate give-up from retry with one name::

        try:
            ...poll...
        except CDPClosedError:
            break                  # tab/browser is gone
        except PureCDPError:
            continue               # transient — back off and retry
    '''


class CDPConnectionClosed(CDPClosedError):
    '''The connection is closed; pending and future operations fail with this.'''


class CDPSessionClosed(CDPClosedError):
    '''The session detached (target closed/crashed) or its connection closed.'''


class CDPCommandTimeout(TimeoutError, PureCDPError):
    '''A CDP command did not return within the requested time.

    Raised only when a bound was asked for — ``session.execute(cmd,
    timeout=...)`` per call, or ``connection.default_command_timeout`` for
    every command on the connection (None, the default, keeps commands
    unbounded: some are legitimately open-ended, e.g. ``Runtime.evaluate``
    awaiting a promise). The message names the CDP method that hung. A late
    response arriving after the timeout is discarded, not misdelivered.
    Both a ``TimeoutError`` (generic handlers keep working) and a
    ``PureCDPError`` (the family catch is universal).
    '''


class CDPTransportError(PureCDPError):
    '''Transport-level failure: websocket handshake rejected, oversized or
    malformed frame, write on a closed transport, discovery endpoint error.'''


class TargetNotFound(PureCDPError):
    '''``browser.attach()`` could not pick a page target: nothing matched
    (the message lists the page targets that DO exist), or ``url_contains``
    matched several (the message lists their ids — pass ``target_id=`` to
    pick one; guessing would mean driving the wrong tab of a browser a human
    may be using).'''


class BrowserLaunchError(PureCDPError):
    '''The browser binary could not be found, failed to start, or never
    published its debugging endpoint.'''


class ProtocolDriftWarning(UserWarning):
    '''The browser sent an event the pinned spec can parse only partially;
    the payload was preserved raw instead of crashing dispatch.'''
