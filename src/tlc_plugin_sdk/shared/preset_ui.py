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
      legacyKey: "yolo_selected_project",       // optional: the config bar's old localStorage key
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
 *   Preset  [ Small objects 1280 · modified   ⌄ ]  Save as…  🗑
 *
 * A preset is a named template of the whole form, inputs included. Picking one copies its values
 * into the form; only Save writes a preset, a run never does. The select lists "Default settings"
 * and then the saved presets as the server lists them, each with a hint and its age. The selected
 * entry gains " · modified" once the form differs from it; picking it again reverts the form.
 *
 * Save as… turns the bar into a name field, prefilled with a free name (the preset the form came
 * from, else the plugin's auto name), text selected: Enter or Save saves, Escape or Cancel closes.
 * A taken name asks "Replace “name”?" first. The trash icon asks "Delete “name”?" in the bar.
 *
 * The page opens as it was last left in this browser: a draft (the form, and the preset it came
 * from) is kept in localStorage on every change. Without one it opens on Default settings, or
 * once on the preset the old config bar remembered under `legacyKey`.
 *
 * Opened with inputs (`contextInputs()` returns an object): those replace the whole input set,
 * inputs it does not name are cleared, and they stay in force while the page is open, also when a
 * preset is picked. They never count as a modification.
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
 *   summary       {function}  (values) => string, shown after the name in the list (optional)
 *   legacyKey     {string}    the old config bar's localStorage key (optional)
 *   draftDelay    {number}    ms before a change is written as the draft (default 300)
 *
 * Returns:
 *   init()         render, load the list, open on the draft/legacy/defaults and the context's inputs
 *   changed()      the form changed in a way no DOM event reports (JS state); recompute and save the draft
 *   refresh()      reload the list
 *   pick(id)       apply a preset ("" for Default settings), as the select does
 *   openSave()     show the name field; save() saves it; closeSave() hides it
 *   openDelete()   ask to delete the selected preset; remove() deletes it; closeDelete() cancels
 *   origin()       {kind: "defaults"|"preset", id, name}: where the form came from
 *   modified()     whether the form differs from its origin
 *   presetId()     the selected preset's id when the form is unchanged from it, else ""
 *   presetName()   its name likewise, for the run body's `project_config.name`
 *   contextInputs() the inputs in force, or null
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
  var autoName = opts.autoName || function () { return pluginId + " preset"; };
  var summary = opts.summary || function () { return ""; };
  var legacyKey = opts.legacyKey || "";
  var draftDelay = opts.draftDelay == null ? 300 : opts.draftDelay;

  var ID_FIELDS = { id: 1, name: 1, created: 1, last_run: 1 };
  // The select and the name field share one slot, with the same basis, so Save as… swaps them in
  // place and nothing moves sideways.
  var CSS =
    ".tlc-presets select, .tlc-presets input.form-control" +
    " { flex: 0 1 640px; min-width: 0; max-width: 640px; font-size: 12px; font-weight: 500; }" +
    ".tlc-presets .tlc-presets-mode { display: inline-flex; align-items: center; gap: 8px; min-width: 0; }" +
    ".tlc-presets .tlc-presets-note { font-size: 11px; color: var(--text-muted); }" +
    ".tlc-presets .tlc-presets-note.is-error { color: var(--danger); }" +
    ".tlc-presets .tlc-presets-icon { display: inline-flex; padding: 4px 6px; line-height: 0; }" +
    ".tlc-presets .tlc-presets-icon svg { width: 14px; height: 14px; }" +
    ".tlc-presets .tlc-presets-text { font-size: 12px; color: var(--text); }";
  var TRASH =
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"' +
    ' stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M2.5 4.5h11M6.5 2.5h3M4 4.5l.7 9h6.6l.7-9M6.5 7v4.5M9.5 7v4.5"/></svg>';

  var list = [];                                       // the saved presets, as the server lists them
  var origin = { kind: "defaults", id: "", name: "" }; // where the form came from
  var originValues = null;                             // the form as it was at the origin
  var ctx = null;                                      // the launch context's inputs, for the page's life
  var isModified = false;
  var draftTimer = null;
  var confirmReplaceId = "";                           // the preset a second Save will replace
  var els = {};

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
    return "tlc-presets:" + computeUrl + ":" + pluginId;
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
        '<input type="text" id="' + m + '-name" class="form-control" maxlength="120" autocomplete="off"' +
          ' aria-label="Preset name" style="display:none">' +
        '<span class="tlc-presets-mode" id="' + m + '-mode-pick">' +
          '<button type="button" class="btn btn-secondary btn-sm" id="' + m + '-saveas">Save as…</button>' +
          '<button type="button" class="btn btn-ghost btn-sm tlc-presets-icon" id="' + m + '-delete"' +
            ' title="Delete this preset" aria-label="Delete this preset">' + TRASH + '</button>' +
          '<span class="tlc-presets-note" id="' + m + '-note"></span>' +
        '</span>' +
        '<span class="tlc-presets-mode" id="' + m + '-mode-save" style="display:none">' +
          '<button type="button" class="btn btn-primary btn-sm" id="' + m + '-save-btn">Save</button>' +
          '<button type="button" class="btn btn-ghost btn-sm" id="' + m + '-cancel-btn">Cancel</button>' +
          '<span class="tlc-presets-note" id="' + m + '-save-note"></span>' +
        '</span>' +
        '<span class="tlc-presets-mode" id="' + m + '-mode-confirm" style="display:none">' +
          '<span class="tlc-presets-text" id="' + m + '-confirm-text"></span>' +
          '<button type="button" class="btn btn-danger btn-sm" id="' + m + '-confirm-ok">Delete</button>' +
          '<button type="button" class="btn btn-ghost btn-sm" id="' + m + '-confirm-cancel">Cancel</button>' +
        '</span>' +
      '</div>';
    ["bar", "select", "name", "mode-pick", "saveas", "delete", "note", "mode-save", "save-btn", "cancel-btn",
     "save-note", "mode-confirm", "confirm-text", "confirm-ok", "confirm-cancel"].forEach(function (s) {
      els[s] = el(s);
    });
  }

  /* One row, three modes: the select or the name field in the same slot, and that mode's buttons after it. */
  function setMode(mode) {
    if (mode === "save") { hide(els.select); show(els.name); } else { show(els.select); hide(els.name); }
    if (mode === "pick") show(els["mode-pick"]); else hide(els["mode-pick"]);
    if (mode === "save") show(els["mode-save"]); else hide(els["mode-save"]);
    if (mode === "confirm") show(els["mode-confirm"]); else hide(els["mode-confirm"]);
  }

  function bind() {
    if (els.select) els.select.addEventListener("change", function () { pick(els.select.value); });
    if (els.saveas) els.saveas.addEventListener("click", openSave);
    if (els["delete"]) els["delete"].addEventListener("click", openDelete);
    if (els["save-btn"]) els["save-btn"].addEventListener("click", function () { save(); });
    if (els["cancel-btn"]) els["cancel-btn"].addEventListener("click", closeSave);
    if (els["confirm-ok"]) els["confirm-ok"].addEventListener("click", function () { remove(); });
    if (els["confirm-cancel"]) els["confirm-cancel"].addEventListener("click", closeDelete);
    if (els.name) {
      els.name.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault(); ev.stopPropagation();
          save();
        } else if (ev.key === "Escape") {
          ev.preventDefault(); ev.stopPropagation();
          closeSave();
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
  function comparable(values) {
    var out = {};
    Object.keys(values || {}).forEach(function (k) {
      if (ctx && inputs.indexOf(k) !== -1) return;
      out[k] = values[k];
    });
    return _tlcPresetStable(out);
  }
  function withContext(values) {
    var out = {};
    Object.keys(values || {}).forEach(function (k) { out[k] = values[k]; });
    if (ctx) inputs.forEach(function (k) { out[k] = ctx[k] == null ? "" : ctx[k]; });
    return out;
  }
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

  /* The origin's values, in collect()'s shape, with the origin's own inputs. */
  function originValuesFrom(ownValues) {
    var out = collect() || {};
    inputs.forEach(function (k) { out[k] = ownValues && ownValues[k] != null ? ownValues[k] : ""; });
    return out;
  }
  /* The defaults, in collect()'s shape: what a form compares with once its preset is gone. */
  function fromDefaults() {
    var d = defaults() || {};
    var out = originValuesFrom(d);
    Object.keys(d).forEach(function (k) { out[k] = d[k]; });
    return out;
  }

  /* ── The list ── */
  function labelFor(p) {
    var hint = "";
    try { hint = summary(valuesOf(p)) || ""; } catch (e) { hint = ""; }
    var age = _tlcPresetAge(p.created);
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
    sel.appendChild(option("", "Default settings" + (origin.kind === "defaults" ? mark : "")));
    if (list.length) {
      var group = document.createElement("optgroup");
      group.label = "Saved presets";
      list.forEach(function (p) {
        group.appendChild(option(p.id, labelFor(p) + (origin.kind === "preset" && origin.id === p.id ? mark : "")));
      });
      sel.appendChild(group);
    }
    sel.value = origin.kind === "preset" ? origin.id : "";
    if (origin.kind === "preset") show(els["delete"]); else hide(els["delete"]);
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
    var was = isModified;
    var current;
    try { current = collect() || {}; } catch (e) { return; }
    isModified = originValues != null && comparable(current) !== comparable(originValues);
    if (was !== isModified) rebuildSelect();
    scheduleDraft(current);
  }
  function changed() { recompute(); }
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
  function readDraft() {
    var s = storage();
    if (!s) return null;
    try {
      var raw = s.getItem(draftKey());
      var draft = raw ? JSON.parse(raw) : null;
      return draft && draft.values && typeof draft.values === "object" ? draft : null;
    } catch (e) { return null; }
  }
  function readLegacy() {
    var s = storage();
    if (!s || !legacyKey) return "";
    try { return s.getItem(legacyKey) || ""; } catch (e) { return ""; }
  }
  function dropLegacy() {
    var s = storage();
    if (!s || !legacyKey) return;
    try { s.removeItem(legacyKey); } catch (e) { /* nothing to drop */ }
  }

  /* ── Applying ── */
  function applyValues(values) {
    var result;
    try { result = apply(withContext(values)); } catch (e) { result = null; }
    return Promise.resolve(result);
  }
  function settle(newOrigin, ownValues) {
    origin = newOrigin;
    originValues = originValuesFrom(ownValues);
    isModified = false;
    rebuildSelect();
    recompute();
  }
  function pick(id) {
    closeSave();
    closeDelete();
    var p = id ? byId(id) : null;
    var own = p ? valuesOf(p) : (defaults() || {});
    var newOrigin = p ? { kind: "preset", id: p.id, name: p.name || "" } : { kind: "defaults", id: "", name: "" };
    return applyValues(own).then(function () { settle(newOrigin, own); });
  }

  /* ── Save as… ── */
  function openSave() {
    closeDelete();
    confirmReplaceId = "";
    var suggested;
    try { suggested = origin.kind === "preset" ? origin.name : autoName(collect()); } catch (e) { suggested = ""; }
    if (els.name) els.name.value = _tlcPresetUniqueName(suggested || pluginId + " preset", names());
    setText(els["save-btn"], "Save");
    setText(els["save-note"], "");
    if (els["save-note"]) els["save-note"].classList.remove("is-error");
    setMode("save");
    if (els.name) {
      if (els.name.focus) els.name.focus();
      if (els.name.select) els.name.select();
    }
  }
  function closeSave() {
    confirmReplaceId = "";
    setMode("pick");
  }
  function save() {
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
    var body;
    try { body = collect() || {}; } catch (e) { body = {}; }
    body.id = existing ? existing.id : "";
    body.name = existing ? existing.name : name;
    body.created = existing ? (existing.created || "") : "";
    return fetchJson(base(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (resp) {
      if (!resp || resp.error) throw new Error((resp && resp.error) || "no answer");
      origin = { kind: "preset", id: resp.id || body.id, name: body.name };
      originValues = originValuesFrom(body);
      isModified = false;
      closeSave();
      return loadList();
    }).then(function () {
      recompute();
      toast((existing ? "Replaced preset “" : "Saved preset “") + body.name + "”", "success");
      return true;
    }).catch(function (e) {
      var msg = e && e.message ? e.message : String(e);
      setText(els["save-note"], "Could not save: " + msg);
      if (els["save-note"]) els["save-note"].classList.add("is-error");
      toast("Could not save the preset: " + msg, "error");
      return false;
    });
  }

  /* ── Delete ── */
  function openDelete() {
    if (origin.kind !== "preset") return;
    closeSave();
    setText(els["confirm-text"], "Delete “" + origin.name + "”?");
    setMode("confirm");
  }
  function closeDelete() {
    setMode("pick");
  }
  function remove() {
    if (origin.kind !== "preset") return Promise.resolve(false);
    var id = origin.id, name = origin.name;
    return fetchJson(base() + "/" + encodeURIComponent(id) + "/delete", { method: "POST" }).then(function (resp) {
      if (resp && resp.error) throw new Error(resp.error);
      // The form stays as it is; it now comes from Default settings, changed.
      origin = { kind: "defaults", id: "", name: "" };
      originValues = fromDefaults();
      closeDelete();
      return loadList();
    }).then(function () {
      recompute();
      toast("Deleted preset “" + name + "”", "success");
      return true;
    }).catch(function (e) {
      closeDelete();
      toast("Could not delete the preset: " + (e && e.message ? e.message : e), "error");
      return false;
    });
  }

  /* ── Opening ── */
  function init() {
    render();
    bind();
    try { ctx = contextInputs() || null; } catch (e) { ctx = null; }
    if (ctx) setText(els.note, "Opened from a table: presets change the settings, the table stays.");
    return loadList().then(function () {
      var draft = readDraft();
      var start, newOrigin;
      if (draft) {
        start = draft.values;
        var p = draft.origin && draft.origin.kind === "preset" ? byId(draft.origin.id) : null;
        newOrigin = p ? { kind: "preset", id: p.id, name: p.name || "" } : { kind: "defaults", id: "", name: "" };
        return applyValues(start).then(function () {
          origin = newOrigin;
          // Compare with the preset as it was picked; with the defaults once the preset is gone.
          originValues = p && draft.originValues && typeof draft.originValues === "object"
            ? draft.originValues : fromDefaults();
          isModified = false;
          rebuildSelect();
          recompute();
        });
      }
      var legacy = readLegacy();
      var lp = legacy ? byId(legacy) : null;
      dropLegacy();
      if (lp) return pick(lp.id);
      return pick("");
    });
  }

  return {
    init: init,
    changed: changed,
    refresh: loadList,
    pick: pick,
    openSave: openSave,
    closeSave: closeSave,
    save: save,
    openDelete: openDelete,
    closeDelete: closeDelete,
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
