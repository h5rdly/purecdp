'''JSON backend: orjson when installed, stdlib json otherwise.

orjson is optional — purecdp stays zero-dependency — but it is markedly
faster on the payloads CDP actually moves (14x dumping commands, ~3-4x loading
large screenshot / DOM-tree messages), so the engine and recorder use it when
present. This is a thin adapter, not ``import orjson as json``: orjson.dumps
returns *bytes* and rejects the ``separators`` kwarg, so a bare alias would
break callers. Both backends here produce compact UTF-8 output and accept
str-or-bytes input, matching what the transports and engine expect.

Only the hot paths (per-message dumps/loads) route through here. Code that
builds JavaScript string literals keeps stdlib ``json`` on purpose: it wants
``str`` out and ASCII-escaped output for safe embedding.
'''

from __future__ import annotations

import typing

try:
    import orjson as _orjson

    HAVE_ORJSON = True

    def dumps(obj: typing.Any) -> str:
        '''Serialize to a compact JSON string (orjson emits UTF-8 bytes).'''
        return _orjson.dumps(obj).decode()

    def loads(data: str | bytes) -> typing.Any:
        return _orjson.loads(data)

except ImportError:  # pragma: no cover - exercised via the stdlib path in CI
    import json as _json

    HAVE_ORJSON = False

    def dumps(obj: typing.Any) -> str:
        return _json.dumps(obj, separators=(',', ':'))

    def loads(data: str | bytes) -> typing.Any:
        return _json.loads(data)


#: Name of the active backend, for diagnostics.
BACKEND = 'orjson' if HAVE_ORJSON else 'json'
