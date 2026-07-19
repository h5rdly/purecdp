'''Tests for the sans-I/O engine (purecdp.engine) — pure, no asyncio, no browser.

Runnable three ways: `python -m unittest`, `python tests/test_engine.py`,
or `python -m pytest` — none require pytest. Plain asserts: don't run with -O.
'''

import json
import pathlib
import sys
import unittest
import warnings

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from purecdp._shared import UnknownEvent  # noqa: E402
from purecdp.engine import (  # noqa: E402
    CommandFailure,
    CommandResult,
    Engine,
    EventReceived,
)
from purecdp.errors import CDPError, CDPProtocolError, ProtocolDriftWarning  # noqa: E402
from purecdp.protocol import page  # noqa: E402


class StartCommandTests(unittest.TestCase):
    def test_ids_increment_and_wire_format(self):
        engine = Engine()
        cmd_id1, wire1 = engine.start_command(page.enable())
        cmd_id2, wire2 = engine.start_command(page.navigate(url='https://x'))
        assert (cmd_id1, cmd_id2) == (1, 2)
        msg1, msg2 = json.loads(wire1), json.loads(wire2)
        assert msg1 == {'id': 1, 'method': 'Page.enable', 'params': {}}
        assert msg2 == {'id': 2, 'method': 'Page.navigate',
                        'params': {'url': 'https://x'}}
        assert engine.pending_count == 2

    def test_session_id_attached_to_request(self):
        engine = Engine()
        _, wire = engine.start_command(page.enable(), session_id='SESS1')
        assert json.loads(wire)['sessionId'] == 'SESS1'


class ReceiveResponseTests(unittest.TestCase):
    def test_result_is_typed_and_pending_cleared(self):
        engine = Engine()
        cmd_id, _ = engine.start_command(page.navigate(url='https://x'),
                                         session_id='SESS1')
        happening = engine.receive(json.dumps(
            {'id': cmd_id, 'sessionId': 'SESS1', 'result': {'frameId': 'F1'}}))
        assert isinstance(happening, CommandResult)
        assert happening.session_id == 'SESS1'
        assert happening.value[0] == page.FrameId('F1')
        assert engine.pending_count == 0

    def test_browser_error_becomes_command_failure(self):
        engine = Engine()
        cmd_id, _ = engine.start_command(page.enable())
        happening = engine.receive(json.dumps({
            'id': cmd_id,
            'error': {'code': -32601, 'message': "'Page.enable' wasn't found",
                      'data': 'detail'},
        }))
        assert isinstance(happening, CommandFailure)
        assert isinstance(happening.error, CDPError)
        assert happening.error.code == -32601
        assert "wasn't found" in happening.error.message
        assert happening.error.data == 'detail'
        assert engine.pending_count == 0

    def test_result_parse_error_contained_in_failure(self):
        engine = Engine()
        cmd_id, _ = engine.start_command(page.navigate(url='https://x'))
        # navigate requires 'frameId' in the result; an empty result is drift
        happening = engine.receive(json.dumps({'id': cmd_id, 'result': {}}))
        assert isinstance(happening, CommandFailure)
        assert isinstance(happening.error, KeyError)

    def test_unknown_command_id_is_protocol_error(self):
        engine = Engine()
        try:
            engine.receive(json.dumps({'id': 99, 'result': {}}))
        except CDPProtocolError:
            pass
        else:
            raise AssertionError('expected CDPProtocolError')


class ReceiveEventTests(unittest.TestCase):
    def test_typed_event_with_session_id(self):
        engine = Engine()
        happening = engine.receive(json.dumps({
            'method': 'Page.loadEventFired', 'sessionId': 'SESS1',
            'params': {'timestamp': 1.5},
        }))
        assert isinstance(happening, EventReceived)
        assert happening.session_id == 'SESS1'
        assert isinstance(happening.event, page.LoadEventFired)
        assert happening.event.timestamp == 1.5

    def test_browser_level_event_has_no_session(self):
        happening = Engine().receive(json.dumps({
            'method': 'Target.targetCrashed',
            'params': {'targetId': 'T1', 'status': 'crashed', 'errorCode': 1},
        }))
        assert happening.session_id is None
        assert happening.event.EVENT_METHOD == 'Target.targetCrashed'

    def test_domain_module_imported_on_demand(self):
        # ensure the module and its memo entry are cleared, then parsing the
        # event (lazily, on .event) imports the domain on demand
        import purecdp.engine as engine_mod
        engine_mod._DOMAIN_KNOWN.pop('Inspector', None)
        sys.modules.pop('purecdp.protocol.inspector', None)
        happening = Engine().receive(json.dumps(
            {'method': 'Inspector.targetCrashed', 'params': {}}))
        assert not isinstance(happening.event, UnknownEvent)  # triggers import
        assert 'purecdp.protocol.inspector' in sys.modules

    def test_unknown_domain_event_degrades_silently(self):
        happening = Engine().receive(json.dumps(
            {'method': 'FutureDomain.newThing', 'params': {'x': 1}}))
        assert isinstance(happening.event, UnknownEvent)
        assert happening.event.params == {'x': 1}

    def test_drifted_event_payload_warns_and_degrades(self):
        # Page.loadEventFired requires 'timestamp'. Parsing is lazy, so the
        # warning fires when .event is first accessed, not on receive().
        happening = Engine().receive(json.dumps(
            {'method': 'Page.loadEventFired', 'params': {}}))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            assert isinstance(happening.event, UnknownEvent)
        assert any(issubclass(w.category, ProtocolDriftWarning) for w in caught)


class MalformedInputTests(unittest.TestCase):
    def test_rejects_garbage(self):
        cases = [
            'not json at all',
            '"a bare string"',
            '[1, 2, 3]',
            json.dumps({'neither': 'response nor event'}),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                try:
                    Engine().receive(raw)
                except CDPProtocolError:
                    pass
                else:
                    raise AssertionError(f'expected CDPProtocolError for {raw!r}')


if __name__ == '__main__':
    unittest.main()
