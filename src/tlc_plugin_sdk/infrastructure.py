# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Infrastructure-plugin contract — the typed provider surface for remote nodes.

An infrastructure plugin (``kind = "infrastructure"`` in its manifest) owns exactly
the provider API: capabilities, create node, node state, delete node, and an
optional preflight.  The host's ``InfraManager`` calls these through the worker
proxy; everything else (node registry, lifecycle state machine, heartbeats, idle
teardown) lives in the host.  A created node is reached by its agent URL alone: the
host talks to the node agent, and the agent proxies host traffic to the node's
loopback-only workers.

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
class ProjectStorage:
    """Where the deployment keeps its projects, as far as a node can reach them.

    ``project_root_url`` is the deployment's project root when a node can write it (a bucket or
    container URL), else ``""``; ``project_scan_urls`` are its node-reachable scan folders. Both are
    a snapshot taken when the node is created, and scan folders change over time: treat them as a
    hint for what a node's storage credential should *at least* cover, never as a boundary to
    refuse or restrict access by. A provider keeps no root of its own, and a job may carry another.
    """

    project_root_url: str = ""
    project_scan_urls: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> ProjectStorage:
        """Parse the ``project_storage`` object of a create body (anything else is empty)."""
        if not isinstance(data, dict):
            return cls()
        scans = data.get("project_scan_urls")
        return cls(
            project_root_url=str(data.get("project_root_url", "") or "").strip(),
            project_scan_urls=[str(u) for u in scans if str(u).strip()] if isinstance(scans, list) else [],
        )


@dataclass
class CreateNodeRequest:
    """What the host sends when it asks a provider to create a node.

    ``compute_spec`` is the pip requirement for the node agent's own distribution, pinned to
    the host's version (``3lc-compute==1.2``): a provider that installs the agent as part of
    creating the node installs exactly this. ``wheelhouse`` is where the host says wheels for
    unpublished builds can be found — a directory on the controller, or a URL to a flat
    index — for the agent install and for every plugin venv the node builds. A provider that
    can ship a directory to the node does so; one that cannot honours a URL and ignores a
    directory. Either is ``""`` when the host has none. ``project_storage`` is the deployment's
    node-reachable project storage (:class:`ProjectStorage`).

    ``agent_port`` is where the node agent listens; ``ports`` are the browser-facing app ports
    (a notebook server, for example) the provider exposes besides ``agent_port``. Workers on the
    node are loopback-only: the host reaches them through the agent, so no worker port is ever
    exposed or listed here.
    """

    node_id: str
    node_type: str
    token: str
    env: dict[str, str] = field(default_factory=dict)
    agent_port: int = 8800
    ports: list[int] = field(default_factory=list)
    idle_ttl_s: float = 1800.0
    flavor: str = "gpu"
    owner: str = ""
    pricing: str = ""
    compute_spec: str = ""
    wheelhouse: str = ""
    project_storage: ProjectStorage = field(default_factory=ProjectStorage)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CreateNodeRequest:
        """Parse from the JSON body the host sends."""
        return cls(
            node_id=str(data.get("node_id", "") or "").strip(),
            node_type=str(data.get("node_type") or data.get("gpu_type") or ""),
            token=str(data.get("token", "") or ""),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            agent_port=int(data.get("agent_port", 8800) or 8800),
            ports=list(data.get("ports") or []),
            idle_ttl_s=float(data.get("idle_ttl_s", 1800.0) or 1800.0),
            flavor=str(data.get("flavor", "gpu") or "gpu"),
            owner=str(data.get("owner", "") or ""),
            pricing=str(data.get("pricing", "") or ""),
            compute_spec=str(data.get("compute_spec", "") or "").strip(),
            wheelhouse=str(data.get("wheelhouse", "") or "").strip(),
            project_storage=ProjectStorage.from_dict(data.get("project_storage")),
        )


@dataclass
class CreateNodeResponse:
    """What a provider returns after successfully creating a node.

    ``agent_url`` is the one address the host needs: it talks to the node agent there and reaches
    the node's workers through the agent's proxy.
    """

    provider_id: str
    agent_url: str
    token: str = ""
    pricing: str = ""
    detail: str = ""
    #: The provider's hourly quote for this node in USD, when it knows one (an on-demand list
    #: price, or the spot bid it placed). The host saves it on the node record and the Hub shows
    #: it as the node's cost; ``None`` means "no quote" and the Hub says so.
    hourly_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON body the host expects."""
        d: dict[str, Any] = {
            "provider_id": self.provider_id,
            "agent_url": self.agent_url,
        }
        if self.token:
            d["token"] = self.token
        if self.pricing:
            d["pricing"] = self.pricing
        if self.detail:
            d["detail"] = self.detail
        if self.hourly_rate is not None:
            d["hourly_rate"] = float(self.hourly_rate)
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

    ``node_types`` lists the node kinds this provider can create — instance types
    for a cloud provider, machine names for a static-machine provider.  The host's
    create-node dialog offers these as choices.

    ``extra`` carries provider-specific data (machine details, pricing tiers, region
    info) that the host passes through to the plugin's own UI fragment.
    """

    provider: str
    node_types: list[str] = field(default_factory=list)
    flavors: list[str] = field(default_factory=lambda: ["gpu"])
    ready: bool = False
    missing: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON body the host expects."""
        d: dict[str, Any] = {
            "provider": self.provider,
            "node_types": self.node_types,
            "flavors": self.flavors,
            "ready": self.ready,
            "missing": self.missing,
        }
        if self.extra:
            d.update(self.extra)
        return d


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

        Called via ``GET /infra/capabilities``.  The ``node_types`` list populates
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

    def preflight(self, node_type: str = "", datacenter: str = "") -> PreflightResponse:
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
    def _preflight(node_type: str = "", datacenter: str = "") -> dict[str, Any]:
        return plugin.preflight(node_type=node_type, datacenter=datacenter).to_dict()

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
