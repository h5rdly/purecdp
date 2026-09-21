'''Tests for target/session lifecycle (M4): contexts, pages, auto-attach,
crash/detach — driven by the stateful FakeBrowser in tests/support.py.

Runnable three ways: `python -m unittest`, `python tests/test_lifecycle.py`,
or `python -m pytest` — none require pytest. Plain asserts: don't run with -O.
'''

import asyncio
import os
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


@unittest.skipIf(sys.platform == 'win32', 'SingletonLock is POSIX-only')
class ProfileLockTests(unittest.TestCase):
    '''launch(user_data_dir=...) pre-checks: refuse a live-locked profile,
    tolerate stale locks, clear last run's DevToolsActivePort.'''

    def make_profile(self, stack, lock_target=None, stale_port=False):
        import tempfile

        profile = stack.enter_context(tempfile.TemporaryDirectory())
        if lock_target is not None:
            os.symlink(lock_target, os.path.join(profile, 'SingletonLock'))
        if stale_port:
            with open(os.path.join(profile, 'DevToolsActivePort'), 'w') as f:
                f.write('38033\n/devtools/browser/dead-uuid\n')
        return profile

    def test_live_lock_names_the_owning_pid(self):
        from contextlib import ExitStack
        import socket
        from purecdp import BrowserLaunchError
        from purecdp.browser import _prepare_reused_profile

        with ExitStack() as stack:
            profile = self.make_profile(
                stack, lock_target=f'{socket.gethostname()}-{os.getpid()}')
            try:
                _prepare_reused_profile(profile)
            except BrowserLaunchError as exc:
                assert f'in use by PID {os.getpid()}' in str(exc)
                assert 'user_data_dir' in str(exc)
            else:
                raise AssertionError('live lock did not block the launch')

    def test_stale_lock_dead_pid_does_not_block(self):
        # Chromium ignores a lock whose PID is gone; so must we.
        import socket
        import subprocess
        from contextlib import ExitStack
        from purecdp.browser import _prepare_reused_profile

        child = subprocess.Popen([sys.executable, '-c', ''])
        child.wait()  # reaped: the PID is definitely dead
        with ExitStack() as stack:
            profile = self.make_profile(
                stack, lock_target=f'{socket.gethostname()}-{child.pid}',
                stale_port=True)
            _prepare_reused_profile(profile)  # must not raise
            assert not os.path.exists(
                os.path.join(profile, 'DevToolsActivePort'))

    def test_other_host_lock_does_not_block(self):
        # a PID stamped by another machine is meaningless here
        from contextlib import ExitStack
        from purecdp.browser import _prepare_reused_profile

        with ExitStack() as stack:
            profile = self.make_profile(
                stack, lock_target=f'not-this-host.example-{os.getpid()}')
            _prepare_reused_profile(profile)  # must not raise

    def test_stale_devtools_port_file_is_cleared(self):
        # last run's port file would win the race against the new browser
        # rewriting it — the endpoint wait would connect to a dead port
        from contextlib import ExitStack
        from purecdp.browser import _prepare_reused_profile

        with ExitStack() as stack:
            profile = self.make_profile(stack, stale_port=True)
            _prepare_reused_profile(profile)
            assert not os.path.exists(
                os.path.join(profile, 'DevToolsActivePort'))


class StderrTailTests(unittest.IsolatedAsyncioTestCase):
    '''_StderrTail keeps the quotable end of the browser's stderr.'''

    async def test_keeps_only_the_tail(self):
        from purecdp.browser import _StderrTail

        reader = asyncio.StreamReader()
        tail = _StderrTail(reader)
        reader.feed_data(b'x' * 100_000)
        reader.feed_data(b'END: The profile appears to be in use\n')
        reader.feed_eof()
        await tail.wait_drained()
        assert tail.text().endswith('END: The profile appears to be in use')
        assert len(tail.text()) <= 2000
        assert tail.suffix().startswith('; browser stderr: ')
        tail.close()

    async def test_silent_stream_adds_no_noise(self):
        from purecdp.browser import _StderrTail

        reader = asyncio.StreamReader()
        reader.feed_eof()
        tail = _StderrTail(reader)
        await tail.wait_drained()
        assert tail.text() == ''
        assert tail.suffix() == ''
        tail.close()

    async def test_no_stream_is_inert(self):
        from purecdp.browser import _StderrTail

        tail = _StderrTail(None)
        await tail.wait_drained()
        assert tail.suffix() == ''
        tail.close()


