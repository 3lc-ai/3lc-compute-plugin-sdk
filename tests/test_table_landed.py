# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""A landed table gets one ending: Open in Project, and Open in Dashboard when the host knows one."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from tlc_plugin_sdk.shared.table_landed import TABLE_LANDED_JS, table_landed_script

if TYPE_CHECKING:
    from pathlib import Path

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _render(tmp_path: Path, api: dict[str, str] | None, *args: object) -> str:
    """Run ``_tlcTableLandedHtml(*args)`` in node with a stub ``PLUGIN_API`` and return the HTML."""
    api_js = "null"
    if api is not None:
        api_js = (
            "{ getConfig: function (k) { return "
            + json.dumps(api)
            + "[k] || ''; }"
            + (
                ", dashboardUrl: function (p) { return 'https://dash.example/built?table=' + p.table; }"
                if api.get("_builds")
                else ""
            )
            + " }"
        )
    harness = tmp_path / "harness.js"
    harness.write_text(
        "var window = { PLUGIN_API: "
        + api_js
        + " };\n"
        + TABLE_LANDED_JS
        + "\nprocess.stdout.write(_tlcTableLandedHtml.apply(null, "
        + json.dumps(list(args))
        + "));\n"
    )
    return subprocess.run(["node", str(harness)], check=True, capture_output=True, text=True).stdout


def test_script_is_exposed_for_injection() -> None:
    assert table_landed_script() is TABLE_LANDED_JS
    assert "function _tlcTableLandedHtml(" in TABLE_LANDED_JS


@needs_node
def test_script_parses(tmp_path: Path) -> None:
    js = tmp_path / "landed.js"
    js.write_text(TABLE_LANDED_JS)
    subprocess.run(["node", "--check", str(js)], check=True, capture_output=True)


@needs_node
def test_open_in_project_lands_on_the_datasets_tab_with_the_table_selected(tmp_path: Path) -> None:
    html = _render(tmp_path, {}, "my proj", "train", "s3://b/p/my proj/datasets/train/tables/t.json")
    assert 'href="/projects/my%20proj?dataset=train&amp;table=s3%3A%2F%2Fb%2Fp%2Fmy%20proj' in html
    assert html.count("#datasets") == 1
    assert "Open in Project" in html and "btn-primary" in html
    assert "Open in Dashboard" not in html  # the host knows no Dashboard
    assert _render(tmp_path, {}, "", "train", "s3://x") == ""  # nothing to link to


@needs_node
def test_dashboard_link_is_the_hosts_when_it_can_build_one(tmp_path: Path) -> None:
    built = _render(tmp_path, {"dashboard_url": "https://dash.example/", "_builds": "1"}, "p", "d", "s3://b/t.json")
    assert 'href="https://dash.example/built?table=s3://b/t.json" target="_blank" rel="noopener"' in built
    older = _render(tmp_path, {"dashboard_url": "https://dash.example/"}, "p", "d", "s3://b/t.json")
    assert 'href="https://dash.example?table=s3%3A%2F%2Fb%2Ft.json"' in older  # older host: concatenated
    secondary = _render(tmp_path, {}, "p", "d", "s3://b/t.json", {"primary": False})
    assert "btn-secondary" in secondary and "btn-primary" not in secondary
