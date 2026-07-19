'''Emit Python modules (one per CDP domain) from the parsed spec IR.'''

from __future__ import annotations

import keyword
import re

from .model import Command, Domain, Event, Property, Spec, TypeDecl, TypeRef

# Names that generated field/param identifiers must never shadow.
BASE_RESERVED = frozenset(keyword.kwlist) | {
    'json', 'params', 'cmd', 'self', 'cls',
    'typing', 'enum', 'dataclass', 'event_class', 'T_JSON_DICT',
}

SCALAR_BASES = {'string': 'str', 'integer': 'int', 'number': 'float', 'binary': 'str'}
PRIMITIVE_ANN = {
    'string': 'str', 'integer': 'int', 'number': 'float', 'boolean': 'bool',
    'any': 'typing.Any', 'object': 'T_JSON_DICT', 'binary': 'str',
}


def snake(name: str) -> str:
    '''CamelCase / camelCase (incl. acronym runs) -> snake_case.'''
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s)
    return s.lower()


def module_name(domain: str) -> str:
    return snake(domain)


def enum_member(value: str) -> str:
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', value)
    s = re.sub(r'[^A-Za-z0-9]+', '_', s).upper().strip('_')
    if not s:
        raise ValueError(f'cannot derive enum member name from {value!r}')
    if s[0].isdigit():
        s = '_' + s
    return s


def cap_first(name: str) -> str:
    return name[0].upper() + name[1:]


def escape_doc(text: str) -> str:
    return text.replace('\\', '\\\\').replace("'''", '"""')


