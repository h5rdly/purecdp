'''An in-page accessibility walker: the accessibility tree ``snapshot()`` /
``act()`` / the MCP server consume, computed from the DOM by JavaScript for
backends with no tree command (WebDriver BiDi has none — Firefox).

It emits the same wire shape as ``Accessibility.getFullAXTree`` — ``nodeId``,
``ignored``, ``role``/``name``/``value`` as ``{"value": …}``, ``properties``,
``childIds``, ``backendDOMNodeId`` — so :func:`snapshot.build_from_wire` and
``_scope_to_dialog`` run unchanged. ``backendDOMNodeId`` is an index into a
registry the script leaves on the page (``globalThis[Symbol.for("purecdp.ax")]``,
an array of elements); the driver resolves a ref by indexing it, and a
navigation empties it exactly as it invalidates Chrome's backend ids.

Scope: roles via explicit ``role`` or an implicit HTML table, accessible names
via an accname subset (aria-labelledby, aria-label, <label>, alt, value of
button inputs, title, placeholder, name-from-content for the roles that allow
it), the states :func:`snapshot._states` reads, values of form controls,
hidden subtrees pruned, open shadow roots and slots flattened, an ``Iframe``
node per frame with no descent (frames are stitched per browsing context).
Approximate by design — an outline for a model, validated against Chrome's
real tree in tests/test_testing_e2e.py (AXWalkerParityE2ETests).
'''

from __future__ import annotations

