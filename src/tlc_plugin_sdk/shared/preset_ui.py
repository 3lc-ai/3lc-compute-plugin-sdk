# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared preset bar for plugins: pick a saved setup, change it, save it under a name.

A JS module injected into a plugin's UI fragment, like ``config_ui.py`` (which it supersedes) and
``alias_ui.py``. The plugin calls ``preset_ui_script()`` and injects the result with
``inject_scripts(raw, preset_ui_script())`` (see ``ui_inject.py``).

The module defines one factory::

    var presets = _tlcPluginPresets({
      pluginId: "yolo",
      apiPath: "/api/plugins/yolo/projects",   // the plugin's config routes: list, save, get, delete
      mount: "tr-presets",                      // the element the bar renders into
      formRoot: "tr-form",                      // input/change events under it mark the form as changed
      collect: function () { ... },             // form -> values (no id, no name)
      apply: function (values) { ... },         // values -> form; may return a promise
      defaults: function () { ... },            // the form's default values
      inputs: ["train_table_url", "val_table_url"],
      contextInputs: function () { ... },       // {key: value} when opened from a table, else null
      autoName: function (values) { ... },      // the suggested name for a new preset
      summary: function (values) { ... },       // optional: a short hint for the list, e.g. "yolo11n · detect"
    });
    presets.init();

The doc block inside ``PRESET_UI_JS`` has the full API. The routes it needs are the config CRUD a
plugin already serves: ``GET <apiPath>``, ``POST <apiPath>`` (``id`` empty for a new preset),
``GET <apiPath>/{id}`` and ``POST <apiPath>/{id}/delete``.
"""

from __future__ import annotations

PRESET_UI_JS = r"""
/* ── Shared plugin presets ─────────────────────────────────────────
 *
 * One bar at the top of the form:
 *
 *   Preset  [ Small objects 1280 · modified   ⌄ ]  Save changes  Save as…
 *
 * A preset contains the complete setup, including inputs, project, destination and run name.
 * Explicitly picking one restores that setup. Opening from external inputs starts a fresh,
 * unsaved setup based on defaults, never a previously selected preset or draft.
 * Only Save writes a preset, a run never does. The select lists "Default settings"
 * and then the saved presets as the server lists them, each with a hint and its age. The selected
 * entry gains " · modified" once any field differs; Save changes updates it. Edited defaults
 * show "Default settings · modified". Picking again reverts the whole setup.
 *
 * Save as… opens a small naming dialog without replacing the preset bar. The name is prefilled
 * with a free name (the preset the form came from, else the plugin's auto name), text selected:
 * Enter or Save saves, Escape or Cancel closes and returns focus to Save as….
 * A taken name asks "Replace “name”?" first.
 *
 * Each saved preset says when it was last used for a run ("used 2 days ago", from the `last_run` the
 * plugin stamps), else when it was saved ("saved last week").
 *
 * Deleting asks nothing. On a Hub whose dropdown list can remove options (`TlcPicker.onRemove`),
 * each saved preset has a trash button in the list, which keeps the list open, and the time is the
 * row's muted hint. On an older Hub the trash sits beside the select and deletes the selected
 * preset, and the time is part of the label. Deleting the preset the form came from leaves the form
 * as it is, now Default settings · modified if it differs from defaults.
 *
 * A draft (the whole form and its normalized baseline) lives in localStorage, scoped by deployment,
 * plugin, storage and project. It restores only in the same context when opened without external
 * inputs. Old unscoped drafts and legacy selections are ignored.
 *
 * `contextInputs()` seeds a new setup; it never overrides later manual changes or explicit picks. After an accepted
 * submission, clear the run name in the form and call submitted() to remove that context's draft.
 *
 * Options:
 *   pluginId      {string}    manifest id; keys the draft
 *   apiPath       {string}    REST base, e.g. "/api/plugins/yolo/projects"
 *   mount         {string}    id of the element the bar renders into
 *   formRoot      {string}    id of the form's root; `input`/`change` under it call changed()
 *   collect       {function}  () => values (no id, no name)
 *   apply         {function}  (values) => void | Promise; fills the form
 *   defaults      {function}  () => the form's default values
 *   inputs        {string[]}  the keys that are inputs (tables, folders)
 *   contextInputs {function}  () => {key: value} | null
 *   autoName      {function}  (values) => string
 *   summary       {function}  (values) => string or [part, …], shown after the name; empty parts
 *                             are left out (optional)
 *   draftDelay    {number}    ms before a change is written as the draft (default 300)
 *
 * Returns:
 *   init()         render, load the list, restore this context's draft or seed defaults with its inputs
 *   changed()      the form changed in a way no DOM event reports (JS state); recompute and save the draft
 *   refresh()      reload the list
 *   pick(id)       apply a preset ("" for Default settings), as the select does
 *   openSave()     open the naming dialog; save() saves it; closeSave() dismisses it
 *   update()       explicitly save the full setup into the selected preset
 *   submitted()    clear this draft after a job is accepted (never on failed submission)
 *   remove(id)     delete a preset (the selected one without an id); asks nothing
 *   origin()       {kind: "defaults"|"preset", id, name}: where the form came from
 *   modified()     whether the form differs from its origin
 *   presetId()     the selected preset's id when the form is unchanged from it, else ""
 *   presetName()   its name likewise, for the run body's `project_config.name`
 *   contextInputs() the initial launch context, or null
 * ──────────────────────────────────────────────────────────────── */
