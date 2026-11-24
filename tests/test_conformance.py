# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The conformance kit: green on the reference provider, and naming what a wrong provider breaks."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk.connections import ConnectionBinding, SecretToken
from tlc_plugin_sdk.infrastructure import (
    CapabilitiesResponse,
    Catalog,
    CatalogFacet,
    CatalogRequest,
    CreateNodeRequest,
    CreateNodeResponse,
    DeploymentProvider,
    DeploymentRequest,
    DeploymentResponse,
    DnsChange,
    DnsZone,
    InfrastructurePlugin,
    NodeCapabilities,
    NodeProvider,
    NodeStateResponse,
    StorageCapabilities,
    StorageFacet,
    StorageListing,
)
from tlc_plugin_sdk.infrastructure.testing import (
    GROUPS,
    FakeProvider,
    assert_conformant,
    check_provider,
    connection_headers,
    main,
)
from tlc_plugin_sdk.shared import config_store

_ZONE = DnsZone(id="Z1", name="deploy.example.com")


def test_the_reference_provider_is_conformant_with_everything_on(tmp_path: Path) -> None:
    plugin = FakeProvider()
    report = check_provider(
        plugin,
        plugin_id="fake",
        config_root=tmp_path,
        create_nodes=True,
        create_deployments=True,
        dns_zone=_ZONE,
        live_storage=True,
    )
    assert report.ok, report.text()
    names = " ".join(c.name for c in report.checks)
    for group in GROUPS:
        assert f"{group}: " in names, f"the {group} group ran"
    assert plugin.zones["Z1"] == {}, "the DNS checks remove what they wrote"
    assert all(d["state"] == "terminated" for d in plugin.deployments.values())


@pytest.mark.parametrize(
    "headers",
    [
        connection_headers(ConnectionBinding("c1", "fake", "KEYLESS", access={"role_name": "r"})),
        connection_headers(
            ConnectionBinding("c1", "fake", "SECRET"), credential=SecretToken("fake", "fake-key-1", "c1")
        ),
    ],
)
def test_the_kit_runs_under_the_connection_it_is_given(tmp_path: Path, headers: dict[str, str]) -> None:
    assert_conformant(FakeProvider(), plugin_id="fake", config_root=tmp_path, create_nodes=True, headers=headers)


def test_the_default_run_creates_nothing(tmp_path: Path) -> None:
    plugin = FakeProvider()
    report = check_provider(plugin, plugin_id="fake", config_root=tmp_path)
    assert report.ok, report.text()
    assert not plugin.nodes and not plugin.deployments and not plugin.zones
    names = [c.name for c in report.checks]
    assert not any(n.startswith("lifecycle:") for n in names)
    assert not any("POST /infra/deployments answers" in n for n in names)
    assert report.operations == plugin.operations()


def test_without_a_config_root_a_temporary_one_is_used_and_the_real_root_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real-root"
    monkeypatch.setattr(config_store, "CONFIG_ROOT", real)
    assert_conformant(FakeProvider(), plugin_id="fake")
    assert not real.exists()
    assert real == config_store.CONFIG_ROOT


def test_skip_leaves_a_group_out(tmp_path: Path) -> None:
    report = check_provider(FakeProvider(), plugin_id="fake", config_root=tmp_path, skip=("storage", "routes", "dns"))
    assert report.ok, report.text()
    assert not any(c.name.startswith(("storage:", "routes:", "dns:")) for c in report.checks)


class _Nodes(InfrastructurePlugin, NodeProvider):
    """A node provider with no Connection: what a machines-like plugin is."""

    def get_ui_fragment(self) -> str:
        return ""

    def capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse(
            provider="nodes",
            display_name="Nodes",
            ready=True,
            nodes=NodeCapabilities(types=["box"], storage_access=["ambient"]),
        )

    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
        return CreateNodeResponse(
            provider_id=f"p-{request.node_id}", agent_url="http://box:8800", storage_access="ambient"
        )

    def node_state(self, provider_id: str) -> NodeStateResponse:
        return NodeStateResponse(state="gone")

    def delete_node(self, provider_id: str) -> NodeStateResponse:
        return NodeStateResponse(state="gone")


def test_the_smallest_node_provider_is_conformant_and_is_sent_no_connection(tmp_path: Path) -> None:
    report = check_provider(_Nodes(), plugin_id="nodes", config_root=tmp_path, create_nodes=True)
    assert report.ok, report.text()
    assert report.operations == ["node.create", "node.state", "node.delete"]


