# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
r"""Shared UI component for URL alias settings.

Generates the HTML + JS block that plugins embed in their UI fragments.
This ensures a consistent alias UI across every plugin that creates
3LC tables.

Usage in a plugin's ``get_ui_fragment()``::

    from tlc_plugin_sdk.shared.alias_ui import alias_ui_script
    from tlc_plugin_sdk.shared.ui_inject import inject_scripts

    raw = Path("ui.html").read_text()
    html = inject_scripts(raw, alias_ui_script())
"""

from __future__ import annotations

from tlc_plugin_sdk.shared.run_target_ui import RUN_TARGET_JS

# The JS helper functions are defined once and shared by all plugins.
# Each plugin calls _tlcAliasSettingsHtml(idPrefix, projectValue, folderValue)
# to render the alias section, and _tlcGetAliasValues(idPrefix) to read values.

# fmt: off
ALIAS_UI_JS = RUN_TARGET_JS + (
    '// ── Shared URL Alias UI ─────────────────────────────────\n'
    "(function(){var st=document.createElement('style');st.textContent='.tlc-tip{position:relative}'+'.tlc-tip "
    '.tlc-tip-text{display:none;position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);w'
    'idth:260px;padding:8px 10px;background:var(--bg-card,#1a2332);color:var(--text-muted,#94a3b8);font-size:11'
    'px;font-weight:400;line-height:1.5;border-radius:6px;border:1px solid var(--border,#2a3a4a);box-shadow:0 4'
    "px 12px rgba(0,0,0,.3);z-index:1000;pointer-events:none;white-space:normal}'+'.tlc-tip:hover .tlc-tip-text"
    "{display:block}';document.head.appendChild(st)})();\n"
    '\n'
    'function _tlcDefaultAliasToken(projectName) {\n'
    "  var token = projectName.toUpperCase().replace(/[^A-Z0-9]/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '"
    "');\n"
    "  if (!token || !/^[A-Z]/.test(token)) token = 'PROJECT_' + token;\n"
    '  return token;\n'
    '}\n'
    '\n'
    'function _tlcSuggestedAliasToken(folder, project) {\n'
    "  var name = String(folder || '').trim().replace(/\\\\/g, '/').replace(/\\/+$/, '').split('/').pop();\n"
    "  return name ? _tlcDefaultAliasToken(name) : (project ? _tlcDefaultAliasToken(project) : '');\n"
    '}\n'
    '\n'
    'function _tlcAliasSettingsHtml(idPrefix, projectValue, folderValue) {\n'
    '  function esc(value) {\n'
    "    return String(value || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')\n"
    '      .replace(/>/g, \'&gt;\').replace(/"/g, \'&quot;\');\n'
    '  }\n'
    '  var token = _tlcSuggestedAliasToken(folderValue, projectValue);\n'
    '  var html = \'<div class="tlc-alias-settings" style="margin-top:12px;padding:12px;border:1px solid var(--b'
    'order);border-radius:6px;background:var(--bg)">\';\n'
    '  html += \'<strong style="display:block;font-size:12px;margin-bottom:8px">URL alias</strong>\';\n'
    '  html += \'<label class="form-label" for="\' + idPrefix + \'-alias-token">Alias name</label>\';\n'
    '  html += \'<input type="text" id="\' + idPrefix + \'-alias-token" class="form-control" value="\' + esc(token)'
    ' + \'" placeholder="From source folder">\';\n'
    '  html += \'<div class="form-help">Suggested from the source folder. Edit to keep a custom name; clear to r'
    "estore the suggestion. Use A–Z, 0–9 and underscores.</div>';\n"
    '  html += \'<label class="form-label" style="margin-top:8px" for="\' + idPrefix + \'-alias-folder">Source roo'
    "t</label>';\n"
    '  html += \'<input type="text" id="\' + idPrefix + \'-alias-folder" class="form-control" value="\' + esc(folde'
    'rValue) + \'" placeholder="From the selected data">\';\n'
    '  html += \'<div class="form-help">Follows the selected source. Use a parent folder when paths need to be r'
    "elative to it.</div>';\n"
    '  html += \'<div id="\' + idPrefix + \'-alias-mapping" role="status" style="margin-top:8px;font-size:12px;ove'
    'rflow-wrap:anywhere"></div>\';\n'
    '  html += \'<div id="\' + idPrefix + \'-alias-location-error" role="alert" style="display:none;margin-top:8px'
    ';color:var(--danger,#dc3545)"></div>\';\n'
    '  html += \'<div class="form-help">Files stay at their source. Other machines need access to that location.'
    " To relocate data, use Storage before selecting it here.</div>';\n"
    "  html += '</div>';\n"
    '  return html;\n'
    '}\n'
    '\n'
    "// The project root a job writes to when nothing is chosen: the compute host's own configured root,\n"
    "// which the host stamps into every run body. Asked once per page; '' when the host cannot say (an\n"
    '// older host answers 404), which means: make no offer.\n'
    'function _tlcDefaultProjectRoot() {\n'
    '  if (window._tlcDefaultRootPromise) return window._tlcDefaultRootPromise;\n'
    '  var API = window.PLUGIN_API;\n'
    "  var base = API && API.getConfig ? String(API.getConfig('compute_service_url') || '').replace(/\\/$/, '') "
    ": '';\n"
    "  if (!API || !base) return Promise.resolve('');\n"
    "  var p = API.authFetch(base + '/api/deployment/storage/')\n"
    '    .then(function(r) { return r.ok ? r.json() : {}; })\n'
    "    .then(function(c) { return String((c && c.project_root_url) || '').replace(/\\/$/, ''); })\n"
    "    .catch(function() { return ''; })\n"
    '    .then(function(root) {\n'
    '      // Remember answers, never failures: a compute that was restarting must be asked again next time.\n'
    '      if (!root) delete window._tlcDefaultRootPromise;\n'
    '      return root;\n'
    '    });\n'
    '  window._tlcDefaultRootPromise = p;\n'
    '  return p;\n'
    '}\n'
    '// Kept for a plugin script that still calls it by the old name; the plugin id no longer matters.\n'
    'function _tlcProjectRootUrl(pluginId) { return _tlcDefaultProjectRoot(); }\n'
    '// The compute host, named the way the person sees it: "This computer" only when the browser runs on it\n'
    '// (a loopback compute URL); else the Hub\'s name for it while runs stay on it, else "The compute host".\n'
    'function _tlcHostName() {\n'
    '  var API = window.PLUGIN_API;\n'
    "  var base = API && API.getConfig ? API.getConfig('compute_service_url') : '';\n"
    "  if (_tlcComputeIsHere(base)) return 'This computer';\n"
    '  var t = _tlcRunTarget();\n'
    "  return (t && t.target === 'local' && t.label) || 'The compute host';\n"
    '}\n'
    '// What a root IS, not which lookup found it: a compute whose own project root is a bucket was\n'
    '// offering it as "This computer — s3://…" (Paul, 2026-09-07). A local root is on the compute host.\n'
    'function _tlcRootLabel(url) {\n'
    "  if (_tlcStorageOf(url) === 'local') return _tlcHostName() + ' — ' + url;\n"
    "  var scheme = (String(url).split('://')[0] || '').toLowerCase();\n"
    "  var kind = scheme === 's3' ? 'S3 bucket'\n"
    "    : scheme === 'gs' ? 'Cloud Storage bucket'\n"
    "    : (scheme === 'az' || scheme === 'abfs' || scheme === 'abfss') ? 'Azure container'\n"
    "    : 'Bucket';\n"
    "  return kind + ' — ' + url;\n"
    '}\n'
    '// Compatibility for older fragments: alias settings no longer offer permanent copies.\n'
    'function _tlcAliasReviewCopy(idPrefix, projectName, folderValue, pluginId, rootOverride, opts) {}\n'
    'function _tlcBindAliasToggle(idPrefix) { return idPrefix; }\n'
    '\n'
    'function _tlcAliasLocationError(idPrefix) {\n'
    '  var folder = _tlcGetAliasValues(idPrefix).alias_folder;\n'
    "  var box = document.getElementById(idPrefix + '-alias-folder');\n"
    "  var root = _tlcSelectedProjectRoot(idPrefix) || (box && box.dataset.projectRoot) || '';\n"
    "  return folder && root && _tlcStorageOf(folder) === 'local' && _tlcStorageOf(root) !== 'local'\n"
    "    ? 'This project is in cloud storage, but its alias would point to a local disk. Select a cloud source "
    "or a local project location. To relocate data, copy it in Storage first.' : '';\n"
    '}\n'
    '\n'
    'function _tlcAliasMapping(idPrefix) {\n'
    "  var el = document.getElementById(idPrefix + '-alias-mapping');\n"
    '  if (!el) return;\n'
    '  var values = _tlcGetAliasValues(idPrefix);\n'
    "  var notice = document.getElementById(idPrefix + '-alias-location-error');\n"
    '  if (notice) {\n'
    '    notice.textContent = _tlcAliasLocationError(idPrefix);\n'
    "    notice.style.display = notice.textContent ? '' : 'none';\n"
    '  }\n'
    '  el.textContent = values.alias_folder\n'
    "    ? (values.alias_token ? '<' + values.alias_token + '> → ' : '') + values.alias_folder\n"
    "    : 'Source root will follow the selected data.';\n"
    '}\n'
    '\n'
    '// Extra legacy arguments are accepted but never enable a copy workflow.\n'
    'function _tlcBindAliasAutoUpdate(idPrefix, projectInputId, folderInputId, pluginId, rootInputId, opts) {\n'
    '  var projInput = document.getElementById(projectInputId);\n'
    "  var tokenInput = document.getElementById(idPrefix + '-alias-token');\n"
    '  var folderInput = folderInputId ? document.getElementById(folderInputId) : null;\n'
    "  var aliasFolderInput = document.getElementById(idPrefix + '-alias-folder');\n"
    '  function sync() { _tlcSyncAliasFromForm(idPrefix, projectInputId, folderInputId); }\n'
    "  var rootInput = document.getElementById(rootInputId || idPrefix + '-project-root');\n"
    "  if (rootInput) rootInput.addEventListener('change', sync);\n"
    '  _tlcDefaultProjectRoot().then(function(root) {\n'
    '    if (aliasFolderInput) aliasFolderInput.dataset.projectRoot = root;\n'
    '    _tlcAliasMapping(idPrefix);\n'
    '  });\n'
    '  [projInput, folderInput].forEach(function(el) {\n'
    "    if (el) { el.addEventListener('input', sync); el.addEventListener('change', sync); }\n"
    '  });\n'
    '  [tokenInput, aliasFolderInput].forEach(function(el) {\n'
    '    if (!el) return;\n'
    "    el.addEventListener('input', function() {\n"
    "      if (el.value.trim()) el.dataset.userEdited = '1';\n"
    '      else delete el.dataset.userEdited;\n'
    '      sync();\n'
    '    });\n'
    "    el.addEventListener('change', sync);\n"
    '  });\n'
    '  sync();\n'
    '}\n'
    '\n'
    'function _tlcSyncAliasFromForm(idPrefix, projectInputId, folderInputId) {\n'
    '  var projEl = document.getElementById(projectInputId);\n'
    "  var tokenEl = document.getElementById(idPrefix + '-alias-token');\n"
    '  var folderEl = folderInputId ? document.getElementById(folderInputId) : null;\n'
    "  var aliasFolderEl = document.getElementById(idPrefix + '-alias-folder');\n"
    '  if (folderEl && aliasFolderEl) {\n'
    "    var source = folderEl.value || '';\n"
    '    if (!aliasFolderEl.dataset.userEdited &&\n'
    '        (aliasFolderEl.dataset.sourceValue !== source || !aliasFolderEl.value)) {\n'
    '      aliasFolderEl.value = source;\n'
    '    }\n'
    '    aliasFolderEl.dataset.sourceValue = source;\n'
    '  }\n'
    '  if (tokenEl && !tokenEl.dataset.userEdited) {\n'
    '    tokenEl.value = _tlcSuggestedAliasToken(aliasFolderEl && aliasFolderEl.value, projEl && projEl.value);'
    '\n'
    '  }\n'
    '  _tlcAliasMapping(idPrefix);\n'
    '}\n'
    '\n'
    'function _tlcSetAliasRoot(idPrefix, rootPath) {\n'
    "  var el = document.getElementById(idPrefix + '-alias-folder');\n"
    '  if (el && rootPath && !el.dataset.userEdited) {\n'
    '    el.value = rootPath;\n'
    "    el.dispatchEvent(new Event('change', {bubbles: true}));\n"
    '  }\n'
    '}\n'
    '\n'
    'function _tlcGetAliasValues(idPrefix) {\n'
    '  return {\n'
    '    alias_enabled: true,\n'
    "    alias_token: String((document.getElementById(idPrefix + '-alias-token') || {}).value || '').trim(),\n"
    "    alias_folder: String((document.getElementById(idPrefix + '-alias-folder') || {}).value || '').trim(),\n"
    '    // Compatibility with older fragments: no permanent-copy request is emitted.\n'
    '    alias_copy_to_root: false,\n'
    "    alias_copy_target: '',\n"
    '  };\n'
    '}\n'
)
# fmt: on


