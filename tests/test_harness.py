# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The harness runs a plugin's own app in-process, from a manifest or an instance."""

from __future__ import annotations

import dataclasses
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from litestar import get, post

from tlc_plugin_sdk.contract import HubPlugin
from tlc_plugin_sdk.harness import PluginHarness, main, read_manifest
from tlc_plugin_sdk.shared import config_store
from tlc_plugin_sdk.shared.config_store import PluginConfigStore


@dataclasses.dataclass
class _Settings:
    id: str = "default"
    created: str = ""
    last_run: str | None = None
    region: str = "unset"


@get("/infra/capabilities", sync_to_thread=False)
def _capabilities() -> dict[str, Any]:
    settings = PluginConfigStore(_Settings, "probe").get_config("default")
    return {"region": settings.region if settings else "none"}


@post("/echo", sync_to_thread=False)
def _echo(data: dict[str, Any]) -> dict[str, Any]:
    return {"got": data}


class _Probe(HubPlugin):
    def __init__(self) -> None:
        self.initialised = 0

    def get_ui_fragment(self) -> str:
        return "<div>probe</div>"

    def get_route_handlers(self) -> list[Any]:
        return [_capabilities, _echo]

    def initialise_runtime(self) -> None:
        self.initialised += 1


def test_calls_the_plugins_own_routes_and_the_generic_ones(tmp_path: Path) -> None:
    plugin = _Probe()
    with PluginHarness(plugin, plugin_id="probe", config_root=tmp_path) as h:
        assert h.post("/echo", json_body={"a": 1}).json() == {"got": {"a": 1}}
        health = h.get("/health").json()
        assert health["plugin"] == "probe"
        assert h.get("/ui").status_code == 200
        assert h.get("/jobs/x/cancel").status_code in (404, 405), "job routes are not mounted"
    assert plugin.initialised == 1


def test_settings_come_from_the_redirected_root_and_the_real_one_is_restored(tmp_path: Path) -> None:
    original = config_store.CONFIG_ROOT
    root = tmp_path / "configs"
    (root / "probe").mkdir(parents=True)
    (root / "probe" / "default.json").write_text(json.dumps({"id": "default", "created": "", "region": "eu-north-1"}))
    with PluginHarness(_Probe(), plugin_id="probe", config_root=root) as h:
        assert h.get("/infra/capabilities").json() == {"region": "eu-north-1"}
        assert root == config_store.CONFIG_ROOT
    assert original == config_store.CONFIG_ROOT


def test_calling_before_entering_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="Enter the harness"):
        PluginHarness(_Probe(), plugin_id="probe").get("/health")


def _write_package(tmp_path: Path, manifest: str, filename: str = "plugin.toml") -> Path:
    package = tmp_path / "src" / "harness_probe_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        textwrap.dedent(
            """
            from tlc_plugin_sdk.contract import HubPlugin

            class Plugin(HubPlugin):
                def get_ui_fragment(self) -> str:
                    return "pkg"
            """
        )
    )
    (package / filename).write_text(textwrap.dedent(manifest))
    return package


def test_reads_a_standalone_plugin_toml(tmp_path: Path) -> None:
    package = _write_package(
        tmp_path,
        """
        id = "probe-pkg"
        kind = "infrastructure"
        [runtime]
        entrypoint = "harness_probe_pkg:Plugin"
        """,
    )
    manifest = read_manifest(package)
    assert (manifest.id, manifest.entrypoint, manifest.kind) == (
        "probe-pkg",
        "harness_probe_pkg:Plugin",
        "infrastructure",
    )


def test_reads_a_pyproject_table_and_defaults_kind_to_compute(tmp_path: Path) -> None:
    package = _write_package(
        tmp_path,
        """
        [project]
        name = "probe"
        [tool.tlc-compute]
        id = "probe-pkg"
        [tool.tlc-compute.runtime]
        entrypoint = "harness_probe_pkg:Plugin"
        """,
        filename="pyproject.toml",
    )
    manifest = read_manifest(package)
    assert (manifest.id, manifest.kind) == ("probe-pkg", "compute")


def test_a_manifest_without_an_entrypoint_is_refused(tmp_path: Path) -> None:
    package = _write_package(tmp_path, 'id = "probe-pkg"\n')
    with pytest.raises(ValueError, match="entrypoint"):
        read_manifest(package)


def test_no_manifest_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_manifest(tmp_path)


def test_from_manifest_imports_an_uninstalled_source_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.path", list(__import__("sys").path))
    package = _write_package(
        tmp_path,
        """
        id = "probe-pkg"
        [runtime]
        entrypoint = "harness_probe_pkg:Plugin"
        """,
    )
    with PluginHarness.from_manifest(package, config_root=tmp_path / "cfg") as h:
        assert h.get("/health").json()["plugin"] == "probe-pkg"

    assert main([str(package), "GET", "/health", "--config-root", str(tmp_path / "cfg")]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["plugin"] == "probe-pkg"
    assert "200 GET /health" in out.err.splitlines()
