'''Tests for the generated bindings (purecdp.protocol) — no browser required.

Runnable three ways: `python -m unittest`, `python tests/test_protocol.py`,
or `python -m pytest` — none require pytest. Plain asserts: don't run with -O.
'''

import importlib
import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import purecdp.protocol as proto  # noqa: E402
from purecdp._shared import (  # noqa: E402
    _EVENT_CLASSES,
    T_JSON_DICT,
    UnknownEvent,
    parse_event,
)


def drive(gen, response: T_JSON_DICT):
    '''Drive a command generator: return (request_dict, parsed_result).'''
    request = gen.send(None)
    try:
        gen.send(response)
    except StopIteration as exc:
        return request, exc.value
    raise AssertionError('command generator yielded more than once')


class ImportTests(unittest.TestCase):
    def test_import_all_domains(self):
        proto.load_all()
        assert len(proto.__all__) == 58
        for name in proto.__all__:
            with self.subTest(domain=name):
                assert importlib.import_module(f'purecdp.protocol.{name}')

    def test_lazy_getattr(self):
        assert proto.page.__name__ == 'purecdp.protocol.page'
        try:
            proto.no_such_domain
        except AttributeError:
            pass
        else:
            raise AssertionError('expected AttributeError for unknown domain')


class TypeKindTests(unittest.TestCase):
    def test_scalar_alias(self):
        from purecdp.protocol import page

        fid = page.FrameId.from_json('F1')
        assert isinstance(fid, str)
        assert fid == 'F1'
        assert fid.to_json() == 'F1'
        assert repr(fid) == "FrameId('F1')"

    def test_enum(self):
        from purecdp.protocol import network

        rt = network.ResourceType.from_json('Document')
        assert rt is network.ResourceType.DOCUMENT
        assert rt.to_json() == 'Document'
        try:
            network.ResourceType.from_json('NotAThing')
        except ValueError:
            pass
        else:
            raise AssertionError('expected ValueError for unknown enum value')

    def test_dict_subclass(self):
        from purecdp.protocol import network

        h = network.Headers.from_json({'Accept': '*/*'})
        assert isinstance(h, dict)
        assert h.to_json() == {'Accept': '*/*'}

    def test_named_array(self):
        from purecdp.protocol import dom

        q = dom.Quad.from_json([1, 2, 3, 4.5])
        assert isinstance(q, dom.Quad)
        assert q.to_json() == [1.0, 2.0, 3.0, 4.5]
        assert all(isinstance(v, float) for v in q.to_json())

    def test_self_referential_dataclass_roundtrip(self):
        from purecdp.protocol import dom

        node_json = {
            'nodeId': 1, 'backendNodeId': 2, 'nodeType': 1, 'nodeName': 'HTML',
            'localName': 'html', 'nodeValue': '',
            'children': [{'nodeId': 3, 'backendNodeId': 4, 'nodeType': 3,
                          'nodeName': '#text', 'localName': '', 'nodeValue': 'hi'}],
        }
        n = dom.Node.from_json(node_json)
        assert isinstance(n.children[0], dom.Node)
        assert n.children[0].node_value == 'hi'
        assert n.to_json() == node_json

    def test_optional_null_treated_as_absent(self):
        from purecdp.protocol import page

        f = page.Frame.from_json({
            'id': 'F1', 'loaderId': 'L1', 'url': 'https://x', 'domainAndRegistry': 'x',
            'securityOrigin': 'https://x', 'mimeType': 'text/html',
            'secureContextType': 'Secure', 'crossOriginIsolatedContextType': 'Isolated',
            'gatedAPIFeatures': [], 'parentId': None,
        })
        assert f.parent_id is None
        assert 'parentId' not in f.to_json()


class CommandTests(unittest.TestCase):
    def test_no_params_no_returns(self):
        from purecdp.protocol import dom

        request, result = drive(dom.disable(), {})
        assert request == {'method': 'DOM.disable'}
        assert result is None

    def test_all_optional_params_omitted(self):
        from purecdp.protocol import page

        request, result = drive(page.enable(), {})
        assert request == {'method': 'Page.enable', 'params': {}}
        assert result is None

    def test_params_serialization_and_optionals(self):
        from purecdp.protocol import runtime

        request, result = drive(
            runtime.evaluate(expression='1+1', return_by_value=True),
            {'result': {'type': 'number', 'value': 2}},
        )
        assert request['method'] == 'Runtime.evaluate'
        assert request['params'] == {'expression': '1+1', 'returnByValue': True}
        obj, exception_details = result
        assert isinstance(obj, runtime.RemoteObject)
        assert obj.value == 2
        assert exception_details is None

    def test_typed_param_to_json(self):
        from purecdp.protocol import target

        request, result = drive(
            target.activate_target(target_id=target.TargetID('T1')), {}
        )
        assert request == {'method': 'Target.activateTarget', 'params': {'targetId': 'T1'}}
        assert result is None

    def test_single_return(self):
        from purecdp.protocol import target

        _, result = drive(target.create_target(url='about:blank'), {'targetId': 'T9'})
        assert isinstance(result, target.TargetID)
        assert result == 'T9'

    def test_tuple_return(self):
        from purecdp.protocol import network

        _, result = drive(
            network.get_response_body(request_id=network.RequestId('R1')),
            {'body': '<html>', 'base64Encoded': False},
        )
        assert result == ('<html>', False)

    def test_tuple_return_with_absent_optionals(self):
        from purecdp.protocol import page

        _, result = drive(
            page.navigate(url='https://example.com'),
            {'frameId': 'F1'},
        )
        assert result[0] == page.FrameId('F1')
        assert all(item is None for item in result[1:])


class EventTests(unittest.TestCase):
    def test_registry_and_parse(self):
        from purecdp.protocol import page

        ev = parse_event('Page.loadEventFired', {'timestamp': 100.5})
        assert isinstance(ev, page.LoadEventFired)
        assert ev.timestamp == 100.5
        assert ev.EVENT_METHOD == 'Page.loadEventFired'

    def test_cross_domain_event_field(self):
        from purecdp.protocol import page

        ev = parse_event(
            'Page.frameRequestedNavigation',
            {'frameId': 'F1', 'reason': 'httpHeaderRefresh',
             'url': 'https://x', 'disposition': 'currentTab'},
        )
        assert isinstance(ev, page.FrameRequestedNavigation)
        assert isinstance(ev.frame_id, page.FrameId)

    def test_unknown_event_fallback(self):
        ev = parse_event('Bogus.notReal', {'x': 1})
        assert isinstance(ev, UnknownEvent)
        assert ev.method == 'Bogus.notReal'
        assert ev.params == {'x': 1}

    def test_every_registered_event_has_from_json(self):
        proto.load_all()
        assert len(_EVENT_CLASSES) > 200
        for method, cls in _EVENT_CLASSES.items():
            with self.subTest(event=method):
                assert callable(cls.from_json)


if __name__ == '__main__':
    unittest.main()
