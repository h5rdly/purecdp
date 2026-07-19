'''Unit tests for the SSE body parser — pure, no browser, stdlib only.

Runnable three ways: `python -m unittest`, `python tests/test_sse.py`, or
`python -m pytest`. Plain asserts: don't run with -O.
'''

import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from purecdp.testing import ServerSentEvent, parse_sse  # noqa: E402


class ParseSSETests(unittest.TestCase):
    def test_simple_message_defaults_to_message_event(self):
        events = parse_sse('data: hello\n\n')
        assert events == [ServerSentEvent(event='message', data='hello')]

    def test_named_event_and_id(self):
        body = 'event: tick\nid: 7\ndata: payload\n\n'
        (event,) = parse_sse(body)
        assert event.event == 'tick'
        assert event.id == '7'
        assert event.data == 'payload'

    def test_multiline_data_is_newline_joined(self):
        body = 'data: line one\ndata: line two\n\n'
        (event,) = parse_sse(body)
        assert event.data == 'line one\nline two'

    def test_leading_space_after_colon_stripped_once(self):
        # only the first space is removed, so a second one is preserved
        (event,) = parse_sse('data:  two-leading-spaces\n\n')
        assert event.data == ' two-leading-spaces'

    def test_field_with_no_value(self):
        # "data" with no colon means an empty data line
        (event,) = parse_sse('data\n\n')
        assert event.data == ''

    def test_comments_and_blank_dispatch(self):
        body = ': this is a comment\ndata: real\n\n'
        (event,) = parse_sse(body)
        assert event.data == 'real'

    def test_multiple_events(self):
        body = 'data: a\n\ndata: b\n\ndata: c\n\n'
        assert [e.data for e in parse_sse(body)] == ['a', 'b', 'c']

    def test_retry_parsed_as_int(self):
        (event,) = parse_sse('retry: 3000\ndata: x\n\n')
        assert event.retry == 3000

    def test_event_only_marker_is_surfaced(self):
        # an `event: done` end marker with no data still comes through (leniency)
        (event,) = parse_sse('event: done\n\n')
        assert event.event == 'done'
        assert event.data == ''

    def test_trailing_block_without_blank_line_is_flushed(self):
        (event,) = parse_sse('data: no terminator')
        assert event.data == 'no terminator'

    def test_crlf_line_endings(self):
        body = 'event: tick\r\ndata: crlf\r\n\r\n'
        (event,) = parse_sse(body)
        assert event.event == 'tick'
        assert event.data == 'crlf'

    def test_empty_body_yields_nothing(self):
        assert parse_sse('') == []
        assert parse_sse('\n\n\n') == []


if __name__ == '__main__':
    unittest.main()
