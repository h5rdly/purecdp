'''Layer 3: find, launch, and clean up a debuggable browser.

The launcher runs the browser with an ephemeral debug port (or the pipe),
connects, and guarantees teardown — kill the process, remove the temporary
profile — as an async context manager. Anything Chromium-based works
(Chromium, Chrome, Brave, Edge, headless-shell).
'''

from __future__ import annotations

import asyncio
import collections
import json
import os
import shutil
import socket
import tempfile
import typing
from contextlib import suppress

from urllib.parse import urlsplit

from .connection import Connection
from .discovery import get_version
from .errors import BrowserLaunchError, TargetNotFound
from .transport.pipe import spawn_pipe_process
from .transport.websocket import WebSocketTransport

#: Checked in order by find_browser(); override with $CDP_BROWSER.
CANDIDATES = (
    'chromium',
    'chromium-browser',
    'google-chrome-stable',
    'google-chrome',
    'chrome',
    'brave',
    'brave-browser',
    'microsoft-edge',
    'headless-shell',
)

#: Locations `which` won't find (macOS bundles).
EXTRA_PATHS = (
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
)

DEFAULT_ARGS = (
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-background-networking',
    '--disable-breakpad',
    '--disable-crash-reporter',
)

#: Launch flags for low-observability (see launch(stealth=True)). Drops the
#: `AutomationControlled` blink feature whose presence flips navigator.webdriver
#: and is a first-line automation tell; requests the modern headless mode whose
#: fingerprint matches headful far more closely than old headless.
STEALTH_ARGS = (
    '--disable-blink-features=AutomationControlled',
)


def find_browser() -> str | None:
    '''Locate a Chromium-based binary: $CDP_BROWSER, then PATH, then known
    bundle locations. Returns None if nothing is found '''
    
    if os.environ.get('PURECDP_NO_BROWSER'):
        return None
    env = os.environ.get('CDP_BROWSER')
    if env:
        return env
    for name in CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    for path in EXTRA_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


class BrowserContext:
    '''An isolated browser context (own cookies/cache/storage — an incognito
    profile, except you can have many). Async context manager; aclose()
    disposes it, closing all its pages without beforeunload.'''

    def __init__(self, connection: Connection, context_id: str):
        self.connection = connection
        self.context_id = context_id

    async def new_session(self, url: str = 'about:blank'):
        return await new_session(self.connection, url,
                                 browser_context_id=self.context_id)

    async def new_page(self, url: str = 'about:blank', **page_kw):
        '''A new tab in this context as a ready ``testing.Page`` — see
        :meth:`Browser.new_page`.'''
        from .testing.page import Page  # lazy: testing sits above this module

        return await Page.create(await self.new_session(url), **page_kw)

    async def aclose(self) -> None:
        from .protocol import browser as _browser
        from .protocol import target as _target

        await self.connection.execute(_target.dispose_browser_context(
            _browser.BrowserContextID(self.context_id)))

    async def __aenter__(self) -> BrowserContext:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


async def new_context(connection: Connection) -> BrowserContext:
    '''Create an isolated BrowserContext on any Connection.'''
    from .protocol import target as _target

    context_id = await connection.execute(_target.create_browser_context())
    return BrowserContext(connection, str(context_id))


async def new_session(
    connection: Connection,
    url: str = 'about:blank',
    *,
    browser_context_id: str | None = None,
):
    '''Create a page target (optionally inside a context) and attach to it.
    Returns the page's Session.'''
    from .protocol import browser as _browser
    from .protocol import target as _target

    context = (_browser.BrowserContextID(browser_context_id)
               if browser_context_id else None)
    target_id = await connection.execute(
        _target.create_target(url=url, browser_context_id=context))
    return await connection.attach(str(target_id))


async def close_page(connection: Connection, session) -> None:
    '''Close a page target; its session ends via Target.detachedFromTarget.'''
    from .protocol import target as _target

    if session.target_id is None:
        raise ValueError('session has no target_id')
    await connection.execute(
        _target.close_target(_target.TargetID(session.target_id)))


