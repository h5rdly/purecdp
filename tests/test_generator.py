'''Unit tests for the code generator (purecdp._generator) itself.

Runnable three ways: `python -m unittest`, `python tests/test_generator.py`,
or `python -m pytest` — none require pytest. Plain asserts: don't run with -O.
'''

import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from purecdp._generator import emit, model  # noqa: E402

SPEC_DIR = pathlib.Path(__file__).resolve().parents[1] / 'spec'

_spec: model.Spec | None = None


def get_spec() -> model.Spec:
    '''Load the vendored spec once per test run.'''
    global _spec
    if _spec is None:
        _spec = model.load(
            [SPEC_DIR / 'browser_protocol.json', SPEC_DIR / 'js_protocol.json']
        )
    return _spec


class NamingTests(unittest.TestCase):
    def test_snake(self):
        cases = [
            ('Page', 'page'),
            ('DOM', 'dom'),
            ('DOMDebugger', 'dom_debugger'),
            ('IndexedDB', 'indexed_db'),
            ('CacheStorage', 'cache_storage'),
            ('frameId', 'frame_id'),
            ('nodeIds', 'node_ids'),
            ('HeadlessExperimental', 'headless_experimental'),
        ]
        for camel, snaked in cases:
            with self.subTest(camel=camel):
                assert emit.snake(camel) == snaked

    def test_enum_member(self):
        cases = [
            ('blockable', 'BLOCKABLE'),
            ('optionally-blockable', 'OPTIONALLY_BLOCKABLE'),
            ('cellular2g', 'CELLULAR2G'),
            ('camelCaseValue', 'CAMEL_CASE_VALUE'),
            ('text/css', 'TEXT_CSS'),
        ]
        for value, member in cases:
            with self.subTest(value=value):
                assert emit.enum_member(value) == member


class SpecModelTests(unittest.TestCase):
    def test_spec_loads_all_domains(self):
        spec = get_spec()
        assert len(spec.domains) == 58
        assert spec.version == '1.3'
        page = spec.domain('Page')
        assert any(c.name == 'navigate' for c in page.commands)
        assert any(e.name == 'loadEventFired' for e in page.events)


class EmitTests(unittest.TestCase):
    def test_emit_all_produces_module_per_domain(self):
        spec = get_spec()
        files = emit.emit_all(spec, pin='test')
        assert len(files) == len(spec.domains) + 1  # + __init__.py
        assert 'page.py' in files
        assert 'dom_debugger.py' in files
        # every emitted file must at least be valid Python
        for name, source in files.items():
            with self.subTest(module=name):
                compile(source, name, 'exec')

    def test_cross_domain_refs_become_imports(self):
        emitter = emit.DomainEmitter(get_spec().domain('Page'), get_spec(), pin='test')
        assert 'network' in emitter.imports
        assert 'network.LoaderId' in emitter.emit_module()

    def test_reserved_names_get_suffixed(self):
        emitter = emit.DomainEmitter(get_spec().domain('Page'), get_spec(), pin='test')
        assert emitter.safe('json') == 'json_'
        assert emitter.safe('class') == 'class_'
        # sibling imports are reserved too
        assert emitter.safe('network') == 'network_'

    def test_generated_tree_is_current(self):
        '''src/purecdp/protocol must match a fresh emit (catches stale checked-in code).

        Uses the real PIN so the header lines compare equal too.
        '''
        import purecdp.protocol

        out_dir = pathlib.Path(purecdp.protocol.__file__).parent
        pin = (SPEC_DIR / 'PIN').read_text().split()[0][:12]
        files = emit.emit_all(get_spec(), pin=pin)
        on_disk = {p.name for p in out_dir.glob('*.py')}
        assert on_disk == set(files)
        for name, source in files.items():
            with self.subTest(module=name):
                assert (out_dir / name).read_text() == source, (
                    f'{name} is stale — rerun the generator'
                )


if __name__ == '__main__':
    unittest.main()
