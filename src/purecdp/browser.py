'''Layer 3: find, launch, and clean up a debuggable browser.

The launcher runs the browser with an ephemeral debug port (or the pipe),
connects, and guarantees teardown — kill the process, remove the temporary
profile — as an async context manager. Anything Chromium-based works
(Chromium, Chrome, Brave, Edge, headless-shell).
'''

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import typing
from contextlib import suppress

from .connection import Connection
from .discovery import get_version
from .errors import BrowserLaunchError
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
    ):
        #: The launched process, or None when we merely connected to a browser
        #: someone else started (see connect()) — then aclose() only detaches.
        self.process = process
        self.connection = connection
        self.user_data_dir = user_data_dir
        self._owns_profile = owns_profile

    async def new_context(self) -> BrowserContext:
        return await new_context(self.connection)

    async def new_session(self, url: str = 'about:blank', *,
                          context: BrowserContext | None = None):
        '''Create a page target (optionally in a context) and attach — returns
        the raw :class:`Session`; wrap it in ``testing.Page.create`` for the
        high-level driver.'''
        return await new_session(
            self.connection, url,
            browser_context_id=context.context_id if context else None)

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

    async def aclose(self) -> None:
        with suppress(Exception):
            await self.connection.aclose()
        # process is None for a connect()ed browser — detach only, never reap
        # or delete a browser/profile we didn't create.
        if self.process is not None and self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
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
    try:
        if pipe:
            process, transport = await spawn_pipe_process(
                argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            url = await _wait_for_endpoint(process, profile, timeout)
            transport = await WebSocketTransport.connect(url)
        connection = Connection(transport)
        await connection.open()
    except BaseException:
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        if owns_profile:
            await asyncio.to_thread(shutil.rmtree, profile, True)
        raise
    return Browser(process, connection, profile, owns_profile)


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

    Pass a browser-level ``ws://…/devtools/browser/…`` ``endpoint`` directly, or
    give ``host``/``port`` and the endpoint is discovered via ``/json/version``.
    '''
    if endpoint is None:
        version = await get_version(host, port, timeout=timeout)
        endpoint = version['webSocketDebuggerUrl']
    transport = await WebSocketTransport.connect(endpoint)
    connection = Connection(transport)
    await connection.open()
    return Browser(None, connection, '', owns_profile=False)


async def _wait_for_endpoint(
    process: asyncio.subprocess.Process, profile: str, timeout: float
) -> str:
    '''Poll the profile's DevToolsActivePort file (line 1: port, line 2:
    browser target path) and build the ws:// URL.'''
    port_file = os.path.join(profile, 'DevToolsActivePort')
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if process.returncode is not None:
            raise BrowserLaunchError(
                f'browser exited with code {process.returncode} before '
                'publishing its debugging endpoint (try headless=False, or a '
                'sandbox-related flag via extra_args)'
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
            )
        await asyncio.sleep(0.05)