PROJECT_LOCATION_JS = (
    "// ── Where the project goes ─────────────────────────────────────────────\n"
    "// A plugin that creates tables writes them under a project root. Every job carries one: the host\n"
    "// stamps its configured root into the run body unless the person chose another. This select offers\n"
    "// that default first and then the deployment's other locations (its scan folders, and roots its data\n"
    "// already lives under), so a choice lands where the Dashboard looks. One choice for the whole Hub —\n"
    "// every plugin that writes tables reads the same key, so picking a bucket in one import page means the\n"
    "// next one already knows. Shown whenever a root is known: with one root it is a single, obvious line\n"
    "// rather than a mystery (Paul, 2026-09-06).\n"
    "function _tlcProjectLocationHtml(idPrefix) {\n"
    '  var html = \'<div class="form-group" id="\' + idPrefix + \'-project-location" style="display:none">\';\n'
    "  html += '<label class=\"form-label\" for=\"' + idPrefix + '-project-root\">Create project in</label>';\n"
    "  html += '<select id=\"' + idPrefix + '-project-root\" class=\"form-control\"></select>';\n"
    "  // Shown in place of the select when there is only one root: a dropdown that cannot drop down\n"
    "  // reads as a broken control (Paul, 2026-09-08). The select stays in the DOM, holding the value.\n"
    "  html += '<div class=\"form-control\" id=\"' + idPrefix + '-project-root-only\"'\n"
    "        + ' style=\"display:none;background:var(--bg);color:var(--text-muted);border-style:dashed\"></div>';\n"
    "  html += '<div class=\"form-help\" id=\"' + idPrefix + '-project-root-help\"></div>';\n"
    "  html += '</div>';\n"
    "  return html;\n"
    "}\n"
    "function _tlcKnownProjectRoots() {\n"
    "  // The deployment's locations — the Object Service's project root and scan folders, plus roots its\n"
    "  // data already lives under — as the host's data API lists them: [{root, label, is_default}]. Writing\n"
    "  // anywhere else is allowed, but the Dashboard will not see it until the deployment scans it.\n"
    "  var API = window.PLUGIN_API;\n"
    "  var data = API && API.data;\n"
    "  if (!data || !data.getLocations) return Promise.resolve([]);\n"
    "  var loaded = data.load ? data.load().catch(function() {}) : Promise.resolve();\n"
    "  return loaded.then(function() {\n"
    "    return (data.getLocations() || []).map(function(loc) {\n"
    "      return { root: String((loc && loc.root) || '').replace(/\\/$/, ''),\n"
    "        label: String((loc && loc.label) || ''), is_default: !!(loc && loc.is_default) };\n"
    "    }).filter(function(loc) { return !!loc.root; });\n"
    "  }).catch(function() { return []; });\n"
    "}\n"
    "function _tlcBindProjectLocation(idPrefix, pluginId) {\n"
    "  var box = document.getElementById(idPrefix + '-project-location');\n"
    "  var sel = document.getElementById(idPrefix + '-project-root');\n"
    "  var help = document.getElementById(idPrefix + '-project-root-help');\n"
    "  if (!box || !sel) return Promise.resolve('');\n"
    "  var key = 'tlc.projectRoot';  // shared: the destination is the person's, not the plugin's\n"
    "  return Promise.all([_tlcDefaultProjectRoot(), _tlcKnownProjectRoots()]).then(function(got) {\n"
    "    var own = got[0], known = got[1];\n"
    "    var options = [];\n"
    "    function add(root, label) {\n"
    "      if (!root || options.some(function(o) { return o.value === root; })) return;\n"
    "      options.push({ value: root, label: label });\n"
    "    }\n"
    "    // The host's default first: it is what a run gets when nothing is chosen.\n"
    "    if (own) { add(own, _tlcRootLabel(own) + ' (default)'); sel.setAttribute('data-own-root', own); }\n"
    "    known.forEach(function(loc) { add(loc.root, _tlcRootLabel(loc.root)); });\n"
    "    var scanned = {};\n"
    "    known.forEach(function(loc) { scanned[loc.root] = true; });\n"
    "    sel.textContent = '';\n"
    "    options.forEach(function(o) { var el = document.createElement('option'); el.value = o.value; "
    "el.textContent = o.label; sel.appendChild(el); });\n"
    "    var remembered = '';\n"
    "    try {\n"
    "      remembered = window.localStorage.getItem(key) || window.localStorage.getItem(key + '.' + pluginId) || '';\n"
    "    } catch (e) {}\n"
    "    if (remembered && options.some(function(o) { return o.value === remembered; })) sel.value = remembered;\n"
    "    function describe() {\n"
    "      if (!help) return;\n"
    "      var isCloud = _tlcStorageOf(sel.value) !== 'local';   // what the root is, not where it came from\n"
    "      var host = _tlcHostName();\n"
    "      var text = isCloud\n"
    "        ? 'The table is written to the bucket; GPU nodes and the Dashboard read it there.'\n"
    "        : 'The table stays on ' + host.charAt(0).toLowerCase() + host.slice(1)"
    " + ', in its 3LC projects folder.';\n"
    "      // A root the deployment does not scan is allowed, but invisible: say so rather than refuse.\n"
    "      if (sel.value && known.length && !scanned[sel.value]) text += "
    "' This deployment does not scan this location, so the Dashboard will not list the project until it does.';\n"
    "      help.textContent = text;\n"
    "    }\n"
    "    sel.addEventListener('change', function() {\n"
    "      try { window.localStorage.setItem(key, sel.value); } catch (e) {}\n"
    "      describe();\n"
    "    });\n"
    "    describe();\n"
    "    // The roots arrive after the form is built, so anything that reads this select — a default\n"
    "    // copy destination, the alias folder that follows it — has already run against an empty one\n"
    "    // and given up. Say so once, the way a person changing it would (Paul, 2026-09-07).\n"
    "    if (options.length) sel.dispatchEvent(new Event('change', { bubbles: true }));\n"
    "    // One root is a statement, not a choice: show it as text and keep the select for its value.\n"
    "    var only = document.getElementById(idPrefix + '-project-root-only');\n"
    "    if (only) {\n"
    "      var single = options.length === 1;\n"
    "      only.textContent = single ? options[0].label : '';\n"
    "      only.style.display = single ? '' : 'none';\n"
    "      sel.style.display = single ? 'none' : '';\n"
    "    }\n"
    "    box.style.display = options.length ? '' : 'none';  // no root at all: nothing to say\n"
    "    sel.disabled = options.length < 2;  // one root: it is a statement, not a choice\n"
    "    sel.dispatchEvent(new Event('change', {bubbles: true}));\n"
    "    return sel.value;\n"
    "  });\n"
    "}\n"
    "// Where the table will actually be written, whichever option is selected. _tlcGetProjectRoot\n"
    "// answers a different question — 'what should I send as an override?' — and says '' when the\n"
    "// selection is the plugin's own root, which is the usual case. A form deriving a default path\n"
    "// from it therefore got nothing and left its field empty (Paul, 2026-09-07).\n"
    "function _tlcSelectedProjectRoot(idPrefix) {\n"
    "  var sel = document.getElementById(idPrefix + '-project-root');\n"
    "  if (!sel || !sel.options.length) return '';\n"
    "  return String(sel.value || '').replace(/\\/$/, '');\n"
    "}\n"
    "function _tlcGetProjectRoot(idPrefix) {\n"
    "  var sel = document.getElementById(idPrefix + '-project-root');\n"
    "  if (!sel || !sel.options.length) return '';\n"
    "  var own = sel.getAttribute('data-own-root') || '';\n"
    "  var value = String(sel.value || '');\n"
    "  return value && value !== own ? value : '';  // '' = the host's default, stamped by the host\n"
    "}\n"
)


def alias_ui_script() -> str:
    """Return the shared alias UI JavaScript block.

    Include this once in a ``<script>`` tag.  Then call:

    - ``_tlcAliasSettingsHtml(prefix, project, folder)`` to render HTML
    - ``_tlcBindAliasToggle(prefix)`` after inserting the HTML
    - ``_tlcBindAliasAutoUpdate(prefix, projectInputId, folderInputId, pluginId, rootInputId, opts)``
      — follows source changes and preserves manually edited names/roots. Legacy copy options
      are ignored; deliberate relocation belongs in Storage, run staging in the host planner.
    - ``_tlcGetAliasValues(prefix)`` at submit time
    - ``_tlcProjectLocationHtml(prefix)`` + ``_tlcBindProjectLocation(prefix, pluginId)`` for the
      "Create project in" choice (the host's default root first, then the deployment's other
      locations); ``_tlcGetProjectRoot(prefix)`` at submit time — ``''`` means the host's default,
      which the host stamps into the run body itself

    Returns:
        JavaScript source string.

    """
    return ALIAS_UI_JS + "\n" + PROJECT_LOCATION_JS
