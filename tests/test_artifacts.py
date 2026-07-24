'''Unit tests for artifacts-on-failure (M12) — all browser-free: the dump
itself runs against fake pages, and the CDPTestCase wiring is exercised by
running throwaway inner test cases (with the browser setup stubbed out)
through a plain unittest.TestResult.

Runnable three ways: `python -m unittest`, `python tests/test_artifacts.py`,
or `python -m pytest`. Plain asserts: don't run with -O.
'''

import asyncio
import os
import pathlib
import sys
import tempfile
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from purecdp.testing import CDPTestCase, Exchange, dump_artifacts  # noqa: E402
from purecdp.testing.artifacts import (  # noqa: E402
    _clip, _sanitize, artifacts_dir,
)
from purecdp.testing.page import ConsoleMessage  # noqa: E402


class FakeRecorder:
    def __init__(self, exchanges):
        self.exchanges = exchanges


class FakePage:
    '''Duck-typed Page: just the surface dump_artifacts touches.'''

    def __init__(self, *, console=(), js_errors=(), recorders=(),
                 broken=False):
        self.console = list(console)
        self.js_errors = list(js_errors)
        self._recorders = list(recorders)
        self._broken = broken

    async def evaluate(self, expression):
        if self._broken:
            raise RuntimeError('session is dead')
        return 'https://fake.example/app'

    async def title(self):
        return 'Fake App'

    async def screenshot(self, *, full_page=False):
        if self._broken:
            raise RuntimeError('renderer gone')
        return b'\x89PNG\r\n\x1a\n fake-pixels'

    async def content(self):
        if self._broken:
            raise RuntimeError('session is dead')
        return '<html><body>fake</body></html>'


def _read(*parts):
    with open(os.path.join(*parts), encoding='utf-8') as f:
        return f.read()


class HelperTests(unittest.TestCase):
    def test_sanitize(self):
        assert _sanitize('tests.test_x.Case.test_y') == 'tests.test_x.Case.test_y'
        assert _sanitize('a b/c\\d:e') == 'a_b_c_d_e'
        assert _sanitize('') == '_'

    def test_clip(self):
        assert _clip('short') == 'short'
        clipped = _clip('x' * 20, cap=10)
        assert clipped.startswith('x' * 10) and '+10 more chars' in clipped

    def test_artifacts_dir_resolution(self):
        assert artifacts_dir('/explicit') == '/explicit'
        old = os.environ.pop('PURECDP_ARTIFACTS_DIR', None)
        try:
            assert artifacts_dir() == 'purecdp-artifacts'
            os.environ['PURECDP_ARTIFACTS_DIR'] = '/from-env'
            assert artifacts_dir() == '/from-env'
        finally:
            if old is None:
                os.environ.pop('PURECDP_ARTIFACTS_DIR', None)
            else:
                os.environ['PURECDP_ARTIFACTS_DIR'] = old


