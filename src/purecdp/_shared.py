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


@dataclass(frozen=True, slots=True)
class Handle:
    '''A remote object held in the page, protocol-neutral. ``id`` is what
    the backend uses to call on / release it (CDP objectId, BiDi handle — or
    the sharedId when no handle was granted); ``node_id`` is the backend's
    node identity when the object is a DOM node and the backend distinguishes
    the two (BiDi sharedId; None on CDP, where objectId serves). ``raw`` is
    the backend-native remote-object record, for callers that asked for one
    (``Page.evaluate(return_by_value=False)``).'''

    id: str
    node_id: str | None = None
    raw: typing.Any = field(default=None, compare=False, hash=False, repr=False)

    @staticmethod
    def coerce(value: Handle | str) -> Handle:
        '''Accept a bare id string (the pre-seam ``Element(page, 'OBJ-1')``
        spelling) as a Handle.'''
        return value if isinstance(value, Handle) else Handle(str(value))
