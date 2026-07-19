'''Stealth: opt-in fingerprint patches applied as init scripts (M8 Track A).

A port of the well-understood puppeteer-extra-plugin-stealth evasions. Each is
a small JavaScript snippet installed via ``page.add_init_script`` so it runs in
every new document *before* the page's own scripts, patching one signal that
headless Chromium leaks (an empty ``navigator.plugins``, a missing
``window.chrome``, a ``navigator.webdriver`` of true, the "Google SwiftShader"
WebGL renderer, …).

This is the layer *above* the CDP-hygiene base (``launch(stealth=True)`` +
``Page.create(capture=False, track_network=False)``). Use them together for the
strongest baseline; see :func:`apply_stealth`.

**Honesty:** these reduce common, cheap detection signals — not a guarantee of
undetectability. Mature checks (CreepJS-class fingerprinting, TLS/HTTP2
signatures) still identify automation; TLS/HTTP2 come from Chromium's network
stack and no init script touches them. Treat this as "no cheap automation
tells", not "invisible".

**Against realm-comparison fingerprinters (CreepJS):** patching a prototype is
itself detectable. CreepJS obtains a *pristine* copy of native functions from a
fresh iframe and diffs it against the page's, so any ``Object.defineProperty``
override here leaves a trace it flags as a "lie" — and it *scores* the presence
of stealth patches, so these evasions can raise its suspicion rather than lower
it. It also cross-checks the page's WebGL renderer against a *worker's*: if the
``webgl.vendor`` evasion runs only in the page, page≠worker is a tell. That is
what ``apply_stealth(workers=True)`` (the default) addresses — the same patches
are injected into worker scope so the two agree. Nothing here defeats the
pristine-realm diff itself; the durable wins remain the CDP-hygiene base and
behavioral realism (:mod:`purecdp.testing.human`), which patch nothing.

Each evasion is independent and opt-in. ``apply_stealth(page)`` applies the
default set; pass ``evasions=[...]`` to choose, and keyword overrides to tune
the spoofed values.
'''

from __future__ import annotations

import json
import typing

if typing.TYPE_CHECKING:
    from .page import Page

# name -> function(config: dict) -> JS snippet. Ordered; insertion = apply order.
_EVASIONS: dict[str, typing.Callable[[dict], str]] = {}


def _evasion(name: str) -> typing.Callable:
    def register(fn: typing.Callable[[dict], str]) -> typing.Callable:
        _EVASIONS[name] = fn
        return fn

    return register


# A shared helper name, declared once at the top of the combined IIFE (see
# build_stealth_script): __pcMask(fn, name) tags fn so a masked
# Function.prototype.toString reports it as native code. Default is a pass-
# through, so evasions route through it whether or not "function.toString" is
# on. The prelude that makes it real:
_TOSTRING_MASK = '''
const nativeToString = Function.prototype.toString;
const registry = new WeakMap();
const proxy = new Proxy(nativeToString, {
  apply(target, thisArg, args) {
    if (registry.has(thisArg))
      return 'function ' + registry.get(thisArg) + '() { [native code] }';
    if (thisArg === proxy || thisArg === nativeToString)
      return 'function toString() { [native code] }';
    return Reflect.apply(target, thisArg, args);
  }
});
Function.prototype.toString = proxy;
__pcMask = function (fn, name) {
  try { registry.set(fn, name || (fn && fn.name) || ''); } catch (e) {}
  return fn;
};
'''.strip()


def _const_getter(target: str, prop: str, js_value: str) -> str:
    '''Snippet installing a spoofed getter for ``target.prop`` that returns the
    constant ``js_value`` (already a JS literal). Routed through ``__pcMask`` so
    a masked ``Function.prototype.toString`` reports it as native code.
    ``target`` is a JS expression — ``navigator``, ``Navigator.prototype``, ….'''
    return (f"Object.defineProperty({target}, '{prop}', {{get: "
            f"__pcMask(function () {{ return {js_value}; }}, 'get {prop}'),"
            f' configurable: true}});')


@_evasion('navigator.webdriver')
def _webdriver(cfg: dict) -> str:
    # a non-automated modern Chrome reports false, not true/undefined
    return _const_getter('Navigator.prototype', 'webdriver', 'false')


@_evasion('navigator.languages')
def _languages(cfg: dict) -> str:
    return _const_getter('navigator', 'languages',
                         json.dumps(list(cfg['languages'])))


@_evasion('navigator.vendor')
def _vendor(cfg: dict) -> str:
    return _const_getter('navigator', 'vendor', json.dumps(cfg['vendor']))


@_evasion('navigator.hardwareConcurrency')
def _hardware_concurrency(cfg: dict) -> str:
    return _const_getter('navigator', 'hardwareConcurrency',
                         str(int(cfg['hardware_concurrency'])))


