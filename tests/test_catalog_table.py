# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The shared catalogue table rides along only for fragments that ask for it, and its JS parses."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING, Any

import pytest
from litestar.testing import TestClient

from tlc_plugin_sdk.asgi_app import build_plugin_app
from tlc_plugin_sdk.contract import ComputePlugin
from tlc_plugin_sdk.shared.catalog_table import CATALOG_MARKER, CATALOG_TABLE_JS

if TYPE_CHECKING:
    from pathlib import Path

    from tlc_plugin_sdk.job_context import JobContext


class _Frag(ComputePlugin):
    def __init__(self, html: str) -> None:
        self._html = html

    def get_ui_fragment(self) -> str:
        return self._html

    def run_job(self, ctx: JobContext) -> None:
        return None


def _ui(html: str) -> str:
    plugin: Any = _Frag(html)
    plugin.id = "p"
    with TestClient(app=build_plugin_app(plugin)) as client:
        resp = client.get("/ui")
        assert resp.status_code == 200
        return resp.text


def test_catalog_script_is_injected_only_for_fragments_that_use_it() -> None:
    with_table = _ui('<div class="tlc-catalog" id="t"></div><script>var x = 1;</script>')
    assert "window.TlcCatalog" in with_table
    assert "window.PluginJobs" in with_table  # the job tracker still comes first
    assert with_table.index("window.PluginJobs") < with_table.index("window.TlcCatalog")
    without = _ui('<div id="t"></div><script>var x = 1;</script>')
    assert "window.TlcCatalog" not in without
    assert CATALOG_MARKER == "tlc-catalog"


def test_catalog_script_exports_the_documented_surface() -> None:
    for name in ("mount:", "update:", "setSelected:", "getSelected:", "setQuery:", "destroy:", "money:"):
        assert name in CATALOG_TABLE_JS, name
    assert "if (window.TlcCatalog) { return; }" in CATALOG_TABLE_JS  # idempotent across fragments


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_catalog_script_parses(tmp_path: Path) -> None:
    js = tmp_path / "tcl.js"
    js.write_text(CATALOG_TABLE_JS)
    subprocess.run(["node", "--check", str(js)], check=True, capture_output=True)