#: JS function ``(maxDepth) => wireNodes``. Keep it self-contained: it runs in
#: the page realm on both backends.
AX_WALKER_JS = r'''
(maxDepth) => {
  const REG = Symbol.for('purecdp.ax');
  const registry = [];
  globalThis[REG] = registry;
  const nodes = [];
  let seq = 0;
  const SKIP = new Set(['script', 'style', 'template', 'noscript', 'head',
                        'meta', 'link', 'title', 'base']);
  const GENERIC = new Set(['div', 'span', 'b', 'i', 'u', 'small', 'big', 'center',
                           'font', 'tt', 'abbr', 'cite', 'q', 'kbd', 'samp',
                           'var', 'bdi', 'bdo', 'data', 'ruby', 'rt', 'rp',
                           'wbr', 'body', 'html', 'picture', 'source', 'track',
                           'map', 'area', 'col', 'colgroup', 'object', 'embed',
                           'param', 'slot', 'address', 'hgroup', 'search', 'ins',
                           'del', 's', 'pre', 'br']);
  const TAG_ROLES = {
    button: 'button', textarea: 'textbox', img: 'image', h1: 'heading',
    h2: 'heading', h3: 'heading', h4: 'heading', h5: 'heading', h6: 'heading',
    nav: 'navigation', main: 'main', header: 'banner', footer: 'contentinfo',
    aside: 'complementary', form: 'form', ul: 'list', ol: 'list', menu: 'list',
    li: 'listitem', table: 'table', thead: 'rowgroup', tbody: 'rowgroup',
    tfoot: 'rowgroup', tr: 'row', td: 'cell', th: 'columnheader',
    dialog: 'dialog', details: 'group', summary: 'DisclosureTriangle',
    option: 'option', optgroup: 'group', label: 'LabelText', p: 'paragraph',
    iframe: 'Iframe', frame: 'Iframe', hr: 'separator', fieldset: 'group',
    legend: 'Legend', figure: 'figure', figcaption: 'Figcaption',
    article: 'article', section: 'region', progress: 'progressbar',
    meter: 'meter', output: 'status', code: 'code', em: 'emphasis',
    strong: 'strong', blockquote: 'blockquote', caption: 'caption',
    dl: 'DescriptionList', dt: 'term', dd: 'definition', mark: 'mark',
    time: 'time', sup: 'superscript', sub: 'subscript', canvas: 'canvas',
    video: 'video', audio: 'audio', math: 'math', svg: 'image',
  };
  const INPUT_ROLES = {
    checkbox: 'checkbox', radio: 'radio', button: 'button', submit: 'button',
    reset: 'button', image: 'button', range: 'slider', number: 'spinbutton',
    search: 'searchbox', file: 'button', color: 'ColorWell', date: 'Date',
    'datetime-local': 'DateTime', month: 'DateTime', week: 'DateTime',
    time: 'InputTime', hidden: null,
  };
  // roles whose accessible name comes from their content (Chrome gives
  // paragraphs, list items, labels, cells-of-text no name of their own)
  const NAME_FROM_CONTENT = new Set(['button', 'link', 'heading', 'cell',
    'columnheader', 'rowheader', 'option', 'tab', 'menuitem', 'menuitemcheckbox',
    'menuitemradio', 'checkbox', 'radio', 'switch', 'tooltip', 'treeitem',
    'DisclosureTriangle']);
  const TEXTUAL = new Set(['textbox', 'searchbox', 'spinbutton', 'slider',
                           'combobox', 'listbox']);
  const collapse = (s) => (s || '').replace(/[\s\u00a0]+/g, ' ').trim();
  const attr = (el, n) => el.getAttribute ? el.getAttribute(n) : null;
  const style = (el, pseudo) => el.ownerDocument.defaultView.getComputedStyle(el, pseudo || null);
  const hidden = (el) => {
    if (el.hidden || attr(el, 'aria-hidden') === 'true') return true;
    const cs = style(el);
    return cs.display === 'none' || cs.visibility === 'hidden';
  };
  const BLOCKISH = /^(block|flex|grid|table|list-item|flow-root)/;
  const cssStrings = (value) => {
    // the string parts of a CSS `content` value: "a" "b" -> ab; with the
    // alternative-text form `"x" / "alt"` the accessible name is the alt
    const text = value.includes(' / ') ? value.slice(value.indexOf(' / ') + 3) : value;
    const parts = [];
    for (const m of text.matchAll(/"((?:[^"\\]|\\.)*)"/g)) parts.push(m[1]);
    return parts.join('');
  };
  const pseudoText = (el, which) => {
    const ps = style(el, which);
    if (ps.display === 'none' || ps.visibility === 'hidden') return '';
    return cssStrings(ps.content || '');
  };
  const transform = (el, text) => {
    const t = style(el).textTransform;
    if (t === 'uppercase') return text.toUpperCase();
    if (t === 'lowercase') return text.toLowerCase();
    if (t === 'capitalize') return text.replace(/\b\p{L}/gu, (ch) => ch.toUpperCase());
    return text;
  };
  // descendants with these roles never lend their text to an ancestor's
  // name (Chrome: a quote, figure or landmark inside a link stays out of it)
  const NO_CONTENT_FOR_ANCESTOR = new Set(['blockquote', 'figure', 'table', 'article',
    'main', 'navigation', 'banner', 'contentinfo', 'complementary', 'region', 'form',
    'dialog', 'alertdialog', 'grid', 'tree', 'treegrid', 'menu', 'menubar', 'toolbar',
    'tablist', 'feed', 'document', 'application']);
  const textOf = (el, depth) => {
    // name-from-content, recursively (accname step 2F): the rendered text —
    // display:none / aria-hidden subtrees skipped (visibility:hidden text
    // still counts, as in Chrome), nested alt/aria-label used, slots
    // flattened, generated content included, block boundaries kept apart
    if (depth > 40) return '';
    const out = [pseudoText(el, '::before')];
    let kids;
    if (el.localName === 'slot') kids = el.assignedNodes({flatten: true});
    else if (el.shadowRoot) kids = [...el.shadowRoot.childNodes];
    else kids = [...el.childNodes];
    for (const k of kids) {
      if (k.nodeType === Node.TEXT_NODE) { out.push(k.data); continue; }
      if (k.nodeType !== Node.ELEMENT_NODE) continue;
      if (SKIP.has(k.localName) || k.hidden || attr(k, 'aria-hidden') === 'true'
          || style(k).display === 'none') continue;
      if (NO_CONTENT_FOR_ANCESTOR.has(roleOf(k))) continue;
      let piece, own = true;
      const al = attr(k, 'aria-label');
      if (al && collapse(al)) piece = al;
      else if (k.localName === 'img' || k.localName === 'area') piece = attr(k, 'alt') || '';
      else if (k.localName === 'svg') piece = (k.querySelector(':scope > title') || {}).textContent || attr(k, 'aria-label') || '';
      else if (k.localName === 'input' && ['button', 'submit', 'reset'].includes((k.type || '').toLowerCase())) piece = k.value || '';
      else { piece = transform(k, textOf(k, depth + 1)); own = false; }
      const block = own || BLOCKISH.test(style(k).display) || k.localName === 'br';
      out.push(block ? ' ' + piece + ' ' : piece);
    }
    out.push(pseudoText(el, '::after'));
    return out.join('');
  };
  const ROLE_ALIAS = {img: 'image', presentation: 'generic', none: 'generic'};
  const roleOf = (el) => {
    const explicit = collapse(attr(el, 'role')).split(' ')[0];
    if (explicit) return ROLE_ALIAS[explicit] || explicit;
    const tag = el.localName;
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      return type in INPUT_ROLES ? INPUT_ROLES[type] : 'textbox';
    }
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'select') return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
    if (tag === 'section') return attr(el, 'aria-label') || attr(el, 'aria-labelledby') ? 'region' : 'generic';
    if (el.isContentEditable && !TAG_ROLES[tag]) return 'textbox';
    if (tag in TAG_ROLES) return TAG_ROLES[tag];
    return 'generic';
  };
  const byIds = (el, ids) => collapse(ids.split(/\s+/)
    .map((id) => { const root = el.getRootNode(); const t = (root.getElementById ? root : el.ownerDocument).getElementById(id);
                   return t ? textOf(t, 0) : ''; })
    .join(' '));
  const nameOf = (el, role) => {
    const lb = attr(el, 'aria-labelledby');
    if (lb) { const n = byIds(el, lb); if (n) return n; }
    const al = attr(el, 'aria-label');
    if (al && collapse(al)) return collapse(al);
    if (el.labels && el.labels.length) return collapse(textOf(el.labels[0], 0));
    const tag = el.localName;
    if (tag === 'img' || tag === 'area') return collapse(attr(el, 'alt'));
    if (tag === 'input' && ['button', 'submit', 'reset'].includes((el.type || '').toLowerCase()))
      return collapse(el.value) || (el.type === 'submit' ? 'Submit' : el.type === 'reset' ? 'Reset' : '');
    if (tag === 'input' && el.type === 'image') return collapse(attr(el, 'alt') || el.value);
    if (tag === 'fieldset') { const l = el.querySelector(':scope > legend'); if (l) return collapse(l.textContent); }
    if (tag === 'figure') { const c = el.querySelector(':scope > figcaption'); if (c) return collapse(c.textContent); }
    if (tag === 'table') { const c = el.querySelector(':scope > caption'); if (c) return collapse(c.textContent); }
    if (tag === 'iframe' || tag === 'frame') return collapse(attr(el, 'title') || attr(el, 'name'));
    if (tag === 'svg') { const t = el.querySelector(':scope > title'); if (t) return collapse(t.textContent); }
    if (NAME_FROM_CONTENT.has(role)) { const t = collapse(transform(el, textOf(el, 0))); if (t) return t; }
    const title = collapse(attr(el, 'title'));
    if (title) return title;
    if (TEXTUAL.has(role)) return collapse(attr(el, 'placeholder'));
    return '';
  };
  const valueOf = (el, role) => {
    const tag = el.localName;
    if (tag === 'select') {
      const sel = [...el.selectedOptions].map((o) => collapse(o.textContent));
      return sel.length ? sel.join(', ') : null;
    }
    if (tag === 'input' || tag === 'textarea') {
      const type = (el.type || '').toLowerCase();
      if (['checkbox', 'radio', 'button', 'submit', 'reset', 'image', 'file', 'hidden'].includes(type)) return null;
      if (!el.value) return null;                    // Chrome shows no value for an empty field
      // a password's el.value is the cleartext: never let it leave the page.
      // Chrome's tree reports bullets; so do we (snapshot re-masks on top).
      if (tag === 'input' && type === 'password') return '\u2022'.repeat(String(el.value).length);
      return String(el.value);
    }
    if (el.isContentEditable && role === 'textbox') return collapse(el.textContent) || null;
    if (role === 'slider' || role === 'spinbutton' || role === 'progressbar' || role === 'meter') {
      const v = attr(el, 'aria-valuetext') || attr(el, 'aria-valuenow');
      if (v != null) return String(v);
    }
    return null;
  };
  const propsOf = (el, role) => {
    const p = [];
    const add = (name, value) => p.push({name, value: {value}});
    if (el.disabled || attr(el, 'aria-disabled') === 'true' || el.closest('fieldset:disabled')) add('disabled', true);
    const tag = el.localName;
    if (tag === 'input' && (el.type === 'checkbox' || el.type === 'radio'))
      add('checked', el.indeterminate ? 'mixed' : el.checked);
    else if (attr(el, 'aria-checked') != null) add('checked', attr(el, 'aria-checked'));
    if (attr(el, 'aria-pressed') != null) add('pressed', attr(el, 'aria-pressed'));
    if (attr(el, 'aria-expanded') != null) add('expanded', attr(el, 'aria-expanded'));
    else if (tag === 'summary' && el.parentElement && el.parentElement.localName === 'details')
      add('expanded', el.parentElement.open);
    else if (role === 'combobox' && tag === 'select') add('expanded', false);  // Chrome: (collapsed)
    if (tag === 'option') add('selected', el.selected);
    else if (attr(el, 'aria-selected') != null) add('selected', attr(el, 'aria-selected'));
    if (el.required || attr(el, 'aria-required') === 'true') add('required', true);
    const inv = attr(el, 'aria-invalid');
    if (inv != null && inv !== 'false') add('invalid', inv);
    else if (el.willValidate && el.value && typeof el.checkValidity === 'function' && !el.checkValidity()) add('invalid', 'true');
    if (role === 'heading') {
      const m = /^h([1-6])$/.exec(tag);
      add('level', Number(attr(el, 'aria-level') || (m ? m[1] : 2)));
    }
    if (role === 'link' && el.href) add('url', el.href);
    if (el.readOnly || attr(el, 'aria-readonly') === 'true') add('readonly', true);
    return p;
  };
  const childrenOf = (el) => {
    const tag = el.localName;
    if (tag === 'iframe' || tag === 'frame' || tag === 'svg') return [];   // leaves
    if (tag === 'select') return [...el.querySelectorAll('option')];
    if (tag === 'details' && !el.open) {
      // a closed <details> hides everything but its <summary> through a UA
      // shadow slot — the light-DOM children's own computed style stays
      // visible, so prune here (Chrome's tree omits them)
      const s = el.querySelector(':scope > summary');
      return s ? [s] : [];
    }
    if (el.shadowRoot) return [...el.shadowRoot.childNodes];
    if (tag === 'slot') return el.assignedNodes({flatten: true});
    return [...el.childNodes];
  };
  const emit = (node) => { node.nodeId = 'ax' + (seq++); nodes.push(node); return node.nodeId; };
  const visit = (node, depth) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const text = collapse(node.data);
      if (!text) return null;
      return emit({ignored: false, role: {value: 'StaticText'}, name: {value: text}, childIds: []});
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return null;
    const el = node;
    const tag = el.localName;
    if (SKIP.has(tag) || hidden(el)) return null;
    const role = roleOf(el);
    if (role === null) return null;                      // input type=hidden
    if ((tag === 'img' || tag === 'area') && el.hasAttribute('alt') && !collapse(attr(el, 'alt')))
      return null;                                       // alt="": presentational
    const explicit = !!attr(el, 'role');
    // only an explicit tabindex makes a generic actionable (native controls
    // already have roles; <a> without href reports tabIndex 0 yet isn't focusable)
    const focusable = el.hasAttribute('tabindex') && el.tabIndex >= 0;
    const named = !!(attr(el, 'aria-label') || attr(el, 'aria-labelledby'));
    const ignored = role === 'generic' && !explicit && !focusable && !named;
    const self = {ignored, role: {value: ignored ? 'generic' : role},
                  backendDOMNodeId: registry.push(el) - 1, childIds: []};
    if (!ignored) {
      const name = nameOf(el, role);
      if (name) self.name = {value: name};
      const value = valueOf(el, role);
      if (value !== null) self.value = {value};
      const props = propsOf(el, role);
      if (props.length) self.properties = props;
    }
    const id = emit(self);
    if (maxDepth == null || depth < maxDepth) {
      for (const child of childrenOf(el)) {
        const cid = visit(child, depth + 1);
        if (cid !== null) self.childIds.push(cid);
      }
    }
    return id;
  };
  const doc = document;
  const root = {ignored: false, role: {value: 'RootWebArea'},
                name: {value: collapse(doc.title)}, childIds: [],
                backendDOMNodeId: registry.push(doc.documentElement) - 1};
  const rootId = emit(root);
  if (doc.body) {
    const bid = visit(doc.body, 1);
    if (bid !== null) root.childIds.push(bid);
  }
  return nodes;
}
'''.strip()

#: ``(index) => element`` over the registry the walker left on the page.
AX_RESOLVE_JS = '(i) => (globalThis[Symbol.for("purecdp.ax")] || [])[i] || null'

#: ``() => [[index, contentWindow], …]`` for the iframes in the registry —
#: the BiDi driver reads each window's browsing-context id off the wire.
AX_IFRAME_WINDOWS_JS = (
    '() => (globalThis[Symbol.for("purecdp.ax")] || [])'
    '.map((el, i) => (el && (el.localName === "iframe" || el.localName === "frame")) ? [i, el.contentWindow] : null)'
    '.filter(Boolean)')
