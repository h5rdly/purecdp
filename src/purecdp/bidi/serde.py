'''BiDi value model <-> Python.

Scripts exchange *LocalValue* (what we send as arguments) and *RemoteValue*
(what comes back), both type-tagged::

    {"type": "number", "value": 2}
    {"type": "object", "value": [["ok", {"type": "boolean", "value": true}]]}
    {"type": "node", "sharedId": "…", "handle": "…", "value": {...}}

Plain Python goes out as LocalValue; a RemoteValue comes back as plain
Python, or as a :class:`~purecdp.testing._driver.Handle` for anything that is
only reachable by reference (DOM nodes, functions, objects past the
serialization depth). Nodes carry both a ``sharedId`` (the node identity:
what ``input.setFiles`` / ``locateNodes`` / call arguments take) and, when
ownership was requested, a ``handle`` (what ``script.disown`` takes).
'''

from __future__ import annotations

import math
import typing

from .._shared import Handle

_SPECIAL_NUMBERS = {'NaN': math.nan, 'Infinity': math.inf,
                    '-Infinity': -math.inf, '-0': -0.0}


def to_local(value: typing.Any) -> dict:
    '''Python -> LocalValue. Handles (and objects carrying ``_handle``, i.e.
    Elements) become remote references; everything else must be JSON-like.'''
    handle = value if isinstance(value, Handle) else getattr(value, '_handle', None)
    if isinstance(handle, Handle):
        return _reference(handle)
    if value is None:
        return {'type': 'null'}
    if isinstance(value, bool):
        return {'type': 'boolean', 'value': value}
    if isinstance(value, int):
        return {'type': 'number', 'value': value}
    if isinstance(value, float):
        if math.isnan(value):
            return {'type': 'number', 'value': 'NaN'}
        if math.isinf(value):
            return {'type': 'number', 'value': 'Infinity' if value > 0 else '-Infinity'}
        if value == 0 and math.copysign(1, value) < 0:
            return {'type': 'number', 'value': '-0'}
        return {'type': 'number', 'value': value}
    if isinstance(value, str):
        return {'type': 'string', 'value': value}
    if isinstance(value, (list, tuple)):
        return {'type': 'array', 'value': [to_local(v) for v in value]}
    if isinstance(value, dict):
        pairs = []
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f'object keys must be str, not {type(k).__name__}')
            pairs.append([k, to_local(v)])
        return {'type': 'object', 'value': pairs}
    raise TypeError(f'{value!r} is neither JSON-like nor a Handle')


def _reference(handle: Handle) -> dict:
    ref: dict = {}
    if handle.node_id is not None:
        ref['sharedId'] = handle.node_id
    if handle.id != handle.node_id:
        ref['handle'] = handle.id
    return ref


def handle_of(remote: dict) -> Handle | None:
    '''A Handle for a RemoteValue that is reachable by reference, else None.
    ``id`` is the handle when ownership was granted, else the sharedId.'''
    shared = remote.get('sharedId')
    owned = remote.get('handle')
    if owned is None and shared is None:
        return None
    return Handle(id=str(owned if owned is not None else shared),
                  node_id=str(shared) if shared is not None else None,
                  raw=remote)


def from_remote(remote: dict) -> typing.Any:
    '''RemoteValue -> Python value, or a Handle for reference-only values.'''
    kind = remote.get('type')
    if kind in ('undefined', 'null'):
        return None
    if kind in ('string', 'boolean'):
        return remote.get('value')
    if kind == 'number':
        value = remote.get('value')
        return _SPECIAL_NUMBERS.get(value, value) if isinstance(value, str) else value
    if kind == 'bigint':
        return int(remote.get('value', 0))
    if kind == 'array' and 'value' in remote:
        return [from_remote(v) for v in remote['value']]
    if kind == 'object' and 'value' in remote:
        return {_key(k): from_remote(v) for k, v in remote['value']}
    if kind == 'map' and 'value' in remote:
        return {_key(k): from_remote(v) for k, v in remote['value']}
    if kind == 'set' and 'value' in remote:
        return [from_remote(v) for v in remote['value']]
    if kind == 'date':
        return remote.get('value')           # ISO string
    if kind == 'regexp':
        pattern = remote.get('value', {})
        return pattern.get('pattern') if isinstance(pattern, dict) else pattern
    if kind == 'node':
        return handle_of(remote)
    # function / window / error / promise / symbol / weakmap / typedarray /
    # arraybuffer / proxy / generator / htmlcollection / nodelist / and any
    # object or array past the serialization depth: by reference or nothing
    handle = handle_of(remote)
    if handle is not None:
        return handle
    return None


def _key(key: typing.Any) -> typing.Any:
    return key if isinstance(key, str) else from_remote(key)
