# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared config bar UI for plugins — JS module injected into plugin fragments.

Follows the same injection pattern as ``alias_ui.py``.  Plugins call
``config_ui_script()`` to get the JS string, then inject it with
``inject_scripts(raw, config_ui_script())`` (see ``ui_inject.py``).

The module exposes a single factory:

    var cfg = _tlcPluginConfig({ prefix, apiPath, storageKey,
                                 collectFn, populateFn, labelFn,
                                 onNew, onLoad });
    cfg.init();

See doc-block inside CONFIG_UI_JS for full API.
"""

from __future__ import annotations

CONFIG_UI_JS = r"""
/* ── Shared Plugin Config Manager ────────────────────────────────
 *
 * Factory that returns a config-bar controller.  Handles:
 *   - list / load / save / saveAsNew / delete via REST
 *   - localStorage for last-selected config ID
 *   - dirty-tracking (snapshot-based) with "Save New" visibility
 *   - dropdown population with custom label
 *
 * Required HTML IDs (by convention, {prefix}-config-select, etc.):
 *   {prefix}-config-select   — <select> dropdown
 *   {prefix}-save-new-btn    — "Save New" button (hidden by default)
 *   {prefix}-delete-btn      — "Delete" button (hidden by default)
 *
 * Options:
 *   prefix      {string}    HTML ID prefix (e.g. "im", "dc")
 *   apiPath     {string}    REST endpoint base (e.g. "/api/plugins/image-metrics/configs")
 *   storageKey  {string}    localStorage key for last config ID
 *   collectFn   {function}  () => formDataObject (must include .name)
 *   populateFn  {function}  (config) => void — fill form from config
 *   labelFn     {function}  (config) => string — dropdown label
 *   onNew       {function}  () => void — extra reset logic
 *   onLoad      {function}  (config) => void — extra post-load logic
 * ──────────────────────────────────────────────────────────────── */