function _tlcPresetAge(iso, nowMs) {
  if (!iso) return "";
  var t = Date.parse(iso);
  if (isNaN(t)) return "";
  var now = nowMs == null ? Date.now() : nowMs;
  var s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return "just now";
  var m = Math.round(s / 60);
  if (m < 60) return m + " min ago";
  var h = Math.round(m / 60);
  if (h < 24) return h + " h ago";
  var d = Math.round(h / 24);
  if (d <= 1) return "yesterday";
  if (d < 7) return d + " days ago";
  if (d < 14) return "last week";
  if (d < 30) return Math.round(d / 7) + " weeks ago";
  var date = new Date(t);
  var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var label = months[date.getMonth()] + " " + date.getDate();
  if (date.getFullYear() !== new Date(now).getFullYear()) label += " " + date.getFullYear();
  return label;
}

/* `name`, or "name (2)", "name (3)", …: the first not in `taken` (case-insensitive). */
function _tlcPresetUniqueName(name, taken) {
  var lower = {};
  (taken || []).forEach(function (n) { lower[String(n || "").trim().toLowerCase()] = true; });
  var base = String(name || "").trim();
  if (!lower[base.toLowerCase()]) return base;
  for (var i = 2; i < 10000; i++) {
    var candidate = base + " (" + i + ")";
    if (!lower[candidate.toLowerCase()]) return candidate;
  }
  return base;
}

/* JSON with sorted keys, so two objects with the same content compare equal whatever their order. */
function _tlcPresetStable(value) {
  if (Array.isArray(value)) return "[" + value.map(_tlcPresetStable).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map(function (k) {
      return JSON.stringify(k) + ":" + _tlcPresetStable(value[k]);
    }).join(",") + "}";
  }
  return JSON.stringify(value === undefined ? null : value);
}

