# purecdp

Pure-Python (stdlib-only) library for the Chrome DevTools Protocol, targeting Python 3.14+.

Three things, layered:

1. **`purecdp.protocol`** — low-level typed bindings, generated from the official
   [devtools-protocol](https://github.com/ChromeDevTools/devtools-protocol) JSON specs.
2. **`purecdp` core** — sans-I/O protocol engine + asyncio transports (websocket / pipe) + browser launcher.
3. **`purecdp.testing`** — opinionated helpers for driving a browser in tests, incl. a pytest plugin.

See [Examples.md](Examples.md) for task-grouped recipes (basics, testing, stealth, LLM agents, frames).

## Quickstart

```python
import asyncio
import purecdp
from purecdp.protocol import page, runtime

async def main():
    async with await purecdp.launch() as browser:   # finds Chromium/Chrome/Brave
        session = await browser.new_page()          # create target + attach

        await session.execute(page.enable())
        waiter = asyncio.create_task(session.wait_for(page.LoadEventFired))
        await asyncio.sleep(0)  # subscribe before triggering
        await session.execute(page.navigate(url="https://example.com"))
        await waiter

        result, _ = await session.execute(
            runtime.evaluate(expression="document.title", return_by_value=True))
        print(result.value)

asyncio.run(main())
```

`purecdp.launch(pipe=True)` uses `--remote-debugging-pipe` instead of a websocket;
`purecdp.discovery.get_version(port=9222)` reaches a browser someone else started.
`browser.new_context()` gives an isolated cookies/storage context (cheap per-test
isolation); `connection.set_auto_attach()` auto-attaches popups/workers/OOPIFs,
resuming paused targets automatically.

## Writing tests with purecdp

The stdlib way (`purecdp.testing.CDPTestCase` — fresh browser, isolated
context, and a `Page` per test):

```python
from purecdp.testing import CDPTestCase, JSError

class MyAppTests(CDPTestCase):
    async def test_stubbed_api(self):
        async def api(request):
            await request.fulfill(body='{"items": []}',
                                  content_type="application/json")
        await self.page.route("*/api/*", api)
        await self.page.goto("https://myapp.example/", wait="idle")
        await self.page.wait_for_selector("#empty-state")
        assert not self.page.js_errors
```

`Page` gives you `goto` (load/idle waits), `evaluate` (promises awaited,
`JSError` raised), `wait_for_selector`/`wait_for_function`, `click`,
`query`/`query_all` (held elements, text-filtered, stale-copy-proof),
`screenshot`, always-on `page.console` / `page.js_errors` capture, and
`page.route()` request stubbing/rewriting/aborting. For traffic-level
assertions there's `page.record()` (full request/response exchanges, with
`parse_sse()` for streamed bodies) and `page.expect_download()` (capture a
download's bytes without touching disk). And when a test fails, its pages
are dumped as diagnostic artifacts — screenshot, HTML, console, recorded
traffic, traceback — under `purecdp-artifacts/<test id>/` before the browser
closes; green tests write nothing.

For SPA-proof tests there are lazy locators: `page.live(selector)` and
`get_by_test_id` / `get_by_text` / `get_by_role` / `get_by_label` hold the
*query*, not a node — every action re-resolves against the current DOM (a
React remount can't leave you a stale handle), auto-waits for actionability,
and one `.should(...)` gives polling assertions:

```python
await page.get_by_test_id("chat-input").fill("hello")
await page.get_by_role("button", name="Send").click()
await page.live(".msg", containing="hello").should(visible=True, count=1)
await page.live(".spinner").should(count=0)          # waits until it's gone
```

Prefer pytest? The optional plugin (auto-registered when installed, or
`-p purecdp.testing.pytest_plugin`) provides a session-scoped `cdp_browser`
and per-test `cdp_page`, and runs `async def` tests itself — no
pytest-asyncio required:

```python
async def test_title(cdp_page):
    await cdp_page.goto("data:text/html,<title>hi</title>")
    assert await cdp_page.title() == "hi"
```

## Low-observability / stealth

For automation that shouldn't advertise itself, purecdp layers three opt-in
pieces (reduces common detection signals — not a promise of undetectability;
CreepJS-class and TLS/HTTP2 fingerprints still identify automation):

```python
import purecdp
from purecdp.testing import Page, apply_stealth

async with await purecdp.launch(stealth=True) as browser:   # AutomationControlled off, headless=new
    session = await browser.new_page()
    page = await Page.create(session, capture=False, track_network=False)  # no Runtime.enable leak
    await apply_stealth(page)                                # fingerprint init-scripts, before goto
    await page.goto("https://example.com")
```

`apply_stealth(page, evasions=[...])` selects a subset; keyword overrides
(`webgl_vendor`, `webgl_renderer`, `languages`, `vendor`,
`hardware_concurrency`) tune the spoofed values.

For behavioral realism (the more durable signal — how input *moves*, all over
trusted Input events):

```python
el = await page.query("#submit")
await el.human_click()             # curved, eased path to a near-center point
await page.human_type("hello@example.com")   # per-key events, human cadence
await page.human_scroll(1200)      # eased wheel steps, not one jump
```

Since purecdp drives a *real* browser, its TLS/HTTP2 (JA3/JA4) fingerprint is
already authentically Chrome's — no spoofing needed, unlike browserless HTTP
scrapers.

## Driving with an LLM (agent surface)

`page.snapshot()` returns a compact, ref-tagged view of the page from the
accessibility tree — small and stable enough to hand an LLM, instead of raw
HTML or screenshots. Each control gets a session ref (`e1`, `e2`, …); password
fields are masked.

```python
snap = await page.snapshot()
print(snap)
# - RootWebArea "Login"
#   - textbox "Email" [e1]
#   - textbox "Password" = "••••••" [e2]
#   - button "Sign in" [e3]

await page.act("e1", "fill", text="me@example.com")   # fill/type/select/click/…
await page.act("e3", "click")
el = await page.element_for_ref("e1")                  # escape hatch: full Element API
```

An optional MCP server exposes this to any Model Context Protocol host — no
extra dependency, stdlib JSON-RPC over stdio:

```sh
python -m purecdp.mcp            # tools: navigate, snapshot, act, evaluate
python -m purecdp.mcp --stealth  # low-observability launch
```

## Layout

```
spec/               vendored protocol JSON + PIN (source commit)
src/purecdp/            the library (src layout)
src/purecdp/_generator/ code generator (dev tool, reads spec/, writes src/purecdp/protocol/)
src/purecdp/protocol/   GENERATED bindings — do not edit by hand
tests/              tests and test utilities
```

## Regenerating the bindings

```sh
python -m purecdp._generator          # uses spec/ and src/purecdp/protocol/ defaults
```

Update `spec/*.json` + `spec/PIN` from a devtools-protocol checkout first when moving the pin.

## Running tests

Stdlib only — no test dependencies:

```sh
python -m unittest              # discovery from repo root
python tests/test_protocol.py   # single file works too
```

`python -m pytest` also works (nicer output, subtest counts) but is never required.