@_evasion('navigator.plugins')
def _plugins(cfg: dict) -> str:
    # headless has an empty PluginArray; fake the stock Chrome PDF set so
    # length/item/namedItem all look real
    return '''
const names = ['PDF Viewer','Chrome PDF Viewer','Chromium PDF Viewer',
               'Microsoft Edge PDF Viewer','WebKit built-in PDF'];
const plugins = names.map(name => ({name, filename: 'internal-pdf-viewer',
  description: 'Portable Document Format', length: 1}));
plugins.item = __pcMask(i => plugins[i] || null, 'item');
plugins.namedItem = __pcMask(n => plugins.find(p => p.name === n) || null, 'namedItem');
plugins.refresh = __pcMask(() => {}, 'refresh');
Object.defineProperty(navigator, 'plugins',
  {get: __pcMask(function () { return plugins; }, 'get plugins'), configurable: true});
Object.defineProperty(navigator, 'mimeTypes',
  {get: __pcMask(function () {
     return {length: plugins.length, item: () => null, namedItem: () => null};
   }, 'get mimeTypes'), configurable: true});
'''.strip()


@_evasion('navigator.permissions')
def _permissions(cfg: dict) -> str:
    # headless answers 'denied' to the notifications query while
    # Notification.permission is 'default' — a classic mismatch tell
    return '''
const original = navigator.permissions.query.bind(navigator.permissions);
navigator.permissions.query = __pcMask(function query(params) {
  return (params && params.name === 'notifications')
    ? Promise.resolve({state: Notification.permission, onchange: null})
    : original(params);
}, 'query');
'''.strip()


@_evasion('window.chrome')
def _chrome(cfg: dict) -> str:
    # headless can lack window.chrome (or its runtime/app/csi/loadTimes
    # members); augment rather than replace so we fill only what's missing
    return '''
const chrome = window.chrome = window.chrome || {};
if (!chrome.app) chrome.app = {isInstalled: false,
  InstallState: {DISABLED: 'disabled', INSTALLED: 'installed',
    NOT_INSTALLED: 'not_installed'},
  RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run',
    RUNNING: 'running'}};
if (!chrome.runtime) chrome.runtime = {OnInstalledReason: {},
  OnRestartRequiredReason: {}, PlatformArch: {}, PlatformOs: {},
  connect: __pcMask(() => {}, 'connect'),
  sendMessage: __pcMask(() => {}, 'sendMessage')};
if (!chrome.csi) chrome.csi = __pcMask(() => ({}), 'csi');
if (!chrome.loadTimes) chrome.loadTimes = __pcMask(() => ({}), 'loadTimes');
'''.strip()


@_evasion('webgl.vendor')
def _webgl(cfg: dict) -> str:
    vendor = json.dumps(cfg['webgl_vendor'])
    renderer = json.dumps(cfg['webgl_renderer'])
    return f'''
const spoof = (getParameter) => __pcMask(function(p) {{
  if (p === 37445) return {vendor};    // UNMASKED_VENDOR_WEBGL
  if (p === 37446) return {renderer};  // UNMASKED_RENDERER_WEBGL
  return getParameter.call(this, p);
}}, 'getParameter');
WebGLRenderingContext.prototype.getParameter =
  spoof(WebGLRenderingContext.prototype.getParameter);
if (window.WebGL2RenderingContext)
  WebGL2RenderingContext.prototype.getParameter =
    spoof(WebGL2RenderingContext.prototype.getParameter);
'''.strip()


@_evasion('media.codecs')
def _media_codecs(cfg: dict) -> str:
    # headless Chromium lacks proprietary codecs (H.264/AAC); a real Chrome
    # answers 'probably'/'maybe' for them
    return '''
const patch = (proto) => {
  const original = proto.canPlayType;
  proto.canPlayType = __pcMask(function(type) {
    if (/mp4a|mp4;|avc1|h264|aac|mpeg/i.test(type || '')) return 'probably';
    if (/ogg|webm|vorbis|opus/i.test(type || '')) return 'probably';
    return original.apply(this, arguments);
  }, 'canPlayType');
};
patch(HTMLMediaElement.prototype);
'''.strip()


@_evasion('navigator.maxTouchPoints')
def _max_touch_points(cfg: dict) -> str:
    # headless reports 0; adapted from undetected-chromedriver. Opt-in: set it
    # to match the *device* you claim to be (0 for a plain desktop, >0 only for
    # a touchscreen/mobile UA — a mismatch is itself a tell).
    return _const_getter('navigator', 'maxTouchPoints',
                         str(int(cfg['max_touch_points'])))


@_evasion('navigator.connection')
def _connection(cfg: dict) -> str:
    # give navigator.connection a realistic round-trip time instead of a
    # headless default (adapted from undetected-chromedriver)
    return f'''
if (navigator.connection) {{
  Object.defineProperty(navigator.connection, 'rtt',
    {{get: __pcMask(function () {{ return {int(cfg['connection_rtt'])}; }}, 'get rtt'),
     configurable: true}});
}}
'''.strip()


