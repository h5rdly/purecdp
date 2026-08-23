# purecdp by example

Runnable, copy-pasteable recipes grouped by what you're trying to do. Every
example is stdlib-only at runtime (the `[faster]` accelerators are optional and
kick in automatically when present). See [README.md](README.md) for the tour and
[DESIGN.md](DESIGN.md) for the why.

The layers, low to high:

- **`purecdp.protocol`** — generated typed bindings, one module per CDP domain.
- **`purecdp` core** — sans-I/O engine + asyncio `Connection`/`Session` + `launch`/`connect`.
- **`purecdp.testing`** — `Page`, the agent surface (`snapshot`/`act`), stealth, frames, MCP.

Jump to:

- [1. Basics](#1-basics) · [2. Low-level protocol flows](#2-low-level-protocol-flows)
- [3. Testing a frontend](#3-testing-a-frontend) · [4. Low-observability / stealth](#4-low-observability--stealth)
- [5. Driving with an LLM (agent surface)](#5-driving-with-an-llm-agent-surface)
- [6. Cross-origin iframes](#6-cross-origin-iframes) · [7. Connecting to a browser you didn't launch](#7-connecting-to-a-browser-you-didnt-launch)
- [8. Grab bag](#8-grab-bag) · [What purecdp is good for](#what-purecdp-is-good-for)

---

## 1. Basics

### The one-liner: `launched_page`

`launched_page` launches a browser, hands you `(browser, page)` with the `Page`
fully armed (Page/Runtime/Network enabled, console + network capture running),
and always tears everything down.

```python
import asyncio
from purecdp.testing import launched_page

async def main():
    async with launched_page() as (browser, page):   # finds Chromium/Chrome/Brave
        await page.goto("https://example.com", wait="load")
        print(await page.title())
        print(await page.evaluate("document.querySelector('h1').textContent"))
        await page.screenshot("example.png")

asyncio.run(main())
```

`page.evaluate` awaits promises by default and raises `JSError` on an exception
or rejection. `wait=` is `"load"` (default), `"idle"` (load + network quiet), or
`"none"` (return as soon as navigation starts). Prefer `"load"` on public
pages: `"idle"` is for apps you control — a third-party-heavy page (ads,
analytics, long-polls) may never go network-quiet, and the timeout will tell
you so. A failed navigation raises `NavigateError` carrying Chrome's raw net
error (`exc.net_error`, e.g. `net::ERR_CONNECTION_TIMED_OUT`) — including when
Chrome silently commits to its own error page instead of reporting one.

### Passing values into `evaluate` — as arguments, never by interpolation

With positional args the expression is a **function declaration**, called with
your values via `Runtime.callFunctionOn` — they travel as protocol arguments,
so no quoting, no `%`-escaping nested braces, no injection risk:

```python
await page.evaluate("(a, b) => a + b", 2, 3)                       # -> 5
await page.evaluate("(sel) => !!document.querySelector(sel)", selector)
await page.evaluate("(o) => o.items.length", {"items": [1, 2, 3]}) # dicts/lists fine

el = await page.query("#row")
await page.evaluate("(el, cls) => el.classList.add(cls)", el, "active")
```

Args must be JSON-serializable — or `Element` handles, which arrive as live
DOM nodes (last line; `el.eval("(el, ...) => ...", ...)` is the same thing
scoped to the element). The old way still works but don't reach for it:

```python
# the anti-pattern: interpolating values into JS source. One quote in
# `needle` and this breaks — or worse, executes it.
await page.evaluate(f"document.title.includes({json.dumps(needle)})")
# instead:
await page.evaluate("(n) => document.title.includes(n)", needle)
```

Every exception purecdp raises — `JSError`, `NavigateError`, `DownloadError`,
`ExpectationError`, `ActionabilityError`, `CDPCommandError`,
`CDPConnectionClosed`, and the rest — inherits `PureCDPError`, so
`except PureCDPError:` catches anything from the library. (`ExpectationError`
also inherits `AssertionError`, so a failed `.should(...)` still registers as a
test *failure*.)

For long-running loops there is one more distinction that matters: *is this
worth retrying?* `CDPSessionClosed` and `CDPConnectionClosed` share the
`CDPClosedError` base — they mean the tab or browser is gone for good — and
`page.alive` is the same fact as a property (purely local, no round-trip):

```python
while page.alive:                      # can't spin on a dead tab
    try:
        await check_something(page)
    except CDPClosedError:
        break                          # tab/browser gone — give up
    except PureCDPError:
        await asyncio.sleep(5)         # transient — back off and retry
```

### Waiting, clicking, reading

```python
async with launched_page() as (browser, page):
    await page.goto("https://news.ycombinator.com", wait="load")

    await page.wait_for(".titleline")
    first = await page.text(".titleline a")          # textContent of the first match
    count = await page.query_count(".titleline")
    print(count, "stories; top:", first)

    # hold an element, filtered by visible text, then click it
    more = await page.query("a", containing="More", index=-1)
    await more.click()
```

`query(...)` polls (riding out SPA re-renders), can filter by `containing=` —
a case-insensitive substring (the `:has-text()` CDP never had) or a
`re.Pattern` tested against the trimmed text (anchors give exact match) — and
picks by `index` (`-1` = newest, for apps that leave stale copies of a widget
in the DOM).

### `wait_for`: appear, disappear, or any state in between

One method, the same condition vocabulary as `.should()`. The two axes to
know: **`count` is the exists axis, `visible` is the rendered axis** — SPAs
*hide* rather than remove, so existence checks lie about what a user can see:

```python
await page.wait_for(".results")                    # present (hidden counts)
await page.wait_for(".spinner", visible=False)     # no VISIBLE match (gone or hidden)
await page.wait_for(".spinner", count=0)           # strictly absent from the DOM
await page.wait_for(".modal", visible=True)        # actually rendered
await page.wait_for(".row", count=3, text="done")  # any should() combination
```

### When a query fails, the error tells you what WAS there

A timed-out `query`/`wait_for`/locator raises `QueryTimeout` (still a
`TimeoutError`, also a `PureCDPError`) whose message is the diagnosis, not a
shrug — three forms:

```text
no '.chip' containing 'Search' within 5s — 4 visible '.chip':
'Search orders' | 'Search help' | 'Orders' | 'Settings'   # wrong text: near-misses listed
no element for '#submit' within 5s — 2 match but none are visible …  # hidden ≠ absent
no element for '#submit' within 5s — selector matches nothing        # actually absent
```

Structured too (`exc.matched`, `exc.visible_count`, `exc.candidates`) — and
since the MCP `act`-by-description tool rides the same path, an agent's failed
click comes back with the candidate captions instead of "timed out" (the error
text is all the model sees).

### Enumerating a set: `query_all`

`query_all` returns *all* matches as held `Element`s (single-shot, no polling) —
for iterating or filtering a group. Each is a real handle, so you can read
properties off it and act on it without a stale-copy race.

```python
# every enabled chip, filtered by a live property read
chips = await page.query_all("[data-testid=filter] button")
enabled = [c for c in chips if not await c.eval("(el) => el.disabled")]
await enabled[-1].mouse_click()

# scoped to a held element, with a text filter (case-insensitive)
card = await page.query(".react-flow__node", containing="caucasian")
(remove_btn,) = await card.query_all("button", containing="remove")
await remove_btn.mouse_click()
```

Use `query` for one element *with* waiting, `query_count` to just count, and
`query_all` to work with the whole set.

---

## 2. Low-level protocol flows

When you want the raw protocol — no `Page` opinions. You drive a `Session` with
generated command objects and typed events. This is the whole library under the
helpers.

```python
import asyncio
import purecdp
from purecdp.protocol import page, runtime

async def main():
    async with await purecdp.launch(headless=True) as browser:
        session = await browser.new_session()           # create target + attach

        await session.execute(page.enable())
        # subscribe BEFORE triggering, or you race the event
        waiter = asyncio.create_task(session.wait_for(page.LoadEventFired))
        await asyncio.sleep(0)
        await session.execute(page.navigate(url="https://example.com"))
        await waiter

        title, _details = await session.execute(
            runtime.evaluate(expression="document.title", return_by_value=True))
        print(title.value)

asyncio.run(main())
```

`launch()` takes the knobs a local run usually needs: `headless=`, `pipe=True`
(a debug pipe instead of a websocket), `stealth=True` (see below),
`ignore_https_errors=True` (accept self-signed / local-HTTPS certs), a
persistent `user_data_dir=`, and `extra_args=[...]` for anything else.

A reused `user_data_dir` gets two guards before the spawn: if a live browser
still holds the profile (its `SingletonLock` names a running PID — common
after a headed run), `launch()` raises `BrowserLaunchError: profile … in use
by PID N` up front instead of letting the new browser rendezvous-and-exit
into a confusing downstream error; and the previous run's stale
`DevToolsActivePort` is deleted so the endpoint wait can't connect to a dead
port. When a launch does fail, the exception quotes the tail of the
browser's own stderr — the "profile appears to be in use" / missing-library
/ sandbox complaints land in the message instead of `/dev/null`.

Teardown is graceful-first: `aclose()` sends `Browser.close` over CDP —
which reaches the *real* browser process even when a launcher wrapper
re-exec'd away from the child we spawned — and only falls back to signals
if the browser doesn't reply and exit on its own. A browser that exits
cleanly releases its profile lock and records a normal exit (no "restore
pages?" bubble on the next headed run). `connect()`-ed browsers are never
closed, only detached from.

Know that plain `headless=True` advertises a `HeadlessChrome/…` user agent,
and some servers quietly refuse it — map tile servers are notorious: the page
works, the basemap renders blank, and it looks like a rendering bug. Fixes,
in order of preference: `launch(stealth=True)` (modern headless, no
`Headless` token), `page.strip_headless_ua()` (version-preserving override),
or `headless=False`.

Commands are unbounded by default — some are legitimately open-ended (a
`runtime.evaluate` awaiting a promise resolves when the promise does). To
bound one, or all of them, and turn a hung browser into a retriable
`CDPCommandTimeout` (a `TimeoutError` and a `PureCDPError`, naming the CDP
method that hung) instead of a killed process:

```python
await session.execute(page.capture_screenshot(), timeout=30)   # this one call
browser.connection.default_command_timeout = 60                # every command
```

### Event streams

`session.listen(*types)` gives an async iterator of typed events; it only
deserializes the events you asked for (the rest are dropped without ever being
parsed — that's most of the throughput win).

```python
from purecdp.protocol import network

async with await purecdp.launch() as browser:
    session = await browser.new_session()
    await session.execute(network.enable())

    stream = session.listen(network.ResponseReceived, buffer_size=4096)
    asyncio.create_task(drive_the_page(session))     # your navigation etc.
    async for event in stream:
        print(event.response.status, event.response.url)
        if str(event.response.url).endswith("/done"):
            break
    stream.close()
```

### Auto-attach: popups, workers, OOPIFs

`connection.set_auto_attach()` attaches child targets automatically (flat mode)
and resumes paused ones for you.

```python
async with await purecdp.launch() as browser:
    conn = browser.connection
    await conn.set_auto_attach(wait_for_debugger=False)
    # every new tab/popup/worker/iframe target now attaches on its own Session,
    # reachable via conn.sessions; hook it with conn.add_session_hook(fn).
```

`pipe=True` on `launch` swaps the websocket for `--remote-debugging-pipe`
(POSIX); everything above is identical over the pipe.

---

## 3. Testing a frontend

### The stdlib way: `CDPTestCase`

Each test gets a fresh browser, an isolated context, and `self.page`. Skips
(doesn't fail) when no browser binary is found. Tests use plain `assert` — no
`self.assertX`, and don't run under `-O`.

```python
from purecdp.testing import CDPTestCase

class LoginTests(CDPTestCase):
    async def test_shows_error_on_bad_password(self):
        await self.page.goto("https://myapp.example/login", wait="idle")
        await self.page.set_value("#email", "me@example.com")   # React-safe write
        await self.page.set_value("#password", "wrong")
        await self.page.query("button[type=submit]")            # wait for it
        await self.page.click("button[type=submit]")
        assert "Invalid" in await self.page.text(".error")
        assert not self.page.js_errors                          # no uncaught JS
```

Prefer pytest? The optional plugin (never a dependency) provides `cdp_browser`
/ `cdp_page` and runs `async def` tests itself:

```python
async def test_title(cdp_page):
    await cdp_page.goto("data:text/html,<title>hi</title>")
    assert await cdp_page.title() == "hi"
```

### Typing: which call fires which events

The classic silent failure: a framework autocompletes on `keydown`, you write
into the field some fast way, `.value` looks perfect — and the fetch never
fires, because nothing ever pressed a key. Know what each call emits:

| call | key events | input events | speed | use when |
|---|---|---|---|---|
| `set_value` | none | synthetic `input`+`change` | instant | controlled (React) inputs, bulk state |
| `el.type(text, insert=True)` | none | one real `input` | 1 round trip | big text, key events unwanted |
| `el.type(text)` | per char | per char | fast | **default** — anything listening to keys |
| `page.human_type` | per char | per char | human cadence | stealth / behavioral realism |
| `page.press("Enter")` | one | if printable | — | submit, shortcuts |

`type()` sends real per-key events by default (0.7.0 — the surprising
behavior became the opt-in, not the default).

### Failures leave artifacts behind

When a `CDPTestCase` test (or a pytest `cdp_page` test) fails, purecdp dumps
what you need to diagnose it — captured *before* the browser closes:

```
purecdp-artifacts/tests.test_app.CartTests.test_checkout/
    info.txt          # outcome + traceback + per-page URL/title + capture status
    screenshot.png    # full-page, one per page the test opened
    page.html         # the DOM at the moment of failure
    console.log       # captured console messages (when any)
    js_errors.log     # uncaught page exceptions (when any)
    network.log       # recorded traffic (see below)
    network.har       # the same capture as a HAR — open in devtools
```

Green tests write nothing. Every capture is best-effort: a crashed renderer
still yields a manifest saying exactly what could be saved, and the dump can
never turn a clean test failure into a confusing teardown error.

Knobs (class attributes; the pytest fixture uses the env equivalents):

```python
class CartTests(CDPTestCase):
    ARTIFACTS = True           # default; PURECDP_NO_ARTIFACTS=1 forces off
    ARTIFACTS_DIR = None       # default $PURECDP_ARTIFACTS_DIR or ./purecdp-artifacts
    ARTIFACTS_NETWORK = True   # also record traffic on every page so failures
                               # include it (off by default; env:
                               # PURECDP_ARTIFACTS_NETWORK=1 for cdp_page)
```

Traffic your test records itself with `page.record()` is dumped regardless of
`ARTIFACTS_NETWORK`. On CI, ship the directory with failing runs:

```yaml
- uses: actions/upload-artifact@v4
  if: failure()
  with: { path: purecdp-artifacts/, if-no-files-found: ignore }
```

`dump_artifacts(pages, dest, exc=...)` is also importable directly for ad-hoc
scripts (call it in your `except` block while the browser is still up).

### Locators (`live`) + auto-retrying assertions — the robust way for SPAs

`page.live(...)` returns a **lazy locator**: it holds the *query*, not a node, and
re-runs it on every action and assertion. That's the fix for the classic
single-page-app flake — a React re-render, a virtual list remount, a chat UI that
keeps stale copies of a widget won't leave you holding a dead handle.

`.should(**conditions)` polls until the conditions hold (or times out), so you
never hand-roll `wait_for` loops. It raises on failure, so it fits plain `assert`
tests — no special assertion objects.

```python
import re
from purecdp.testing import CDPTestCase

class TodoAppTests(CDPTestCase):
    async def test_add_and_remove_items(self):
        await self.page.goto("https://app.example/todos", wait="idle")

        await self.page.get_by_test_id("new-item").fill("buy milk")
        await self.page.get_by_role("button", name="Add").click()

        # poll until the state settles — no manual waiting
        await self.page.live(".todo-item").should(count=1)
        await self.page.get_by_test_id("summary").should(text=re.compile(r"\d+ of \d+ done"))

        # re-render safe: this re-resolves each call, even if the node remounted
        item = self.page.live(".todo-item", containing="milk")
        await item.live("button").last.click()          # its delete button
        await item.should(count=0)
```

Building blocks:

```python
# construct
page.live("css selector", containing="text")   # substring filter, case-insensitive
page.live(".node", containing=re.compile(r"^AND$"))  # regex on the trimmed text (exact match via anchors)
page.get_by_test_id("column-picker")            # [data-testid="..."]
page.get_by_text("Continue")                    # innermost element with that text
page.get_by_role("button", name="Continue")     # approximate ARIA role + accessible name
page.get_by_label("Minimum value")              # form control by its label

# refine (each returns a new locator — immutable)
loc.first ; loc.last ; loc.nth(2) ; loc.containing("x") ; loc.live("button")

# act (re-resolves the first match, auto-waits for actionability)
await loc.click()                 # trusted mouse click; .click(trusted=False) resolves wrappers
await loc.fill("1000")            # React-safe value write
await loc.type("hello") ; await loc.hover() ; await loc.select("opt")
await loc.text() ; await loc.value() ; await loc.count()
el = await loc.get()              # escape hatch to the eager Element API

# assert (polls until all hold, else raises ExpectationError = a test failure)
await loc.should(visible=True, text="Invalid")
await loc.should(count=0)                        # asserts it's gone
await loc.should(value="1000", enabled=True)

# observe (one round trip, no waiting, no raising — for accumulate-and-continue
# suites that record PASS/FAIL themselves instead of failing fast)
obs = await loc.probe()   # {count, present, visible, enabled, checked, text, value}
```

`query` / `query_all` / `Element` remain as the lower-level eager layer; `live` is
the ergonomic default. (It works inside cross-origin frames too — `frame.live(...)`.)

### Stub the network — assert on behavior, not a live backend

```python
class CartTests(CDPTestCase):
    async def test_empty_cart(self):
        # path-keyed mocks; a trailing "/" is a prefix match, anything else exact
        await self.page.mock_api({
            "/api/cart": {"items": []},
            "/api/user/": lambda path: {"id": path.rsplit("/", 1)[-1]},
        })
        await self.page.goto("https://shop.example/cart", wait="idle")
        await self.page.wait_for("#empty-state")
```

For full control over a request, use `route()` directly. Its pattern is an
fnmatch glob or a `callable(url) -> bool` predicate (the same form `record()`
takes), so a match rule too fiddly for a glob is just a function:

```python
async def handler(request):
    if request.method == "POST":
        await request.fulfill(status=201, json={"ok": True})
    else:
        await request.abort()                # or request.continue_(url=...)
await self.page.route("*/api/orders", handler)
await self.page.route(lambda u: u.endswith("/track") and "beacon" not in u, handler)
```

### Assert on the traffic itself

`page.record()` captures full exchanges (request body, status, response body)
for URLs matching a filter — "assert on the traffic, not the DOM". (It's
synchronous — the recorder starts listening immediately — but `await
page.record()` also works and is a no-op, because on an all-async `Page`
everyone types it eventually.)

```python
rec = self.page.record(needle="/api/track")
turn = rec.expect()                          # arm BEFORE the trigger
await self.page.click("#buy")
ex = await turn.value                        # blocks until the next match lands
assert ex.request_json["event"] == "purchase"
assert ex.status == 200
```

An `Exchange` is **flat** — no `ex.request.method` nesting to guess at:

```python
ex.url, ex.method, ex.status, ex.mime          # the one-line summary
ex.request_body, ex.body                       # raw: str | None, bytes | None
ex.response_body                               # alias for ex.body (symmetry)
ex.request_json, ex.json, ex.text              # parsed conveniences
ex.request_headers, ex.response_headers        # wire-truth dicts (see below)
ex.failed, ex.body_error, ex.redacted          # what went wrong / was withheld
ex.timestamp, ex.duration                      # epoch seconds; total seconds
```

`expect(json=True)` resolves to the first new exchange whose body parses as
JSON, skipping interleaved non-JSON responses (a dev proxy's HTML error page)
— after `.value` resolves, `turn.skipped` lists exactly what was passed over
(log it if you care), the skipped ones stay in `rec.exchanges`, and `turn.new`
lists everything captured since arming. The lower-level
`rec.wait_for_next(previous_len)` is still there when you want to walk
exchanges by index (burst-safe: exchanges landing together come back one per
call, oldest first).

A recorder can carry its own wait default — an endpoint's latency profile has
nothing to do with the page's DOM default, so declare it once where the
endpoint is named instead of remembering `timeout=` at every `expect()`:

```python
llm = self.page.record(needle="/api/chat", default_timeout=120)   # slow route
prices = self.page.record(needle="/api/prices")                   # page default is fine
```

When a wait comes up empty it raises `ExpectTimeout` (a `TimeoutError` and a
`PureCDPError`), and the message reports what the recorder DID see instead of
a bare "timed out": exchanges completed since arming but skipped by
`json=True`, requests still in flight (sent, no response — a hung endpoint
looks exactly like this), and how many requests never matched the filter —
exactly what you need when reverse-engineering someone else's page:

```
ExpectTimeout: no JSON-bodied exchange matching '/GetObjectInfo' completed
within 10s; 2 still in flight (request sent, no response): https://x/api/slow;
4 requests never matched the recorder's filter
```

Exchanges carry the **wire** headers (`ex.request_headers` / `ex.response_headers`
— the `*ExtraInfo` events merged in, so `Cookie`, `Origin`, `Sec-*` and
`Set-Cookie` are all visible), plus `ex.timestamp` (epoch seconds) and
`ex.duration`. The whole capture exports as a standard HAR — `rec.har()` for
the dict, `rec.save_har("capture.har")` for a file any devtools or HAR viewer
opens — making captures shareable and diffable. Failing tests get one for
free: the artifacts dump writes `network.har` next to `network.log`.

To ask whether a request went wrong, check `ex.failed` (a net error / abort /
block, from the browser) — **not** `ex.body_error`. A bodyless response (a 204
preflight, a 304, a HEAD) simply has no body to fetch, so `ex.body` is `None`
and *neither* field is set; `body_error` fires only when a body was expected
but couldn't be retrieved:

```python
assert not ex.failed                 # the request itself succeeded
assert ex.status == 204              # a preflight — bodyless, and that's fine
```

### Private recording — visibility without the secrets

Recording a card-entry or login flow shouldn't mean the PAN or password lands
in memory, an artifacts dump, or a HAR. Two levers, both applied **at
capture** — the secret never enters the process:

```python
rec = self.page.record(bodies=False)         # metadata-only, recorder-wide:
                                             # URL/method/status/headers/timing/
                                             # sizes — no bodies, either side

rec = self.page.record(                      # full privacy for matching URLs:
    redact=lambda url: "/payment" in url)    # no bodies AND credential headers
                                             # (Authorization, Cookie, Set-Cookie)
                                             # masked to "<redacted>"
```

A withheld exchange has `ex.redacted == True`, `body_error` stays `None` (a
choice, not a failure), and the byte counts survive — `ex.request_body_size`
and `ex.body_size` (the response's total wire length). Reading `.json`/`.text`
on one raises with a message that says *redacted*, `expect(json=True)` counts
it as skipped, and `rec.har()` exports sizes plus a `"body redacted"` comment
— never the bytes. Other exchanges on the same recorder are untouched, so you
keep watching the traffic *around* the sensitive step instead of turning the
recorder off.

For a streamed (`text/event-stream`) response, `parse_sse` turns the captured
body into events (any per-frame encoding — base64, JSON — is yours to decode):

```python
from purecdp.testing import parse_sse

ex = await turn.value
for event in parse_sse(ex.text):
    if event.event == "done":
        break
    handle(event.data)
```

### Capture a download

`page.expect_download()` catches a file download triggered inside the block and
hands back the bytes — no writing to a real Downloads folder, no polling the
disk.

```python
async with self.page.expect_download() as info:
    await self.page.click("#export-csv")     # the trigger
download = await info.value
assert download.suggested_filename == "report.csv"
rows = (await download.read()).decode().splitlines()
assert len(rows) == expected_row_count
# await download.save_as("out.csv")          # keep it, if you want
```

It enables downloads for the whole connection, so pair it with a browser/context
you own (the `CDPTestCase` default); raises `DownloadError` on cancel/timeout.

### Reuse a logged-in session across runs

```python
# once, after logging in:
state = await page.storage_state()           # cookies + localStorage
pathlib.Path("state.json").write_text(json.dumps(state))

# later runs — skip the login flow entirely:
await page.goto("https://myapp.example/")    # need the origin first for localStorage
await page.set_storage_state(json.loads(pathlib.Path("state.json").read_text()))
```

Other testing niceties: `page.expect_navigation()` (await a click-triggered
load without racing it), `page.expect_download()` (capture a download),
`page.query_all()` (enumerate/filter a set of held elements),
`page.handle_dialogs()` (auto-accept alerts/confirms), `page.seed_session_storage()`
/ `add_init_script()` (plant state before the SPA boots), `page.console` /
`page.js_errors` (always-on capture).

---

## 4. Low-observability / stealth

Honest framing: these reduce common, cheap detection signals — **not** a promise
of undetectability. CreepJS-class realm-diff fingerprinting and TLS/HTTP2 (JA3/
JA4) signatures still identify automation; no init script touches the network
stack. Treat it as "no cheap automation tells", not "invisible". Because purecdp
drives a *real* browser, its TLS fingerprint is already authentically Chrome's.

Three layers, strongest when combined:

```python
import purecdp
from purecdp.testing import Page, apply_stealth

async with await purecdp.launch(stealth=True) as browser:  # AutomationControlled off, headless=new
    session = await browser.new_session()
    # skip Runtime.enable — enabling it is itself detectable (isAutomatedWithCDP)
    page = await Page.create(session, capture=False, track_network=False)
    await apply_stealth(page)                              # fingerprint init-scripts, BEFORE goto
    await page.goto("https://example.com")
```

Pick a subset and tune spoofed values:

```python
await apply_stealth(
    page,
    evasions=["navigator.webdriver", "webgl.vendor", "navigator.plugins"],
    webgl_vendor="Intel Inc.",
    webgl_renderer="Intel Iris OpenGL Engine",
    languages=("en-GB", "en"),
)
```

`workers=True` (default) injects the same patches into worker scope so a
worker's WebGL/navigator values agree with the page's (a mismatch is a known
tell). The opt-in extras — `navigator.maxTouchPoints`, `navigator.connection`,
`function.toString` — need a deliberate choice (the last is itself a realm-diff
tell) and are off by default.

### Behavioral realism (the more durable signal)

How input *moves* — all over trusted CDP Input events, not JS:

```python
el = await page.query("#submit")
await el.human_click()                    # curved, eased path to a near-center point
await page.human_type("hello@example.com")   # per-key events, human cadence
await page.human_scroll(1200)             # eased wheel steps, not one jump
```

`page.cursor` is a stateful cursor whose position persists across moves.

---

## 5. Driving with an LLM (agent surface)

`page.snapshot()` returns a compact, ref-tagged view built from the
accessibility tree — small and stable enough to hand a model instead of raw HTML
or a screenshot. Each control gets a ref (`e1`, `e2`, …); password fields are
masked. `act()` performs one action on a ref; `element_for_ref()` is the escape
hatch to the full `Element` API.

```python
snap = await page.snapshot()
print(snap)
# - RootWebArea "Login"
#   - textbox "Email" [e1]
#   - textbox "Password" = "••••••" [e2]
#   - button "Sign in" [e3]

await page.act("e1", "fill", text="me@example.com")   # click/hover/focus/fill/type/select/scroll
await page.act("e3", "click")
```

`act(..., stable=True)` (the default) waits for the target to be actionable —
visible, enabled, settled across animation frames, and not obscured — before it
fires, raising `ActionabilityError` on timeout. It's the safety gate a model
needs on real, animated pages. Pass `stable=False` to fire immediately.

### Auto-dismiss cookie / consent banners

```python
page.add_auto_dismiss("#cookie-accept", ".consent button.agree")
# banners matching these are clicked away while any stable action waits for its target
await page.act("e5", "click")
```

### A minimal agent loop

```python
async def run_agent(page, model, goal):
    await page.goto(goal["url"], wait="idle")
    for _ in range(20):
        snap = await page.snapshot()
        step = await model.decide(goal["task"], str(snap))   # your LLM call
        if step["action"] == "done":
            return step.get("answer")
        await page.act(step["ref"], step["action"],
                       text=step.get("text"), values=step.get("values"))
```

### Expose it over MCP (no extra dependency)

A stdlib JSON-RPC-over-stdio Model Context Protocol server wrapping the same
surface. Tools: `navigate`, `snapshot`, `act`, and — off by default —
`evaluate`.

```sh
python -m purecdp.mcp                 # headless; tools: navigate, snapshot, act
python -m purecdp.mcp --headful       # visible window
python -m purecdp.mcp --stealth       # low-observability launch
python -m purecdp.mcp --allow-eval    # also expose arbitrary-JS evaluate (sharp edge)
```

`evaluate` runs arbitrary page JavaScript, so it's gated behind `--allow-eval`
(neither advertised nor callable otherwise).

---

## 6. Cross-origin iframes

A cross-origin (out-of-process) iframe runs in its own renderer with its own CDP
session, so the page's JS can't reach into it. `page.frame(...)` correlates the
`<iframe>` to that session and returns a `Frame` — the same
query/evaluate/snapshot/act surface as `Page`, scoped inside the frame, with
trusted Input routed to the frame's own session.

```python
from purecdp.testing import FrameNotFound

async with launched_page() as (browser, page):
    await page.goto("https://host.example/checkout", wait="idle")

    # identify by CSS selector for the <iframe> (robust) or url= (fnmatch)
    pay = await page.frame("iframe#payment", timeout=5)
    await pay.query("input[name=card]")
    await pay.evaluate("document.querySelector('input[name=card]').value = '4242...'")

    # nest to any depth — auto-attach has already propagated down
    inner = await pay.frame("iframe.threeds")
```

Only genuinely cross-origin iframes need this; same-origin frames stay reachable
from the page's own JS. First `frame()` call arms flat auto-attach on the page;
pages that never call it pay nothing.

### One stitched snapshot across every frame

`snapshot(cross_frame=True)` splices out-of-process iframes into a single
outline, so a consent/payment iframe's controls appear inline and `act(ref)`
reaches into them transparently — the agent never has to know a frame boundary
was there.

```python
snap = await page.snapshot(cross_frame=True)
print(snap)                       # controls from the top doc AND the OOPIFs, one tree
await page.act("e7", "fill", text="4242424242424242")   # ref lived in the payment iframe
```

The default (`cross_frame=False`) is the plain single-frame snapshot with zero
extra cost.

---

## 7. Attaching to a browser you didn't launch

`connect()` attaches over CDP to an already-running browser. The headline use
is a **privacy/authorization boundary**: a human logs in — password, MFA,
consent — in their own browser window, and the agent *attaches afterwards*.
The credentials never touch the automation; the agent inherits the
authenticated session and nothing else. Unlike `launch`, `connect()` neither
starts nor owns the browser: `aclose()` just detaches and leaves it running.

```python
import purecdp

# the human ran:  brave --remote-debugging-port=9222   and logged in
async with await purecdp.connect("http://127.0.0.1:9222") as browser:
    page = await browser.new_page()           # NEW tab, but the SAME profile:
    await page.goto("https://portal.example/dashboard")   # in, no login flow

# equivalent endpoint forms: host=/port= kwargs, or a browser-level ws://
# URL straight through (Chrome-in-Docker, a browserless/cloud session):
browser = await purecdp.connect(host="127.0.0.1", port=9222)
browser = await purecdp.connect("ws://127.0.0.1:9222/devtools/browser/abc123")
```

`new_page()` is `new_session()` + `Page.create()` in one call (kwargs forward:
`default_timeout`, `capture`, `track_network`; also on contexts —
`browser.new_page(context=ctx)`). Keep the two-step when you want the raw
`Session`.

`new_session()` beats hunting for "the tab the user logged in on" by URL —
a fresh tab in the same profile shares its cookies, and no other script is
fighting you for it. But when the *state you need lives in the open tab
itself* (an SPA mid-flow, an unsaved form), attach to it directly:

```python
page = await browser.attach(url_contains="kais")     # the tab, armed as a Page
page = await browser.attach(target_id="A1B2...")     # or by exact id
```

No match raises `TargetNotFound` listing the page targets that *do* exist;
several matches raise it too, with the candidate ids — pass `target_id=` to
pick one, because guessing would mean driving the wrong tab of a browser a
human may be using. Arming enables CDP domains on the tab; for a
low-observability attach pass `capture=False, track_network=False`.

For a raw listing, `purecdp.discovery.list_targets(port=9222)` returns every
target as a dict (`id`, `type`, `url`, `title`, `webSocketDebuggerUrl`); feed
an `id` to `connection.attach()` for a bare `Session` — and when you're done
with a tab, `browser.close_target(id)` closes it (works for targets you never
attached to; no hand-rolled `/json/close` HTTP calls).

---

## 8. Grab bag

Handy one-liners drawn from the `Page` surface.

```python
# Isolated contexts (cheap per-scenario isolation — own cookies/storage)
ctx = await browser.new_context()
p1 = await Page.create(await browser.new_session(context=ctx))

# Emulation
await page.set_viewport(390, 844, device_scale_factor=3, mobile=True)
await page.emulate_media(color_scheme="dark", reduced_motion="reduce")
await page.set_geolocation(48.8566, 2.3522)
await page.set_timezone("Europe/Paris")

# PDF (headless only) and full-page screenshot
await page.pdf("out.pdf", landscape=True, print_background=True)
await page.screenshot("full.png", full_page=True)

# Inject before the app boots
await page.add_init_script("window.__TEST__ = true;")
await page.add_style_tag(content="* { transition: none !important; }")  # kill animations

# Keyboard: real key events (synthetic setters never type Enter)
await (await page.query("#search")).focus()
await page.insert_text("purecdp")
await page.press("Enter")

# Cookies
await page.set_cookies([{"name": "sid", "value": "abc", "url": "https://x.example"}])
```

---

## What purecdp is good for

- **End-to-end / integration testing** of web frontends — `CDPTestCase` or the
  pytest plugin, network stubbing, traffic assertions, isolated contexts. Zero
  test dependencies.
- **LLM browser agents** — the ref-tagged `snapshot`/`act` surface (plus the MCP
  server) is purpose-built to hand a page to a model and let it act, across frame
  boundaries.
- **Scraping / automation that shouldn't advertise itself** — the stealth base +
  behavioral realism, honestly scoped.
- **Bots and flows over a real browser** — form filling, file uploads, PDF
  generation, screenshotting, dialog handling.
- **Driving cloud / remote browsers** — `connect()` to browserless, Docker, or any
  `--remote-debugging-port` endpoint.
- **A clean CDP substrate** — the sans-I/O engine + typed protocol bindings are a
  dependency-free base to build your own higher-level tooling on.

Not a goal: defeating mature anti-bot fingerprinting (see the stealth honesty
note), or a synchronous / Selenium-style API — purecdp is asyncio-native.
