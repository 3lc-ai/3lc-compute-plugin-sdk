# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501 — the embedded CSS/JS keeps its own line lengths
"""Shared client widget: a sortable, searchable catalogue table with a per-row action.

Infrastructure plugins list what a provider offers — GPU types, instance types, sizes — and
every one of them needs the same thing: columns a person can sort (price, memory, availability),
a search box, one button per row that starts a node of that type, and a row that opens to show
what else the provider knows about it. Building that per plugin gave two different lists with
two different bugs; this is the one implementation, injected into a fragment that asks for it.

Ask for it by giving the table's container the class ``tlc-catalog`` (:data:`CATALOG_MARKER`);
the worker's ``/ui`` handler then injects :data:`CATALOG_TABLE_JS` ahead of the fragment's own
script and ``window.TlcCatalog`` is available::

    var table = TlcCatalog.mount(document.getElementById('gpu-table'), {
      rowId: function(r) { return r.id; },
      columns: [
        { key: 'name', label: 'GPU', type: 'text', sub: function(r) { return r.id; } },
        { key: 'memory_gb', label: 'VRAM', type: 'num', unit: 'GB' },
        { key: 'price_hour', label: 'Price', type: 'price' },
        { key: 'stock', label: 'Stock', type: 'status', status: function(r) { return { level: 'ok', text: 'in stock' }; } },
      ],
      sort: { key: 'memory_gb', dir: 'desc' },
      search: { placeholder: 'Search GPU types' },
      selectedId: 'NVIDIA A40',
      onSelect: function(r) { ... },                       // the Default radio column; a row click only opens details
      action: { label: 'Spin up', onClick: function(r) { ... }, disabled: function(r) { return r.available === false; } },
      details: function(r) { return [['Secure cloud', '$1.20/h'], ['Community cloud', '$0.80/h']]; },
      unavailable: function(r) { return r.available === false; },
    });
    table.update(rows);        // re-render with fresh rows; keeps sort, search, expanded rows
    table.setSelected(id);

Column types: ``text`` (left, optional ``sub`` line), ``num`` (right, tabular, optional ``unit``),
``price`` (right, ``$0.00/h``; a missing price sorts last and prints "—"), ``status`` (a dot and
words from ``column.status(row)`` → ``{level: ok|warn|bad|muted, text, title?}``). Every column
may give ``value(row)`` (sort key and default text), ``format(row)`` (display text), ``title(row)``
``priority`` (3 = hidden on narrow screens) and ``width`` (a fixed column width, so a header that
changes with a mode switch does not move the other columns). A row click opens or closes its details
(Enter/Space too). With ``onSelect`` the table gains a leading "Default" column of radio buttons
(header via ``selectHeader``): choosing the default is one visible click, and looking at a type
never changes a saved default. The
selected row uses the hub's selection colours with readable text on top; open details sit on the
page background, distinct from hover. Headers announce their sort.
"""

from __future__ import annotations

CATALOG_MARKER = "tlc-catalog"

