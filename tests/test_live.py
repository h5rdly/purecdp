'''Unit tests for the Live locator's browser-free parts: step building,
refinement immutability, getter -> selector compilation, the should() condition
logic, and that the resolver JS is syntactically valid. The re-resolution and
actions are covered by LiveE2ETests against a real browser.

Runnable three ways: `python -m unittest`, `python tests/test_live.py`, or
`python -m pytest`. Plain asserts: don't run with -O.
'''

import asyncio
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from purecdp.testing import ExpectationError, Live  # noqa: E402
from purecdp.testing.live import (  # noqa: E402
    _LIVE_JS, _CONDITIONS, _css_str, _failing, _role_css,
)


class StepBuildingTests(unittest.TestCase):
    def test_getters_compile_to_steps(self):
        base = Live(None, [])
        assert base.get_by_test_id('cols')._steps == [
            {'kind': 'css', 'sel': '[data-testid="cols"]'}]
        assert base.get_by_text('Go')._steps == [
            {'kind': 'text', 'q': 'Go', 'exact': False}]
        assert base.get_by_text('Go', exact=True)._steps[0]['exact'] is True
        assert base.get_by_label('Min')._steps == [
            {'kind': 'label', 'q': 'Min', 'exact': False}]
        role = base.get_by_role('button', name='Continue')._steps[0]
        assert role['name'] == 'Continue' and 'button' in role['sel']

    def test_refinement_chains_and_is_immutable(self):
        base = Live(None, [{'kind': 'css', 'sel': '.node'}])
        chained = base.containing('x').live('button').last
        assert chained._steps == [
            {'kind': 'css', 'sel': '.node'},
            {'kind': 'filter', 'containing': 'x'},
            {'kind': 'css', 'sel': 'button', 'containing': None},
            {'kind': 'index', 'n': -1},
        ]
        assert base._steps == [{'kind': 'css', 'sel': '.node'}]   # unchanged
        assert base.nth(2)._steps[-1] == {'kind': 'index', 'n': 2}
        assert base.first._steps[-1] == {'kind': 'index', 'n': 0}

    def test_css_string_escaping(self):
        assert _css_str('a"b\\c') == '"a\\"b\\\\c"'

    def test_role_css_known_and_fallback(self):
        assert 'button' in _role_css('button')
        assert _role_css('BUTTON') == _role_css('button')       # case-insensitive
        assert _role_css('nonesuch') == '[role="nonesuch"]'

    def test_repr(self):
        r = repr(Live(None, [{'kind': 'css', 'sel': '.card', 'containing': 'x'},
                             {'kind': 'index', 'n': -1}]))
        assert '.card' in r and 'containing' in r and '[-1]' in r


class ShouldLogicTests(unittest.TestCase):
    PRESENT = {'count': 2, 'present': True, 'text': 'Loading 5 samples',
               'value': '1000', 'visible': True, 'enabled': True, 'checked': False}
    ABSENT = {'count': 0, 'present': False, 'text': None, 'value': None,
              'visible': False, 'enabled': None, 'checked': None}

    def test_present_conditions(self):
        assert _failing({'count': 2}, self.PRESENT) == []
        assert _failing({'count': 3}, self.PRESENT) == ['count']
        assert _failing({'text': 'samples'}, self.PRESENT) == []          # substring, ci
        assert _failing({'text': re.compile(r'\d+ samples')}, self.PRESENT) == []
        assert _failing({'text': 'nope'}, self.PRESENT) == ['text']
        assert _failing({'visible': True, 'enabled': True}, self.PRESENT) == []
        assert _failing({'visible': False}, self.PRESENT) == ['visible']
        assert _failing({'value': '1000'}, self.PRESENT) == []
        assert _failing({'checked': True}, self.PRESENT) == ['checked']
        assert _failing({'checked': False}, self.PRESENT) == []

    def test_absent_element(self):
        assert _failing({'visible': False}, self.ABSENT) == []            # gone => not visible
        assert _failing({'count': 0}, self.ABSENT) == []
        assert _failing({'visible': True}, self.ABSENT) == ['visible']
        assert _failing({'text': 'x'}, self.ABSENT) == ['text']          # absent => text fails

    def test_unknown_condition_rejected(self):
        assert 'attr' not in _CONDITIONS and {'text', 'visible', 'count'} <= _CONDITIONS
        try:
            asyncio.run(Live(None, []).should(attr='x'))
        except ValueError as exc:
            assert 'attr' in str(exc)
        else:
            raise AssertionError('expected ValueError for unknown condition')

    def test_expectation_error_is_assertion_error(self):
        assert issubclass(ExpectationError, AssertionError)


@unittest.skipUnless(shutil.which('node'), 'node not installed')
class ResolverJsTests(unittest.TestCase):
    def test_live_js_is_valid_javascript(self):
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
            f.write('const f = ' + _LIVE_JS + ';\n')
            path = f.name
        try:
            proc = subprocess.run(['node', '--check', path],
                                  capture_output=True, text=True)
            assert proc.returncode == 0, proc.stderr
        finally:
            pathlib.Path(path).unlink()


if __name__ == '__main__':
    unittest.main()
