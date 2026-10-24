'''Dogfood: purecdp's walker snapshot vs the native tree on real pages, as a
plain drive — the API a user writes, not MCP.

    python benchmarks/walker_dogfood.py [out_dir]   (needs network: real public pages)

Per site: both outlines saved, timings, node/ref counts, controls the walker
MISSES (present in native, core roles) and EXTRA ones, then one action flow
driven purely through walker refs.
'''
import asyncio, pathlib, re, sys, time, traceback
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
import purecdp
from purecdp.testing import launched_page

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.')
CORE = {'button', 'link', 'textbox', 'searchbox', 'checkbox', 'radio', 'combobox',
        'listbox', 'option', 'heading', 'image', 'dialog', 'slider', 'spinbutton',
        'tab', 'menuitem', 'switch'}

SITES = [
    ('wikipedia', 'https://en.wikipedia.org/wiki/Main_Page', 'search'),
    ('hn', 'https://news.ycombinator.com/', None),
    ('github-login', 'https://github.com/login', 'login-form'),
    ('duckduckgo', 'https://duckduckgo.com/', 'search'),
    ('aria-apg-dialog', 'https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/examples/dialog/', 'dialog'),
    ('shoelace', 'https://shoelace.style/components/button', None),   # web components / shadow DOM
    ('mdn-form', 'https://developer.mozilla.org/en-US/docs/Learn_web_development/Extensions/Forms/Your_first_form', None),
]


def norm(name):
    return re.sub(r'[\s\u00a0]+', ' ', name or '').strip()


def controls(snap):
    return {(n['role'], norm(n['name'])) for n in snap.nodes if n['ref'] and n['role'] in CORE}


def summarize(label, native, walked, t_native, t_walker):
    nat, wal = controls(native), controls(walked)
    missing = sorted(nat - wal)
    extra = sorted(wal - nat)
    print(f'\n=== {label}')
    print(f'  native : {t_native*1000:6.0f} ms  {len(native.nodes):5d} rows  {len(native.refs):5d} refs  {len(nat):4d} core controls')
    print(f'  walker : {t_walker*1000:6.0f} ms  {len(walked.nodes):5d} rows  {len(walked.refs):5d} refs  {len(wal):4d} core controls')
    print(f'  outline bytes: native {len(native.text):,}  walker {len(walked.text):,}')
    print(f'  MISSING in walker ({len(missing)}): ' + ('; '.join(f'{r} {n[:60]!r}' for r, n in missing[:10]) or 'none'))
    print(f'  EXTRA in walker   ({len(extra)}): ' + ('; '.join(f'{r} {n[:60]!r}' for r, n in extra[:10]) or 'none'))
    return missing, extra


def find(snap, role, name_re):
    return snap.find(role, re.compile(name_re, re.I))   # was a hand-rolled loop: now API


async def flow(page, kind, snap):
    '''One real interaction through walker refs; returns a one-line verdict.'''
    if kind == 'search':
        ref = find(snap, 'searchbox', '.') or find(snap, 'textbox', 'search') or find(snap, 'combobox', 'search')
        if not ref:
            return 'no search box found in walker outline'
        await page.act(ref, 'type', text='accessibility tree')
        value = await (await page.element_for_ref(ref)).eval('(el) => el.value')
        return f'typed into {ref} ({snap.meta[ref]["role"]} "{snap.meta[ref]["name"]}") -> value {value!r}'
    if kind == 'login-form':
        user = find(snap, 'textbox', 'username|email')
        pw = find(snap, 'textbox', 'password')
        btn = find(snap, 'button', 'sign in')
        if not (user and pw and btn):
            return f'login controls not all found: user={user} pw={pw} btn={btn}'
        await page.act(user, 'fill', text='someone@example.com')
        await page.act(pw, 'fill', text='hunter2')
        after = await page.snapshot(engine='walker')
        pw_row = next(n for n in after.nodes if n['ref'] == pw)
        return f'filled {user}+{pw}; password row renders as {pw_row["value"]!r} (must be bullets), submit = {btn}'
    if kind == 'dialog':
        btn = find(snap, 'button', 'add delivery address')
        if not btn:
            return 'opener button not found'
        await page.act(btn, 'click')
        await asyncio.sleep(0.3)
        scoped = await page.snapshot(engine='walker', scope='dialog')
        return ('dialog scoped: ' + str(scoped.text.count('\n') + 1) + ' rows; first: ' + scoped.text.splitlines()[0][:80]
                + ' | has textbox: ' + str(any(n["role"] == "textbox" for n in scoped.nodes)))
    return 'no flow'


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = []
    async with launched_page() as (browser, page):
        for label, url, kind in SITES:
            try:
                await page.goto(url, timeout=30)
                await asyncio.sleep(1.0)   # let late JS settle (not a purecdp wait: a dogfood choice)
                t = time.perf_counter(); native = await page.snapshot(engine='native'); t_native = time.perf_counter() - t
                t = time.perf_counter(); walked = await page.snapshot(engine='walker'); t_walker = time.perf_counter() - t
                (OUT / f'{label}.native.txt').write_text(native.text, encoding='utf-8')
                (OUT / f'{label}.walker.txt').write_text(walked.text, encoding='utf-8')
                missing, extra = summarize(label, native, walked, t_native, t_walker)
                verdict = await flow(page, kind, walked) if kind else 'no flow'
                print(f'  flow: {verdict}')
                report.append((label, len(missing), len(extra), verdict))
            except Exception as exc:
                print(f'\n=== {label}: FAILED {type(exc).__name__}: {exc}')
                traceback.print_exc(limit=2)
                report.append((label, None, None, f'FAILED {exc!r}'))
    print('\n--- summary')
    for label, miss, extra, verdict in report:
        print(f'{label:<18} missing={miss} extra={extra}  {verdict[:90]}')

asyncio.run(main())
