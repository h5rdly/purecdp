'''Tests for target/session lifecycle (M4): contexts, pages, auto-attach,
crash/detach — driven by the stateful FakeBrowser in tests/support.py.

Runnable three ways: `python -m unittest`, `python tests/test_lifecycle.py`,
or `python -m pytest` — none require pytest. Plain asserts: don't run with -O.
'''

import asyncio
import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

try:
    from .support import FakeBrowser, FakeTransport, drain
except ImportError:
    from support import FakeBrowser, FakeTransport, drain

import purecdp  # noqa: E402
from purecdp import CDPSessionClosed, Connection  # noqa: E402
from purecdp.protocol import page, target  # noqa: E402


class PrefsSeedingTests(unittest.TestCase):
    '''launch(prefs=...) seeds <profile>/Default/Preferences before start.'''

    def test_dotted_and_nested_merge(self):
        import json
        import tempfile
        from purecdp.browser import _write_prefs

        with tempfile.TemporaryDirectory() as profile:
            _write_prefs(profile, {
                'credentials_enable_service': False,
                'profile.password_manager_enabled': False,
                'profile.default_content_setting_values.images': 2,
            })
            path = pathlib.Path(profile, 'Default', 'Preferences')
            data = json.loads(path.read_text(encoding='latin1'))
            assert data['credentials_enable_service'] is False
            # dotted keys expand into a nested tree, siblings coexist
            assert data['profile']['password_manager_enabled'] is False
            assert data['profile']['default_content_setting_values']['images'] == 2

    def test_merges_into_existing_preferences(self):
        import json
        import tempfile
        from purecdp.browser import _write_prefs

        with tempfile.TemporaryDirectory() as profile:
            default = pathlib.Path(profile, 'Default')
            default.mkdir()
            (default / 'Preferences').write_text(
                json.dumps({'profile': {'exit_type': 'Normal', 'keep': 1}}),
                encoding='latin1')
            _write_prefs(profile, {'profile.password_manager_enabled': False})
            data = json.loads((default / 'Preferences').read_text('latin1'))
            assert data['profile']['keep'] == 1                    # preserved
            assert data['profile']['password_manager_enabled'] is False  # added


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        fake = FakeBrowser()
        transport = FakeTransport(fake)
        return fake, transport, Connection(transport)

    async def test_new_page_attaches_with_target_info(self):
        fake, transport, conn = self.make()
        async with conn:
            session = await purecdp.new_page(conn, 'about:blank')
            await drain()
            assert session.target_id == 'T-1'
            assert session.target_info is not None
            assert str(session.target_info.url) == 'about:blank'
            await session.execute(page.enable())  # session is usable

    async def test_close_page_ends_session_keeps_connection(self):
        fake, transport, conn = self.make()
        async with conn:
            session = await purecdp.new_page(conn, 'about:blank')
            await purecdp.close_page(conn, session)
            await drain()
            assert session.closed
            assert 'T-1' not in fake.targets
            assert not conn.closed
            # connection still fully usable
            session2 = await purecdp.new_page(conn, 'about:blank')
            assert session2.target_id == 'T-2'

    async def test_context_pages_and_dispose(self):
        fake, transport, conn = self.make()
        async with conn:
            context = await purecdp.new_context(conn)
            assert context.context_id == 'CTX-1'
            p1 = await context.new_page('about:blank')
            p2 = await context.new_page('about:blank')
            assert fake.targets[p1.target_id]['ctx'] == 'CTX-1'
            assert fake.targets[p2.target_id]['ctx'] == 'CTX-1'
            await context.aclose()
            await drain()
            assert p1.closed and p2.closed
            assert 'CTX-1' not in fake.contexts
            assert not fake.targets

    async def test_auto_attach_creates_and_resumes_sessions(self):
        fake, transport, conn = self.make()
        async with conn:
            await conn.set_auto_attach(wait_for_debugger=True)
            sent = transport.sent[-1]
            assert sent['method'] == 'Target.setAutoAttach'
            assert sent['params'] == {'autoAttach': True,
                                      'waitForDebuggerOnStart': True,
                                      'flatten': True}
            # create a target withOUT manually attaching
            tid = await conn.execute(target.create_target(url='https://x'))
            await drain()
            session = conn.sessions[f'SESS-{tid}']
            assert session.target_id == str(tid)
            assert str(session.target_info.url) == 'https://x'
            # the paused target was resumed automatically
            await drain()
            assert fake.resumed == [f'SESS-{tid}']

    async def test_auto_resume_can_be_disabled(self):
        fake, transport, conn = self.make()
        async with conn:
            conn.resume_waiting_targets = False
            await conn.set_auto_attach(wait_for_debugger=True)
            await conn.execute(target.create_target(url='https://x'))
            await drain()
            assert fake.resumed == []

    async def test_session_hook_fires_on_every_attach(self):
        # the hook that propagates auto-attach into nested OOPIFs (M10 Phase 4)
        fake, transport, conn = self.make()
        async with conn:
            seen = []

            async def hook(session):
                seen.append(str(session.target_id))

            remove = conn.add_session_hook(hook)
            await conn.set_auto_attach(wait_for_debugger=False)
            tid = await conn.execute(target.create_target(url='https://x'))
            await drain()
            assert seen == [str(tid)]
            remove()  # unregister stops further calls
            await conn.execute(target.create_target(url='https://y'))
            await drain()
            assert seen == [str(tid)]

    async def test_target_crash_closes_session_with_error(self):
        fake, transport, conn = self.make()
        async with conn:
            session = await purecdp.new_page(conn, 'about:blank')
            stream = session.listen()
            transport.push(fake.crash(session.target_id))
            await drain()
            assert session.closed
            try:
                await asyncio.wait_for(anext(stream), timeout=2)
            except CDPSessionClosed:
                pass
            else:
                raise AssertionError('expected CDPSessionClosed from stream')
            try:
                await session.execute(page.enable())
            except CDPSessionClosed:
                pass
            else:
                raise AssertionError('expected CDPSessionClosed from execute')
            assert not conn.closed

    async def test_target_info_changed_updates_session(self):
        fake, transport, conn = self.make()
        async with conn:
            session = await purecdp.new_page(conn, 'about:blank')
            await drain()
            transport.push(fake.info_changed(session.target_id, 'https://after'))
            await drain()
            assert str(session.target_info.url) == 'https://after'

    async def test_detach_leaves_target_running(self):
        fake, transport, conn = self.make()
        async with conn:
            session = await purecdp.new_page(conn, 'about:blank')
            await session.detach()
            await drain()
            assert session.closed
            assert session.target_id in fake.targets  # target still alive


if __name__ == '__main__':
    unittest.main()