@unittest.skipIf(sys.platform == 'win32', 'uses a /bin/sh fake browser')
class LaunchFailureTests(unittest.IsolatedAsyncioTestCase):
    '''A browser that dies at startup must say so — with its own stderr —
    not surface some downstream connect error. No real browser needed.'''

    async def test_early_exit_quotes_child_stderr(self):
        import stat
        import tempfile
        from purecdp import BrowserLaunchError

        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, 'fakebrowser')
            with open(fake, 'w') as f:
                f.write('#!/bin/sh\n'
                        'echo "boom: profile is locked" >&2\n'
                        'exit 21\n')
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
            try:
                await purecdp.launch(browser_path=fake)
            except BrowserLaunchError as exc:
                assert 'exited with code 21' in str(exc)
                assert 'boom: profile is locked' in str(exc)
            else:
                raise AssertionError('fake browser launch succeeded')


class GracefulCloseTests(unittest.IsolatedAsyncioTestCase):
    '''aclose() asks the browser to exit (Browser.close over CDP — reaches
    the real browser whatever PID it re-exec'd to) before any signal.'''

    def make(self):
        fake = FakeBrowser()
        transport = FakeTransport(fake)
        return fake, Connection(transport)

    async def spawn_child(self, code):
        return await asyncio.create_subprocess_exec(
            sys.executable, '-c', code, stdin=asyncio.subprocess.PIPE)

    async def test_browser_close_sent_and_no_signal_needed(self):
        # the "browser": exits by itself when Browser.close arrives
        child = await self.spawn_child('import sys; sys.stdin.read()')
        fake, conn = self.make()
        fake.on_browser_close = lambda: child.stdin.close()
        await conn.open()
        browser = purecdp.Browser(child, conn, '', False)
        await browser.aclose()
        assert fake.browser_close_requests == 1
        assert child.returncode == 0  # clean exit, no signal involved

    async def test_hung_browser_still_gets_signalled(self):
        # replies ok to Browser.close but never exits → fallback ladder
        child = await self.spawn_child('import time; time.sleep(60)')
        fake, conn = self.make()
        await conn.open()
        browser = purecdp.Browser(child, conn, '', False)
        browser.graceful_close_timeout = 0.2  # keep the grace wait short
        await browser.aclose()
        assert fake.browser_close_requests == 1
        assert child.returncode != 0  # SIGTERM'd (-15 POSIX / 1 Windows)

    async def test_connected_browser_is_never_closed(self):
        # process=None = connect()ed: a browser a human may be using —
        # aclose() must only detach, never send Browser.close
        fake, conn = self.make()
        await conn.open()
        browser = purecdp.Browser(None, conn, '', False)
        await browser.aclose()
        assert fake.browser_close_requests == 0


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        fake = FakeBrowser()
        transport = FakeTransport(fake)
        return fake, transport, Connection(transport)

    async def test_new_session_attaches_with_target_info(self):
        fake, transport, conn = self.make()
        async with conn:
            session = await purecdp.new_session(conn, 'about:blank')
            await drain()
            assert session.target_id == 'T-1'
            assert session.target_info is not None
            assert str(session.target_info.url) == 'about:blank'
            await session.execute(page.enable())  # session is usable

    async def test_unrealized_target_raises_instead_of_hanging(self):
        # Vivaldi-style: createTarget hands out an id but the tab never comes
        # to exist (URL stays empty, no renderer) — new_session must raise,
        # not return a session whose every command hangs forever
        fake, transport, conn = self.make()
        fake.unrealized_targets = True
        fake.product = 'Vivaldi/7.7.3851.50'
        async with conn:
            try:
                await purecdp.new_session(conn, 'https://example.org/',
                                          realize_timeout=0.2)
            except purecdp.TargetNotRealized as exc:
                assert 'Vivaldi/7.7.3851.50' in str(exc)  # names the culprit
                assert 'browser.attach()' in str(exc)     # says the way out
            else:
                raise AssertionError('expected TargetNotRealized')
            await drain()
            # the zombie was closed, not left to trap the next attach()
            assert not fake.targets

    async def test_realize_check_can_be_skipped(self):
        fake, transport, conn = self.make()
        fake.unrealized_targets = True
        async with conn:
            session = await purecdp.new_session(conn, 'about:blank',
                                                realize_timeout=None)
            assert session.target_id == 'T-1'

    async def test_browser_realize_timeout_forwards(self):
        fake, transport, conn = self.make()
        fake.unrealized_targets = True
        async with conn:
            browser = purecdp.Browser(None, conn, '', False)
            browser.realize_timeout = 0.1
            try:
                await browser.new_session()
            except purecdp.TargetNotRealized:
                pass
            else:
                raise AssertionError('expected TargetNotRealized')

    async def test_attach_is_idempotent_per_target(self):
        # a second CDP attach would create a second session that duplicates
        # every event (the auto-attach + explicit-attach combo); attach()
        # must hand back the session it already holds instead.
        fake, transport, conn = self.make()
        async with conn:
            session = await purecdp.new_session(conn, 'about:blank')
            attaches = sum(1 for m in transport.sent
                           if m['method'] == 'Target.attachToTarget')
            again = await conn.attach(session.target_id)
            assert again is session
            assert sum(1 for m in transport.sent
                       if m['method'] == 'Target.attachToTarget') == attaches
            # a CLOSED session must not satisfy the dedupe
            await purecdp.close_page(conn, session)
            await drain()
            assert session.closed

    async def test_browser_close_target_by_id(self):
        # Browser.close_target closes by bare id — no Session object needed,
        # the connect()-to-a-running-browser flow where the id came from
        # discovery.list_targets()
        from purecdp.browser import Browser
        fake, transport, conn = self.make()
        async with conn:
            session = await purecdp.new_session(conn, 'about:blank')
            browser = Browser(None, conn, '', False)
            ok = await browser.close_target(session.target_id)
            await drain()
            assert ok is True
            assert 'T-1' not in fake.targets
            assert session.closed

    async def test_close_page_ends_session_keeps_connection(self):
        fake, transport, conn = self.make()
        async with conn:
            session = await purecdp.new_session(conn, 'about:blank')
            await purecdp.close_page(conn, session)
            await drain()
            assert session.closed
            assert 'T-1' not in fake.targets
            assert not conn.closed
            # connection still fully usable
            session2 = await purecdp.new_session(conn, 'about:blank')
            assert session2.target_id == 'T-2'

    async def test_context_pages_and_dispose(self):
        fake, transport, conn = self.make()
        async with conn:
            context = await purecdp.new_context(conn)
            assert context.context_id == 'CTX-1'
            p1 = await context.new_session('about:blank')
            p2 = await context.new_session('about:blank')
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
            session = await purecdp.new_session(conn, 'about:blank')
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
            session = await purecdp.new_session(conn, 'about:blank')
            await drain()
            transport.push(fake.info_changed(session.target_id, 'https://after'))
            await drain()
            assert str(session.target_info.url) == 'https://after'

    async def test_detach_leaves_target_running(self):
        fake, transport, conn = self.make()
        async with conn:
            session = await purecdp.new_session(conn, 'about:blank')
            await session.detach()
            await drain()
            assert session.closed
            assert session.target_id in fake.targets  # target still alive


if __name__ == '__main__':
    unittest.main()
