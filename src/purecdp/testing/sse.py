'''Parse a Server-Sent Events (``text/event-stream``) body into events.

Generic EventSource wire-format parsing (WHATWG HTML spec) — stdlib only, no
network. Handy to assert on a *captured* SSE response body (see
:class:`~purecdp.testing.recorder.NetworkRecorder`, whose ``Exchange.text`` is
the raw stream). Any application-level encoding of a frame's ``data`` (base64,
JSON, ...) is the caller's to decode — that part is product-specific and stays
out of here.
'''

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ServerSentEvent:
    '''One dispatched SSE event. ``event`` defaults to ``"message"`` (the
    EventSource default); ``data`` is the newline-joined data lines.'''

    event: str = 'message'
    data: str = ''
    id: str | None = None
    retry: int | None = None


def parse_sse(text: str) -> list[ServerSentEvent]:
    '''Parse an SSE stream body into a list of :class:`ServerSentEvent`.

    Follows the EventSource algorithm: ``data:`` lines accumulate (joined by
    newlines), a blank line dispatches the buffered event, ``:`` lines are
    comments, and a single leading space after a field's colon is stripped.

    One deliberate leniency for testing: a block carrying only an ``event:``
    (or ``retry:``) and no ``data:`` — e.g. an ``event: done`` end-of-stream
    marker — is still surfaced, where a strict browser EventSource would drop
    it. A trailing block not terminated by a blank line is flushed too, since
    captured bodies often omit the final newline.
    '''
    events: list[ServerSentEvent] = []
    data: list[str] = []
    event_type = ''
    retry: int | None = None
    last_id: str | None = None
    have_fields = False

    def dispatch() -> None:
        nonlocal data, event_type, retry, have_fields
        if have_fields:
            events.append(ServerSentEvent(
                event=event_type or 'message',
                data='\n'.join(data),
                id=last_id, retry=retry))
        data = []
        event_type = ''
        retry = None
        have_fields = False

    for line in text.splitlines():
        if line == '':
            dispatch()
            continue
        if line.startswith(':'):
            continue  # comment line
        field, _, value = line.partition(':')
        if value.startswith(' '):
            value = value[1:]
        if field == 'data':
            data.append(value)
            have_fields = True
        elif field == 'event':
            event_type = value
            have_fields = True
        elif field == 'id':
            if '\x00' not in value:  # ids containing NUL are ignored (spec)
                last_id = value
            have_fields = True
        elif field == 'retry':
            if value.isdigit():
                retry = int(value)
            have_fields = True
        # any other field name is ignored, per spec

    dispatch()  # flush a trailing, un-terminated block
    return events