class Browser:
    '''A launched browser process plus its CDP connection.

    Async context manager; aclose() is idempotent and always reaps the
    process and (if we created it) the temporary profile directory.
    '''

    def __init__(
        self,
        process: asyncio.subprocess.Process | None,
        connection: Connection,
        user_data_dir: str,
        owns_profile: bool,
        *,
        stderr_tail: _StderrTail | None = None,
    ):
        #: The launched process, or None when we merely connected to a browser
        #: someone else started (see connect()) — then aclose() only detaches.
        self.process = process
        self.connection = connection
        self.user_data_dir = user_data_dir
        self._owns_profile = owns_profile
        self._stderr_tail = stderr_tail

    async def new_context(self) -> BrowserContext:
        return await new_context(self.connection)

    async def new_session(self, url: str = 'about:blank', *,
                          context: BrowserContext | None = None):
        '''Create a page target (optionally in a context) and attach — returns
        the raw :class:`Session`; wrap it in ``testing.Page.create`` for the
        high-level driver (or use :meth:`new_page`, which does both).'''
        return await new_session(
            self.connection, url,
            browser_context_id=context.context_id if context else None)

    async def new_page(self, url: str = 'about:blank', *,
                       context: BrowserContext | None = None, **page_kw):
        '''A new tab as a ready ``testing.Page`` — ``new_session()`` +
        ``Page.create()`` in one call. ``page_kw`` forwards to
        :meth:`testing.Page.create` (``default_timeout``, ``capture``,
        ``track_network``). The page dies with the browser; ``page.stop()``
        only for early teardown.

        (History: 0.4.0 removed a ``new_page`` that returned a raw Session —
        the name lied. This one returns an actual Page.)'''
        from .testing.page import Page  # lazy: testing sits above this module

        session = await self.new_session(url, context=context)
        return await Page.create(session, **page_kw)

    async def attach(self, *, url_contains: str | None = None,
                     target_id: str | None = None, **page_kw):
        '''An EXISTING tab as a ready ``testing.Page`` — find the page target
        whose URL contains ``url_contains`` (or the one with ``target_id``),
        attach, and arm. The missing half of :func:`connect`: connect() gets
        you the browser a human logged into, attach() gets you their tab.

        Raises :class:`TargetNotFound` when nothing matches (the message
        lists the page targets that DO exist) or when ``url_contains``
        matches several tabs (the message lists their ids — pass
        ``target_id=`` to pick one; guessing would mean driving the wrong
        tab). ``page_kw`` forwards to :meth:`testing.Page.create`; arming
        enables CDP domains on the tab, so for a low-observability attach
        pass ``capture=False, track_network=False``.'''
        from .protocol import target as _target
        from .testing.page import Page  # lazy: testing sits above this module

        if (url_contains is None) == (target_id is None):
            raise ValueError('pass exactly one of url_contains= / target_id=')
        infos = await self.connection.execute(_target.get_targets())
        pages = [i for i in infos if i.type == 'page']

        def listing(items):
            return ', '.join(f'{i.target_id} {i.url[:80]}' for i in items[:8])

        if target_id is not None:
            matches = [i for i in pages if str(i.target_id) == target_id]
        else:
            matches = [i for i in pages if url_contains in i.url]
        if not matches:
            wanted = (f'target id {target_id!r}' if target_id is not None
                      else f'URL containing {url_contains!r}')
            raise TargetNotFound(
                f'no page target with {wanted}; open page targets: '
                f'{listing(pages) or "none"}')
        if len(matches) > 1:
            raise TargetNotFound(
                f'{len(matches)} page targets match {url_contains!r} — pass '
                f'target_id= to pick one: {listing(matches)}')
        session = await self.connection.attach(str(matches[0].target_id))
        return await Page.create(session, **page_kw)

    async def close_page(self, session) -> None:
        await close_page(self.connection, session)

    async def close_target(self, target_id: str) -> bool:
        '''Close any target by id — including one this client never attached
        to, e.g. an id from ``discovery.list_targets()`` on a ``connect()``-ed
        browser. Rounds out the lifecycle without hand-rolled ``/json/close``
        HTTP calls. Returns CDP's success flag.'''
        from .protocol import target as _target

        return await self.connection.execute(
            _target.close_target(_target.TargetID(target_id)))

    async def __aenter__(self) -> Browser:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    #: Bound on the graceful half of aclose(): the Browser.close round-trip,
    #: and then how long the browser gets to exit on its own before signals.
    graceful_close_timeout: float = 3.0

    async def aclose(self) -> None:
        # Graceful first, signals as fallback: Browser.close travels over the
        # CDP socket, not via PID, so it reaches the real browser even when a
        # launcher wrapper re-exec'd away from the child we hold — and the
        # browser tears down its own process tree, releases the profile's
        # SingletonLock, and records a clean exit (no "restore pages?" bubble
        # on the next headed run). Only granted the grace wait on a confirmed
        # reply, so a hung browser still gets killed as fast as before.
        graceful = False
        if self.process is not None and self.process.returncode is None:
            from .protocol import browser as _browser

            try:
                await self.connection.execute(
                    _browser.close(), timeout=self.graceful_close_timeout)
                graceful = True
            except Exception:
                pass
        with suppress(Exception):
            await self.connection.aclose()
        # process is None for a connect()ed browser — detach only, never
        # close, reap, or delete a browser/profile we didn't create.
        if self.process is not None and self.process.returncode is None:
            if graceful:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.process.wait(),
                                           timeout=self.graceful_close_timeout)
            if self.process.returncode is None:
                with suppress(ProcessLookupError):
                    self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        self.process.kill()
                    await self.process.wait()
        if self._stderr_tail is not None:
            self._stderr_tail.close()
        if self._owns_profile:
            await asyncio.to_thread(shutil.rmtree, self.user_data_dir, True)