function _tlcPluginPresets(opts) {
  var pluginId = opts.pluginId || "";
  var apiPath = opts.apiPath || "";
  var mountId = opts.mount;
  var formRootId = opts.formRoot || "";
  var collect = opts.collect;
  var apply = opts.apply;
  var defaults = opts.defaults || function () { return {}; };
  var inputs = opts.inputs || [];
  var contextInputs = opts.contextInputs || function () { return null; };
  var tableContext = opts.tableContext || null;
  var autoName = opts.autoName || function () { return pluginId + " preset"; };
  var summary = opts.summary || function () { return ""; };
  var draftDelay = opts.draftDelay == null ? 300 : opts.draftDelay;

  var ID_FIELDS = { id: 1, name: 1, created: 1, last_run: 1 };
  // Keep the bar stable. The native dialog supplies modal focus behavior; shared Hub classes
  // supply its surfaces, header and buttons. Position near the bar in tall flow-layout frames.
  var CSS =
    ".tlc-presets select" +
    " { flex: 0 1 320px; min-width: 0; max-width: 320px; height: 36px; box-sizing: border-box;" +
    " font-size: 12px; font-weight: 500; }" +
    ".tlc-presets .tlc-presets-mode { display: inline-flex; align-items: center; gap: 8px; min-width: 0; }" +
    ".tlc-presets-note { font-size: 12px; color: var(--text-muted); }" +
    ".tlc-presets-note.is-error { color: var(--danger); }" +
    ".tlc-presets .tlc-presets-icon { display: inline-flex; padding: 4px 6px; line-height: 0; }" +
    ".tlc-presets .tlc-presets-icon svg { width: 14px; height: 14px; }" +
    ".tlc-presets-dialog { margin: 0; padding: 0; inset: auto; color: var(--text); }" +
    ".tlc-presets-dialog:not([open]) { display: none; }" +
    ".tlc-presets-dialog > .modal-header { margin: 0; padding: 14px 18px 12px; }" +
    ".tlc-presets-dialog .modal-body { padding: 16px 18px 18px; }" +
    ".tlc-presets-dialog label { display: block; margin-bottom: 8px; font-size: 12px; font-weight: 600; }" +
    ".tlc-presets-dialog input { width: 100%; box-sizing: border-box; }" +
    ".tlc-presets-dialog .tlc-presets-note { margin: 10px 0 0; }" +
    ".tlc-presets-dialog .tlc-presets-note:empty { display: none; }";
  var TRASH =
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"' +
    ' stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M2.5 4.5h11M6.5 2.5h3M4 4.5l.7 9h6.6l.7-9M6.5 7v4.5M9.5 7v4.5"/></svg>';

  var list = [];                                       // the saved presets, as the server lists them
  var origin = { kind: "defaults", id: "", name: "" }; // where the form came from
  var originValues = null;                             // the form as it was at the origin
  var ctx = null;                                      // initial launch values, never reapplied on preset picks
  var isModified = false;
  var draftTimer = null;
  var confirmReplaceId = "";                           // the preset a second Save will replace
  var inList = false;                                  // the Hub's list deletes presets (TlcPicker.onRemove)
  var els = {};
  var applying = false;
  var applyingInput;
  var activeApply = Promise.resolve();
  var saving = false;
  var initialized = false;
  var applyChain = Promise.resolve();
  var scopeKey = "";
  var tableIdentity = null;
  var lastForm = null;
  var tableUndo = null;
  var tableNoticeTimer = null;

  /* ── Host access ── */
  function api() { return (typeof window !== "undefined" && window.PLUGIN_API) || {}; }
  function fetchJson(url, reqOpts) {
    var a = api();
    var p = a.authFetch ? a.authFetch(url, reqOpts || {}) : fetch(url, reqOpts || {});
    return p.then(function (r) { return r.json(); });
  }
  function base() {
    var a = api();
    var computeUrl = (a.getConfig && a.getConfig("compute_service_url")) || "";
    return computeUrl + apiPath;
  }
  function toast(message, type) {
    var a = api();
    if (a.showToast) { try { a.showToast(message, type); } catch (e) { /* the host's toast is optional */ } }
  }
  function storage() {
    try { return window.localStorage; } catch (e) { return null; }
  }
  function draftKey() {
    var a = api();
    var computeUrl = (a.getConfig && a.getConfig("compute_service_url")) || "";
    return "tlc-preset-draft:v2:" + computeUrl + ":" + pluginId + ":" + scopeKey;
  }

  /* ── Elements ── */
  function el(suffix) { return document.getElementById(mountId + "-" + suffix); }
  function show(e) { if (e) e.style.display = ""; }
  function hide(e) { if (e) e.style.display = "none"; }
  function setText(e, text) { if (e) e.textContent = text; }

  function render() {
    var mount = document.getElementById(mountId);
    if (!mount) return;
    if (!document.getElementById("tlc-presets-style")) {
      var style = document.createElement("style");
      style.id = "tlc-presets-style";
      style.textContent = CSS;
      document.head.appendChild(style);
    }
    var m = mountId;
    mount.innerHTML =
      '<div class="plugin-config-bar tlc-presets" id="' + m + '-bar">' +
        '<span class="bar-label">Preset</span>' +
        '<select id="' + m + '-select" class="form-control" aria-label="Preset"></select>' +
        '<span class="tlc-presets-mode" id="' + m + '-mode-pick">' +
          '<button type="button" class="btn btn-secondary btn-sm" id="' + m + '-update"' +
            ' style="display:none">Save changes</button>' +
          '<button type="button" class="btn btn-secondary btn-sm" id="' + m + '-saveas">Save as…</button>' +
          '<button type="button" class="btn btn-ghost btn-sm tlc-presets-icon" id="' + m + '-delete"' +
            ' title="Delete this preset" aria-label="Delete this preset">' + TRASH + '</button>' +
          '<span class="tlc-presets-note" id="' + m + '-note" role="status" aria-live="polite"></span>' +
          '<button type="button" class="btn btn-ghost btn-sm" id="' + m + '-undo-table"' +
            ' style="display:none">Undo</button>' +
        '</span>' +
      '</div>' +
      '<dialog class="modal-box tlc-presets-dialog" id="' + m + '-save-dialog"' +
        ' aria-labelledby="' + m + '-save-title">' +
        '<div class="modal-header">' +
          '<h2 class="modal-title" id="' + m + '-save-title">Save preset</h2>' +
          '<button type="button" class="modal-close" id="' + m + '-close-btn" aria-label="Close">' +
            '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor"' +
              ' stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="m4 4 8 8m0-8-8 8"/></svg>' +
          '</button>' +
        '</div>' +
        '<div class="modal-body">' +
          '<label for="' + m + '-name">Preset name</label>' +
          '<input type="text" id="' + m + '-name" class="form-control" maxlength="120" autocomplete="off"' +
            ' aria-describedby="' + m + '-save-note">' +
          '<p class="tlc-presets-note" id="' + m + '-save-note" role="status" aria-live="polite"></p>' +
        '</div>' +
        '<div class="modal-footer">' +
          '<button type="button" class="btn btn-secondary" id="' + m + '-cancel-btn">Cancel</button>' +
          '<button type="button" class="btn btn-primary" id="' + m + '-save-btn">Save</button>' +
        '</div>' +
      '</dialog>';
    ["bar", "select", "name", "mode-pick", "saveas", "update", "delete", "note", "save-dialog", "save-btn",
     "cancel-btn", "close-btn", "save-note", "undo-table"].forEach(function (s) {
      els[s] = el(s);
    });
  }

  function bind() {
    if (els["undo-table"]) els["undo-table"].addEventListener("click", function () { undoTableChange(); });
    if (els.select) els.select.addEventListener("change", function () {
      pick(els.select.value).catch(function () {}); // pick reports the error
    });
    if (els.saveas) els.saveas.addEventListener("click", openSave);
    if (els.update) els.update.addEventListener("click", update);
    if (els["delete"]) els["delete"].addEventListener("click", function () { remove(); });
    if (els["save-btn"]) els["save-btn"].addEventListener("click", function () { save(); });
    function dismissSave() { if (!saving) closeSave(); }
    if (els["cancel-btn"]) els["cancel-btn"].addEventListener("click", dismissSave);
    if (els["close-btn"]) els["close-btn"].addEventListener("click", dismissSave);
    if (els["save-dialog"]) {
      els["save-dialog"].addEventListener("cancel", function (ev) { ev.preventDefault(); dismissSave(); });
      els["save-dialog"].addEventListener("close", function () {
        if (els.saveas && els.saveas.focus) els.saveas.focus({preventScroll: true});
      });
      els["save-dialog"].addEventListener("click", function (ev) {
        if (ev.target !== els["save-dialog"]) return;
        var rect = els["save-dialog"].getBoundingClientRect();
        if (ev.clientX < rect.left || ev.clientX > rect.right || ev.clientY < rect.top || ev.clientY > rect.bottom) {
          dismissSave();
        }
      });
    }
    if (els.name) {
      els.name.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault(); ev.stopPropagation();
          save();
        } else if (ev.key === "Escape") {
          ev.preventDefault(); ev.stopPropagation();
          dismissSave();
        }
      });
      els.name.addEventListener("input", function () {
        if (!confirmReplaceId) return;
        confirmReplaceId = "";
        setText(els["save-btn"], "Save");
        setText(els["save-note"], "");
        els["save-note"].classList.remove("is-error");
      });
    }
    var root = formRootId ? document.getElementById(formRootId) : null;
    if (root) {
      root.addEventListener("input", changed);
      root.addEventListener("change", changed);
    }
  }

  /* ── Values ── */
  function valuesOf(record) {
    var out = {};
    Object.keys(record || {}).forEach(function (k) { if (!ID_FIELDS[k]) out[k] = record[k]; });
    return out;
  }
  function clone(value) { return JSON.parse(JSON.stringify(value || {})); }
  // Input paths may be nested. The shared helper does not know plugin field names.
  function contextValue(values, path) {
    var keys = path.split("."), value = values;
    for (var i = 0; i < keys.length; i++) {
      if (!value || !Object.prototype.hasOwnProperty.call(value, keys[i])) return undefined;
      value = value[keys[i]];
    }
    return value;
  }
  function settings(values) {
    return clone(valuesOf(values));
  }
  function comparable(values) { return _tlcPresetStable(settings(values)); }
  function byId(id) {
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return null;
  }
  function byName(name) {
    var key = String(name || "").trim().toLowerCase();
    for (var i = 0; i < list.length; i++) {
      if (String(list[i].name || "").trim().toLowerCase() === key) return list[i];
    }
    return null;
  }
  function names() { return list.map(function (p) { return p.name || ""; }); }

  /* Capture normalized settings after the plugin has finished applying them. */
  function originValuesFrom() {
    return settings(collect());
  }
  /* The defaults, in collect()'s shape: what a form compares with once its preset is gone. */
  function fromDefaults() {
    var d = defaults() || {};
    var out = originValuesFrom();
    Object.keys(d).forEach(function (k) { out[k] = d[k]; });
    return out;
  }

  /* ── The list ── */
  /* The plugin's summary of a preset, without the parts it does not know. */
  function summaryOf(p) {
    var parts;
    try { parts = summary(valuesOf(p)); } catch (e) { parts = ""; }
    if (!Array.isArray(parts)) parts = [parts];
    return parts.filter(function (x) { return x != null && String(x).trim() !== ""; }).join(" · ");
  }
  /* When the preset was last used for a run (`last_run`, stamped by the plugin), else when it was saved. */
  function ageOf(p) {
    var used = _tlcPresetAge(p.last_run);
    if (used) return "used " + used;
    var saved = _tlcPresetAge(p.created);
    return saved ? "saved " + saved : "";
  }
  /* "Name — summary"; the age joins it only where the list cannot show it as a muted hint. */
  function labelFor(p) {
    var hint = summaryOf(p);
    var age = inList ? "" : ageOf(p);
    var label = p.name || "Untitled";
    if (hint) label += " — " + hint;
    if (age) label += (hint ? " · " : " — ") + age;
    return label;
  }
  function option(value, label) {
    var o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    return o;
  }
  function rebuildSelect() {
    var sel = els.select;
    if (!sel) return;
    sel.innerHTML = "";
    if (sel.options && sel.options.length) sel.options.length = 0;
    var mark = isModified ? " · modified" : "";
    sel.appendChild(option("", "Default settings"));
    if (origin.kind === "defaults" && isModified) {
      var custom = option("__custom__", "Default settings · modified");
      custom.disabled = true;
      sel.appendChild(custom);
    }
    if (list.length) {
      var group = document.createElement("optgroup");
      group.label = "Saved presets";
      list.forEach(function (p) {
        var o = option(p.id, labelFor(p) + (origin.kind === "preset" && origin.id === p.id ? mark : ""));
        if (inList) {
          o.setAttribute("data-removable", "");
          var age = ageOf(p);
          if (age) o.setAttribute("data-hint", age);
        }
        group.appendChild(o);
      });
      sel.appendChild(group);
    }
    sel.value = origin.kind === "preset" ? origin.id : (isModified ? "__custom__" : "");
    if (!inList && origin.kind === "preset") show(els["delete"]); else hide(els["delete"]);
    if (origin.kind === "preset" && isModified) show(els.update); else hide(els.update);
    busy();
  }
  function busy() {
    ["select", "saveas", "update", "delete", "save-btn"].forEach(function (key) {
      if (els[key]) els[key].disabled = applying || saving || !initialized;
    });
    ["name", "cancel-btn", "close-btn"].forEach(function (key) {
      if (els[key]) els[key].disabled = saving;
    });
    setText(els["save-btn"], saving ? "Saving…" : (confirmReplaceId ? "Replace" : "Save"));
  }
  function loadList() {
    return fetchJson(base()).then(function (data) {
      list = Array.isArray(data) ? data : [];
      rebuildSelect();
    }).catch(function (e) {
      list = [];
      rebuildSelect();
      toast("Could not load presets: " + (e && e.message ? e.message : e), "error");
    });
  }

  /* ── Modified state and the draft ── */
  function recompute() {
    if (applying || !initialized) return;
    var was = isModified;
    var current;
    try { current = collect() || {}; } catch (e) { return; }
    // Keep the last complete setup while a new primary input is being resolved.
    // Partial URLs and failed reads must neither destroy it nor overwrite its draft.
    if (tableContext && tableIdentity && contextValue(current, tableContext.input) !== tableIdentity.url) {
      if (draftTimer) { clearTimeout(draftTimer); draftTimer = null; }
    } else {
      lastForm = clone(current);
      scheduleDraft(current);
    }
    isModified = originValues != null && comparable(current) !== comparable(originValues);
    if (was !== isModified) rebuildSelect();
  }
  function changed() { if (!applying) clearTableUndo(); recompute(); }
  function scheduleDraft(current) {
    if (draftTimer) clearTimeout(draftTimer);
    draftTimer = setTimeout(function () { draftTimer = null; writeDraft(current); }, draftDelay);
  }
  function writeDraft(current) {
    var s = storage();
    if (!s) return;
    try {
      s.setItem(draftKey(), JSON.stringify({
        origin: origin, originValues: originValues, values: current || collect(), at: new Date().toISOString(),
      }));
    } catch (e) { /* storage may be full or blocked */ }
  }
  function readDraft(key) {
    var s = storage();
    if (!s) return null;
    try {
      var raw = s.getItem(key || draftKey());
      var draft = raw ? JSON.parse(raw) : null;
      return draft && draft.values && typeof draft.values === "object" ? draft : null;
    } catch (e) { return null; }
  }
  /* ── Applying ── */
  function applyValues(values, resolvedTable) {
    applying = true;
    applyingInput = tableContext ? contextValue(values, tableContext.input) : undefined;
    tableIdentity = null;
    if (draftTimer) { clearTimeout(draftTimer); draftTimer = null; }
    busy();
    activeApply = Promise.resolve().then(function () {
      return apply(clone(values), resolvedTable);
    }).finally(function () {
      applying = false;
      busy();
    });
    return activeApply;
  }
  function settle(newOrigin) {
    origin = newOrigin;
    originValues = originValuesFrom();
    isModified = false;
    rebuildSelect();
    recompute();
  }
  function pick(id) {
    clearTableUndo();
    closeSave();
    var p = id ? byId(id) : null;
    var own = p ? valuesOf(p) : (defaults() || {});
    var newOrigin = p ? { kind: "preset", id: p.id, name: p.name || "" } : { kind: "defaults", id: "", name: "" };
    applyChain = applyChain.catch(function () {}).then(function () {
      return applyValues(Object.assign(clone(defaults()), settings(own))).then(function () { settle(newOrigin); });
    });
    return applyChain.catch(function (e) {
      toast("Could not open preset: " + (e && e.message ? e.message : e), "error");
      throw e;
    });
  }

  /* The plugin calls this after reading its primary table, before updating task/model UI.
   * During apply (initial launch, preset selection or Undo) it records identity only.
   * A manual input change derives the destination and resets defaults only across projects.
   * The optional second apply argument reuses this read instead of fetching the table twice. */
  function tableResolved(url, info) {
    if (!tableContext || !info || info.error) return Promise.resolve(false);
    if (contextValue(collect(), tableContext.input) !== url) return Promise.resolve(false);
    var next = {url: url, info: clone(info)};
    // A person may enter another table while model controls from an apply are still loading.
    // That answer is a new input, not metadata belonging to the in-progress saved setup.
    if (applying && url !== applyingInput) {
      return activeApply.then(function () { return tableResolved(url, info); });
    }
    if (applying || !initialized) {
      tableIdentity = next;
      if (info.project_url) scopeKey = tableScope(info.project_name, info.project_root_url);
      return Promise.resolve(false);
    }
    if (tableIdentity && tableIdentity.url === url) return Promise.resolve(false);
    // Unknown project metadata is not evidence of a different project.
    if (!info.project_url || !info.project_name) return Promise.resolve(false);
    applyChain = applyChain.catch(function () {}).then(function () {
      if (contextValue(collect(), tableContext.input) !== url) return false;
      if (tableIdentity && tableIdentity.url === url) return false;
      return applyTableChange(url, info);
    });
    return applyChain;
  }

  function applyTableChange(url, info) {
    var next = {url: url, info: clone(info)};
    var previous = tableIdentity;
    var reset = !!(previous && previous.info.project_url && previous.info.project_url !== info.project_url);
    var undo = {values: clone(lastForm || collect()), origin: clone(origin), baseline: clone(originValues),
      identity: previous, scope: scopeKey};
    var current = clone(collect());
    var values = reset ? clone(defaults()) : current;
    if (reset) (tableContext.preserve || []).forEach(function (key) { values[key] = current[key]; });
    values[tableContext.input] = url;
    values[tableContext.project] = info.project_name;
    // Preserve the old project's unfinished setup before moving to the new draft scope.
    if (reset && lastForm) writeDraft(lastForm);
    clearTableUndo();
    return applyValues(values, next).then(function () {
      if (contextValue(collect(), tableContext.input) !== url) {
        tableIdentity = previous; scopeKey = undo.scope;
        return true;
      }
      scopeKey = tableScope(info.project_name, info.project_root_url);
      if (reset) {
        origin = {kind: "defaults", id: "", name: ""};
        originValues = originValuesFrom();
        var d = defaults() || {};
        originValues[tableContext.input] = d[tableContext.input] || "";
        originValues[tableContext.project] = d[tableContext.project] || "";
        tableUndo = undo;
        setText(els.note, "Project changed to " + info.project_name + ". Settings reset.");
        show(els["undo-table"]);
        tableNoticeTimer = setTimeout(clearTableUndo, 15000);
      }
      rebuildSelect();
      recompute();
      return true;
    });
  }

  function tableScope(project, root) {
    return _tlcPresetStable({project: project || "", projectRoot: root || "",
      objectService: (api().getConfig && api().getConfig("object_service_url")) || ""});
  }
  function clearTableUndo() {
    if (tableNoticeTimer) { clearTimeout(tableNoticeTimer); tableNoticeTimer = null; }
    if (tableUndo) setText(els.note, "");
    tableUndo = null;
    hide(els["undo-table"]);
  }
  function undoTableChange() {
    if (!tableUndo || applying || saving) return Promise.resolve();
    var undo = tableUndo;
    clearTableUndo();
    return applyValues(undo.values, undo.identity).then(function () {
      origin = undo.origin; originValues = undo.baseline; scopeKey = undo.scope;
      rebuildSelect(); recompute();
    });
  }

  /* ── Save as… ── */
  function openSave() {
    if (saving || applying || !initialized || !els["save-dialog"]) return;
    confirmReplaceId = "";
    var suggested;
    try { suggested = origin.kind === "preset" ? origin.name : autoName(collect()); } catch (e) { suggested = ""; }
    if (els.name) els.name.value = _tlcPresetUniqueName(suggested || pluginId + " preset", names());
    setText(els["save-btn"], "Save");
    setText(els["save-note"], "");
    if (els["save-note"]) els["save-note"].classList.remove("is-error");
    var dialog = els["save-dialog"];
    // A flow-layout iframe can be much taller than the visible Hub page. Keep the dialog
    // near the clicked bar instead of centering it far down the iframe and scrolling there.
    var rect = els.bar.getBoundingClientRect();
    dialog.style.top = Math.max(16, Math.min(rect.bottom + 8, window.innerHeight - 260)) + "px";
    dialog.style.left = Math.max(16, Math.min(rect.left, window.innerWidth - 436)) + "px";
    if (!dialog.open) dialog.showModal();
    if (els.name) {
      if (els.name.focus) els.name.focus();
      if (els.name.select) els.name.select();
    }
  }
  function closeSave() {
    confirmReplaceId = "";
    if (els["save-dialog"] && els["save-dialog"].open) els["save-dialog"].close();
  }
  function save() {
    if (saving || applying) return Promise.resolve(false);
    var name = els.name ? String(els.name.value || "").trim() : "";
    if (!name) {
      var auto;
      try { auto = autoName(collect()); } catch (e) { auto = ""; }
      name = _tlcPresetUniqueName(auto || pluginId + " preset", names());
    }
    var existing = byName(name);
    if (existing && confirmReplaceId !== existing.id) {
      confirmReplaceId = existing.id;
      setText(els["save-note"], "Replace “" + existing.name + "”?");
      if (els["save-note"]) els["save-note"].classList.remove("is-error");
      setText(els["save-btn"], "Replace");
      return Promise.resolve(false);
    }
    return saveRecord(name, existing);
  }
  function update() {
    var existing = origin.kind === "preset" ? byId(origin.id) : null;
    if (!existing || !isModified || saving || applying) return Promise.resolve(false);
    return saveRecord(existing.name, existing);
  }
  function saveRecord(name, existing) {
    var body;
    try { body = settings(collect()); } catch (e) { return Promise.resolve(false); }
    body.id = existing ? existing.id : "";
    body.name = existing ? existing.name : name;
    // A replaced preset keeps when it was made and when it was last used.
    body.created = existing ? (existing.created || "") : "";
    body.last_run = existing ? (existing.last_run || null) : null;
    saving = true;
    busy();
    return fetchJson(base(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (resp) {
      if (!resp || resp.error) throw new Error((resp && resp.error) || "no answer");
      origin = { kind: "preset", id: resp.id || body.id, name: body.name };
      // The form can still be edited while saving. Compare it with what was sent, not
      // what happens to be in the form when the response arrives.
      originValues = settings(body);
      isModified = comparable(collect()) !== comparable(originValues);
      closeSave();
      return loadList();
    }).then(function () {
      recompute();
      toast((existing ? "Updated preset “" : "Saved preset “") + body.name + "”", "success");
      return true;
    }).catch(function (e) {
      var msg = e && e.message ? e.message : String(e);
      setText(els["save-note"], "Could not save: " + msg);
      if (els["save-note"]) els["save-note"].classList.add("is-error");
      toast("Could not save the preset: " + msg, "error");
      return false;
    }).finally(function () { saving = false; busy(); });
  }

  /* ── Delete: asks nothing ── */
  function deletePreset(id) {
    var p = byId(id);
    var name = p ? (p.name || "") : "";
    return fetchJson(base() + "/" + encodeURIComponent(id) + "/delete", { method: "POST" }).then(function (resp) {
      if (resp && resp.error) throw new Error(resp.error);
      if (origin.kind === "preset" && origin.id === id) {
        // The form stays as it is; it now comes from Default settings, changed.
        origin = { kind: "defaults", id: "", name: "" };
        originValues = fromDefaults();
      }
      return loadList();
    }).then(function () {
      recompute();
      return name;
    });
  }
  /* The bar's trash, and the API: the given preset, else the selected one. */
  function remove(id) {
    var target = id || (origin.kind === "preset" ? origin.id : "");
    if (!target) return Promise.resolve(false);
    return deletePreset(target).then(function () { return true; }, function (e) {
      toast("Could not delete the preset: " + (e && e.message ? e.message : e), "error");
      return false;
    });
  }

  /* ── Opening ── */
  function init() {
    render();
    bind();
    // The Hub's list removes options in place, keeping the list open; the list shows the error itself.
    var picker = typeof window !== "undefined" ? window.TlcPicker : null;
    inList = !!(picker && typeof picker.onRemove === "function" && els.select);
    if (inList) picker.onRemove(els.select, function (option) { return deletePreset(option.value); });
    try { ctx = contextInputs() || null; } catch (e) { ctx = null; }
    scopeKey = _tlcPresetStable({
      project: (api().context || {}).projectName || "",
      inputs: ctx,
      objectService: (api().getConfig && api().getConfig("object_service_url")) || "",
      projectRoot: (api().getConfig && api().getConfig("project_root_url")) || "",
    });
    var legacyDraftKey = draftKey();
    if (tableContext) scopeKey = tableScope((ctx || {}).project_name || (api().context || {}).projectName,
      (api().getConfig && api().getConfig("project_root_url")) || "");
    return loadList().then(function () {
      // An explicit table/folder launch always means a new setup, even if those same
      // inputs were used earlier. Only a bare/project launch resumes an unfinished draft.
      var externalInputs = inputs.some(function (path) { return !!contextValue(ctx, path); });
      var draft = externalInputs ? null : (readDraft() || readDraft(legacyDraftKey));
      var start, newOrigin;
      if (draft) {
        start = draft.values;
        var p = draft.origin && draft.origin.kind === "preset" ? byId(draft.origin.id) : null;
        newOrigin = p ? { kind: "preset", id: p.id, name: p.name || "" } : { kind: "defaults", id: "", name: "" };
        return applyValues(start).then(function () {
          origin = newOrigin;
          // Keep the normalized baseline for defaults too: detection/model defaults are not edits.
          // A preset may have been updated in another project/tab. Preserve this draft's
          // values, but compare with the currently saved settings rather than an old copy.
          originValues = p ? settings(p) : (
            draft.origin && draft.origin.kind === "defaults" && draft.originValues
              && typeof draft.originValues === "object" ? draft.originValues : fromDefaults());
          initialized = true;
          isModified = false;
          rebuildSelect();
          recompute();
          if (legacyDraftKey !== draftKey()) {
            writeDraft(collect());
            try { storage().removeItem(legacyDraftKey); } catch (e) {}
          }
        });
      }
      start = Object.assign(clone(defaults()), clone(ctx));
      // The opened primary table is authoritative for a fresh external launch.
      // Saved presets and drafts keep their deliberately chosen output project.
      if (tableContext && contextValue(ctx, tableContext.input)) start[tableContext.project] = "";
      return applyValues(start).then(function () {
        origin = {kind: "defaults", id: "", name: ""};
        originValues = originValuesFrom();
        // Preserve model/task normalization in the baseline, but external input and project
        // choices are changes from defaults and must be visibly unsaved.
        var d = defaults() || {};
        Object.keys(ctx || {}).forEach(function (key) {
          if (Object.prototype.hasOwnProperty.call(d, key)) originValues[key] = clone(d)[key];
          else delete originValues[key];
        });
        initialized = true;
        rebuildSelect();
        recompute();
      });
    });
  }

  // The accepted job keeps its exact configuration. Reopening starts a fresh run; changing
  // fields in the still-open form creates a new draft. Never delete a draft on a failed submit.
  function submitted() {
    clearTableUndo();
    // The plugin may have cleared its run name. Refresh the modified marker before
    // removing the draft, without saving or changing the named preset.
    recompute();
    if (draftTimer) { clearTimeout(draftTimer); draftTimer = null; }
    var s = storage();
    if (s) { try { s.removeItem(draftKey()); } catch (e) {} }
  }

  return {
    init: init,
    changed: changed,
    tableResolved: tableResolved,
    undoTableChange: undoTableChange,
    refresh: loadList,
    pick: pick,
    openSave: openSave,
    closeSave: closeSave,
    save: save,
    update: update,
    submitted: submitted,
    remove: remove,
    origin: function () { return { kind: origin.kind, id: origin.id, name: origin.name }; },
    modified: function () { return isModified; },
    presetId: function () { return origin.kind === "preset" && !isModified ? origin.id : ""; },
    presetName: function () { return origin.kind === "preset" && !isModified ? origin.name : ""; },
    contextInputs: function () { return ctx; },
  };
}
"""


def preset_ui_script() -> str:
    """Return the shared preset bar JavaScript for injection into plugin UI fragments."""
    return PRESET_UI_JS
