'''purecdp vs rustwright on the same browser, same page, same ops.

    python benchmarks/orchestrate.py [--n 40] [--browser /path/to/chromium]

Serves a local page, runs benchmarks/runner.py once per engine in a fresh
subprocess (purecdp from ../src; user site-packages stay visible for rustwright), and prints a side-by-side
table. Methodology notes are in the Roadmap ("Rustwright gap batch", 07-18;
re-run 10-24). rustwright is optional: absent, only purecdp is measured.
'''

import argparse
import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / 'src'

ROWS = '\n'.join(f'<tr><td>row {i}</td><td><button>act {i}</button></td>'
                 f'<td><input value="v{i}"></td></tr>' for i in range(300))
PAGE = f'''<!doctype html><html><head><title>bench</title></head><body>
<h1>Benchmark page</h1>
<form onsubmit="return false">
  <label>Name <input id="name"></label>
  <button id="btn" onclick="document.getElementById('status').textContent =
    'clicked ' + (++window.__n)">Go</button>
  <p id="status">idle</p>
</form>
<table>{ROWS}</table>
<script>window.__n = 0;</script>
</body></html>'''.encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(PAGE)))
        self.end_headers()
        try:
            self.wfile.write(PAGE)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser abandoned a re-navigation mid-body

    def log_message(self, *args):
        pass


OPS = [('import_ms', 'import library'), ('launch_ms', 'launch + page + close'),
       ('goto_ms', 'goto (load)'), ('eval_ms', 'evaluate("1 + 1") per call'),
       ('eval_arg_ms', 'evaluate(fn, arg) per call'), ('title_ms', 'title'),
       ('query_text_ms', 'query + text'), ('click_ms', 'click *'),
       ('fill_ms', 'fill *'), ('snapshot_ms', 'a11y snapshot (native tree) **'),
       ('snapshot_walker_ms', 'a11y snapshot (engine=walker) **'),
       ('screenshot_ms', 'screenshot (png)'), ('big_eval_ms', 'evaluate 20k-item array'),
       ('client_rss_light_mib', 'client RSS after light ops (MiB)'),
       ('client_rss_mib', 'client RSS at end (MiB)'), ('client_maxrss_mib', 'client max RSS (MiB)'),
       ('browser_tree_rss_mib', 'browser tree RSS (MiB)')]


def run(impl, url, browser, n):
    env = {**os.environ, 'PYTHONPATH': str(SRC)}
    proc = subprocess.run([sys.executable, str(HERE / 'runner.py'), '--impl', impl,
                           '--url', url, '--browser', browser, '--n', str(n)],
                          capture_output=True, text=True, env=env, cwd=str(HERE))
    if proc.returncode != 0:
        print(f'{impl} failed:\n{proc.stderr[-2000:]}', file=sys.stderr)
        return None
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=40)
    ap.add_argument('--browser', default=None)
    args = ap.parse_args()
    sys.path.insert(0, str(SRC))
    import purecdp
    browser = args.browser or purecdp.find_browser()
    if not browser:
        sys.exit('no Chromium-based browser found')
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{httpd.server_address[1]}/'
    results = {impl: run(impl, url, browser, args.n) for impl in ('purecdp', 'rustwright')}
    a, b = results['purecdp'], results['rustwright']
    print(f'browser: {browser}   n={args.n}   purecdp {a and a.get("version")}'
          f'   rustwright {b and b.get("version")}')
    print(f'{"op":<32}{"purecdp":>12}{"rustwright":>12}{"pc/rw":>8}')
    for key, label in OPS:
        va = a.get(key) if a else None
        vb = b.get(key) if b else None
        ratio = f'{va / vb:.2f}' if va and vb else ''
        print(f'{label:<32}{va if va is not None else "-":>12}{vb if vb is not None else "-":>12}{ratio:>8}')
    print('*  rustwright click/fill use force=True, no_wait_after=True (skips its actionability\n'
          '   waits); purecdp click is a synthetic JS click, fill = native-setter write.\n'
          '** purecdp page.snapshot() (CDP AX tree -> ref outline) vs rustwright\n'
          '   locator("body").aria_snapshot() — similar purpose, different formats.')
    httpd.shutdown()


if __name__ == '__main__':
    main()
