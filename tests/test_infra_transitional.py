# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The transitional deployment surface: a deployment created as a ``flavor == "workspace"`` node."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk.harness import PluginHarness
from tlc_plugin_sdk.infrastructure import (
    CapabilitiesResponse,
    CreateNodeRequest,
    CreateNodeResponse,
    InfrastructurePlugin,
    NodeCapabilities,
    NodeProvider,
    NodeStateResponse,
    WorkspaceFacet,
    WorkspaceListing,
    WorkspacePlacement,
    WorkspaceRequest,
    provided_operations,
)
from tlc_plugin_sdk.infrastructure.testing import FakeProvider


@pytest.fixture
def fake(tmp_path: Path) -> Any:
    with PluginHarness(FakeProvider(), plugin_id="fake", config_root=tmp_path) as h:
        yield h


def _workspace(**more: Any) -> dict[str, Any]:
    return CreateNodeRequest(
        node_id="w1",
        node_type="fake-cpu",
        token="t",
        owner="alice",
        flavor="workspace",
        workspace=WorkspaceRequest(name="ws", workspace_id="rec-1", bootstrap_token="boot-secret-1", **more),
    ).to_dict()


def test_a_workspace_create_answers_services_and_is_listed(fake: PluginHarness) -> None:
    r = fake.post("/infra/nodes", json_body=_workspace())
    assert r.status_code == 201
    created = CreateNodeResponse.from_dict(r.json())
    assert set(created.services) == {"object_service_url", "compute_service_url"} and not created.agent_url
    listing = WorkspaceListing.from_dict(fake.get("/infra/workspaces?owner=alice").json())
    assert [w.provider_id for w in listing.workspaces] == [created.provider_id]
    assert fake.get("/infra/workspaces?owner=bob").json() == {"workspaces": []}


def test_the_workspace_object_travels_only_with_the_workspace_flavor() -> None:
    wire = _workspace(region="eu-west-1", vpc_id="vpc-1", dns_zone_id="Z1", dns_zone_name="deploy.example.com")
    assert wire["flavor"] == "workspace" and wire["workspace"]["region"] == "eu-west-1"
    request = CreateNodeRequest.from_dict(wire)
    assert request.flavor == "workspace" and request.workspace.dns_zone_name == "deploy.example.com"
    assert request.workspace.bootstrap_token == "boot-secret-1" and "boot-secret-1" not in repr(request)
    node = CreateNodeRequest(node_id="n", node_type="t", token="t").to_dict()
    assert "flavor" not in node and "workspace" not in node


def test_a_deployments_placement_and_ingress_survive_the_typed_boundary() -> None:
    answer = {
        "provider_id": "i-1",
        "services": {"object_service_url": "https://os.x", "compute_service_url": "https://c.x"},
        "placement": {"region": "eu-west-1", "vpc_id": "vpc-1"},
        "ingress": {"namespace": "i-abc.deploy.example.com", "records": [{"name": "os-x"}]},
    }
    created = CreateNodeResponse.from_dict(answer)
    assert created.placement == answer["placement"] and created.ingress == answer["ingress"]
    assert created.to_dict() == {**answer, "storage_access": "none"}


def test_the_transitional_operations_are_listed_for_a_workspace_provider(fake: PluginHarness) -> None:
    assert fake.get("/infra/capabilities").json()["operations"][-1] == "workspace.create"

    class Aws(InfrastructurePlugin, NodeProvider, WorkspaceFacet):
        private_operations = ("workspace.sweep", "workspace.name_check", "made.up")

        def get_ui_fragment(self) -> str:
            return ""

        def capabilities(self) -> CapabilitiesResponse:
            return CapabilitiesResponse(provider="aws", display_name="AWS", nodes=NodeCapabilities())

        def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
            return CreateNodeResponse(provider_id="p")

        def node_state(self, provider_id: str) -> NodeStateResponse:
            return NodeStateResponse(state="running")

        def delete_node(self, provider_id: str) -> NodeStateResponse:
            return NodeStateResponse(state="terminated")

        def list_workspaces(self, *, owner: str = "") -> WorkspaceListing:
            return WorkspaceListing()

    assert provided_operations(Aws()) == [
        "node.create",
        "node.state",
        "node.delete",
        "workspace.create",
        "workspace.name_check",
        "workspace.sweep",
    ]
    with PluginHarness(Aws(), plugin_id="aws") as h:
        caps = h.get("/infra/capabilities").json()
        assert "deployments" not in caps and "workspace.create" in caps["operations"]


def test_a_provider_without_the_facet_refuses_a_workspace_and_scrubs_its_token(tmp_path: Path) -> None:
    class NodesOnly(InfrastructurePlugin, NodeProvider):
        def get_ui_fragment(self) -> str:
            return ""

        def capabilities(self) -> CapabilitiesResponse:
            return CapabilitiesResponse(provider="nodes", display_name="N", nodes=NodeCapabilities())

        def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
            msg = f"refused {request.workspace.bootstrap_token}"
            raise RuntimeError(msg)

        def node_state(self, provider_id: str) -> NodeStateResponse:
            return NodeStateResponse(state="gone")

        def delete_node(self, provider_id: str) -> NodeStateResponse:
            return NodeStateResponse(state="gone")

    with PluginHarness(NodesOnly(), plugin_id="nodes", config_root=tmp_path) as h:
        r = h.post("/infra/nodes", json_body=_workspace())
        assert r.status_code == 400 and "no deployment" in r.json()["detail"]
        assert h.get("/infra/workspaces").status_code == 404

    class Leaky(FakeProvider):
        def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
            msg = f"refused {request.workspace.bootstrap_token}"
            raise RuntimeError(msg)

    with PluginHarness(Leaky(), plugin_id="fake", config_root=tmp_path) as h:
        r = h.post("/infra/nodes", json_body=_workspace())
        assert r.status_code == 502 and r.json()["detail"] == "refused ***"


def test_detailed_diagnostics_retention_and_placement_are_opt_in(fake: PluginHarness) -> None:
    for method, path in (
        ("GET", "/infra/workspace-diagnostics/x"),
        ("POST", "/infra/workspace-retain/x"),
        ("GET", "/infra/workspace-placement"),
    ):
        r = fake.call(method, path)
        assert r.status_code == 501 and r.json()["code"] == "not_supported", path


def test_placement_discovery_filters_malformed_collections() -> None:
    placement = WorkspacePlacement.from_dict({"regions": [{"id": "r"}, "junk"], "defaults": {"region": "r", "x": None}})
    assert placement.regions == [{"id": "r"}] and placement.defaults == {"region": "r"}
    assert WorkspacePlacement.from_dict(placement.to_dict()) == placement


def test_a_fake_without_the_transitional_surface_lists_only_generation_2_deployments(tmp_path: Path) -> None:
    default = FakeProvider()
    assert isinstance(default, WorkspaceFacet) and "workspace.create" in provided_operations(default)
    typed = FakeProvider(transitional=False)
    assert isinstance(typed, FakeProvider) and not isinstance(typed, WorkspaceFacet)
    operations = provided_operations(typed)
    assert "deployment.create" in operations
    assert not [op for op in operations if op.startswith("workspace.")]
    with PluginHarness(typed, plugin_id="fake", config_root=tmp_path) as h:
        assert not [op for op in h.get("/infra/capabilities").json()["operations"] if op.startswith("workspace.")]
        assert h.get("/infra/workspaces").status_code == 404
        refused = h.post("/infra/nodes", json_body=_workspace())
        assert refused.status_code == 400
