'''Artifacts on failure: dump what a red test needs for diagnosis (M12).

When a test fails, :class:`CDPTestCase` (and the pytest ``cdp_page`` fixture)
call :func:`dump_artifacts` BEFORE the browser closes: a screenshot, the page
HTML, captured console messages and uncaught JS errors per page, plus a
manifest (``info.txt``) with the traceback — and, when a
:class:`~purecdp.testing.recorder.NetworkRecorder` is live on the page
(your test's own ``page.record()``, or one armed by ``ARTIFACTS_NETWORK``),
the recorded traffic. Green tests write nothing.

Every capture is best-effort: a crashed renderer or a dead session must never
turn a clean test failure into a confusing teardown error, so each item is
individually suppressed and the manifest records what was captured and what
raised.
'''

from __future__ import annotations

import os
import shutil
import time
import traceback
import typing

if typing.TYPE_CHECKING:
    from .page import Page

#: Max response/request body characters quoted per exchange in network.log.
_BODY_CAP = 10_000

_SAFE = frozenset(
    'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-')


def _sanitize(name: str) -> str:
    '''A test id as a filesystem-safe directory name.'''
    return ''.join(c if c in _SAFE else '_' for c in name) or '_'


def artifacts_dir(base: str | None = None) -> str:
    '''The base artifacts directory: explicit ``base``, else
    ``$PURECDP_ARTIFACTS_DIR``, else ``./purecdp-artifacts``.'''
    return (base or os.environ.get('PURECDP_ARTIFACTS_DIR')
            or 'purecdp-artifacts')


def _clip(text: str, cap: int = _BODY_CAP) -> str:
    if len(text) <= cap:
        return text
    return f'{text[:cap]}… (+{len(text) - cap} more chars)'


def _format_console(messages: typing.Iterable) -> str:
    return '\n'.join(f'[{m.kind}] {m.text}' for m in messages) + '\n'


def _format_network(recorders: typing.Iterable) -> str:
    lines = []
    for recorder in recorders:
        for e in recorder.exchanges:
            status = e.status if e.status is not None else '?'
            lines.append(f'{e.method} {e.url} -> {status} {e.mime or ""}')
            if e.request_body:
                lines.append(f'  request: {_clip(e.request_body)}')
            if e.body_error:
                lines.append(f'  body: <unavailable: {e.body_error}>')
            elif e.body:
                lines.append(f'  body: {_clip(e.text)}')
            lines.append('')
    return '\n'.join(lines)


async def dump_artifacts(
    pages: typing.Sequence[Page],
    dest: str,
    *,
    test_id: str = '',
    exc: BaseException | str | None = None,
) -> str:
    '''Write diagnostic artifacts for ``pages`` into directory ``dest``
    (created; wiped first so runs never mix). Returns ``dest``.

    Per page (files suffixed ``-2``, ``-3``… beyond the first): a full-page
    ``screenshot.png``, the ``page.html`` outerHTML, ``console.log`` and
    ``js_errors.log`` (when non-empty), and ``network.log`` from any
    :class:`NetworkRecorder` live on the page (when it recorded anything).
    ``info.txt`` is the manifest: test id, the exception/traceback, each
    page's URL and title, and per-item capture status — a page that stopped
    answering still yields a manifest saying exactly what could be saved.

    Callable directly too (e.g. from a ``launched_page`` script's except
    block); ``exc`` accepts an exception or pre-formatted text.
    '''
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    manifest = [f'test: {test_id}' if test_id else 'test: (unknown)']
    if isinstance(exc, BaseException):
        kind = 'FAIL' if isinstance(exc, AssertionError) else 'ERROR'
        manifest.append(f'outcome: {kind} ({type(exc).__name__})')
    else:
        manifest.append('outcome: FAIL')
    manifest.append(f'time: {time.strftime("%Y-%m-%dT%H:%M:%S%z")}')
    if exc is not None:
        text = (''.join(traceback.format_exception(exc)).rstrip()
                if isinstance(exc, BaseException) else str(exc).rstrip())
        manifest += ['', '== traceback ==', text]

    for i, page in enumerate(pages):
        sfx = '' if i == 0 else f'-{i + 1}'
        url = title = '?'
        try:
            url = await page.evaluate('location.href')
            title = await page.title()
        except BaseException as e:
            manifest += ['', f'== page {i + 1}: (unreachable: {e!r}) ==']
        else:
            manifest += ['', f'== page {i + 1}: {url} {title!r} ==']

        async def _shot(page=page, sfx=sfx):
            raw = await page.screenshot(full_page=True)
            with open(os.path.join(dest, f'screenshot{sfx}.png'), 'wb') as f:
                f.write(raw)
            return f'{len(raw)} bytes'

        async def _html(page=page, sfx=sfx):
            html = await page.content()
            with open(os.path.join(dest, f'page{sfx}.html'), 'w',
                      encoding='utf-8') as f:
                f.write(html)
            return f'{len(html)} chars'

        async def _console(page=page, sfx=sfx):
            messages = getattr(page, 'console', ())
            if not messages:
                return 'empty (not written)'
            with open(os.path.join(dest, f'console{sfx}.log'), 'w',
                      encoding='utf-8') as f:
                f.write(_format_console(messages))
            return f'{len(messages)} messages'

        async def _js_errors(page=page, sfx=sfx):
            errors = getattr(page, 'js_errors', ())
            if not errors:
                return 'empty (not written)'
            with open(os.path.join(dest, f'js_errors{sfx}.log'), 'w',
                      encoding='utf-8') as f:
                f.write('\n'.join(str(e) for e in errors) + '\n')
            return f'{len(errors)} errors'

        async def _network(page=page, sfx=sfx):
            recorders = getattr(page, '_recorders', ())
            total = sum(len(r.exchanges) for r in recorders)
            if not total:
                return (f'no recorder' if not recorders
                        else 'no exchanges (not written)')
            with open(os.path.join(dest, f'network{sfx}.log'), 'w',
                      encoding='utf-8') as f:
                f.write(_format_network(recorders))
            return f'{total} exchanges ({len(recorders)} recorder(s))'

        for name, capture in ((f'screenshot{sfx}.png', _shot),
                              (f'page{sfx}.html', _html),
                              (f'console{sfx}.log', _console),
                              (f'js_errors{sfx}.log', _js_errors),
                              (f'network{sfx}.log', _network)):
            try:
                manifest.append(f'{name}: {await capture()}')
            except BaseException as e:
                manifest.append(f'{name}: CAPTURE FAILED: {e!r}')

    with open(os.path.join(dest, 'info.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(manifest) + '\n')
    return dest
