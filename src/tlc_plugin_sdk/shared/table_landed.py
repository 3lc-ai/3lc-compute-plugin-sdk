# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""What a table-creating plugin offers when a table has just landed: view it, or open the Dashboard.

For plugins that *make* a table — importers, converters. An export plugin ends somewhere else entirely
and is none of this module's business.

Every importer ends the same way — a table now exists — and every importer had written its own ending.
The 3LC importer offered "View in Project · Export" for COCO and YOLO, "View in Project · Open in
Dashboard · Export" for CSV, "View in Project" alone in its job list, and the Hugging Face plugin a bare
link to the project (Paul, 2026-09-08). Four endings to the same event, and none of them the same.

One ending, here: **Open in Project** and **Open in Dashboard**. No export — the person just imported
something; offering to export it back out is noise at that moment.

"Open in Project" goes straight to the Datasets tab with the new table selected
(``?dataset=…&table=…#datasets``), never to the project's default tab, where you would land on Runs and
have to go and find what you just made.
"""

from __future__ import annotations

TABLE_LANDED_JS = r"""
// _tlcTableLandedHtml(project, dataset, tableUrl, opts) -> HTML for the actions on a landed table.
//
// opts.primary  (default true)  render "Open in Project" as the primary button
//
// Buttons only, and always these two names: a caller with several tables to report names each one in
// its own row rather than folding the name into a button ("View train" said neither where it went nor
// that a Dashboard button existed — Paul, 2026-09-08).
//
// Returns "" when there is no project to link to. The Dashboard button appears only when the host
// knows a Dashboard URL and the table has one.
function _tlcTableLandedHtml(project, dataset, tableUrl, opts) {
  opts = opts || {};
  var e = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  if (!project) return '';
  var view = '/projects/' + encodeURIComponent(project) +
    '?dataset=' + encodeURIComponent(dataset || '') +
    (tableUrl ? '&table=' + encodeURIComponent(tableUrl) : '') + '#datasets';
  var cls = opts.primary === false ? 'btn btn-secondary btn-sm' : 'btn btn-primary btn-sm';
  var html = '<a href="' + e(view) + '" class="' + cls + '">Open in Project</a>';
  // The Dashboard link is the host's to build: it carries object_service, so the Dashboard opens
  // against the workspace this browser is on rather than its own default. Concatenating
  // dashboard_url + '?table=' worked on a laptop and pointed somewhere else anywhere else.
  var api = window.PLUGIN_API || {};
  var known = '';
  try { known = api.getConfig ? String(api.getConfig('dashboard_url') || '') : ''; } catch (err) { known = ''; }
  if (known && tableUrl) {
    var href = api.dashboardUrl
      ? api.dashboardUrl({ table: tableUrl })                       // the host knows how
      : known.replace(/\/$/, '') + '?table=' + encodeURIComponent(tableUrl);  // older host
    html += ' <a href="' + e(href) + '" target="_blank" rel="noopener"' +
      ' class="btn btn-secondary btn-sm">Open in Dashboard</a>';
  }
  return html;
}
"""


def table_landed_script() -> str:
    """The shared "a table landed" actions, for :func:`~tlc_plugin_sdk.shared.ui_inject.inject_scripts`."""
    return TABLE_LANDED_JS