function _tlcPluginConfig(opts) {
  var prefix     = opts.prefix;
  var apiPath    = opts.apiPath;
  var storageKey = opts.storageKey;
  var collectFn  = opts.collectFn;
  var populateFn = opts.populateFn;
  var labelFn    = opts.labelFn  || function(c) { return c.name || "Untitled"; };
  var onNew      = opts.onNew    || function() {};
  var onLoad     = opts.onLoad   || function() {};

  var _currentId = null;
  var _snapshot  = null;
  var _timer     = null;

  var _selId     = prefix + "-config-select";
  var _saveNewId = prefix + "-save-new-btn";
  var _deleteId  = prefix + "-delete-btn";

  /* ── Auth-aware fetch ── */
  function _fetch(url, reqOpts) {
    if (window.PLUGIN_API && window.PLUGIN_API.authFetch) {
      return window.PLUGIN_API.authFetch(url, reqOpts || {});
    }
    return fetch(url, reqOpts || {});
  }

  function _baseUrl() {
    var base = "";
    if (window.PLUGIN_API && window.PLUGIN_API.getConfig) {
      base = window.PLUGIN_API.getConfig("compute_service_url") || "";
    }
    return base + apiPath;
  }

  /* ── Snapshot / dirty ── */
  function takeSnapshot() {
    try { _snapshot = JSON.stringify(collectFn()); } catch(e) { _snapshot = null; }
  }

  function checkDirty() {
    var btn = document.getElementById(_saveNewId);
    if (!btn) return;
    try {
      var data = collectFn();
      var hasName = data.name && data.name.trim();
      if (!_currentId) {
        // New config: show "Save New" as soon as a name is entered
        btn.style.display = hasName ? "" : "none";
      } else {
        // Existing config: show "Save New" when form has changed
        var dirty = JSON.stringify(data) !== _snapshot;
        btn.style.display = dirty ? "" : "none";
      }
    } catch(e) {}
  }

  function scheduleAutoSave() {
    clearTimeout(_timer);
    _timer = setTimeout(checkDirty, 300);
  }

  /* ── CRUD ── */
  function loadAll() {
    _fetch(_baseUrl())
      .then(function(r) { return r.json(); })
      .then(function(configs) {
        var sel = document.getElementById(_selId);
        if (!sel) return;
        var prev = sel.value || (_currentId || "");
        sel.innerHTML = '<option value="">New config</option>';
        (Array.isArray(configs) ? configs : []).forEach(function(c) {
          var opt = document.createElement("option");
          opt.value = c.id;
          opt.textContent = labelFn(c);
          sel.appendChild(opt);
        });
        if (prev) sel.value = prev;
      })
      .catch(function() {});
  }

  function loadConfig(configId) {
    _fetch(_baseUrl() + "/" + configId)
      .then(function(r) { return r.json(); })
      .then(function(c) {
        if (c.error) return;
        _currentId = c.id;
        try { localStorage.setItem(storageKey, c.id); } catch(e) {}
        populateFn(c);
        var sel = document.getElementById(_selId);
        if (sel) sel.value = c.id;
        var saveBtn = document.getElementById(_saveNewId);
        if (saveBtn) saveBtn.style.display = "";
        var delBtn = document.getElementById(_deleteId);
        if (delBtn) delBtn.style.display = "";
        onLoad(c);
        setTimeout(function() { takeSnapshot(); checkDirty(); }, 100);
        loadAll();
      })
      .catch(function(e) { console.warn("Config load failed:", e); });
  }

  function onSelect(val) {
    if (val) { loadConfig(val); } else { newConfig(); }
  }

  function newConfig() {
    _currentId = null;
    try { localStorage.removeItem(storageKey); } catch(e) {}
    var sel = document.getElementById(_selId);
    if (sel) sel.value = "";
    var saveBtn = document.getElementById(_saveNewId);
    if (saveBtn) saveBtn.style.display = "none";
    var delBtn = document.getElementById(_deleteId);
    if (delBtn) delBtn.style.display = "none";
    onNew();
    takeSnapshot();
    loadAll();
  }

  function saveAsNew() {
    var data = collectFn();
    data.id = "";
    if (!data.name || !data.name.trim()) {
      alert("Please enter a config name.");
      return;
    }
    _fetch(_baseUrl(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    })
    .then(function(r) { return r.json(); })
    .then(function(resp) {
      if (resp.error) { alert("Error: " + resp.error); return; }
      _currentId = resp.id;
      try { localStorage.setItem(storageKey, _currentId); } catch(e) {}
      var delBtn = document.getElementById(_deleteId);
      if (delBtn) delBtn.style.display = "";
      takeSnapshot();
      checkDirty();
      loadAll();
    })
    .catch(function(e) { alert("Save failed: " + e); });
  }

  function save(silent) {
    var data = collectFn();
    data.id = _currentId || "";
    if (!data.name || !data.name.trim()) {
      if (!silent) alert("Please enter a config name.");
      return;
    }
    _fetch(_baseUrl(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    })
    .then(function(r) { return r.json(); })
    .then(function(resp) {
      if (resp.error) { if (!silent) alert("Error: " + resp.error); return; }
      _currentId = resp.id;
      try { localStorage.setItem(storageKey, _currentId); } catch(e) {}
      var delBtn = document.getElementById(_deleteId);
      if (delBtn) delBtn.style.display = "";
      takeSnapshot();
      checkDirty();
      loadAll();
    })
    .catch(function(e) { if (!silent) alert("Save failed: " + e); });
  }

  function deleteConfig() {
    if (!_currentId) return;
    if (!confirm("Delete this config?")) return;
    _fetch(_baseUrl() + "/" + _currentId + "/delete", { method: "POST" })
      .then(function() { newConfig(); })
      .catch(function() {});
  }

  function init() {
    var savedId = null;
    try { savedId = localStorage.getItem(storageKey); } catch(e) {}
    if (savedId) {
      loadConfig(savedId);
    } else {
      loadAll();
    }
  }

  return {
    init: init,
    loadAll: loadAll,
    loadConfig: loadConfig,
    onSelect: onSelect,
    newConfig: newConfig,
    save: save,
    saveAsNew: saveAsNew,
    deleteConfig: deleteConfig,
    scheduleAutoSave: scheduleAutoSave,
    takeSnapshot: takeSnapshot,
    checkDirty: checkDirty,
    getCurrentId: function() { return _currentId; },
    setCurrentId: function(id) { _currentId = id; },
  };
}
"""


def config_ui_script() -> str:
    """Return the shared config bar JavaScript for injection into plugin UI fragments."""
    return CONFIG_UI_JS
