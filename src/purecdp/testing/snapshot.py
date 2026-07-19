'''Accessibility snapshot for agents (M9): a compact, ref-tagged view of the
page an LLM can act on — ``click e3`` instead of parsing HTML or pixels.

``Page.snapshot()`` fetches the Chrome accessibility tree and passes it here;
:func:`build_snapshot` turns it into a small indented outline where every
actionable node carries a session-scoped ref (``e1``, ``e2``, …). Feed the text
to a model, then ``Page.act(ref, ...)`` / ``Page.element_for_ref(ref)`` resolves
the ref back to a real DOM node.

The builder is a pure function over plain dicts (no protocol types), so it is
directly unit-testable without a browser.
'''

from __future__ import annotations

import typing

#: Roles that never get a ref — pure text carriers. They still appear in the
#: outline (for context/naming) but you don't act on them.
_TEXT_ROLES = frozenset({'StaticText', 'InlineTextBox', 'LineBreak', 'text'})

#: What the browser reports for a password field's value once masked — a run of
#: bullets. We re-mask defensively regardless (see _mask_password).
_BULLETS = '•●*'

_MASK = '•' * 6


def _truthy(value: typing.Any) -> bool:
    return value in (True, 'true', 'mixed')


def _mask_password(role: str, name: str, value: str) -> bool:
    '''Heuristic: a text field that is (or looks like) a password. Name-based
    and value-based — not a security guarantee, just so snapshots don't echo
    secrets to an LLM. Chrome already reports password values as bullets; this
    also catches fields merely *named* password.'''
    if role not in ('textbox', 'searchbox'):
        return False
    if value and all(ch in _BULLETS for ch in value):
        return True
    return 'password' in name.lower()


def _states(props: dict) -> list[str]:
    '''Compact, truthy-only state flags from the node's AX properties.'''
    out: list[str] = []
    if _truthy(props.get('disabled')):
        out.append('disabled')
    checked = props.get('checked')
    if checked == 'mixed':
        out.append('mixed')
    elif _truthy(checked):
        out.append('checked')
    pressed = props.get('pressed')
    if _truthy(pressed):
        out.append('pressed')
    expanded = props.get('expanded')
    if expanded is not None:
        out.append('expanded' if _truthy(expanded) else 'collapsed')
    if _truthy(props.get('selected')):
        out.append('selected')
    if _truthy(props.get('required')):
        out.append('required')
    # 'invalid' defaults to 'false'; any other present value is a real state
    if props.get('invalid') not in (None, 'false', False):
        out.append('invalid')
    return out


class Snapshot:
    '''The result of :func:`build_snapshot`. ``str(snapshot)`` (or ``.text``)
    is the outline to show a model; ``.refs`` maps each ref to a backend DOM
    node id; ``.nodes`` is the flat, ordered node list for programmatic use.'''

    def __init__(
        self,
        text: str,
        refs: dict[str, int],
        meta: dict[str, dict],
        nodes: list[dict],
        frames: dict[str, typing.Any] | None = None,
    ):
        self.text = text
        #: ref (e.g. "e3") -> backend DOM node id.
        self.refs = refs
        #: ref -> {"role", "name"} (for messages/inspection).
        self.meta = meta
        #: flat, document-ordered rows: {depth, role, name, value, states, ref}.
        self.nodes = nodes
        #: ref -> the frame token it belongs to (None = the top frame). Only
        #: populated by a cross-frame stitched snapshot; drives act() routing.
        self.frames = frames or {}

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return f'Snapshot({len(self.refs)} refs)'


def _line(node: dict) -> str:
    parts = ['  ' * node['depth'], '- ', node['role'] or 'generic']
    if node['name']:
        parts.append(f' "{node["name"]}"')
    if node['value'] is not None:
        parts.append(f' = "{node["value"]}"')
    if node['states']:
        parts.append(f' ({", ".join(node["states"])})')
    if node['ref']:
        parts.append(f' [{node['ref']}]')
    return ''.join(parts)


def _from_wire(n: dict) -> dict:
    '''Map one raw ``Accessibility.getFullAXTree`` node (camelCase wire dict)
    to the plain dict :func:`build_snapshot` wants.

    Read straight from the wire rather than the typed ``AXNode``: snapshot only
    needs a handful of fields and would otherwise pay ``from_json`` on every
    node just to flatten it back to this. Field names track the AX spec (stable);
    unknown fields are ignored.'''
    role = n.get('role')
    name = n.get('name')
    value = n.get('value')
    props = n.get('properties')
    backend = n.get('backendDOMNodeId')
    return {
        'id': n['nodeId'],
        'ignored': n['ignored'],
        'role': (role.get('value', '') if role else '') or '',
        'name': (name.get('value') if name else '') or '',
        'value': value.get('value') if value else None,
        'props': ({p['name']: p.get('value', {}).get('value') for p in props}
                  if props else {}),
        'backend': int(backend) if backend is not None else None,
        'children': n.get('childIds') or [],
    }


def build_from_wire(wire_nodes: typing.Sequence[dict]) -> Snapshot:
    '''Build a :class:`Snapshot` directly from raw AX wire nodes (the list in
    ``Accessibility.getFullAXTree``'s ``nodes``).'''
    return build_snapshot([_from_wire(n) for n in wire_nodes])


def build_snapshot(nodes: typing.Sequence[dict]) -> Snapshot:
    '''Build a :class:`Snapshot` from AX nodes (plain dicts).

    Each input dict needs: ``id``, ``ignored`` (bool), ``role`` (str),
    ``name`` (str), ``value`` (str|None), ``props`` (dict name->value),
    ``backend`` (int|None), ``children`` (list[str] of child ids). Ignored
    nodes are flattened away (their children promote to the parent's depth);
    every emitted non-text node with a backend id gets a ref in document order.
    '''
    by_id = {n['id']: n for n in nodes}
    referenced = {c for n in nodes for c in n['children']}
    roots = [n['id'] for n in nodes if n['id'] not in referenced]

    out: list[dict] = []
    refs: dict[str, int] = {}
    meta: dict[str, dict] = {}
    frames: dict[str, typing.Any] = {}
    counter = [0]

    def walk(node_id: str, depth: int) -> None:
        node = by_id.get(node_id)
        if node is None:
            return
        if node['ignored']:
            for child in node['children']:
                walk(child, depth)
            return

        role = node['role'] or ''
        name = node['name'] or ''
        value = node['value']
        ref = None
        if node['backend'] is not None and role not in _TEXT_ROLES:
            counter[0] += 1
            ref = f'e{counter[0]}'
            refs[ref] = node['backend']
            meta[ref] = {'role': role, 'name': name}
            # a stitched cross-frame node carries its frame token (see _agent)
            frames[ref] = node.get('frame')
        if value is not None and _mask_password(role, name, str(value)):
            value = _MASK
        out.append({
            'depth': depth, 'role': role, 'name': name,
            'value': value, 'states': _states(node['props']), 'ref': ref,
        })
        for child in node['children']:
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)

    text = '\n'.join(_line(n) for n in out)
    return Snapshot(text, refs, meta, out, frames)
