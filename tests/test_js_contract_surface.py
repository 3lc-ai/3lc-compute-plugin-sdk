# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Keep the shipped ``PluginJobs`` client and its ``.d.ts`` declaration in lockstep.

``JOB_TRACKER_JS`` (the client a plugin's ``ui.html`` calls) and the
``PluginJobsApi`` interface in ``contract/plugin-api.d.ts`` (what a plugin
type-checks against) are two halves of one contract — if one grows a method the
other must too.
"""

from __future__ import annotations

import re
from pathlib import Path

import tlc_plugin_sdk
from tlc_plugin_sdk.shared.job_tracker import JOB_TRACKER_JS


def _dts_text() -> str:
    path = Path(tlc_plugin_sdk.__file__).parent / "contract" / "plugin-api.d.ts"
    return path.read_text(encoding="utf-8")


def _js_exported_names() -> set[str]:
    match = re.search(r"window\.PluginJobs\s*=\s*\{([^}]*)\}", JOB_TRACKER_JS)
    assert match, "could not find the window.PluginJobs export object"
    return set(re.findall(r"(\w+)\s*:", match.group(1)))


def _dts_pluginjobs_members() -> set[str]:
    dts = _dts_text()
    block = re.search(r"export interface PluginJobsApi\s*\{(.*?)\n\}", dts, re.DOTALL)
    assert block, "could not find the PluginJobsApi interface"
    # Member declarations: `name(` (optionally generic) at the start of a line;
    # JSDoc comment lines start with `*`, so they never match.
    return set(re.findall(r"^\s*(\w+)\s*(?:<[^>]*>)?\(", block.group(1), re.MULTILINE))


def test_pluginjobs_client_and_dts_agree() -> None:
    js_names = _js_exported_names()
    dts_names = _dts_pluginjobs_members()
    assert js_names, "no exported PluginJobs names parsed from the client"
    assert js_names == dts_names, f"client exports {js_names} but the d.ts declares {dts_names}"


def test_list_is_present_on_both_sides() -> None:
    # The 0.3 addition — guard it explicitly so a regression is unambiguous.
    assert "list" in _js_exported_names()
    assert "list" in _dts_pluginjobs_members()