def _undot(key: str, value: typing.Any) -> dict:
    '''Expand a dotted pref key into a nested dict: ``("a.b", 1) -> {"a":{"b":1}}``.'''
    if '.' in key:
        head, rest = key.split('.', 1)
        return {head: _undot(rest, value)}
    return {key: value}


def _merge_nested(a: dict, b: dict) -> dict:
    '''Deep-merge ``b`` into ``a`` (leaf values in ``a`` are overwritten).'''
    for key, bv in b.items():
        av = a.get(key)
        if isinstance(av, dict) and isinstance(bv, dict):
            _merge_nested(av, bv)
        else:
            a[key] = bv
    return a


def _write_prefs(profile: str, prefs: dict) -> None:
    '''Merge ``prefs`` into ``<profile>/Default/Preferences`` before launch —
    how to set profile options that have no command-line flag (content
    settings, ``intl.accept_languages``, ``credentials_enable_service``, …).
    Keys may be dotted (``"profile.default_content_setting_values.images"``) or
    nested dicts. Adapted from undetected-chromedriver's prefs handling.'''
    default_dir = os.path.join(profile, 'Default')
    os.makedirs(default_dir, exist_ok=True)
    path = os.path.join(default_dir, 'Preferences')
    merged: dict = {}
    if os.path.exists(path):
        with suppress(OSError, ValueError):
            with open(path, encoding='latin1') as f:
                merged = json.load(f)
    for key, value in prefs.items():
        _merge_nested(merged, _undot(key, value))
    with open(path, 'w', encoding='latin1') as f:
        json.dump(merged, f)


def _profile_lock_holder(profile: str) -> int | None:
    '''PID of the live browser holding this profile's Singleton lock, or None.

    Chromium's profile lock is a symlink ``SingletonLock -> hostname-PID``
    (POSIX; on Windows the readlink fails and the check is skipped). A dead
    PID is a stale lock left by a crash — Chromium ignores those, so we must
    too; a lock stamped by another host can't be liveness-checked, so it
    doesn't block either (the browser's own stderr says if it's real).'''
    try:
        holder = os.readlink(os.path.join(profile, 'SingletonLock'))
    except OSError:
        return None
    host, sep, pid_text = holder.rpartition('-')
    if not sep or not pid_text.isdigit() or host != socket.gethostname():
        return None
    pid = int(pid_text)
    try:
        os.kill(pid, 0)  # existence probe, delivers nothing
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid  # alive, just not ours
    except OSError:
        return None
    return pid


