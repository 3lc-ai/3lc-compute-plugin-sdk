# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
r"""Shared UI component for URL alias settings.

Generates the HTML + JS block that plugins embed in their UI fragments.
This ensures consistent alias UI across importer, SAM3, and any future
plugin that creates 3LC tables.

Usage in a plugin's ``get_ui_fragment()``::

    from tlc_plugin_sdk.shared.alias_ui import alias_ui_script
    from tlc_plugin_sdk.shared.ui_inject import inject_scripts

    raw = Path("ui.html").read_text()
    html = inject_scripts(raw, alias_ui_script())
"""

from __future__ import annotations

# The JS helper functions are defined once and shared by all plugins.
# Each plugin calls _tlcAliasSettingsHtml(idPrefix, projectValue, folderValue)
# to render the alias section, and _tlcGetAliasValues(idPrefix) to read values.

# fmt: off
ALIAS_UI_JS = (
    "// ── Shared URL Alias UI ─────────────────────────────────\n"
    "(function(){var st=document.createElement('style');st.textContent="
    "'.tlc-tip{position:relative}'"
    "+'.tlc-tip .tlc-tip-text{display:none;position:absolute;bottom:calc(100% + 8px);"
    "left:50%;transform:translateX(-50%);width:260px;padding:8px 10px;"
    "background:var(--bg-card,#1a2332);color:var(--text-muted,#94a3b8);"
    "font-size:11px;font-weight:400;line-height:1.5;border-radius:6px;"
    "border:1px solid var(--border,#2a3a4a);box-shadow:0 4px 12px rgba(0,0,0,.3);"
    "z-index:1000;pointer-events:none;white-space:normal}'"
    "+'.tlc-tip:hover .tlc-tip-text{display:block}';"
    "document.head.appendChild(st)})();\n"
    "\n"
    "function _tlcDefaultAliasToken(projectName) {\n"
    "  var token = projectName.toUpperCase()"
    ".replace(/[^A-Z0-9]/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '');\n"
    "  if (!token || !/^[A-Z]/.test(token)) token = 'PROJECT_' + token;\n"
    "  return token;\n"
    "}\n"
    "\n"
    "function _tlcAliasSettingsHtml(idPrefix, projectValue, folderValue) {\n"
    "  var token = projectValue ? _tlcDefaultAliasToken(projectValue) : '';\n"
    "  var s = 'margin-top:12px;padding:10px 12px;border:1px solid var(--border)';\n"
    "  s += ';border-radius:6px;background:var(--bg)';\n"
    "  var html = '<div class=\"tlc-alias-settings\" style=\"' + s + '\">';\n"
    "  html += '<div style=\"display:flex;align-items:center;gap:6px\">';\n"
    "  html += '<label style=\"display:flex;align-items:center;gap:6px;"
    "font-size:12px;font-weight:600;cursor:pointer;flex:1\">';\n"
    "  html += '<input type=\"checkbox\" id=\"' + idPrefix"
    " + '-alias-enabled\" checked> Register URL Alias';\n"
    "  html += '<span class=\"tlc-tip\" style=\"display:inline-flex;align-items:center;"
    "justify-content:center;width:15px;height:15px;border-radius:50%;"
    "background:var(--border);color:var(--text-muted);font-size:10px;"
    "font-weight:700;cursor:help;flex-shrink:0;position:relative\">"
    "?<span class=\"tlc-tip-text\">URL aliases make image paths portable "
    "across machines. Instead of hardcoding absolute paths, the table "
    "stores &lt;ALIAS&gt;/images \\u2014 so the same table works on any "
    "machine where the alias is configured. Recommended for all new "
    "tables.</span></span>';\n"
    "  html += '</label>';\n"
    "  html += '<button type=\"button\" id=\"' + idPrefix + '-alias-toggle\" "
    "style=\"background:none;border:none;color:var(--text-muted);cursor:pointer;"
    "font-size:10px;padding:2px 6px\" "
    "onclick=\"var f=document.getElementById(\\'' + idPrefix + '-alias-fields\\');"
    "var open=f.style.display!==\\'none\\';f.style.display=open?\\'none\\':\\'\\';"
    "this.textContent=open?\\'Details ▸\\':\\'Details ▾\\'\">Details ▸</button>';\n"
    "  html += '</div>';\n"
    "  html += '<div id=\"' + idPrefix + '-alias-fields\" style=\"display:none;"
    "margin-top:8px\">';\n"
    "  html += '<div style=\"display:grid;"
    "grid-template-columns:1fr 1fr;gap:8px\">';\n"
    "  html += '<div class=\"form-group\" style=\"margin-bottom:0\">';\n"
    "  html += '<label class=\"form-label\" style=\"font-size:11px\">"
    "Alias Token</label>';\n"
    "  html += '<input type=\"text\" id=\"' + idPrefix + '-alias-token\"';\n"
    "  html += ' class=\"form-control\" style=\"font-size:11px\"';\n"
    "  html += ' value=\"' + (token || '') + '\"';\n"
    "  html += ' placeholder=\"PROJECT_NAME\">';\n"
    "  html += '<div style=\"font-size:10px;color:var(--text-muted);"
    "margin-top:2px\">';\n"
    "  html += 'Must be UPPER_SNAKE_CASE (A-Z, 0-9, _)</div>';\n"
    "  html += '</div>';\n"
    "  html += '<div class=\"form-group\" style=\"margin-bottom:0\">';\n"
    "  html += '<label class=\"form-label\" style=\"font-size:11px\">"
    "Alias Folder</label>';\n"
    "  html += '<input type=\"text\" id=\"' + idPrefix + '-alias-folder\"';\n"
    "  html += ' class=\"form-control\" style=\"font-size:11px\"';\n"
    "  html += ' value=\"' + (folderValue || '') + '\"';\n"
    "  html += ' placeholder=\"Auto-detected from input\">';\n"
    "  html += '<div style=\"font-size:10px;color:var(--text-muted);"
    "margin-top:2px\">';\n"
    "  html += 'Folder that the alias points to</div>';\n"
    "  html += '</div>';\n"
    "  html += '</div>';\n"
    "  html += '</div>';\n"
    "  html += '</div>';\n"
    "  return html;\n"
    "}\n"
    "\n"
    "function _tlcBindAliasToggle(idPrefix) {\n"
    "  var cb = document.getElementById(idPrefix + '-alias-enabled');\n"
    "  var fields = document.getElementById(idPrefix + '-alias-fields');\n"
    "  if (!cb || !fields) return;\n"
    "  cb.addEventListener('change', function() {\n"
    "    fields.style.display = cb.checked ? '' : 'none';\n"
    "    document.getElementById(idPrefix + '-alias-toggle')"
    ".style.display = cb.checked ? '' : 'none';\n"
    "  });\n"
    "}\n"
    "\n"
    "function _tlcBindAliasAutoUpdate("
    "idPrefix, projectInputId, folderInputId) {\n"
    "  var projInput = document.getElementById(projectInputId);\n"
    "  var tokenInput = document.getElementById("
    "idPrefix + '-alias-token');\n"
    "  var folderInput = folderInputId"
    " ? document.getElementById(folderInputId) : null;\n"
    "  var aliasFolderInput = document.getElementById("
    "idPrefix + '-alias-folder');\n"
    "  if (projInput && tokenInput) {\n"
    "    projInput.addEventListener('input', function() {\n"
    "      if (!tokenInput.dataset.userEdited)\n"
    "        tokenInput.value = _tlcDefaultAliasToken(projInput.value);\n"
    "    });\n"
    "    tokenInput.addEventListener('input', function() {\n"
    "      tokenInput.dataset.userEdited = '1';\n"
    "    });\n"
    "  }\n"
    "  if (folderInput && aliasFolderInput) {\n"
    "    folderInput.addEventListener('input', function() {\n"
    "      if (!aliasFolderInput.dataset.userEdited)\n"
    "        aliasFolderInput.value = folderInput.value;\n"
    "    });\n"
    "    aliasFolderInput.addEventListener('input', function() {\n"
    "      aliasFolderInput.dataset.userEdited = '1';\n"
    "    });\n"
    "  }\n"
    "}\n"
    "\n"
    "function _tlcSyncAliasFromForm("
    "idPrefix, projectInputId, folderInputId) {\n"
    "  var projEl = document.getElementById(projectInputId);\n"
    "  var tokenEl = document.getElementById(idPrefix + '-alias-token');\n"
    "  var folderEl = folderInputId"
    " ? document.getElementById(folderInputId) : null;\n"
    "  var aliasFolderEl = document.getElementById("
    "idPrefix + '-alias-folder');\n"
    "  if (projEl && tokenEl && !tokenEl.dataset.userEdited) {\n"
    "    tokenEl.value = projEl.value"
    " ? _tlcDefaultAliasToken(projEl.value) : '';\n"
    "  }\n"
    "  if (folderEl && aliasFolderEl"
    " && !aliasFolderEl.dataset.userEdited) {\n"
    "    aliasFolderEl.value = folderEl.value || '';\n"
    "  }\n"
    "}\n"
    "\n"
    "function _tlcSetAliasRoot(idPrefix, rootPath) {\n"
    "  var el = document.getElementById(idPrefix + '-alias-folder');\n"
    "  if (el && rootPath) {\n"
    "    el.value = rootPath;\n"
    "    el.dataset.userEdited = '1';\n"
    "  }\n"
    "}\n"
    "\n"
    "function _tlcGetAliasValues(idPrefix) {\n"
    "  var cb = document.getElementById(idPrefix + '-alias-enabled');\n"
    "  return {\n"
    "    alias_enabled: cb ? cb.checked : true,\n"
    "    alias_token: (document.getElementById(\n"
    "      idPrefix + '-alias-token') || {}).value || '',\n"
    "    alias_folder: (document.getElementById(\n"
    "      idPrefix + '-alias-folder') || {}).value || '',\n"
    "  };\n"
    "}\n"
)
# fmt: on


def alias_ui_script() -> str:
    """Return the shared alias UI JavaScript block.

    Include this once in a ``<script>`` tag.  Then call:

    - ``_tlcAliasSettingsHtml(prefix, project, folder)`` to render HTML
    - ``_tlcBindAliasToggle(prefix)`` after inserting the HTML
    - ``_tlcBindAliasAutoUpdate(prefix, projectInputId, folderInputId)``
    - ``_tlcGetAliasValues(prefix)`` at submit time

    Returns:
        JavaScript source string.

    """
    return ALIAS_UI_JS
