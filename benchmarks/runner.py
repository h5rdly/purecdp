'''One engine, one process: time the same page operations and report JSON.

    python benchmarks/runner.py --impl purecdp|rustwright --url URL --browser BIN --n 40

Driven by orchestrate.py; runnable by hand. Both engines get the same browser
binary, headless, default launch args, one warm browser for every op after
"launch". Medians in milliseconds; RSS in MiB (client process + the browser
process tree it spawned).
'''

import argparse
import json
import os
import resource
import statistics
import sys
import time

BIG_EVAL = "Array.from({length: 20000}, (_, i) => 'item-' + i)"


def ms(samples):
    return round(statistics.median(samples) * 1000, 3)


def rss_mib(pid=None):
    try:
        with open(f'/proc/{pid or "self"}/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def tree_rss_mib(root=None):
    '''Sum of VmRSS over every descendant of ``root`` (default: this process).'''
    root = root or os.getpid()
    children: dict[int, list[int]] = {}
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            with open(f'/proc/{name}/status') as f:
                ppid = next(int(l.split()[1]) for l in f if l.startswith('PPid:'))
        except (OSError, StopIteration):
            continue
        children.setdefault(ppid, []).append(int(name))
    total, stack = 0.0, list(children.get(root, []))
    while stack:
        pid = stack.pop()
        total += rss_mib(pid) or 0.0
        stack.extend(children.get(pid, []))
    return round(total, 1)


def timed(fn, n):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t)
    return ms(out)


def per_call(fn, calls=40, blocks=5):
    '''Median over ``blocks`` of the mean per-call time of ``calls`` tight calls.'''
    out = []
    for _ in range(blocks):
        t = time.perf_counter()
        for _ in range(calls):
            fn()
        out.append((time.perf_counter() - t) / calls)
    return ms(out)


# ---------------------------------------------------------------- purecdp

async def run_purecdp(url, browser_path, n):
    t0 = time.perf_counter()
    import purecdp
    results = {'import_ms': round((time.perf_counter() - t0) * 1000, 1),
               'version': purecdp.__version__}

    async def launch_cycle():
        b = await purecdp.launch(browser_path)
        p = await b.new_page()
        await p.evaluate('1')
        await b.aclose()
    samples = []
    for _ in range(3):
        t = time.perf_counter(); await launch_cycle(); samples.append(time.perf_counter() - t)
    results['launch_ms'] = ms(samples)

    browser = await purecdp.launch(browser_path)
    page = await browser.new_page()
    await page.goto(url)

    async def atimed(coro_fn, count):
        out = []
        for _ in range(count):
            t = time.perf_counter(); await coro_fn(); out.append(time.perf_counter() - t)
        return ms(out)

    async def aper_call(coro_fn, calls=40, blocks=5):
        out = []
        for _ in range(blocks):
            t = time.perf_counter()
            for _ in range(calls):
                await coro_fn()
            out.append((time.perf_counter() - t) / calls)
        return ms(out)

    results['goto_ms'] = await atimed(lambda: page.goto(url), n)
    results['eval_ms'] = await aper_call(lambda: page.evaluate('1 + 1'))
    results['eval_arg_ms'] = await aper_call(lambda: page.evaluate('(a) => a + 1', 41))
    results['title_ms'] = await atimed(page.title, n)
    results['client_rss_light_mib'] = round(rss_mib(), 1)   # after the CDP-bound ops only

    async def text():
        el = await page.query('#status')
        return await el.text()
    results['query_text_ms'] = await atimed(text, n)

    async def click():
        await (await page.query('#btn')).click()
    results['click_ms'] = await atimed(click, n)

    async def fill():
        await page.set_value('#name', 'purecdp')
    results['fill_ms'] = await atimed(fill, n)
    results['snapshot_ms'] = await atimed(lambda: page.snapshot(engine='native'), 10)
    results['snapshot_walker_ms'] = await atimed(lambda: page.snapshot(engine='walker'), 10)
    results['screenshot_ms'] = await atimed(page.screenshot, 10)
    results['big_eval_ms'] = await atimed(lambda: page.evaluate(BIG_EVAL), 10)
    results['client_rss_mib'] = round(rss_mib(), 1)
    results['client_maxrss_mib'] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    results['browser_tree_rss_mib'] = tree_rss_mib()
    await browser.aclose()
    return results


# ---------------------------------------------------------------- rustwright

def run_rustwright(url, browser_path, n):
    t0 = time.perf_counter()
    import rustwright
    from rustwright import sync_playwright
    results = {'import_ms': round((time.perf_counter() - t0) * 1000, 1),
               'version': getattr(rustwright, '__version__', None) or _dist_version('rustwright')}
    with sync_playwright() as p:
        def launch_cycle():
            b = p.chromium.launch(headless=True, executable_path=browser_path)
            pg = b.new_page()
            pg.evaluate('1')
            b.close()
        results['launch_ms'] = timed(launch_cycle, 3)

        browser = p.chromium.launch(headless=True, executable_path=browser_path)
        page = browser.new_page()
        page.goto(url)
        results['goto_ms'] = timed(lambda: page.goto(url), n)
        results['eval_ms'] = per_call(lambda: page.evaluate('1 + 1'))
        results['eval_arg_ms'] = per_call(lambda: page.evaluate('(a) => a + 1', 41))
        results['title_ms'] = timed(page.title, n)
        results['client_rss_light_mib'] = round(rss_mib(), 1)
        results['query_text_ms'] = timed(lambda: page.query_selector('#status').text_content(), n)
        # force=True / no_wait_after skip Playwright's actionability + navigation
        # waits — the closest to purecdp's synthetic click, still not identical
        results['click_ms'] = timed(lambda: page.click('#btn', force=True, no_wait_after=True), n)
        results['fill_ms'] = timed(lambda: page.fill('#name', 'rustwright', force=True, no_wait_after=True), n)
        body = page.locator('body')
        results['snapshot_ms'] = timed(body.aria_snapshot, 10)
        results['screenshot_ms'] = timed(page.screenshot, 10)
        results['big_eval_ms'] = timed(lambda: page.evaluate(BIG_EVAL), 10)
        results['client_rss_mib'] = round(rss_mib(), 1)
        results['client_maxrss_mib'] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
        results['browser_tree_rss_mib'] = tree_rss_mib()
        browser.close()
    return results


def _dist_version(name):
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--impl', required=True, choices=['purecdp', 'rustwright'])
    ap.add_argument('--url', required=True)
    ap.add_argument('--browser', required=True)
    ap.add_argument('--n', type=int, default=40)
    args = ap.parse_args()
    if args.impl == 'purecdp':
        import asyncio
        results = asyncio.run(run_purecdp(args.url, args.browser, args.n))
    else:
        results = run_rustwright(args.url, args.browser, args.n)
    results['impl'] = args.impl
    print(json.dumps(results))


if __name__ == '__main__':
    main()
