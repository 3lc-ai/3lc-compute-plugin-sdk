# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the InfrastructurePlugin contract and typed dataclasses."""

from __future__ import annotations

from typing import Any

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from tlc_plugin_sdk.contract import HubPlugin
from tlc_plugin_sdk.infrastructure import (
    CapabilitiesResponse,
    CreateNodeRequest,
    CreateNodeResponse,
    InfrastructurePlugin,
    NodeStateResponse,
    PreflightCheck,
    PreflightResponse,
)

# ── Dataclass unit tests ─────────────────────────────────────────────────────


class TestCreateNodeRequest:
    def test_from_dict_full(self) -> None:
        req = CreateNodeRequest.from_dict({
            "node_id": " n1 ",
            "node_type": "A100",
            "token": "tok",
            "env": {"K": "V"},
            "agent_port": 9900,
            "ports": [8801, 8802],
            "idle_ttl_s": 600,
            "flavor": "gpu",
            "owner": "user@x",
            "pricing": "spot",
        })
        assert req.node_id == "n1"
        assert req.node_type == "A100"
        assert req.token == "tok"
        assert req.env == {"K": "V"}
        assert req.agent_port == 9900
        assert req.ports == [8801, 8802]
        assert req.idle_ttl_s == 600.0
        assert req.owner == "user@x"
        assert req.pricing == "spot"

    def test_from_dict_minimal(self) -> None:
        req = CreateNodeRequest.from_dict({"node_id": "n1", "token": "tok", "node_type": "H100"})
        assert req.node_id == "n1"
        assert req.node_type == "H100"
        assert req.env == {}
        assert req.agent_port == 8800
        assert req.ports == []
        assert req.idle_ttl_s == 1800.0
        assert req.flavor == "gpu"
        assert req.owner == ""
        assert req.pricing == ""

    def test_from_dict_gpu_type_compat(self) -> None:
        """The host may still send ``gpu_type`` — from_dict accepts both names."""
        req = CreateNodeRequest.from_dict({"node_id": "n1", "token": "tok", "gpu_type": "H100"})
        assert req.node_type == "H100"

    def test_from_dict_node_type_wins(self) -> None:
        """When both keys are present, ``node_type`` wins."""
        req = CreateNodeRequest.from_dict({
            "node_id": "n1",
            "token": "tok",
            "node_type": "new",
            "gpu_type": "old",
        })
        assert req.node_type == "new"

    def test_from_dict_none_coercion(self) -> None:
        req = CreateNodeRequest.from_dict({
            "node_id": "n1",
            "token": "tok",
            "node_type": "H100",
            "env": None,
            "agent_port": None,
            "idle_ttl_s": None,
        })
        assert req.env == {}
        assert req.agent_port == 8800
        assert req.idle_ttl_s == 1800.0


class TestCreateNodeResponse:
    def test_to_dict_minimal(self) -> None:
        resp = CreateNodeResponse(provider_id="p1", agent_url="http://x:8800", worker_url_template="http://x:{port}")
        d = resp.to_dict()
        assert d == {"provider_id": "p1", "agent_url": "http://x:8800", "worker_url_template": "http://x:{port}"}
        assert "token" not in d
        assert "pricing" not in d

    def test_to_dict_with_optionals(self) -> None:
        resp = CreateNodeResponse(
            provider_id="p1",
            agent_url="http://x:8800",
            worker_url_template="http://x:{port}",
            token="tok",
            pricing="spot",
            detail="ok",
        )
        d = resp.to_dict()
        assert d["token"] == "tok"
        assert d["pricing"] == "spot"
        assert d["detail"] == "ok"


class TestNodeStateResponse:
    def test_to_dict(self) -> None:
        resp = NodeStateResponse(state="running", detail="pid 1234")
        assert resp.to_dict() == {"state": "running", "detail": "pid 1234"}

    def test_to_dict_no_detail(self) -> None:
        resp = NodeStateResponse(state="terminated")
        assert resp.to_dict() == {"state": "terminated"}


class TestPreflightResponse:
    def test_to_dict(self) -> None:
        resp = PreflightResponse(
            ok=True,
            checks=[PreflightCheck(name="reachable", ok=True, detail="ssh ok")],
            summary="all good",
        )
        d = resp.to_dict()
        assert d["ok"] is True
        assert len(d["checks"]) == 1
        assert d["checks"][0]["name"] == "reachable"
        assert d["summary"] == "all good"


