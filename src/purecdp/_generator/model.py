'''Intermediate representation of the CDP spec, parsed from the JSON files.'''

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

PRIMITIVES = {'string', 'integer', 'number', 'boolean', 'any', 'object', 'binary'}


@dataclass(frozen=True)
class TypeRef:
    '''A use of a type: primitive, array, or reference to a named type.'''

    kind: str  # one of PRIMITIVES, or 'array', or 'ref'
    ref: str | None = None  # for kind='ref': 'Name' or 'Domain.Name'
    items: TypeRef | None = None  # for kind='array'
    enum: tuple[str, ...] | None = None  # inline enum values (documentation only)

    @staticmethod
    def parse(obj: dict) -> TypeRef:
        if '$ref' in obj:
            return TypeRef(kind='ref', ref=obj['$ref'])
        kind = obj['type']
        if kind == 'array':
            return TypeRef(kind='array', items=TypeRef.parse(obj['items']))
        if kind not in PRIMITIVES:
            raise ValueError(f'unknown type kind {kind!r} in {obj!r}')
        return TypeRef(kind=kind, enum=tuple(obj['enum']) if 'enum' in obj else None)


@dataclass(frozen=True)
class Property:
    '''A type property, or a command/event parameter, or a command return.'''

    name: str
    type: TypeRef
    description: str | None = None
    optional: bool = False
    deprecated: bool = False
    experimental: bool = False

    @staticmethod
    def parse(obj: dict) -> Property:
        return Property(
            name=obj['name'],
            type=TypeRef.parse(obj),
            description=obj.get('description'),
            optional=obj.get('optional', False),
            deprecated=obj.get('deprecated', False),
            experimental=obj.get('experimental', False),
        )


@dataclass(frozen=True)
class TypeDecl:
    '''A named type declared in a domain's 'types' list.'''

    id: str
    kind: str  # one of PRIMITIVES or 'array'
    description: str | None = None
    deprecated: bool = False
    experimental: bool = False
    enum: tuple[str, ...] | None = None  # kind='string' enums
    properties: tuple[Property, ...] | None = None  # kind='object'; None if absent
    items: TypeRef | None = None  # kind='array'

    @staticmethod
    def parse(obj: dict) -> TypeDecl:
        props = obj.get('properties')
        return TypeDecl(
            id=obj['id'],
            kind=obj['type'],
            description=obj.get('description'),
            deprecated=obj.get('deprecated', False),
            experimental=obj.get('experimental', False),
            enum=tuple(obj['enum']) if 'enum' in obj else None,
            properties=tuple(Property.parse(p) for p in props) if props is not None else None,
            items=TypeRef.parse(obj['items']) if 'items' in obj else None,
        )


@dataclass(frozen=True)
class Command:
    name: str
    description: str | None = None
    deprecated: bool = False
    experimental: bool = False
    parameters: tuple[Property, ...] = ()
    returns: tuple[Property, ...] = ()

    @staticmethod
    def parse(obj: dict) -> Command:
        return Command(
            name=obj['name'],
            description=obj.get('description'),
            deprecated=obj.get('deprecated', False),
            experimental=obj.get('experimental', False),
            parameters=tuple(Property.parse(p) for p in obj.get('parameters', ())),
            returns=tuple(Property.parse(p) for p in obj.get('returns', ())),
        )


@dataclass(frozen=True)
class Event:
    name: str
    description: str | None = None
    deprecated: bool = False
    experimental: bool = False
    parameters: tuple[Property, ...] = ()

    @staticmethod
    def parse(obj: dict) -> Event:
        return Event(
            name=obj['name'],
            description=obj.get('description'),
            deprecated=obj.get('deprecated', False),
            experimental=obj.get('experimental', False),
            parameters=tuple(Property.parse(p) for p in obj.get('parameters', ())),
        )


@dataclass(frozen=True)
class Domain:
    name: str
    description: str | None = None
    deprecated: bool = False
    experimental: bool = False
    types: tuple[TypeDecl, ...] = ()
    commands: tuple[Command, ...] = ()
    events: tuple[Event, ...] = ()

    @staticmethod
    def parse(obj: dict) -> Domain:
        return Domain(
            name=obj['domain'],
            description=obj.get('description'),
            deprecated=obj.get('deprecated', False),
            experimental=obj.get('experimental', False),
            types=tuple(TypeDecl.parse(t) for t in obj.get('types', ())),
            commands=tuple(Command.parse(c) for c in obj.get('commands', ())),
            events=tuple(Event.parse(e) for e in obj.get('events', ())),
        )


@dataclass(frozen=True)
class Spec:
    version: str
    domains: tuple[Domain, ...]

    def domain(self, name: str) -> Domain:
        for d in self.domains:
            if d.name == name:
                return d
        raise KeyError(name)


def load(paths: list[pathlib.Path]) -> Spec:
    '''Load and merge one or more protocol JSON files into a single Spec.'''
    domains: list[Domain] = []
    versions: set[str] = set()
    seen: set[str] = set()
    for path in paths:
        raw = json.loads(path.read_text(encoding='utf-8'))
        versions.add('{major}.{minor}'.format(**raw['version']))
        for dom_obj in raw['domains']:
            dom = Domain.parse(dom_obj)
            if dom.name in seen:
                raise ValueError(f'duplicate domain {dom.name} in {path}')
            seen.add(dom.name)
            domains.append(dom)
    if len(versions) != 1:
        raise ValueError(f'inconsistent protocol versions: {versions}')
    return Spec(version=versions.pop(), domains=tuple(domains))
