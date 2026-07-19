'''CLI: python -m purecdp._generator [--spec DIR] [--out DIR]'''

from __future__ import annotations

import argparse
import pathlib
import sys

from . import emit, model


def repo_root() -> pathlib.Path:
    # src/purecdp/_generator/__main__.py -> repo root is three levels above src/
    return pathlib.Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    root = repo_root()
    ap = argparse.ArgumentParser(prog='purecdp._generator')
    ap.add_argument('--spec', type=pathlib.Path, default=root / 'spec',
                    help='directory containing browser_protocol.json, js_protocol.json, PIN')
    ap.add_argument('--out', type=pathlib.Path,
                    default=root / 'src' / 'purecdp' / 'protocol',
                    help='output package directory (existing *.py files are removed)')
    args = ap.parse_args(argv)

    spec_files = [args.spec / 'browser_protocol.json', args.spec / 'js_protocol.json']
    for f in spec_files:
        if not f.is_file():
            ap.error(f'spec file not found: {f}')
    pin_file = args.spec / 'PIN'
    pin = pin_file.read_text().split()[0][:12] if pin_file.is_file() else 'unpinned'

    spec = model.load(spec_files)
    files = emit.emit_all(spec, pin)

    args.out.mkdir(parents=True, exist_ok=True)
    for stale in args.out.glob('*.py'):
        if stale.name not in files:
            stale.unlink()
    changed = 0
    for name, source in sorted(files.items()):
        path = args.out / name
        if not path.is_file() or path.read_text(encoding='utf-8') != source:
            path.write_text(source, encoding='utf-8')
            changed += 1
    print(f'{len(spec.domains)} domains -> {len(files)} files '
          f'({changed} written) in {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
