'''base64 backend: pybase64 (SIMD) when installed, stdlib base64 otherwise.

CDP moves base64 in bulk — screenshots, PDFs, and recorded response bodies —
and pybase64 decodes them ~3-4x faster than the stdlib. Optional: purecdp
stays zero-dependency; these are drop-in replacements (identical signatures)
used only where large blobs are decoded/encoded.
'''

from __future__ import annotations

try:
    import pybase64 as _backend

    HAVE_PYBASE64 = True
except ImportError:  # pragma: no cover - stdlib path exercised in CI
    import base64 as _backend

    HAVE_PYBASE64 = False

b64decode = _backend.b64decode
b64encode = _backend.b64encode

#: Name of the active backend, for diagnostics.
BACKEND = 'pybase64' if HAVE_PYBASE64 else 'base64'
