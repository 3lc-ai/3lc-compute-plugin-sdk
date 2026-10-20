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


def test_guide_is_an_optional_browser_only_capability() -> None:
    dts = _dts_text()
    assert "guide?: PluginGuide | null;" in dts
    block = re.search(r"export interface PluginGuide\s*\{(.*?)\n\}", dts, re.DOTALL)
    assert block
    assert "readonly version: 1;" in block.group(1)
    assert set(re.findall(r"^\s*(\w+)\(", block.group(1), re.MULTILINE)) == {"register", "complete", "dispose"}
    assert "register(tips: readonly PluginGuideTip[]): void;" in block.group(1)
    tip = re.search(r"export interface PluginGuideTip\s*\{(.*?)\n\}", dts, re.DOTALL)
    assert tip
    assert set(re.findall(r"^\s*(\w+)\??:", tip.group(1), re.MULTILINE)) == {
        "id",
        "target",
        "title",
        "body",
        "task",
        "experience",
    }


def test_list_is_present_on_both_sides() -> None:
    # The 0.3 addition — guard it explicitly so a regression is unambiguous.
    assert "list" in _js_exported_names()
    assert "list" in _dts_pluginjobs_members()


# Helpers the shared scripts define for their own use. Global (the scripts are plain <script> code) but
# not part of the contract, so the .d.ts leaves them out.
_INTERNAL_HELPERS = {
    "_tlcAliasMapping",
    "_tlcSuggestedAliasToken",
    "_tlcDsBrowseUrl",
    "_tlcDsBucketReach",
    "_tlcDsLocationList",
    "_tlcDsReachNote",
    "_tlcHostName",
    "_tlcHubAsksForData",
    "_tlcKnownProjectRoots",
    "_tlcProjectRootUrl",  # a pre-0.5 name for _tlcDefaultProjectRoot, kept working, not advertised
}


def test_every_shared_widget_helper_is_declared() -> None:
    from tlc_plugin_sdk.shared.alias_override_ui import alias_override_ui_script
    from tlc_plugin_sdk.shared.alias_ui import alias_ui_script
    from tlc_plugin_sdk.shared.data_source_ui import data_source_ui_script

    js = alias_ui_script() + data_source_ui_script() + alias_override_ui_script()
    defined = set(re.findall(r"^function (_tlc\w+)\(", js, re.MULTILINE)) - _INTERNAL_HELPERS
    declared = set(re.findall(r"^\s*function (_tlc\w+)\(", _dts_text(), re.MULTILINE))
    assert defined <= declared, f"undeclared helpers: {sorted(defined - declared)}"


def test_alias_auto_update_is_declared_with_its_real_arity() -> None:
    dts = _dts_text()
    block = re.search(r"function _tlcBindAliasAutoUpdate\((.*?)\): void;", dts, re.DOTALL)
    assert block
    params = [p.strip().split(":")[0].rstrip("?") for p in block.group(1).split(",") if p.strip()]
    assert params == ["idPrefix", "projectInputId", "folderInputId", "pluginId", "rootInputId", "opts"]


def test_the_run_target_carries_its_label_and_the_plan_is_optional() -> None:
    dts = _dts_text()
    assert "getRunTarget?(): PluginRunTarget;" in dts
    target = re.search(r"export interface PluginRunTarget\s*\{(.*?)\n\}", dts, re.DOTALL)
    assert target
    assert set(re.findall(r"^\s*(\w+)\??:", target.group(1), re.MULTILINE)) == {
        "target",
        "node_id",
        "ready",
        "label",
        "files_root",
        "browse_roots",
    }
    assert "planRun?(body: Record<string, unknown>): Promise<PluginRunPlan>;" in dts
