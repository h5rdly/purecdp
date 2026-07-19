'''Hand-written support module shared by the generated bindings and the engine.

Kept dependency-free and tiny: everything here must be importable before any
generated module and must never import purecdp.protocol.
'''

from __future__ import annotations

import typing
from dataclasses import dataclass, field

#: JSON object as produced/consumed by CDP messages.
T_JSON_DICT = dict[str, typing.Any]

#: CDP event method name ("Domain.event") -> generated event dataclass.
#: Populated as a side effect of importing generated domain modules.
_EVENT_CLASSES: dict[str, type] = {}


def event_class(method: str):
    '''Class decorator used by generated code to register an event dataclass.'''

    def decorate(cls: type) -> type:
        cls.EVENT_METHOD = method  # type: ignore[attr-defined]
        _EVENT_CLASSES[method] = cls
        return cls

    return decorate


@dataclass
class UnknownEvent:
    '''An event whose method is not in the pinned protocol spec (or whose
    domain module has not been imported). Raw payload is preserved.'''

    method: str
    params: T_JSON_DICT = field(default_factory=dict)


def parse_event(method: str, params: T_JSON_DICT) -> typing.Any:
    '''Parse a CDP event into its typed dataclass.

    Falls back to :class:`UnknownEvent` for unregistered methods so that a
    protocol-drifted Chrome never crashes the dispatcher. Note that only
    imported domain modules register their events; ``purecdp.protocol.load_all()``
    imports everything.
    '''
    cls = _EVENT_CLASSES.get(method)
    if cls is None:
        return UnknownEvent(method=method, params=params)
    return cls.from_json(params)