CATALOG_TABLE_CSS = r"""
.tcl-root { position: relative; }
.tcl-bar { display: flex; align-items: center; gap: 10px; margin: 0 0 8px; flex-wrap: wrap; }
.tcl-search { position: relative; flex: 1 1 220px; max-width: 360px; }
.tcl-search input {
  width: 100%; box-sizing: border-box; font: inherit; font-size: 12.5px; padding: 6px 28px 6px 30px;
  border: 1px solid var(--border, #d0d4dc); border-radius: 8px; background: var(--bg-card, transparent); color: inherit;
}
.tcl-search input:focus { outline: none; border-color: var(--ac, var(--accent, #4f7be8)); box-shadow: 0 0 0 2px var(--ac-soft, rgba(79,123,232,.18)); }
.tcl-search svg { position: absolute; left: 10px; top: 50%; width: 13px; height: 13px; transform: translateY(-50%); color: var(--text-muted, #7a8290); pointer-events: none; }
.tcl-search .tcl-clear {
  position: absolute; right: 6px; top: 50%; transform: translateY(-50%); border: 0; background: transparent; cursor: pointer;
  color: var(--text-muted, #7a8290); font-size: 14px; line-height: 1; padding: 2px 5px; border-radius: 6px;
}
.tcl-search .tcl-clear:hover { color: inherit; background: var(--row-hover-bg, rgba(127,127,127,.12)); }
.tcl-count { font-size: 11.5px; color: var(--text-muted, #7a8290); font-variant-numeric: tabular-nums; white-space: nowrap; }
.tcl-wrap { overflow-x: auto; border: 1px solid var(--border-light, var(--border, #e3e6ec)); border-radius: 10px; }
table.tcl { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 12.5px; }
.tcl th, .tcl td { padding: 9px 12px; text-align: left; vertical-align: middle; border-bottom: 1px solid var(--border-light, var(--border, #e3e6ec)); }
.tcl thead th {
  font-size: 10.5px; font-weight: 650; letter-spacing: .04em; text-transform: uppercase; color: var(--text-muted, #7a8290);
  background: var(--bg-card, transparent); position: sticky; top: 0; z-index: 1; white-space: nowrap; user-select: none;
}
.tcl thead th.tcl-sortable { cursor: pointer; }
.tcl thead th.tcl-sortable:hover { color: inherit; }
.tcl thead th .tcl-arrow { display: inline-block; width: 10px; margin-left: 4px; opacity: .35; font-size: 9px; }
.tcl thead th[aria-sort="ascending"] .tcl-arrow, .tcl thead th[aria-sort="descending"] .tcl-arrow { opacity: 1; color: var(--ac, var(--accent, #4f7be8)); }
.tcl thead th[aria-sort="ascending"], .tcl thead th[aria-sort="descending"] { color: inherit; }
.tcl .tcl-num, .tcl .tcl-price { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.tcl th.tcl-num, .tcl td.tcl-num { min-width: 72px; }
.tcl th.tcl-price, .tcl td.tcl-price { min-width: 128px; }
.tcl .tcl-act { text-align: right; width: 1%; white-space: nowrap; }
.tcl th.tcl-pick, .tcl td.tcl-pick { width: 1%; white-space: nowrap; text-align: center; padding-left: 6px; padding-right: 6px; }
.tcl td.tcl-pick input { margin: 0; width: 15px; height: 15px; cursor: pointer; accent-color: var(--ac, var(--accent, #4f7be8)); }
.tcl tbody tr.tcl-row { cursor: pointer; transition: background .12s ease; }
.tcl tbody tr.tcl-row:hover, .tcl tbody tr.tcl-row:focus-visible { background: var(--row-hover-bg, rgba(127,127,127,.08)); outline: none; }
.tcl tbody tr.tcl-row.open td, .tcl tr.tcl-details td { background: var(--bg, color-mix(in srgb, var(--text-muted, #7a8290) 6%, transparent)); }
.tcl tbody tr.tcl-row.open td { border-bottom-color: transparent; }
.tcl tbody tr.tcl-row.sel { outline: var(--selection-outline, 1px solid var(--golden-border, var(--ac, var(--accent, #4f7be8)))); outline-offset: -1px; border-radius: var(--radius, 6px); }
.tcl tbody tr.tcl-row.sel td { background: var(--row-selected-bg, color-mix(in srgb, var(--ac, var(--accent, #4f7be8)) 10%, transparent)); }
/* On the selection colour (orange in the 3LC light theme) the accent chip and muted text lose contrast: use the theme's text colours there. */
.tcl tbody tr.tcl-row.sel .tcl-tag { color: var(--text, inherit); background: var(--bg-card, rgba(255,255,255,.6)); border: 1px solid var(--border, transparent); }
.tcl tbody tr.tcl-row.sel .tcl-sub, .tcl tbody tr.tcl-row.sel .tcl-unit, .tcl tbody tr.tcl-row.sel .tcl-chev { color: var(--text-secondary, var(--text, inherit)); }
.tcl tbody tr.tcl-row.sel .tcl-price strong { color: var(--text, inherit); }
.tcl tbody tr.tcl-row.unavail td { color: var(--text-muted, #7a8290); }
.tcl tbody tr.tcl-row.unavail .tcl-main { color: var(--text-muted, #7a8290); }
.tcl tbody tr:last-child td { border-bottom: 0; }
.tcl .tcl-main { font-weight: 600; display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.tcl .tcl-sub { font-size: 10.5px; color: var(--text-muted, #7a8290); margin-top: 2px; }
.tcl .tcl-tag {
  font-size: 9.5px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; padding: 1px 6px; border-radius: 999px;
  color: var(--ac, var(--accent, #4f7be8)); background: var(--ac-soft, rgba(79,123,232,.14));
}
.tcl .tcl-unit { font-size: 10px; color: var(--text-muted, #7a8290); margin-left: 3px; font-weight: 500; }
.tcl .tcl-price strong { font-weight: 650; }
.tcl .tcl-st { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.tcl .tcl-st::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
.tcl .tcl-st-ok { color: var(--ok, #2e9e5b); }
.tcl .tcl-st-warn { color: var(--warn, #c98a00); }
.tcl .tcl-st-bad { color: var(--bad, #d05353); }
.tcl .tcl-st-muted { color: var(--text-muted, #7a8290); }
.tcl .tcl-st-muted::before { background: transparent; border: 1px solid currentColor; box-sizing: border-box; }
.tcl .tcl-chev { display: inline-block; width: 14px; color: var(--text-muted, #7a8290); font-size: 10px; transition: transform .15s ease; }
.tcl tr.open .tcl-chev { transform: rotate(90deg); }
.tcl .tcl-btn {
  font: inherit; font-size: 11.5px; font-weight: 650; padding: 5px 11px; border-radius: 8px; cursor: pointer; white-space: nowrap;
  color: #fff; background: var(--ac, var(--accent, #4f7be8)); border: 1px solid transparent; transition: filter .12s ease, transform .12s ease;
}
.tcl .tcl-btn:hover { filter: brightness(1.08); }
.tcl .tcl-btn:active { transform: translateY(1px); }
.tcl .tcl-btn:focus-visible { outline: 2px solid var(--ac, var(--accent, #4f7be8)); outline-offset: 2px; }
.tcl .tcl-btn[disabled] { cursor: not-allowed; opacity: .45; filter: grayscale(.4); }
.tcl tr.tcl-details td { padding: 0 12px 12px 38px; }
.tcl .tcl-dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 8px 22px; padding: 10px 0 2px; }
.tcl .tcl-dl div { min-width: 0; }
.tcl .tcl-dl dt { font-size: 10px; font-weight: 650; letter-spacing: .04em; text-transform: uppercase; color: var(--text-muted, #7a8290); margin: 0 0 2px; }
.tcl .tcl-dl dd { margin: 0; font-size: 12px; overflow-wrap: anywhere; }
.tcl .tcl-dl dd.tcl-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
.tcl .tcl-empty { padding: 22px 12px; text-align: center; color: var(--text-muted, #7a8290); font-size: 12.5px; }
@media (max-width: 760px) { .tcl [data-pri="3"] { display: none; } }
@media (prefers-reduced-motion: reduce) { .tcl tbody tr.tcl-row, .tcl .tcl-chev, .tcl .tcl-btn { transition: none; } }
"""