class _Wrong(InfrastructurePlugin, NodeProvider, DeploymentProvider, StorageFacet, CatalogFacet):
    """A provider that breaks the contract in several places at once."""

    def get_ui_fragment(self) -> str:
        return ""

    def capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse(
            provider="other", display_name="Wrong", ready=True, nodes=NodeCapabilities(types=["t"])
        )

    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
        return CreateNodeResponse(provider_id=f"p-{id(request)}", storage_access="identity")

    def node_state(self, provider_id: str) -> NodeStateResponse:
        msg = "exploded with hunter2-secret"
        raise RuntimeError(msg)

    def delete_node(self, provider_id: str) -> NodeStateResponse:
        msg = "already gone"
        raise RuntimeError(msg)

    def secret_values(self) -> list[str]:
        return []

    def storage_capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(kind="bucket", label="B")

    def list_storage(self, *, fallback_url: str = "") -> StorageListing:
        return StorageListing(capabilities=self.storage_capabilities())

    def catalog(self, request: CatalogRequest) -> Catalog:
        msg = "no prices today"
        raise RuntimeError(msg)

    def create_deployment(self, request: DeploymentRequest) -> DeploymentResponse:
        return DeploymentResponse(provider_id="d", addresses=[], endpoints={"object_service_url": "http://plain"})

    def deployment_state(self, provider_id: str, *, diagnostics: str = "") -> NodeStateResponse:
        return NodeStateResponse(state="running")

    def delete_deployment(self, provider_id: str) -> NodeStateResponse:
        return NodeStateResponse(state="terminated")


def test_a_wrong_provider_is_named_check_by_check(tmp_path: Path) -> None:
    report = check_provider(
        _Wrong(), plugin_id="wrong", config_root=tmp_path, create_nodes=True, create_deployments=True
    )
    failed = {c.name for c in report.failures()}
    assert "shape: GET /infra/capabilities answers 200" in failed, "deployments undeclared: the SDK refuses to answer"

    class Declared(_Wrong):
        def capabilities(self) -> CapabilitiesResponse:
            from tlc_plugin_sdk.infrastructure import AccountSchema, DeploymentCapabilities

            caps = super().capabilities()
            caps.account = AccountSchema(noun="A")
            caps.deployments = DeploymentCapabilities(certificate="machine")
            return caps

    report = check_provider(
        Declared(), plugin_id="wrong", config_root=tmp_path, create_nodes=True, create_deployments=True
    )
    failed = {c.name for c in report.failures()}
    for expected in (
        "shape: provider equals the plugin id",
        "lifecycle: create answers agent_url",
        "lifecycle: create answers a storage_access the capabilities declare",
        "lifecycle: a repeated create with the same node_id returns the same node (idempotent)",
        "lifecycle: DELETE /infra/nodes/{id} answers 200 twice (idempotent)",
        "storage: every True flag in storage_capabilities() has its method overridden",
        "catalog: POST /infra/catalog (gpu_node) answers a Catalog",
        "deployments: create answers addresses or a failure",
        "deployments: endpoints are the two https service urls",
        "deployments: a deployment named by address needs deployments.sslip",
    ):
        assert expected in failed, f"{expected!r} not among {sorted(failed)}"
    with pytest.raises(AssertionError, match="FAIL shape: provider equals the plugin id"):
        assert_conformant(Declared(), plugin_id="wrong", config_root=tmp_path)


def test_a_dns_facet_that_applies_half_a_change_is_caught(tmp_path: Path) -> None:
    class HalfAtomic(FakeProvider):
        def change_records(self, change: DnsChange) -> None:
            for record in change.create:
                super().change_records(DnsChange(zone=change.zone, create=[record]))
            super().change_records(DnsChange(zone=change.zone, delete=change.delete))

    report = check_provider(HalfAtomic(), plugin_id="fake", config_root=tmp_path, dns_zone=_ZONE)
    assert [c.name for c in report.failures()] == ["dns: a refused change applied nothing"]


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
        "--create-deployments",
        "--dns-zone",
        "Z1=deploy.example.com",
        "--live-storage",
        "--node-type",
        "fake-gpu",
        "--skip",
        "routes",
    ])
    out = capsys.readouterr().out
    assert code == 0, out
    assert out.startswith("operations: connection.grant, connection.grant_update")
    assert "ok   lifecycle: POST /infra/nodes answers 201" in out
    assert "ok   dns: a refused change applied nothing" in out
    assert "routes:" not in out
