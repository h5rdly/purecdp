'''Tests for the JSON backend shim (purecdp._json).

Runs the same assertions whichever backend is active, so it passes with or
without orjson installed. Plain asserts: don't run with -O.
'''

import json as _stdlib
import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from purecdp import _json  # noqa: E402


class JsonShimTests(unittest.TestCase):
    def test_dumps_returns_compact_str(self):
        wire = _json.dumps({'id': 1, 'method': 'Page.enable', 'params': {}})
        assert isinstance(wire, str)
        assert ' ' not in wire  # compact: no separator spaces
        assert _stdlib.loads(wire) == {'id': 1, 'method': 'Page.enable',
                                       'params': {}}

    def test_round_trip_including_types(self):
        obj = {'i': 42, 'f': 1.5, 'b': True, 'n': None, 's': 'x',
               'list': [1, 2, {'k': 'v'}]}
        assert _json.loads(_json.dumps(obj)) == obj

    def test_unicode_round_trips(self):
        obj = {'expression': "'ünïcødé — 日本語'"}
        assert _json.loads(_json.dumps(obj)) == obj

    def test_loads_accepts_str_and_bytes(self):
        assert _json.loads('{"a":1}') == {'a': 1}
        assert _json.loads(b'{"a":1}') == {'a': 1}

    def test_backend_reported(self):
        assert _json.BACKEND in ('orjson', 'json')
        assert _json.BACKEND == ('orjson' if _json.HAVE_ORJSON else 'json')

    def test_parity_with_stdlib(self):
        # whatever the backend, output must parse to the same object stdlib does
        for obj in ({'a': 1}, [1, 'two', 3.0, None], {'nested': {'x': [True]}}):
            with self.subTest(obj=obj):
                assert _stdlib.loads(_json.dumps(obj)) == \
                    _stdlib.loads(_stdlib.dumps(obj))


if __name__ == '__main__':
    unittest.main()
