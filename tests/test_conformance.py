# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The conformance kit: the reference provider passes, a wrong one is named, groups skip, the CLI runs."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk.infrastructure import (
    CapabilitiesResponse,
    CreateNodeRequest,
    CreateNodeResponse,
    InfrastructurePlugin,
    NodeStateResponse,
    ObjectListing,
    StorageCapabilities,
    StorageFacet,
    StorageListing,
)
from tlc_plugin_sdk.infrastructure.testing import GROUPS, FakeProvider, assert_conformant, check_provider, main


def test_the_reference_provider_is_conformant_with_everything_on(tmp_path: Path) -> None:
    assert_conformant(FakeProvider(), plugin_id="fake", config_root=tmp_path, create_nodes=True, live_storage=True)


def test_the_default_run_needs_no_cloud_and_no_lifecycle(tmp_path: Path) -> None:
    report = check_provider(FakeProvider(), plugin_id="fake", config_root=tmp_path)
    assert report.ok, report.text()
    assert report.facets == ["storage", "catalog", "workspaces"]
    groups = {c.name.split(":")[0] for c in report.checks}
    assert "lifecycle" not in groups
    assert {"shape", "preflight", "errors", "settings", "storage", "catalog", "workspaces", "routes"} <= groups
    assert not any("list_objects" in c.name for c in report.checks), "live storage is opt-in"
    assert "passed, 0 failed" in report.text()


def test_without_a_config_root_a_temporary_one_is_used_and_the_real_root_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tlc_plugin_sdk.shared import config_store

    real = tmp_path / "real-home-configs"
    monkeypatch.setattr(config_store, "CONFIG_ROOT", real)
    report = check_provider(FakeProvider(), plugin_id="fake", skip=("routes",))
    assert report.ok, report.text()
    assert any(c.name == "settings: an unreadable settings file answers 409" for c in report.checks)
    assert not real.exists(), "the default run never touches the configured settings root"
    assert real == config_store.CONFIG_ROOT, "the redirect was restored"


class _NoTypes(FakeProvider):
    def capabilities(self) -> CapabilitiesResponse:
        caps = super().capabilities()
        caps.node_types = []
        return caps


def test_preflight_is_skipped_with_a_sentence_when_there_is_nothing_to_preflight(tmp_path: Path) -> None:
    report = check_provider(_NoTypes(), plugin_id="fake", config_root=tmp_path)
    assert report.ok, report.text()
    skipped = [c for c in report.checks if c.name == "preflight: skipped"]
    assert len(skipped) == 1
    assert "no node_types" in skipped[0].detail
    assert "ok   preflight: skipped — capabilities list no node_types" in report.text()


def test_skip_leaves_a_group_out(tmp_path: Path) -> None:
    report = check_provider(FakeProvider(), plugin_id="fake", config_root=tmp_path, skip=GROUPS)
    assert report.checks == []
    report = check_provider(FakeProvider(), plugin_id="fake", config_root=tmp_path, skip=("shape", "errors"))
    assert not any(c.name.startswith(("shape:", "errors:")) for c in report.checks)
    assert any(c.name.startswith("storage:") for c in report.checks)


class _Wrong(InfrastructurePlugin, StorageFacet):
    """Flags without methods, a workspace flavor without the facet, a 500 for an unknown node."""

    def get_ui_fragment(self) -> str:
        return "<div/>"

    def capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse(provider="wrong", node_types=["t"], flavors=["gpu", "workspace"], ready=True)

    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
        return CreateNodeResponse(provider_id="p", agent_url="http://x")

    def node_state(self, provider_id: str) -> NodeStateResponse:
        if provider_id == "conformance-missing":
            from litestar.exceptions import HTTPException

            raise HTTPException(status_code=500, detail="boom")
        return NodeStateResponse(state="running")

    def delete_node(self, provider_id: str) -> NodeStateResponse:
        return NodeStateResponse(state="terminated")

    def storage_capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(kind="bucket", label="B")

    def list_storage(self, *, fallback_url: str = "") -> StorageListing:
        return StorageListing(capabilities=self.storage_capabilities())

    def list_objects(self, url: str, *, next_token: str = "") -> ObjectListing:
        return ObjectListing(url=url)


def test_a_wrong_provider_is_named_check_by_check(tmp_path: Path) -> None:
    with pytest.raises(AssertionError) as info:
        assert_conformant(_Wrong(), plugin_id="wrong", config_root=tmp_path, create_nodes=True)
    text = str(info.value)
    assert "FAIL shape: flavors lists workspace iff WorkspaceFacet" in text
    assert "FAIL storage: every True flag in storage_capabilities() has its method overridden" in text
    assert (
        "flags without a method: ['creatable', 'upload', 'download', 'delete', 'transfer', 'rename', 'bundle']" in text
    )
    assert "FAIL errors: an unknown node id answers 200 (gone/unknown) or 404, never 5xx" in text
    assert "ok   lifecycle: POST /infra/nodes answers 2xx" in text


def test_a_settings_layer_next_to_hand_rolled_settings_is_flagged(tmp_path: Path) -> None:
    from litestar import get

    @get("/settings", sync_to_thread=False)
    def _mine() -> dict[str, Any]:
        return {}

    class Both(FakeProvider):
        def get_route_handlers(self) -> list[Any]:
            return [*super().get_route_handlers(), _mine]

    report = check_provider(Both(), plugin_id="fake", config_root=tmp_path, skip=("settings",))
    failed = [c for c in report.failures() if c.name.startswith("routes:")]
    assert [c.detail for c in failed] == ["GET /settings"]


def test_the_cli_runs_a_plugin_from_its_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plugin_dir = tmp_path / "src" / "fake_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        textwrap.dedent(
            """
            id = "fake"
            kind = "infrastructure"
            [runtime]
            entrypoint = "tlc_plugin_sdk.infrastructure.testing:FakeProvider"
            """
        )
    )
    code = main([
        str(plugin_dir),
        "--config-root",
        str(tmp_path / "configs"),
        "--create-nodes",
        "--live-storage",
        "--node-type",
        "fake-gpu",
        "--skip",
        "routes",
    ])
    out = capsys.readouterr().out
    assert code == 0, out
    assert out.startswith("facets: storage, catalog, workspaces")
    assert "ok   lifecycle: POST /infra/nodes answers 2xx" in out
    assert "routes:" not in out