@_evasion('function.toString')
def _function_tostring(cfg: dict) -> str:
    # replace Function.prototype.toString so every function the other evasions
    # route through __pcMask reports "[native code]" — hides the patch from a
    # cheap fn.toString() check. NOTE: a masked toString is itself detectable
    # by realm-diff fingerprinters (CreepJS's hasToStringProxy); this defeats
    # naive checks only. Include it *before* the evasions it should cover.
    return _TOSTRING_MASK


#: Opt-in-only evasions — excluded from the default set because they need a
#: deliberate choice (a device-appropriate touch count / connection) or carry a
#: detectability trade-off of their own (a masked toString is a realm-diff tell).
OPT_IN_EVASIONS: tuple[str, ...] = (
    'navigator.maxTouchPoints', 'navigator.connection', 'function.toString')

#: All available evasion names.
EVASIONS: tuple[str, ...] = tuple(_EVASIONS)

#: Evasions applied by default (the low-risk, well-understood set).
DEFAULT_EVASIONS: tuple[str, ...] = tuple(
    n for n in _EVASIONS if n not in OPT_IN_EVASIONS)

_DEFAULT_CONFIG = {
    'languages': ('en-US', 'en'),
    'vendor': 'Google Inc.',
    'hardware_concurrency': 8,
    'webgl_vendor': 'Intel Inc.',
    'webgl_renderer': 'Intel Iris OpenGL Engine',
    'max_touch_points': 1,
    'connection_rtt': 100,
}


def build_stealth_script(
    evasions: typing.Sequence[str] | None = None, **overrides: typing.Any
) -> str:
    '''Build the combined init-script for the selected evasions.

    Each snippet is wrapped in its own try/catch so one failing evasion can't
    break the others or the page. Exposed for inspection/testing;
    :func:`apply_stealth` is the normal entry point.
    '''
    names = list(DEFAULT_EVASIONS if evasions is None else evasions)
    unknown = [n for n in names if n not in _EVASIONS]
    if unknown:
        raise ValueError(
            f'unknown evasion(s): {unknown}; available: {list(EVASIONS)}')
    config = {**_DEFAULT_CONFIG, **overrides}
    # function.toString installs the shared __pcMask helper, so it must run
    # before the evasions that register their patched functions with it.
    ordered = ([n for n in names if n == 'function.toString']
               + [n for n in names if n != 'function.toString'])
    # __pcMask defaults to a pass-through; function.toString (if selected)
    # reassigns it to the real masker. Either way evasions can call it.
    parts = ['let __pcMask = (fn) => fn;']
    parts += [f'try {{\n{_EVASIONS[name](config)}\n}} catch (e) {{}}'
              for name in ordered]
    return "(() => {\n'use strict';\n" + '\n'.join(parts) + '\n})();'


async def apply_stealth(
    page: Page,
    *,
    evasions: typing.Sequence[str] | None = None,
    workers: bool = True,
    **overrides: typing.Any,
) -> None:
    '''Install fingerprint evasions as an init script on ``page``.

    Call **before** navigating (the script runs on every new document). Applies
    :data:`DEFAULT_EVASIONS` unless ``evasions`` names a subset (choose from
    :data:`EVASIONS`; the extras in :data:`OPT_IN_EVASIONS` —
    ``navigator.maxTouchPoints``, ``navigator.connection``,
    ``function.toString`` — are off by default). Keyword overrides tune spoofed
    values: ``languages``, ``vendor``, ``hardware_concurrency``,
    ``webgl_vendor``, ``webgl_renderer``, ``max_touch_points``,
    ``connection_rtt``.

    With ``workers=True`` (default) the same patches are also injected into
    every dedicated/shared/service worker as it starts, so a worker's WebGL
    renderer / navigator values agree with the page's — a mismatch is a known
    detection tell (see the module docstring). This arms paused auto-attach on
    the page's session; pass ``workers=False`` to touch only the page, or if you
    manage auto-attach yourself.

    Pairs with ``launch(stealth=True)`` and
    ``Page.create(capture=False, track_network=False)`` for the full baseline.
    '''
    script = build_stealth_script(evasions, **overrides)
    await page.add_init_script(script)
    if workers:
        await _install_worker_stealth(page, script)


async def _install_worker_stealth(page: Page, script: str) -> None:
    '''Inject ``script`` into each worker as it attaches paused, before its own
    code runs (init scripts don't reach workers). Uses a connection target-init
    hook + paused auto-attach; the combined script's per-evasion try/catch makes
    the page-only snippets harmless no-ops in worker scope.'''
    from ..protocol import runtime as runtime_proto

    session = page.session

    async def inject(worker_session: typing.Any) -> None:
        info = worker_session.target_info
        if info is not None and 'worker' in (info.type or ''):
            await worker_session.execute(
                runtime_proto.evaluate(expression=script))

    session.connection.add_target_init_hook(inject)
    await session.set_auto_attach(wait_for_debugger=True)