class TestCapabilitiesResponse:
    def test_to_dict(self) -> None:
        resp = CapabilitiesResponse(provider="test", node_types=["A100"], ready=True)
        d = resp.to_dict()
        assert d["provider"] == "test"
        assert d["node_types"] == ["A100"]
        assert d["flavors"] == ["gpu"]
        assert d["ready"] is True
        assert d["missing"] == []

    def test_extra_merged(self) -> None:
        resp = CapabilitiesResponse(
            provider="machines",
            node_types=["devbox"],
            ready=True,
            extra={"machines": [{"name": "devbox", "gpu": "RTX 4090"}]},
        )
        d = resp.to_dict()
        assert d["machines"] == [{"name": "devbox", "gpu": "RTX 4090"}]
        assert d["node_types"] == ["devbox"]

    def test_extra_empty_not_in_dict(self) -> None:
        resp = CapabilitiesResponse(provider="test", node_types=[], ready=False)
        d = resp.to_dict()
        assert "machines" not in d


# ── InfrastructurePlugin hierarchy tests ─────────────────────────────────────


class TestPluginHierarchy:
    def test_is_hub_plugin_subclass(self) -> None:
        assert issubclass(InfrastructurePlugin, HubPlugin)

    def test_cannot_instantiate_without_abstracts(self) -> None:
        with pytest.raises(TypeError):
            InfrastructurePlugin()  # type: ignore[abstract]


# ── Integration: default route handlers via TestClient ────────────────────────


class _StubProvider(InfrastructurePlugin):
    """Concrete implementation for testing the default route handlers."""

    def get_ui_fragment(self) -> str:
        return "<div>stub</div>"

    def capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse(
            provider="stub",
            node_types=["T4", "A100"],
            ready=True,
            extra={"region": "us-east-1"},
        )

    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
        return CreateNodeResponse(
            provider_id=f"stub-{request.node_type}",
            agent_url=f"http://stub:{request.agent_port}",
            worker_url_template="http://stub:{port}",
        )

    def node_state(self, provider_id: str) -> NodeStateResponse:
        if provider_id == "missing":
            return NodeStateResponse(state="gone", detail="no such node")
        return NodeStateResponse(state="running")

    def delete_node(self, provider_id: str) -> NodeStateResponse:
        return NodeStateResponse(state="terminated", detail=f"stopped {provider_id}")


@pytest.fixture()
def stub_client() -> TestClient[Litestar]:
    plugin = _StubProvider()
    plugin.id = "stub"
    handlers = plugin.get_route_handlers()
    app = Litestar(route_handlers=handlers)
    return TestClient(app)


class TestDefaultRouteHandlers:
    def test_capabilities(self, stub_client: TestClient[Litestar]) -> None:
        resp = stub_client.get("/infra/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "stub"
        assert data["node_types"] == ["T4", "A100"]
        assert data["ready"] is True
        assert data["region"] == "us-east-1"

    def test_preflight_default(self, stub_client: TestClient[Litestar]) -> None:
        resp = stub_client.get("/infra/preflight")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_create_node(self, stub_client: TestClient[Litestar]) -> None:
        resp = stub_client.post("/infra/nodes", json={
            "node_id": "n1",
            "node_type": "A100",
            "token": "tok",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["provider_id"] == "stub-A100"
        assert "agent_url" in data

    def test_create_node_gpu_type_compat(self, stub_client: TestClient[Litestar]) -> None:
        """The host may still send ``gpu_type`` — accepted for backward compat."""
        resp = stub_client.post("/infra/nodes", json={
            "node_id": "n1",
            "gpu_type": "A100",
            "token": "tok",
        })
        assert resp.status_code == 201
        assert resp.json()["provider_id"] == "stub-A100"

    def test_create_node_missing_fields(self, stub_client: TestClient[Litestar]) -> None:
        resp = stub_client.post("/infra/nodes", json={"node_type": "A100"})
        assert resp.status_code == 400

    def test_node_state(self, stub_client: TestClient[Litestar]) -> None:
        resp = stub_client.get("/infra/nodes/some-id")
        assert resp.status_code == 200
        assert resp.json()["state"] == "running"

    def test_node_state_gone(self, stub_client: TestClient[Litestar]) -> None:
        resp = stub_client.get("/infra/nodes/missing")
        assert resp.status_code == 200
        assert resp.json()["state"] == "gone"

    def test_delete_node(self, stub_client: TestClient[Litestar]) -> None:
        resp = stub_client.delete("/infra/nodes/some-id")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "terminated"
        assert "stopped some-id" in data["detail"]

    def test_extend_route_handlers(self) -> None:
        """A subclass can extend the default handlers with custom routes."""
        from litestar import get as http_get

        @http_get("/custom")
        def custom_route() -> dict[str, str]:
            return {"custom": "yes"}

        class ExtendedProvider(_StubProvider):
            def get_route_handlers(self) -> list[Any]:
                return [*super().get_route_handlers(), custom_route]

        plugin = ExtendedProvider()
        plugin.id = "extended"
        handlers = plugin.get_route_handlers()
        assert len(handlers) == 6  # 5 infra + 1 custom
        app = Litestar(route_handlers=handlers)
        with TestClient(app) as client:
            resp = client.get("/custom")
            assert resp.status_code == 200
            assert resp.json() == {"custom": "yes"}