def _prepare_reused_profile(profile: str) -> None:
    '''Guards for launching into a caller-supplied ``user_data_dir``.

    Refuse a profile a live browser still owns — Chromium would signal that
    instance and exit immediately, and the failure surfaces far away from the
    cause. And drop a stale ``DevToolsActivePort`` left by a previous run:
    the endpoint wait polls that file and can read last run's dead port
    before the new browser rewrites it, dying with a bare
    ``ConnectionRefusedError``. The file is only an advertisement — the
    browser recreates it on every start.'''
    holder = _profile_lock_holder(profile)
    if holder is not None:
        raise BrowserLaunchError(
            f'profile {profile} is in use by PID {holder} (SingletonLock) — '
            'close that browser or pass a different user_data_dir')
    with suppress(OSError):
        os.remove(os.path.join(profile, 'DevToolsActivePort'))


class _StderrTail:
    '''Keep the tail of the browser's stderr for diagnostics.

    The stream must be drained for the browser's whole life — a full pipe
    would block the child — so a background task reads it forever, keeping
    only the last few chunks: the part worth quoting when a launch fails
    ("The profile appears to be in use…", sandbox errors, missing libs).'''

    def __init__(self, stream: asyncio.StreamReader | None):
        self._chunks: collections.deque[bytes] = collections.deque(maxlen=8)
        self._task = (asyncio.ensure_future(self._drain(stream))
                      if stream is not None else None)

    async def _drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(4096):
            self._chunks.append(chunk)

    async def wait_drained(self, timeout: float = 0.3) -> None:
        '''Give the drain a moment to reach EOF before quoting the tail —
        the child just exited and its last words may still be in the pipe.
        Bounded: a renderer that inherited the fd can hold it open forever.'''
        if self._task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout)
        except Exception:
            pass  # timed out, or the drain died — quote whatever we have

    def text(self) -> str:
        return b''.join(self._chunks).decode(errors='replace').strip()[-2000:]

    def suffix(self) -> str:
        '''``"; browser stderr: …"`` when there is any output, else ``""``.'''
        text = self.text()
        return f'; browser stderr: {text}' if text else ''

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()