CATALOG_TABLE_JS = (
    r"""
// ── Shared catalogue table (sortable, searchable, per-row action, expandable rows) ──
(function(){
  if (window.TlcCatalog) { return; }
  var CSS = """
    + repr(CATALOG_TABLE_CSS)
    + r""";
  function ensureCss() {
    if (document.getElementById('tcl-css')) return;
    var s = document.createElement('style'); s.id = 'tcl-css'; s.textContent = CSS; document.head.appendChild(s);
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function money(v) { return isNum(v) && v > 0 ? '$' + (v < 10 ? v.toFixed(2) : v.toFixed(v < 100 ? 2 : 0)) + '/h' : ''; }
  function rawValue(col, row) {
    if (typeof col.value === 'function') return col.value(row);
    return row[col.key];
  }
  function displayText(col, row) {
    if (typeof col.format === 'function') { var f = col.format(row); return f === undefined || f === null ? '' : String(f); }
    var v = rawValue(col, row);
    if (col.type === 'price') return money(typeof v === 'string' ? parseFloat(v) : v);
    if (v === undefined || v === null || v === '') return '';
    return String(v);
  }
  function sortValue(col, row) {
    var v = rawValue(col, row);
    if (col.type === 'num' || col.type === 'price') {
      var n = typeof v === 'string' ? parseFloat(v) : v;
      return isNum(n) && !(col.type === 'price' && n <= 0) ? n : null;
    }
    if (col.type === 'status' && typeof col.status === 'function') {
      var st = col.status(row) || {};
      return { ok: 0, warn: 1, muted: 2, bad: 3 }[st.level] !== undefined ? { ok: 0, warn: 1, muted: 2, bad: 3 }[st.level] : 2;
    }
    return v === undefined || v === null ? '' : String(v).toLowerCase();
  }
  function compare(a, b, dir) {
    var na = a === null || a === undefined || a === '', nb = b === null || b === undefined || b === '';
    if (na && nb) return 0;
    if (na) return 1;            // missing values sort last whatever the direction
    if (nb) return -1;
    var r = (typeof a === 'number' && typeof b === 'number') ? a - b : String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
    return dir === 'desc' ? -r : r;
  }

  function mount(container, opts) {
    if (!container) throw new Error('TlcCatalog.mount: container is required');
    ensureCss();
    opts = opts || {};
    var columns = (opts.columns || []).slice();
    var rowId = opts.rowId || function(r) { return r.id; };
    var state = {
      rows: opts.rows || [],
      sort: opts.sort ? { key: opts.sort.key, dir: opts.sort.dir || 'asc' } : null,
      query: '',
      open: {},
      selected: opts.selectedId || '',
    };
    var searchKeys = (opts.search && opts.search.keys) || columns.filter(function(c) { return c.type !== 'num' && c.type !== 'price'; }).map(function(c) { return c.key; });

    container.classList.add('tcl-root');
    container.textContent = '';
    var bar = el('div', 'tcl-bar');
    var search = el('div', 'tcl-search');
    var input = el('input'); input.type = 'search'; input.setAttribute('aria-label', (opts.search && opts.search.placeholder) || 'Search');
    input.placeholder = (opts.search && opts.search.placeholder) || 'Search';
    var icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('viewBox', '0 0 24 24'); icon.setAttribute('fill', 'none'); icon.setAttribute('stroke', 'currentColor'); icon.setAttribute('stroke-width', '2');
    icon.innerHTML = '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>';
    var clear = el('button', 'tcl-clear', '×'); clear.type = 'button'; clear.title = 'Clear search'; clear.setAttribute('aria-label', 'Clear search'); clear.style.display = 'none';
    search.appendChild(icon); search.appendChild(input); search.appendChild(clear);
    var count = el('span', 'tcl-count');
    bar.appendChild(search); bar.appendChild(count);
    var wrap = el('div', 'tcl-wrap');
    var table = el('table', 'tcl');
    var thead = el('thead'); var headRow = el('tr'); thead.appendChild(headRow);
    var tbody = el('tbody');
    table.appendChild(thead); table.appendChild(tbody); wrap.appendChild(table);
    container.appendChild(bar); container.appendChild(wrap);

    // Header: an expander column, the Default radio column (when a default can be chosen), the declared columns, the action column.
    var chevTh = el('th'); chevTh.setAttribute('aria-hidden', 'true'); chevTh.style.width = '22px'; headRow.appendChild(chevTh);
    var canPick = typeof opts.onSelect === 'function';
    var pickName = 'tcl-pick-' + Math.random().toString(36).slice(2, 8);
    if (canPick) { var pickTh = el('th', 'tcl-pick', opts.selectHeader || 'Default'); pickTh.setAttribute('scope', 'col'); pickTh.title = opts.selectTitle || 'The type new nodes use when none is named'; headRow.appendChild(pickTh); }
    columns.forEach(function(col) {
      var th = el('th', 'tcl-sortable' + (col.type === 'num' || col.type === 'price' ? ' tcl-' + col.type : ''));
      th.setAttribute('scope', 'col');
      if (col.width) th.style.width = typeof col.width === 'number' ? col.width + 'px' : String(col.width);  // a fixed column keeps the layout still when its header changes
      if (col.priority) th.setAttribute('data-pri', String(col.priority));
      th.appendChild(document.createTextNode(col.label || col.key));
      th.appendChild(el('span', 'tcl-arrow', '▲'));
      th.tabIndex = 0;
      th.setAttribute('role', 'columnheader');
      function toggleSort() {
        // First click: numbers largest first, prices cheapest first, text A-Z; a second click flips.
        if (state.sort && state.sort.key === col.key) state.sort.dir = state.sort.dir === 'asc' ? 'desc' : 'asc';
        else state.sort = { key: col.key, dir: col.type === 'num' ? 'desc' : 'asc' };
        render();
      }
      th.addEventListener('click', toggleSort);
      th.addEventListener('keydown', function(ev) { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggleSort(); } });
      col._th = th;
      headRow.appendChild(th);
    });
    if (opts.action) { var actTh = el('th', 'tcl-act'); actTh.setAttribute('scope', 'col'); actTh.appendChild(document.createTextNode(opts.action.header || '')); headRow.appendChild(actTh); }
    var colSpan = columns.length + 1 + (canPick ? 1 : 0) + (opts.action ? 1 : 0);

    input.addEventListener('input', function() { state.query = input.value.trim().toLowerCase(); clear.style.display = state.query ? '' : 'none'; render(); });
    clear.addEventListener('click', function() { input.value = ''; state.query = ''; clear.style.display = 'none'; render(); input.focus(); });

    function matches(row) {
      if (!state.query) return true;
      var hay = [];
      searchKeys.forEach(function(k) {
        var col = null;
        for (var i = 0; i < columns.length; i++) if (columns[i].key === k) { col = columns[i]; break; }
        if (col) { hay.push(displayText(col, row)); if (typeof col.sub === 'function') hay.push(col.sub(row) || ''); }
        else if (row[k] !== undefined && row[k] !== null) hay.push(String(row[k]));
      });
      if (typeof opts.searchText === 'function') hay.push(opts.searchText(row) || '');
      var text = hay.join(' ').toLowerCase();
      return state.query.split(/\s+/).every(function(w) { return text.indexOf(w) !== -1; });
    }

    function sorted(rows) {
      if (!state.sort) return rows;
      var col = null;
      for (var i = 0; i < columns.length; i++) if (columns[i].key === state.sort.key) { col = columns[i]; break; }
      if (!col) return rows;
      var keyed = rows.map(function(r, i) { return { r: r, k: sortValue(col, r), i: i }; });
      keyed.sort(function(a, b) { return compare(a.k, b.k, state.sort.dir) || (a.i - b.i); });
      return keyed.map(function(x) { return x.r; });
    }

    function detailsRow(row) {
      var pairs = typeof opts.details === 'function' ? (opts.details(row) || []) : [];
      var tr = el('tr', 'tcl-details'); var td = el('td'); td.colSpan = colSpan;
      if (!pairs.length) td.appendChild(el('div', 'tcl-sub', 'No further details from the provider.'));
      else {
        var dl = el('dl', 'tcl-dl');
        pairs.forEach(function(p) {
          if (!p || p.length < 2 || p[1] === undefined || p[1] === null || p[1] === '') return;
          var d = el('div'); d.appendChild(el('dt', null, p[0]));
          var dd = el('dd', p[2] === 'mono' ? 'tcl-mono' : null, p[1]); d.appendChild(dd); dl.appendChild(d);
        });
        td.appendChild(dl);
      }
      tr.appendChild(td); return tr;
    }

    function render() {
      columns.forEach(function(col) {
        var active = state.sort && state.sort.key === col.key;
        col._th.setAttribute('aria-sort', active ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
        col._th.querySelector('.tcl-arrow').textContent = active && state.sort.dir === 'desc' ? '▼' : '▲';
      });
      var shown = sorted(state.rows.filter(matches));
      count.textContent = state.rows.length ? (shown.length === state.rows.length ? state.rows.length + (opts.noun ? ' ' + opts.noun : '') : shown.length + ' of ' + state.rows.length + (opts.noun ? ' ' + opts.noun : '')) : '';
      tbody.textContent = '';
      if (!shown.length) {
        var tr0 = el('tr'); var td0 = el('td', 'tcl-empty', state.rows.length ? 'Nothing matches "' + state.query + '".' : (opts.empty || 'Nothing to show.'));
        td0.colSpan = colSpan; tr0.appendChild(td0); tbody.appendChild(tr0); return;
      }
      shown.forEach(function(row) {
        var id = String(rowId(row));
        var tr = el('tr', 'tcl-row'); tr.setAttribute('data-id', id); tr.tabIndex = 0;
        var isOpen = !!state.open[id];
        var unavail = typeof opts.unavailable === 'function' && opts.unavailable(row);
        if (id === state.selected) tr.classList.add('sel');
        if (isOpen) tr.classList.add('open');
        if (unavail) tr.classList.add('unavail');
        tr.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
        var chev = el('td'); chev.appendChild(el('span', 'tcl-chev', '▶')); tr.appendChild(chev);
        if (canPick) {
          var pick = el('td', 'tcl-pick');
          var radio = el('input'); radio.type = 'radio'; radio.name = pickName; radio.checked = id === state.selected;
          radio.setAttribute('aria-label', 'Use ' + displayText(columns[0], row) + ' as the default for new nodes');
          radio.title = radio.checked ? 'The default for new nodes' : 'Use as the default for new nodes';
          radio.addEventListener('click', function(ev) { ev.stopPropagation(); });
          radio.addEventListener('change', function() { if (!radio.checked) return; state.selected = id; opts.onSelect(row); render(); });
          pick.appendChild(radio); tr.appendChild(pick);
        }
        columns.forEach(function(col) {
          var td = el('td', col.type === 'num' || col.type === 'price' ? 'tcl-' + col.type : null);
          if (col.priority) td.setAttribute('data-pri', String(col.priority));
          if (typeof col.title === 'function') { var t = col.title(row); if (t) td.title = t; }
          if (col.type === 'status' && typeof col.status === 'function') {
            var st = col.status(row) || {};
            var span = el('span', 'tcl-st tcl-st-' + (st.level || 'muted'), st.text || '');
            if (st.title) span.title = st.title;
            td.appendChild(span);
          } else if (col.type === 'text') {
            var main = el('div', 'tcl-main'); main.appendChild(document.createTextNode(displayText(col, row)));
            if (id === state.selected && opts.selectedTag !== false && !canPick) main.appendChild(el('span', 'tcl-tag', opts.selectedTag || 'default'));
            td.appendChild(main);
            if (typeof col.sub === 'function') { var sub = col.sub(row); if (sub) td.appendChild(el('div', 'tcl-sub', sub)); }
          } else if (col.type === 'num') {
            var text = displayText(col, row);
            if (text) { td.appendChild(document.createTextNode(text)); if (col.unit) td.appendChild(el('span', 'tcl-unit', col.unit)); }
            else td.appendChild(el('span', 'tcl-sub', '—'));
          } else if (col.type === 'price') {
            var p = displayText(col, row);
            if (p) { var strong = el('strong', null, p); td.appendChild(strong); } else td.appendChild(el('span', 'tcl-sub', '—'));
          } else {
            td.appendChild(document.createTextNode(displayText(col, row)));
          }
          tr.appendChild(td);
        });
        if (opts.action) {
          var act = el('td', 'tcl-act');
          var btn = el('button', 'tcl-btn', typeof opts.action.label === 'function' ? opts.action.label(row) : (opts.action.label || 'Start'));
          btn.type = 'button';
          var dis = typeof opts.action.disabled === 'function' ? opts.action.disabled(row) : false;
          if (dis) { btn.disabled = true; if (typeof dis === 'string') btn.title = dis; }
          else if (opts.action.title) btn.title = typeof opts.action.title === 'function' ? opts.action.title(row) : opts.action.title;
          btn.addEventListener('click', function(ev) { ev.stopPropagation(); if (opts.action.onClick) opts.action.onClick(row, btn); });
          act.appendChild(btn); tr.appendChild(act);
        }
        function toggle() {
          state.open[id] = !state.open[id];   // open or close the details; the default is chosen inside them
          render();
          var again = tbody.querySelector('tr.tcl-row[data-id="' + id.replace(/"/g, '\\"') + '"]');
          if (again) again.focus({ preventScroll: true });
        }
        tr.addEventListener('click', function(ev) { if (ev.target && ev.target.closest && ev.target.closest('button, a, input')) return; toggle(); });
        tr.addEventListener('keydown', function(ev) { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggle(); } });
        tbody.appendChild(tr);
        if (isOpen) tbody.appendChild(detailsRow(row));
      });
    }

    render();
    return {
      update: function(rows) { state.rows = rows || []; render(); },
      setSelected: function(id) { state.selected = id === undefined || id === null ? '' : String(id); render(); },
      getSelected: function() { return state.selected; },
      setQuery: function(q) { input.value = q || ''; state.query = String(q || '').trim().toLowerCase(); clear.style.display = state.query ? '' : 'none'; render(); },
      destroy: function() { container.textContent = ''; container.classList.remove('tcl-root'); },
    };
  }

  window.TlcCatalog = { mount: mount, money: money };
})();
"""
)


def catalog_table_script() -> str:
    """The injectable client (see the module docstring)."""
    return CATALOG_TABLE_JS
