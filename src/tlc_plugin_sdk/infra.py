# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Infrastructure-plugin contract — the typed provider surface for remote nodes.

An infrastructure plugin (``kind = "infrastructure"`` in its manifest) owns exactly
the provider API: capabilities, create node, node state, delete node, and an
optional preflight.  The host's ``InfraManager`` calls these through the worker
proxy; everything else (node registry, lifecycle state machine, heartbeats, idle
teardown) lives in the host.

Subclass :class:`InfrastructurePlugin` and implement the four abstract methods.
The base class provides a default :meth:`get_route_handlers` that auto-mounts
Litestar handlers for the five ``/infra/*`` routes, delegating to the abstract
methods — so a provider plugin that uses the typed contract gets its HTTP surface
for free.  Override ``get_route_handlers`` to add plugin-specific routes (settings
CRUD, machine checks, etc.) alongside the default infra routes.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from tlc_plugin_sdk.contract import HubPlugin

# ── Typed request / response shapes ──────────────────────────────────────────
#
# These mirror the wire format between the host's InfraManager and the provider
# plugin's worker.  The host sends dicts today (the wire is JSON); these
# dataclasses let the plugin author work with typed, documented fields instead of
# raw dicts, and the default route handlers handle the dict↔dataclass conversion.


@dataclass
class CreateNodeRequest:
    """What the host sends when it asks a provider to create a node."""

    node_id: str
    gpu_type: str
    token: str
    env: dict[str, str] = field(default_factory=dict)
    agent_port: int = 8800
    ports: list[int] = field(default_factory=list)
    idle_ttl_s: float = 1800.0
    flavor: str = "gpu"
    owner: str = ""
    pricing: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CreateNodeRequest:
        """Parse from the JSON body the host sends."""
        return cls(
            node_id=str(data.get("node_id", "") or "").strip(),
            gpu_type=str(data.get("gpu_type", "") or ""),
            token=str(data.get("token", "") or ""),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            agent_port=int(data.get("agent_port", 8800) or 8800),
            ports=list(data.get("ports") or []),
            idle_ttl_s=float(data.get("idle_ttl_s", 1800.0) or 1800.0),
            flavor=str(data.get("flavor", "gpu") or "gpu"),
            owner=str(data.get("owner", "") or ""),
            pricing=str(data.get("pricing", "") or ""),
        )


@dataclass
class CreateNodeResponse:
    """What a provider returns after successfully creating a node."""

    provider_id: str
    agent_url: str
    worker_url_template: str
    token: str = ""
    pricing: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON body the host expects."""
        d: dict[str, Any] = {
            "provider_id": self.provider_id,
            "agent_url": self.agent_url,
            "worker_url_template": self.worker_url_template,
        }
        if self.token:
            d["token"] = self.token
        if self.pricing:
            d["pricing"] = self.pricing
        if self.detail:
            d["detail"] = self.detail
        return d


#: The states a provider can report for a node it manages.
NodeProviderState = Literal["running", "terminated", "gone", "unknown"]


@dataclass
class NodeStateResponse:
    """What a provider returns for a node-state query."""

    state: NodeProviderState
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        """Serialize to the JSON body the host expects."""
        d: dict[str, str] = {"state": self.state}
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class PreflightCheck:
    """One check in a preflight response."""

    name: str
    ok: bool
    level: str = "info"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON."""
        return {"name": self.name, "ok": self.ok, "level": self.level, "detail": self.detail}


@dataclass
class PreflightResponse:
    """The result of a preflight check on a provider."""

    ok: bool
    checks: list[PreflightCheck] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON body the host passes through."""
        return {"ok": self.ok, "checks": [c.to_dict() for c in self.checks], "summary": self.summary}


@dataclass
class CapabilitiesResponse:
    """What a provider returns for a capabilities query.

    The ``gpu_types`` list names what the host's create dialog offers.  Provider
    plugins may include additional fields — the host passes the dict through.
    """

    provider: str
    gpu_types: list[str] = field(default_factory=list)
    flavors: list[str] = field(default_factory=lambda: ["gpu"])
    ready: bool = False
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON body the host expects."""
        return {
            "provider": self.provider,
            "gpu_types": self.gpu_types,
            "flavors": self.flavors,
            "ready": self.ready,
            "missing": self.missing,
        }


# ── The plugin base class ────────────────────────────────────────────────────


class InfrastructurePlugin(HubPlugin):
    """Base class for infrastructure-provider plugins (``kind = "infrastructure"``).

    A provider plugin implements the four abstract methods below.  The default
    :meth:`get_route_handlers` mounts Litestar handlers for the five ``/infra/*``
    routes that delegate to these methods, so the plugin gets its HTTP surface for
    free.

    To add plugin-specific routes (settings CRUD, machine checks, …), override
    ``get_route_handlers`` and append to the default list::

        def get_route_handlers(self):
            return [*super().get_route_handlers(), my_settings_get, my_settings_post]

    """

    @abstractmethod
    def capabilities(self) -> CapabilitiesResponse:
        """Report what this provider offers.

        Called via ``GET /infra/capabilities``.  The ``gpu_types`` list populates
        the host's create-node dialog.
        """
        ...

    @abstractmethod
    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
        """Create a node (start the agent process on the provider's infrastructure).

        Called via ``POST /infra/nodes``.  The host supplies the ``node_id`` and
        ``token`` for the agent; the provider starts the agent and returns the
        ``provider_id`` the host uses for subsequent status/delete calls.

        Raises:
            Any exception is caught by the route handler and returned as an HTTP error.
        """
        ...

    @abstractmethod
    def node_state(self, provider_id: str) -> NodeStateResponse:
        """Query the state of a previously created node.

        Called via ``GET /infra/nodes/{provider_id}``.

        Returns:
            ``running`` if the agent is alive, ``terminated`` or ``gone`` if it is
            not, ``unknown`` if the provider cannot tell (e.g. unreachable machine).
        """
        ...

    @abstractmethod
    def delete_node(self, provider_id: str) -> NodeStateResponse:
        """Terminate a node and clean up provider resources.

        Called via ``DELETE /infra/nodes/{provider_id}``.  Must be idempotent — a
        repeated delete on a stopped node returns ``terminated`` or ``gone``.
        """
        ...

    def preflight(self, gpu_type: str = "", datacenter: str = "") -> PreflightResponse:
        """Optional pre-start checks (funds, stock, quota, reachability).

        Called via ``GET /infra/preflight``.  The default returns an unconditional
        pass; override to run provider-specific checks.
        """
        return PreflightResponse(ok=True, summary="no preflight checks")

    # ── Default route handlers ───────────────────────────────────────────────

    def get_route_handlers(self) -> list[Any]:
        """Litestar handlers for the five ``/infra/*`` routes, delegating to the typed methods.

        Override and extend (via ``super().get_route_handlers() + [...]``) to add
        plugin-specific routes alongside the infra contract.
        """
        return _build_infra_handlers(self)


def _build_infra_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    """Build Litestar route handlers that delegate to the plugin's typed methods.

    Imported lazily so ``import tlc_plugin_sdk`` stays cheap (the server stack
    is only needed when a worker actually serves).
    """
    from litestar import delete as http_delete
    from litestar import get as http_get
    from litestar import post as http_post
    from litestar.exceptions import HTTPException

    @http_get("/infra/capabilities", sync_to_thread=True)
    def _capabilities() -> dict[str, Any]:
        return plugin.capabilities().to_dict()

    @http_get("/infra/preflight", sync_to_thread=True)
    def _preflight(gpu_type: str = "", datacenter: str = "") -> dict[str, Any]:
        return plugin.preflight(gpu_type=gpu_type, datacenter=datacenter).to_dict()

    @http_post("/infra/nodes", sync_to_thread=True)
    def _create_node(data: dict[str, Any]) -> dict[str, Any]:
        req = CreateNodeRequest.from_dict(data)
        if not req.node_id or not req.token:
            raise HTTPException(status_code=400, detail="The create call needs both a node_id and a token")
        return plugin.create_node(req).to_dict()

    @http_get("/infra/nodes/{provider_id:str}", sync_to_thread=True)
    def _node_state(provider_id: str) -> dict[str, Any]:
        return plugin.node_state(provider_id).to_dict()

    @http_delete("/infra/nodes/{provider_id:str}", status_code=200, sync_to_thread=True)
    def _delete_node(provider_id: str) -> dict[str, Any]:
        return plugin.delete_node(provider_id).to_dict()

    return [_capabilities, _preflight, _create_node, _node_state, _delete_node]