async def launch(
    browser_path: str | None = None,
    *,
    headless: bool = True,
    pipe: bool = False,
    stealth: bool = False,
    ignore_https_errors: bool = False,
    user_data_dir: str | None = None,
    prefs: dict | None = None,
    extra_args: typing.Sequence[str] = (),
    timeout: float = 30.0,
) -> Browser:
    '''Launch a browser and return a Browser with an open Connection.

    Websocket mode uses ``--remote-debugging-port=0`` and reads the ephemeral
    endpoint from the profile's DevToolsActivePort file (no port collisions,
    no HTTP discovery). ``pipe=True`` uses ``--remote-debugging-pipe``
    (POSIX only).

    ``prefs`` seeds the profile's ``Default/Preferences`` before start (dotted
    or nested keys) — the way to set options with no command-line flag, e.g.
    ``{"credentials_enable_service": False}`` to silence the password manager.

    ``ignore_https_errors=True`` accepts invalid/self-signed certificates
    (``--ignore-certificate-errors``) — for local-HTTPS and dev-proxy setups.
    '''
    path = browser_path or find_browser()
    if path is None:
        raise BrowserLaunchError(
            'no browser binary found; install Chromium/Chrome/Brave, '
            'or set $CDP_BROWSER, or pass browser_path='
        )
    owns_profile = user_data_dir is None
    profile = user_data_dir or tempfile.mkdtemp(prefix='purecdp-profile-')
    if not owns_profile:
        _prepare_reused_profile(profile)
    if prefs:
        _write_prefs(profile, prefs)

    argv = [path, f'--user-data-dir={profile}', *DEFAULT_ARGS]
    if stealth:
        argv += STEALTH_ARGS
    if ignore_https_errors:
        # self-signed / local-HTTPS workflows (mkcert, dev proxies)
        argv.append('--ignore-certificate-errors')
    if headless:
        # modern headless matches headful's fingerprint far better than old
        argv.append('--headless=new' if stealth else '--headless')
    argv.append('--remote-debugging-pipe' if pipe else '--remote-debugging-port=0')
    argv += [*extra_args, 'about:blank']

    process: asyncio.subprocess.Process | None = None
    stderr_tail = _StderrTail(None)
    try:
        if pipe:
            process, transport = await spawn_pipe_process(
                argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            stderr_tail = _StderrTail(process.stderr)
        else:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            stderr_tail = _StderrTail(process.stderr)
            url = await _wait_for_endpoint(process, profile, timeout,
                                           stderr_tail)
            transport = await WebSocketTransport.connect(url)
        connection = Connection(transport)
        await connection.open()
    except BaseException as exc:
        died = process is not None and process.returncode is not None
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        if died:
            await stderr_tail.wait_drained()
        note = stderr_tail.suffix()
        stderr_tail.close()
        if owns_profile:
            await asyncio.to_thread(shutil.rmtree, profile, True)
        if (died and isinstance(exc, Exception)
                and not isinstance(exc, BrowserLaunchError)):
            # e.g. a websocket connect that failed because the browser was
            # already gone — surface the exit and its stderr instead of the
            # misleading downstream error.
            raise BrowserLaunchError(
                f'browser exited with code {process.returncode} during '
                f'launch{note}') from exc
        raise
    return Browser(process, connection, profile, owns_profile,
                   stderr_tail=stderr_tail)


async def connect(
    endpoint: str | None = None,
    *,
    host: str = '127.0.0.1',
    port: int = 9222,
    timeout: float = 30.0,
) -> Browser:
    '''Attach to an already-running browser over CDP and return a Browser.

    Unlike :func:`launch`, this neither starts nor owns the browser: ``aclose()``
    just closes the connection, leaving the browser (and its profile) running.
    This is how you drive a browser someone else started — a Chrome-in-Docker /
    browserless / Skyvern cloud session, or a local ``chromium
    --remote-debugging-port=9222``.

    Accepted ``endpoint`` forms: a browser-level ``ws://…/devtools/browser/…``
    URL (used directly), or the ``http://host:port`` everyone tries first —
    resolved to the websocket endpoint via ``/json/version``, exactly like
    passing ``host``/``port``.
    '''
    if endpoint is not None and endpoint.startswith(('http://', 'https://')):
        parsed = urlsplit(endpoint)
        host = parsed.hostname or host
        port = parsed.port or port
        endpoint = None
    if endpoint is not None and not endpoint.startswith(('ws://', 'wss://')):
        raise ValueError(
            f'unsupported endpoint {endpoint!r} — pass a ws:// browser '
            'endpoint, an http://host:port debugging address, or host=/port=')
    if endpoint is None:
        version = await get_version(host, port, timeout=timeout)
        endpoint = version['webSocketDebuggerUrl']
    transport = await WebSocketTransport.connect(endpoint)
    connection = Connection(transport)
    await connection.open()
    return Browser(None, connection, '', owns_profile=False)


async def _wait_for_endpoint(
    process: asyncio.subprocess.Process, profile: str, timeout: float,
    stderr_tail: _StderrTail,
) -> str:
    '''Poll the profile's DevToolsActivePort file (line 1: port, line 2:
    browser target path) and build the ws:// URL.'''
    port_file = os.path.join(profile, 'DevToolsActivePort')
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if process.returncode is not None:
            await stderr_tail.wait_drained()
            raise BrowserLaunchError(
                f'browser exited with code {process.returncode} before '
                'publishing its debugging endpoint (try headless=False, or a '
                f'sandbox-related flag via extra_args){stderr_tail.suffix()}'
            )
        try:
            with open(port_file) as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        if len(lines) >= 2 and lines[0].strip().isdigit():
            return f'ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}'
        if asyncio.get_running_loop().time() > deadline:
            raise BrowserLaunchError(
                f'browser did not publish {port_file} within {timeout}s'
                f'{stderr_tail.suffix()}'
            )
        await asyncio.sleep(0.05)
