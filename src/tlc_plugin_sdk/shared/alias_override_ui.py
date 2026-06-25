# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
r"""Shared UI component for URL alias overrides.

Generates the HTML + JS block that plugins embed in their UI fragments.
This ensures consistent alias override UI across exporter, YOLO, timm,
SAM3, and any future plugin that consumes existing 3LC tables.

The override section is automatically hidden when all alias paths are
already local to the compute service (i.e. accessible on disk).

Usage in a plugin's ``get_ui_fragment()``::

    from tlc_plugin_sdk.shared.alias_override_ui import alias_override_ui_script
    from tlc_plugin_sdk.shared.ui_inject import inject_scripts

    raw = Path("ui.html").read_text()
    html = inject_scripts(raw, alias_override_ui_script())
"""

from __future__ import annotations

# fmt: off
ALIAS_OVERRIDE_UI_JS = (
    "// \u2500\u2500 Shared URL Alias Override UI \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
    "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
    "function _tlcAliasOverrideHtml(idPrefix) {\n"
    "  var s = 'margin-top:12px;padding:12px;border:1px solid var(--border)';\n"
    "  s += ';border-radius:6px;background:var(--bg)';\n"
    "  var html = '<div class=\"tlc-alias-override\" id=\"' + idPrefix"
    " + '-alias-override-container\" style=\"' + s + '\">';\n"
    "  html += '<label style=\"display:flex;align-items:center;gap:6px;"
    "font-size:12px;font-weight:600;cursor:pointer\">';\n"
    "  html += '<input type=\"checkbox\" id=\"' + idPrefix"
    " + '-alias-override-enabled\"> Override URL Aliases';\n"
    "  html += '</label>';\n"
    "  html += '<div style=\"margin:4px 0 8px 0;font-size:11px;"
    "color:var(--text-muted)\">';\n"
    "  html += 'Redirect alias paths to local storage for faster I/O "
    "\u2014 ';\n"
    "  html += 'e.g. use a local SSD copy instead of S3. ';\n"
    "  html += '<a href=\"https://docs.3lc.ai/3lc/latest/user-guide/"
    "sharing.html#alias-best-practices\" ';\n"
    "  html += 'target=\"_blank\" style=\"color:var(--accent)\">"
    "Learn more</a>';\n"
    "  html += '</div>';\n"
    "  html += '<div id=\"' + idPrefix + '-alias-override-fields\""
    " style=\"display:none\">';\n"
    "  html += '<div id=\"' + idPrefix + '-alias-override-list\""
    " style=\"font-size:11px;color:var(--text-muted)\">"
    "Select a table to see its aliases</div>';\n"
    "  html += '</div>';\n"
    "  html += '</div>';\n"
    "  return html;\n"
    "}\n"
    "\n"
    "function _tlcBindAliasOverrideToggle(idPrefix) {\n"
    "  var cb = document.getElementById(idPrefix + '-alias-override-enabled');\n"
    "  var fields = document.getElementById(idPrefix + '-alias-override-fields');\n"
    "  if (!cb || !fields) return;\n"
    "  cb.addEventListener('change', function() {\n"
    "    fields.style.display = cb.checked ? '' : 'none';\n"
    "  });\n"
    "}\n"
    "\n"
    "function _tlcFetchAndPopulateOverrides(idPrefix, tableUrl, savedOverrides) {\n"
    "  var list = document.getElementById(idPrefix + '-alias-override-list');\n"
    "  var container = document.getElementById(idPrefix + '-alias-override-container');\n"
    "  if (!list) return;\n"
    "  if (!tableUrl) {\n"
    "    list.innerHTML = '<span style=\"font-size:11px;color:var(--text-muted)\">"
    "Select a table to see its aliases</span>';\n"
    "    if (container) container.style.display = '';\n"
    "    return;\n"
    "  }\n"
    "  list.innerHTML = '<span style=\"font-size:11px;color:var(--text-muted)\">"
    "Loading aliases...</span>';\n"
    "  var _aliasPath = '/api/aliases/for-table?url='"
    " + encodeURIComponent(tableUrl);\n"
    "  window.PLUGIN_API.computeFetch(_aliasPath)\n"
    "    .then(function(r) { return r.json(); })\n"
    "    .then(function(data) {\n"
    "      var aliases = data.aliases || [];\n"
    "      if ((data.all_local || aliases.length === 0) && !savedOverrides) {\n"
    "        if (container) container.style.display = 'none';\n"
    "        return;\n"
    "      }\n"
    "      if (container) container.style.display = '';\n"
    "      var html = '<div style=\"display:grid;grid-template-columns:auto 1fr 1fr;"
    "gap:6px 8px;align-items:center\">';\n"
    "      html += '<div style=\"font-weight:600;font-size:10px\">Token</div>';\n"
    "      html += '<div style=\"font-weight:600;font-size:10px\">Current Path</div>';\n"
    "      html += '<div style=\"font-weight:600;font-size:10px\">Override Path</div>';\n"
    "      for (var i = 0; i < aliases.length; i++) {\n"
    "        var a = aliases[i];\n"
    "        html += '<div style=\"font-family:monospace;font-size:11px;"
    "white-space:nowrap\">&lt;' + a.token + '&gt;</div>';\n"
    "        html += '<div style=\"font-size:11px;color:var(--text-muted);"
    "overflow:hidden;text-overflow:ellipsis;white-space:nowrap\""
    " title=\"' + (a.current_path || '') + '\">' + (a.current_path || '"
    "<em>unresolved</em>') + '</div>';\n"
    "        html += '<input type=\"text\" class=\"form-control\" style=\"font-size:11px\"';\n"
    "        html += ' data-alias-token=\"' + a.token + '\"';\n"
    "        html += ' data-alias-original=\"' + (a.current_path || '') + '\"';\n"
    "        html += ' id=\"' + idPrefix + '-alias-override-' + a.token + '\"';\n"
    "        html += ' placeholder=\"Leave empty to keep current\">';\n"
    "      }\n"
    "      html += '</div>';\n"
    "      list.innerHTML = html;\n"
    "      if (savedOverrides) _tlcRestoreAliasOverrides(idPrefix, savedOverrides);\n"
    "    })\n"
    "    .catch(function(err) {\n"
    "      list.innerHTML = '<span style=\"color:var(--danger,red);"
    "font-size:11px\">Failed to load aliases</span>';\n"
    "      console.error('Alias override fetch error:', err);\n"
    "    });\n"
    "}\n"
    "\n"
    "function _tlcRestoreAliasOverrides(idPrefix, saved) {\n"
    "  if (!saved || !saved.overrides || !saved.overrides.length) return;\n"
    "  var cb = document.getElementById(idPrefix + '-alias-override-enabled');\n"
    "  var fields = document.getElementById(idPrefix + '-alias-override-fields');\n"
    "  if (saved.enabled && cb) {\n"
    "    cb.checked = true;\n"
    "    if (fields) fields.style.display = '';\n"
    "  }\n"
    "  for (var i = 0; i < saved.overrides.length; i++) {\n"
    "    var ov = saved.overrides[i];\n"
    "    var input = document.getElementById(\n"
    "      idPrefix + '-alias-override-' + ov.token);\n"
    "    if (input && ov.path) input.value = ov.path;\n"
    "  }\n"
    "}\n"
    "\n"
    "function _tlcGetAliasOverrides(idPrefix) {\n"
    "  var cb = document.getElementById(idPrefix + '-alias-override-enabled');\n"
    "  var enabled = cb ? cb.checked : false;\n"
    "  var overrides = [];\n"
    "  if (enabled) {\n"
    "    var inputs = document.querySelectorAll(\n"
    "      '#' + idPrefix + '-alias-override-list input[data-alias-token]');\n"
    "    for (var i = 0; i < inputs.length; i++) {\n"
    "      var val = inputs[i].value.trim();\n"
    "      if (val) {\n"
    "        overrides.push({\n"
    "          token: inputs[i].getAttribute('data-alias-token'),\n"
    "          path: val\n"
    "        });\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "  return {enabled: enabled, overrides: overrides};\n"
    "}\n"
)
# fmt: on


def alias_override_ui_script() -> str:
    """Return the shared alias override UI JavaScript block.

    Include this once in a ``<script>`` tag.  Then call:

    - ``_tlcAliasOverrideHtml(prefix)`` to render HTML
    - ``_tlcBindAliasOverrideToggle(prefix)`` after inserting the HTML
    - ``_tlcFetchAndPopulateOverrides(prefix, tableUrl, savedOverrides)``
    - ``_tlcGetAliasOverrides(prefix)`` at submit time

    The override section is automatically hidden when all alias paths
    are local to the compute service filesystem.

    Returns:
        JavaScript source string.

    """
    return ALIAS_OVERRIDE_UI_JS