class DomainEmitter:
    def __init__(self, domain: Domain, spec: Spec, pin: str):
        self.domain = domain
        self.spec = spec
        self.pin = pin
        self.imports = self._collect_imports()
        self.reserved = BASE_RESERVED | self.imports

    # ---------- naming ----------

    def safe(self, name: str) -> str:
        '''snake_case identifier for a CDP property/parameter name.'''
        ident = snake(name)
        if ident in self.reserved:
            ident += '_'
        return ident

    def qualify(self, ref: str) -> str:
        '''Python name for a $ref, from the perspective of this domain.'''
        if '.' in ref:
            dom, name = ref.split('.', 1)
            if dom == self.domain.name:
                return name
            return f'{module_name(dom)}.{name}'
        return ref

    def _walk_typerefs(self):
        def walk(t: TypeRef):
            yield t
            if t.items is not None:
                yield from walk(t.items)

        for td in self.domain.types:
            if td.items is not None:
                yield from walk(td.items)
            for p in td.properties or ():
                yield from walk(p.type)
        for c in self.domain.commands:
            for p in list(c.parameters) + list(c.returns):
                yield from walk(p.type)
        for e in self.domain.events:
            for p in e.parameters:
                yield from walk(p.type)

    def _collect_imports(self) -> set[str]:
        mods: set[str] = set()
        for t in self._walk_typerefs():
            if t.kind == 'ref' and '.' in t.ref:
                dom = t.ref.split('.', 1)[0]
                if dom != self.domain.name:
                    mods.add(module_name(dom))
        return mods

    # ---------- type expressions ----------

    def ann(self, t: TypeRef) -> str:
        if t.kind == 'ref':
            return self.qualify(t.ref)
        if t.kind == 'array':
            return f'list[{self.ann(t.items)}]'
        return PRIMITIVE_ANN[t.kind]

    def from_expr(self, t: TypeRef, var: str) -> str:
        if t.kind == 'ref':
            return f'{self.qualify(t.ref)}.from_json({var})'
        if t.kind == 'array':
            inner = self.from_expr(t.items, 'i')
            if inner == 'i':
                return f'list({var})'
            return f'[{inner} for i in {var}]'
        if t.kind == 'number':
            return f'float({var})'
        return var

    def to_expr(self, t: TypeRef, var: str) -> str:
        if t.kind == 'ref':
            return f'{var}.to_json()'
        if t.kind == 'array':
            inner = self.to_expr(t.items, 'i')
            if inner == 'i':
                return f'list({var})'
            return f'[{inner} for i in {var}]'
        return var

    # ---------- docstrings ----------

    def _doc_lines(self, description: str | None, *, deprecated=False,
                   experimental=False, extra: list[str] | None = None) -> list[str]:
        lines: list[str] = []
        if description:
            lines += escape_doc(description).splitlines()
        flags = []
        if experimental:
            flags.append('**EXPERIMENTAL**')
        if deprecated:
            flags.append('**DEPRECATED**')
        if flags:
            if lines:
                lines.append('')
            lines.append(' '.join(flags))
        if extra:
            if lines:
                lines.append('')
            lines += extra
        return lines

    def _docstring(self, indent: str, lines: list[str]) -> list[str]:
        if not lines:
            return []
        if len(lines) == 1 and not lines[0].endswith("'"):
            return [f"{indent}'''{lines[0]}'''"]
        out = [f"{indent}'''{lines[0]}"]
        out += [f'{indent}{ln}'.rstrip() for ln in lines[1:]]
        out.append(f"{indent}'''")
        return out

    def _param_doc(self, p: Property) -> str:
        bits = []
        if p.optional:
            bits.append('*(Optional)*')
        if p.description:
            bits.append(' '.join(escape_doc(p.description).splitlines()))
        if p.type.enum:
            bits.append('Allowed values: ' + ', '.join(p.type.enum))
        if p.experimental:
            bits.append('*(EXPERIMENTAL)*')
        if p.deprecated:
            bits.append('*(DEPRECATED)*')
        return ' '.join(bits)

    # ---------- named types ----------

    def emit_type(self, td: TypeDecl) -> list[str]:
        if td.kind == 'string' and td.enum:
            return self._emit_enum(td)
        if td.kind in SCALAR_BASES:
            return self._emit_scalar(td)
        if td.kind == 'array':
            return self._emit_named_array(td)
        if td.kind == 'object':
            if td.properties is None:
                return self._emit_dict_type(td)
            return self._emit_dataclass(td)
        if td.kind == 'any':
            return [f'{td.id} = typing.Any']
        raise ValueError(f'cannot emit named type {self.domain.name}.{td.id} ({td.kind})')

    def _type_doc(self, td: TypeDecl) -> list[str]:
        return self._docstring('    ', self._doc_lines(
            td.description, deprecated=td.deprecated, experimental=td.experimental))

    def _emit_enum(self, td: TypeDecl) -> list[str]:
        out = [f'class {td.id}(enum.StrEnum):']
        out += self._type_doc(td)
        members = {}
        for value in td.enum:
            name = enum_member(value)
            if name in members:
                raise ValueError(f'enum member collision in {self.domain.name}.{td.id}: {name}')
            members[name] = value
            out.append(f'    {name} = {value!r}')
        out += [
            '',
            '    def to_json(self) -> str:',
            '        return self.value',
            '',
            '    @classmethod',
            f'    def from_json(cls, json: str) -> {td.id}:',
            '        return cls(json)',
        ]
        return out

    def _emit_scalar(self, td: TypeDecl) -> list[str]:
        base = SCALAR_BASES[td.kind]
        out = [f'class {td.id}({base}):']
        out += self._type_doc(td)
        out += [
            f'    def to_json(self) -> {base}:',
            f'        return {base}(self)',
            '',
            '    @classmethod',
            f'    def from_json(cls, json: {base}) -> {td.id}:',
            '        return cls(json)',
            '',
            '    def __repr__(self) -> str:',
            f"        return f'{td.id}({{{base}.__repr__(self)}})'",
        ]
        return out

    def _emit_named_array(self, td: TypeDecl) -> list[str]:
        out = [f'class {td.id}(list):']
        out += self._type_doc(td)
        out += [
            '    def to_json(self) -> list:',
            f'        return {self.to_expr(TypeRef(kind='array', items=td.items), 'self')}',
            '',
            '    @classmethod',
            f'    def from_json(cls, json: list) -> {td.id}:',
            f'        return cls({self.from_expr(TypeRef(kind='array', items=td.items), 'json')})',
            '',
            '    def __repr__(self) -> str:',
            f"        return f'{td.id}({{list.__repr__(self)}})'",
        ]
        return out

    def _emit_dict_type(self, td: TypeDecl) -> list[str]:
        out = [f'class {td.id}(dict):']
        out += self._type_doc(td)
        out += [
            '    def to_json(self) -> T_JSON_DICT:',
            '        return dict(self)',
            '',
            '    @classmethod',
            f'    def from_json(cls, json: T_JSON_DICT) -> {td.id}:',
            '        return cls(json)',
            '',
            '    def __repr__(self) -> str:',
            f"        return f'{td.id}({{dict.__repr__(self)}})'",
        ]
        return out

    @staticmethod
    def _ordered(props: tuple[Property, ...]) -> list[Property]:
        return [p for p in props if not p.optional] + [p for p in props if p.optional]

    def _emit_fields(self, props: list[Property]) -> list[str]:
        out = []
        for p in props:
            fname = self.safe(p.name)
            doc = self._param_doc(p)
            if doc:
                out.append(f'    #: {doc}')
            if p.optional:
                out.append(f'    {fname}: {self.ann(p.type)} | None = None')
            else:
                out.append(f'    {fname}: {self.ann(p.type)}')
        return out

    def _emit_from_json(self, cls_name: str, props: list[Property]) -> list[str]:
        out = [
            '    @classmethod',
            f'    def from_json(cls, json: T_JSON_DICT) -> {cls_name}:',
        ]
        if not props:
            out.append('        return cls()')
            return out
        out.append('        return cls(')
        for p in props:
            fname = self.safe(p.name)
            expr = self.from_expr(p.type, f'json[{p.name!r}]')
            if p.optional:
                expr = f'{expr} if json.get({p.name!r}) is not None else None'
            out.append(f'            {fname}={expr},')
        out.append('        )')
        return out

    def _emit_dataclass(self, td: TypeDecl) -> list[str]:
        props = self._ordered(td.properties)
        out = ['@dataclass(slots=True)', f'class {td.id}:']
        out += self._type_doc(td)
        out += self._emit_fields(props)
        if td.properties or not out[-1].startswith("    '''"):
            out.append('')
        # to_json
        out += ['    def to_json(self) -> T_JSON_DICT:',
                '        json: T_JSON_DICT = {}']
        for p in props:
            fname = self.safe(p.name)
            expr = self.to_expr(p.type, f'self.{fname}')
            if p.optional:
                out.append(f'        if self.{fname} is not None:')
                out.append(f'            json[{p.name!r}] = {expr}')
            else:
                out.append(f'        json[{p.name!r}] = {expr}')
        out.append('        return json')
        out.append('')
        out += self._emit_from_json(td.id, props)
        return out

    # ---------- commands ----------

    def emit_command(self, c: Command) -> list[str]:
        fname = self.safe(c.name)
        params = self._ordered(c.parameters)
        sig_parts = []
        for p in params:
            pn = self.safe(p.name)
            if p.optional:
                sig_parts.append(f'{pn}: {self.ann(p.type)} | None = None')
            else:
                sig_parts.append(f'{pn}: {self.ann(p.type)}')

        returns = list(c.returns)
        if not returns:
            ret_ann = 'None'
        elif len(returns) == 1:
            r = returns[0]
            ret_ann = f'{self.ann(r.type)} | None' if r.optional else self.ann(r.type)
        else:
            anns = [f'{self.ann(r.type)} | None' if r.optional else self.ann(r.type)
                    for r in returns]
            ret_ann = 'tuple[' + ', '.join(anns) + ']'
        gen_ann = f'typing.Generator[T_JSON_DICT, T_JSON_DICT, {ret_ann}]'

        sig = f'def {fname}(' + ', '.join(sig_parts) + f') -> {gen_ann}:'
        if len(sig) > 100 and sig_parts:
            out = [f'def {fname}(']
            out += [f'        {part},' for part in sig_parts]
            out.append(f') -> {gen_ann}:')
        else:
            out = [sig]

        extra = [f':param {self.safe(p.name)}: {self._param_doc(p)}' for p in params]
        if len(returns) == 1:
            extra.append(f':returns: {self._param_doc(returns[0])}')
        elif len(returns) > 1:
            extra.append(':returns: A tuple with the following items:')
            for i, r in enumerate(returns):
                extra.append(f'    {i}. **{r.name}** — {self._param_doc(r)}')
        out += self._docstring('    ', self._doc_lines(
            c.description, deprecated=c.deprecated, experimental=c.experimental,
            extra=extra))

        method = f'{self.domain.name}.{c.name}'
        if params:
            out.append('    params: T_JSON_DICT = {}')
            for p in params:
                expr = self.to_expr(p.type, self.safe(p.name))
                if p.optional:
                    out.append(f'    if {self.safe(p.name)} is not None:')
                    out.append(f'        params[{p.name!r}] = {expr}')
                else:
                    out.append(f'    params[{p.name!r}] = {expr}')
            cmd = f"{{'method': {method!r}, 'params': params}}"
        else:
            cmd = f"{{'method': {method!r}}}"

        if not returns:
            out.append(f'    yield {cmd}')
        else:
            out.append(f'    json = yield {cmd}')
            if len(returns) == 1:
                r = returns[0]
                expr = self.from_expr(r.type, f'json[{r.name!r}]')
                if r.optional:
                    expr = f'{expr} if json.get({r.name!r}) is not None else None'
                out.append(f'    return {expr}')
            else:
                out.append('    return (')
                for r in returns:
                    expr = self.from_expr(r.type, f'json[{r.name!r}]')
                    if r.optional:
                        expr = f'{expr} if json.get({r.name!r}) is not None else None'
                    out.append(f'        {expr},')
                out.append('    )')
        return out

    # ---------- events ----------

    def emit_event(self, e: Event) -> list[str]:
        cls_name = cap_first(e.name)
        method = f'{self.domain.name}.{e.name}'
        props = self._ordered(e.parameters)
        out = [f'@event_class({method!r})', '@dataclass(slots=True)',
               f'class {cls_name}:']
        out += self._docstring('    ', self._doc_lines(
            e.description, deprecated=e.deprecated, experimental=e.experimental))
        out += self._emit_fields(props)
        if props or out[-1].endswith("'''"):
            out.append('')
        out += self._emit_from_json(cls_name, props)
        return out

    # ---------- module ----------

    def emit_module(self) -> str:
        d = self.domain
        # class-name collision check (named types vs event classes)
        type_names = {t.id for t in d.types}
        event_names = {cap_first(e.name) for e in d.events}
        clash = type_names & event_names
        if clash:
            raise ValueError(f'class name collision in {d.name}: {clash}')

        header = [
            f'# DO NOT EDIT — generated by purecdp._generator from {d.name} '
            f'(devtools-protocol {self.pin})',
        ]
        doc = self._doc_lines(d.description, deprecated=d.deprecated,
                              experimental=d.experimental)
        title = f'CDP domain: {d.name}.'
        header += self._docstring('', [title] + ([''] + doc if doc else []))
        header += [
            '',
            'from __future__ import annotations',
            '',
            'import enum',
            'import typing',
            'from dataclasses import dataclass',
            '',
            'from .._shared import T_JSON_DICT, event_class',
        ]
        if self.imports:
            header.append('')
            header.append('from . import ' + ', '.join(sorted(self.imports)))

        blocks: list[list[str]] = []
        for td in d.types:
            blocks.append(self.emit_type(td))
        for c in d.commands:
            blocks.append(self.emit_command(c))
        for e in d.events:
            blocks.append(self.emit_event(e))

        body = []
        for b in blocks:
            body += ['', ''] + b
        return '\n'.join(header + body) + '\n'


