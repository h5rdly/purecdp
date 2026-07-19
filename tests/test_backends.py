'''Tests for the optional-accelerator shims (_b64 base64, _loop event loop).

Pass with or without pybase64 / uvloop installed — they assert behavior, not
which backend is active. Plain asserts: don't run with -O.
'''

import asyncio
import base64 as _stdlib_b64
import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import purecdp  # noqa: E402
from purecdp import _b64, _loop  # noqa: E402


class Base64ShimTests(unittest.TestCase):
    def test_round_trip_matches_stdlib(self):
        for blob in (b'', b'hi', bytes(range(256)) * 8):
            with self.subTest(n=len(blob)):
                enc = _b64.b64encode(blob)
                assert enc == _stdlib_b64.b64encode(blob)
                assert _b64.b64decode(enc) == blob

    def test_decodes_stdlib_encoded(self):
        # what Chrome sends: standard base64; our decode must accept it
        payload = _stdlib_b64.b64encode(b'\x89PNG\r\n\x1a\n' + bytes(500))
        assert _b64.b64decode(payload).startswith(b'\x89PNG')

    def test_backend_reported(self):
        assert _b64.BACKEND in ('pybase64', 'base64')
        assert _b64.BACKEND == ('pybase64' if _b64.HAVE_PYBASE64 else 'base64')


class LoopShimTests(unittest.TestCase):
    def test_new_event_loop_is_usable(self):
        loop = _loop.new_event_loop()
        try:
            assert loop.run_until_complete(_answer()) == 42
        finally:
            loop.close()

    def test_run_executes_coroutine(self):
        assert purecdp.run(_answer()) == 42

    def test_run_cleans_up_like_asyncio_run(self):
        # a spawned task should be cancelled on exit, not leak (asyncio.Runner)
        async def spawn_and_return():
            asyncio.create_task(asyncio.sleep(100))
            return 'ok'
        assert purecdp.run(spawn_and_return()) == 'ok'

    def test_backend_reported(self):
        assert (_loop.FAST_LOOP) == (_loop.new_event_loop is not
                                     asyncio.new_event_loop)


async def _answer() -> int:
    await asyncio.sleep(0)
    return 42


if __name__ == '__main__':
    unittest.main()
