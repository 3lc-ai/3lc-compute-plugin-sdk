# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The deprecated override card stays out of the way on a Hub that asks where a run's data is itself."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk.shared.alias_override_ui import alias_override_ui_script

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_FAKE_DOM = (Path(__file__).parent / "fixtures" / "fake_dom.js").read_text(encoding="utf-8")


def _card(tmp_path: Path, *, plan_run: bool) -> Any:
    harness = tmp_path / "override.js"
    harness.write_text(
        _FAKE_DOM
        + "\n"
        + alias_override_ui_script()
        + "\nvar api = fakeApi({'/api/aliases/for-table': {aliases: [{token: 'FIRE', current_path: '/x'}]}});\n"
        + "api.computeFetch = api.authFetch;\n"
        + ("api.planRun = function () { return Promise.resolve({}); };\n" if plan_run else "")
        + "window.PLUGIN_API = api;\n"
        + "['tr-alias-override-list', 'tr-alias-override-container'].forEach(addEl);\n"
        + "var html = _tlcAliasOverrideHtml('tr');\n"
        + "_tlcFetchAndPopulateOverrides('tr', 's3://b/p/datasets/d/tables/t', null);\n"
        + "tick().then(function () { process.stdout.write(JSON.stringify({html: html, fetched: api.fetched,"
        + " shown: _els['tr-alias-override-container'].style.display !== 'none',"
        + " list: _els['tr-alias-override-list'].innerHTML, overrides: _tlcGetAliasOverrides('tr')})); });\n"
    )
    done = subprocess.run(["node", str(harness)], check=True, capture_output=True, text=True)
    return json.loads(done.stdout)


@needs_node
def test_a_hub_that_plans_runs_gets_no_override_card(tmp_path: Path) -> None:
    out = _card(tmp_path, plan_run=True)
    assert out["html"] == "" and out["fetched"] == [] and not out["shown"] and out["list"] == ""
    assert out["overrides"] == {"enabled": False, "overrides": []}


@needs_node
def test_an_older_hub_keeps_the_card(tmp_path: Path) -> None:
    out = _card(tmp_path, plan_run=False)
    assert "Read this data from somewhere else for this run" in out["html"]
    assert out["fetched"] == ["/api/aliases/for-table?url=s3%3A%2F%2Fb%2Fp%2Fdatasets%2Fd%2Ftables%2Ft"]
    assert out["shown"] and "&lt;FIRE&gt;" in out["list"]


def test_the_module_says_it_is_deprecated() -> None:
    import tlc_plugin_sdk.shared.alias_override_ui as module

    assert module.__doc__ is not None and ".. deprecated::" in module.__doc__