class DumpArtifactsTests(unittest.TestCase):
    def test_full_dump_two_pages(self):
        pages = [
            FakePage(console=[ConsoleMessage('log', 'hello'),
                              ConsoleMessage('error', 'boom')],
                     js_errors=[ValueError('ouch')],
                     recorders=[FakeRecorder([Exchange(
                         url='https://api.example/q', method='POST',
                         request_body='{"q": 1}', status=200,
                         mime='application/json', body=b'{"ok": true}')])]),
            FakePage(),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'case')
            exc = AssertionError('expected 2, saw 1')
            try:
                raise exc
            except AssertionError:
                asyncio.run(dump_artifacts(pages, dest, test_id='pkg.Case.test_x',
                                           exc=exc))
            names = sorted(os.listdir(dest))
            assert names == ['console.log', 'info.txt', 'js_errors.log',
                             'network.har', 'network.log', 'page-2.html',
                             'page.html', 'screenshot-2.png', 'screenshot.png']
            with open(os.path.join(dest, 'screenshot.png'), 'rb') as f:
                assert f.read().startswith(b'\x89PNG')
            assert '[error] boom' in _read(dest, 'console.log')
            assert 'ouch' in _read(dest, 'js_errors.log')
            net = _read(dest, 'network.log')
            assert 'POST https://api.example/q -> 200' in net
            assert '{"ok": true}' in net and '{"q": 1}' in net
            import json as json_mod
            har = json_mod.loads(_read(dest, 'network.har'))
            (entry,) = har['log']['entries']
            assert entry['request']['url'] == 'https://api.example/q'
            assert entry['response']['content']['text'] == '{"ok": true}'
            info = _read(dest, 'info.txt')
            assert 'test: pkg.Case.test_x' in info
            assert 'outcome: FAIL (AssertionError)' in info
            assert 'expected 2, saw 1' in info          # traceback included
            assert 'https://fake.example/app' in info
            # page 2 had nothing optional: only its always-on files exist
            assert 'console-2.log: empty (not written)' in info

    def test_broken_page_still_yields_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'case')
            asyncio.run(dump_artifacts([FakePage(broken=True)], dest,
                                       test_id='t', exc=ValueError('x')))
            info = _read(dest, 'info.txt')
            assert 'outcome: ERROR (ValueError)' in info
            assert 'unreachable' in info
            assert 'screenshot.png: CAPTURE FAILED' in info
            assert not os.path.exists(os.path.join(dest, 'screenshot.png'))

    def test_string_exc_and_rerun_wipes_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'case')
            os.makedirs(dest)
            with open(os.path.join(dest, 'stale.txt'), 'w') as f:
                f.write('from a previous run')
            asyncio.run(dump_artifacts([], dest, test_id='t',
                                       exc='formatted traceback text'))
            assert sorted(os.listdir(dest)) == ['info.txt']   # stale wiped
            assert 'formatted traceback text' in _read(dest, 'info.txt')


class _BrowserlessCase(CDPTestCase):
    '''CDPTestCase with the browser stubbed out, for wiring tests.'''

    ARTIFACTS_DIR = None       # set per test

    async def asyncSetUp(self):
        self._pages = [FakePage(console=[ConsoleMessage('log', 'wired')])]

    async def check_fails(self):
        assert False, 'deliberate failure'

    async def check_errors(self):
        raise ValueError('deliberate error')

    async def check_passes(self):
        assert True

    async def check_skips(self):
        self.skipTest('deliberate skip')


def _run_one(name, artifacts_dir_):
    case = _BrowserlessCase(name)
    case.ARTIFACTS_DIR = artifacts_dir_
    result = unittest.TestResult()
    case.run(result)
    return case, result


class TestCaseWiringTests(unittest.TestCase):
    def test_failure_dumps(self):
        with tempfile.TemporaryDirectory() as tmp:
            case, result = _run_one('check_fails', tmp)
            assert len(result.failures) == 1                  # still a FAILURE
            assert isinstance(case._artifact_exc, AssertionError)
            (subdir,) = os.listdir(tmp)
            assert 'check_fails' in subdir
            info = _read(tmp, subdir, 'info.txt')
            assert 'outcome: FAIL (AssertionError)' in info
            assert 'deliberate failure' in info
            assert '[log] wired' in _read(tmp, subdir, 'console.log')

    def test_error_dumps_as_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            case, result = _run_one('check_errors', tmp)
            assert len(result.errors) == 1                    # still an ERROR
            info = _read(tmp, os.listdir(tmp)[0], 'info.txt')
            assert 'outcome: ERROR (ValueError)' in info

    def test_pass_and_skip_write_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _case, result = _run_one('check_passes', tmp)
            assert result.wasSuccessful() and os.listdir(tmp) == []
            _case, result = _run_one('check_skips', tmp)
            assert len(result.skipped) == 1 and os.listdir(tmp) == []

    def test_env_kill_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ['PURECDP_NO_ARTIFACTS'] = '1'
            try:
                _case, result = _run_one('check_fails', tmp)
            finally:
                del os.environ['PURECDP_NO_ARTIFACTS']
            assert len(result.failures) == 1 and os.listdir(tmp) == []

    def test_artifacts_class_knob_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = _BrowserlessCase('check_fails')
            case.ARTIFACTS_DIR = tmp
            case.ARTIFACTS = False
            result = unittest.TestResult()
            case.run(result)
            assert len(result.failures) == 1 and os.listdir(tmp) == []


if __name__ == '__main__':
    unittest.main()
