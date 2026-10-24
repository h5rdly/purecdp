'''WebDriver BiDi: the second wire protocol purecdp speaks (Firefox).

Same websocket transport as CDP, different framing and value model. This
package is the protocol-level half — :class:`BidiConnection` (commands,
events, session), :mod:`.serde` (LocalValue/RemoteValue <-> Python) — and
``purecdp.testing._bidi_driver`` plugs it into the driver seam so ``Page``
works unchanged on top. ``launch(browser="firefox")`` is the front door.

Status (M20 phase 1): connection + serde + scripting/navigation on the
driver; the rest of the Page surface raises ``NotSupported`` until phases
2–4 land. Not single-protocol by design: Chromium stays on CDP.
'''

from .connection import BidiCommandError, BidiConnection, BidiEvent, connect

__all__ = ['BidiCommandError', 'BidiConnection', 'BidiEvent', 'connect']