def emit_init(spec: Spec, pin: str) -> str:
    mods = sorted(module_name(d.name) for d in spec.domains)
    domain_map = sorted((d.name, module_name(d.name)) for d in spec.domains)
    lines = [
        f'# DO NOT EDIT — generated by purecdp._generator (devtools-protocol {pin})',
        f"'''Generated CDP bindings, protocol version {spec.version}.",
        '',
        'Domain modules load lazily on first attribute access (PEP 562).',
        'Event classes register with purecdp._shared only when their module is',
        'imported; call load_all() to populate the full event registry, or',
        'import_domain() to resolve a single domain by its CDP name.',
        "'''",
        '',
        'from __future__ import annotations',
        '',
        'import importlib',
        'import typing',
        '',
        '__all__ = [',
    ]
    lines += [f'    {m!r},' for m in mods]
    lines += [
        ']',
        '',
        '#: CDP domain name -> module name in this package.',
        'DOMAIN_MODULES: dict[str, str] = {',
    ]
    lines += [f'    {dom!r}: {mod!r},' for dom, mod in domain_map]
    lines += [
        '}',
        '',
        '',
        'def import_domain(domain: str) -> typing.Any:',
        "    '''Import and return the module for a CDP domain name (e.g. 'DOM').",
        '',
        '    Raises KeyError for domains not in the pinned spec.',
        "    '''",
        "    return importlib.import_module('.' + DOMAIN_MODULES[domain], __name__)",
        '',
        '',
        'def __getattr__(name: str) -> typing.Any:',
        '    if name in __all__:',
        "        return importlib.import_module('.' + name, __name__)",
        "    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')",
        '',
        '',
        'def load_all() -> None:',
        "    '''Import every domain module (fully populates the event registry).'''",
        '    for name in __all__:',
        "        importlib.import_module('.' + name, __name__)",
        '',
    ]
    return '\n'.join(lines)


def emit_all(spec: Spec, pin: str) -> dict[str, str]:
    '''Return {filename: source} for the whole purecdp.protocol package.'''
    out: dict[str, str] = {'__init__.py': emit_init(spec, pin)}
    for d in spec.domains:
        out[module_name(d.name) + '.py'] = DomainEmitter(d, spec, pin).emit_module()
    return out
