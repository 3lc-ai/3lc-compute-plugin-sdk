# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
r"""Where this page's runs go, for the shared widgets that care: the data-source picker and the alias card.

Not injected on its own: :func:`~tlc_plugin_sdk.shared.data_source_ui.data_source_ui_script` and
:func:`~tlc_plugin_sdk.shared.alias_ui.alias_ui_script` each carry it, so a fragment that uses either
widget gets these helpers. Both carry the same text, so a fragment that injects both defines each
function twice, identically.

.. code-block:: javascript

    _tlcComputeIsHere(computeUrl)  // true when the compute host is this browser's machine
    _tlcRunTarget()                // {target, node_id, ready, label, files_root}, or null
    _tlcStorageOf(pathOrUrl)       // 'local', or 'scheme://bucket'
"""

from __future__ import annotations

# fmt: off
RUN_TARGET_JS = (
    "// -- Shared run target ------\n"
    "// The browser's machine and the compute host are the same only when the compute URL is a loopback\n"
    "// address: only then is the host's disk \"This computer\".\n"
    "function _tlcComputeIsHere(computeUrl) {\n"
    "  return /^https?:\\/\\/(localhost|127\\.0\\.0\\.1|\\[::1\\]|0\\.0\\.0\\.0)(:|\\/|$)/i"
    ".test(String(computeUrl || ''));\n"
    "}\n"
    "// Where runs from this page go: the Hub's \"Run on:\" choice (PLUGIN_API.getRunTarget). null on a Hub\n"
    "// that predates it, which means: behave as before run targets existed. A node choice without a node id\n"
    "// is the compute host. label is the Hub's name for the target; '' from a Hub that does not say.\n"
    "function _tlcRunTarget() {\n"
    "  var API = window.PLUGIN_API;\n"
    "  if (!API || typeof API.getRunTarget !== 'function') return null;\n"
    "  var t = null;\n"
    "  try { t = API.getRunTarget(); } catch (e) { return null; }\n"
    "  if (!t || (t.target !== 'node' && t.target !== 'local')) return null;\n"
    "  var node = t.target === 'node' && !!t.node_id;\n"
    "  return { target: node ? 'node' : 'local', node_id: node ? String(t.node_id) : '', ready: t.ready !== false,\n"
    "           label: String(t.label || ''), files_root: String(t.files_root || ''), browse_roots: t.browse_roots };\n"
    "}\n"
    "function _tlcStorageOf(pathOrUrl) {\n"
    "  // 'local', or 'scheme://bucket' — what decides whether two places share storage.\n"
    "  var m = /^([a-z][a-z0-9+.-]*):\\/\\/([^/]+)/i.exec(String(pathOrUrl || '').trim());\n"
    "  return m ? (m[1].toLowerCase() + '://' + m[2]) : 'local';\n"
    "}\n"
)
# fmt: on


def run_target_ui_script() -> str:
    """Return the shared run-target helpers (already part of the data-source and alias scripts)."""
    return RUN_TARGET_JS
