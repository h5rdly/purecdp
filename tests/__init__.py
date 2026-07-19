'''purecdp test package.

Stdlib-only: `python -m unittest` from the repo root discovers everything.
pytest also works (`python -m pytest`) but is never required.

Makes src/ importable for the unittest runner (pytest gets the same via the
`pythonpath` ini option in pyproject.toml).
'''

import pathlib
import sys

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
