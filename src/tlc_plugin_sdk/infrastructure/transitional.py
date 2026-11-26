# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The transitional deployment surface: a deployment created as a ``flavor == "workspace"`` node.

Everything a provider and a host need to launch a deployment through ``POST /infra/nodes`` lives
here or in a block marked ``transitional`` in the module that must hold it:

- This module: :class:`WorkspaceRequest`, :class:`WorkspaceInstance`, :class:`WorkspaceListing`,
  :class:`WorkspacePlacement`, :class:`WorkspaceFacet` and :data:`TRANSITIONAL_OPERATIONS`.
- ``types.py``: ``CreateNodeRequest.flavor`` and ``.workspace``; ``CreateNodeResponse.services``,
  ``.ingress`` and ``.placement``.
- ``routes.py``: ``workspace_handlers`` and the workspace branch of ``POST /infra/nodes``.
- ``plugin.py``: the transitional entries of ``provided_operations``.
- ``connections.py``: ``ConnectionBinding.configuration``.
- ``testing.py``: the workspace branch of ``FakeProvider`` and the ``workspaces`` check group.

The typed deployment group (:class:`~tlc_plugin_sdk.infrastructure.DeploymentProvider`) is what a
provider implements for a deployment; this surface carries the path a host still drives.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from tlc_plugin_sdk.infrastructure._wire import (
    _any_dict,
    _bool,
    _dict_list,
    _mapping,
    _str,
    _str_dict,
    _str_list,
    _stripped,
)
from tlc_plugin_sdk.infrastructure.errors import NotSupported

if TYPE_CHECKING:
    from tlc_plugin_sdk.infrastructure.types import NodeStateResponse

__all__ = [
    "OPERATION_WORKSPACE_CREATE",
    "OPERATION_WORKSPACE_NAME_CHECK",
    "OPERATION_WORKSPACE_SWEEP",
    "TRANSITIONAL_OPERATIONS",
    "WorkspaceFacet",
    "WorkspaceInstance",
    "WorkspaceListing",
    "WorkspacePlacement",
    "WorkspaceRequest",
]

#: The provider creates a deployment from ``POST /infra/nodes`` with ``flavor == "workspace"``.
OPERATION_WORKSPACE_CREATE = "workspace.create"
#: The provider serves its own ``GET /infra/deployments/name-check``.
OPERATION_WORKSPACE_NAME_CHECK = "workspace.name_check"
#: The provider serves its own ``POST /infra/deployments/sweep``.
OPERATION_WORKSPACE_SWEEP = "workspace.sweep"
#: The operation ids of this surface, in the order ``capabilities.operations`` lists them.
TRANSITIONAL_OPERATIONS: tuple[str, ...] = (
    OPERATION_WORKSPACE_CREATE,
    OPERATION_WORKSPACE_NAME_CHECK,
    OPERATION_WORKSPACE_SWEEP,
)


@dataclass
class WorkspaceRequest:
    """The ``workspace`` object of a create body for a ``flavor == "workspace"`` node.

    ``workspace_id`` is the Config Service record the machine becomes, ``config_service_url`` where
    it redeems, and ``bootstrap_token`` the single-use token the machine redeems at
    ``POST {config_service_url}/v1/workspaces/{workspace_id}/bootstrap`` for its two service keys.
    The token is the one secret of the launch: a provider puts it in the machine's startup data
    and keeps nothing (it is scrubbed from the SDK's error answers); a host sends it on the one
    create call and never persists it. ``region`` and ``vpc_id`` say where the stack goes; empty
    leaves the provider's default. ``dns_zone_id`` and ``dns_zone_name`` name the host's deployment
    DNS zone: a provider that supports hostnames gives the workspace public names under that zone,
    derived from ``name``, and removes them when the workspace goes; both empty leaves the
    provider's own naming.
    """

    project_root_url: str = ""
    mode: str = "full"
    public: bool = False
    name: str = ""
    bootstrap_plugins: list[str] = field(default_factory=list)
    data_buckets: list[str] = field(default_factory=list)
    create_bucket: bool = False
    seed_provider_configs: bool = False
    workspace_id: str = ""
    config_service_url: str = ""
    bootstrap_token: str = field(default="", repr=False)
    region: str = ""
    vpc_id: str = ""
    dns_zone_id: str = ""
    dns_zone_name: str = ""
    subnet_id: str = ""
    availability_zone: str = ""
    launcher_service_id: str = ""
    detailed_diagnostics: bool = False
    project_scan_urls: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> WorkspaceRequest:
        """Parse the ``workspace`` object (anything else is empty; transient keys are dropped)."""
        d = _mapping(data)
        return cls(
            project_root_url=_stripped(d.get("project_root_url")),
            mode=_str(d.get("mode")) or "full",
            public=_bool(d.get("public")),
            name=_str(d.get("name")),
            bootstrap_plugins=_str_list(d.get("bootstrap_plugins")),
            data_buckets=_str_list(d.get("data_buckets")),
            project_scan_urls=_str_list(d.get("project_scan_urls")),
            create_bucket=_bool(d.get("create_bucket")),
            seed_provider_configs=_bool(d.get("seed_provider_configs")),
            workspace_id=_stripped(d.get("workspace_id")),
            config_service_url=_stripped(d.get("config_service_url")),
            bootstrap_token=_stripped(d.get("bootstrap_token")),
            region=_stripped(d.get("region")),
            vpc_id=_stripped(d.get("vpc_id")),
            subnet_id=_stripped(d.get("subnet_id")),
            availability_zone=_stripped(d.get("availability_zone")),
            launcher_service_id=_stripped(d.get("launcher_service_id")),
            detailed_diagnostics=_bool(d.get("detailed_diagnostics")),
            dns_zone_id=_stripped(d.get("dns_zone_id")),
            dns_zone_name=_stripped(d.get("dns_zone_name")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the object the host sends."""
        return {
            "project_root_url": self.project_root_url,
            "mode": self.mode,
            "public": self.public,
            "name": self.name,
            "bootstrap_plugins": list(self.bootstrap_plugins),
            "data_buckets": list(self.data_buckets),
            "project_scan_urls": list(self.project_scan_urls),
            "create_bucket": self.create_bucket,
            "seed_provider_configs": self.seed_provider_configs,
            "workspace_id": self.workspace_id,
            "config_service_url": self.config_service_url,
            "bootstrap_token": self.bootstrap_token,
            "region": self.region,
            "vpc_id": self.vpc_id,
            "subnet_id": self.subnet_id,
            "availability_zone": self.availability_zone,
            "launcher_service_id": self.launcher_service_id,
            "detailed_diagnostics": self.detailed_diagnostics,
            "dns_zone_id": self.dns_zone_id,
            "dns_zone_name": self.dns_zone_name,
        }


@dataclass
class WorkspaceInstance:
    """One workspace instance a provider reports (tracked by the host or not).

    ``extra`` is emit-only: ``to_dict`` merges it first and a typed key wins; ``from_dict`` never fills it.
    """

    provider_id: str
    name: str = ""
    owner: str = ""
    instance_type: str = ""
    state: str = ""
    public_ip: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> WorkspaceInstance:
        """Parse one listed instance."""
        d = _mapping(data)
        return cls(
            provider_id=_str(d.get("provider_id")),
            name=_str(d.get("name")),
            owner=_str(d.get("owner")),
            instance_type=_str(d.get("instance_type")),
            state=_str(d.get("state")),
            public_ip=_str(d.get("public_ip")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize one instance."""
        d: dict[str, Any] = dict(self.extra)
        d.update({
            "provider_id": self.provider_id,
            "name": self.name,
            "owner": self.owner,
            "instance_type": self.instance_type,
            "state": self.state,
            "public_ip": self.public_ip,
        })
        return d


@dataclass
class WorkspaceListing:
    """The answer of ``GET /infra/workspaces``."""

    workspaces: list[WorkspaceInstance] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkspaceListing:
        """Parse the answer."""
        items = data.get("workspaces")
        return cls(
            workspaces=[WorkspaceInstance.from_dict(i) for i in items] if isinstance(items, (list, tuple)) else []
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the answer."""
        return {"workspaces": [w.to_dict() for w in self.workspaces]}


@dataclass
class WorkspacePlacement:
    """Provider-neutral placement choices under the current Connection.

    Choice rows contain ``id`` and ``name``; subnets additionally identify ``zone_id`` and
    ``network_id``. Defaults use WorkspaceRequest keys. Cost rows under ``shared`` and
    ``deployment`` contain label, hourly, currency, description and reused when known.
    An empty answer means no selectable placement, never permission to use another account.
    """

    regions: list[dict[str, Any]] = field(default_factory=list)
    networks: list[dict[str, Any]] = field(default_factory=list)
    zones: list[dict[str, Any]] = field(default_factory=list)
    subnets: list[dict[str, Any]] = field(default_factory=list)
    defaults: dict[str, str] = field(default_factory=dict)
    costs: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkspacePlacement:
        """Read discovery without retaining unknown fields."""
        return cls(
            regions=_dict_list(data.get("regions")),
            networks=_dict_list(data.get("networks")),
            zones=_dict_list(data.get("zones")),
            subnets=_dict_list(data.get("subnets")),
            defaults=_str_dict(data.get("defaults")),
            costs=_any_dict(data.get("costs")),
            warnings=_str_list(data.get("warnings")),
            blockers=_str_list(data.get("blockers")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize discovery, including empty lists and absent quotes."""
        return {
            "regions": [dict(row) for row in self.regions],
            "networks": [dict(row) for row in self.networks],
            "zones": [dict(row) for row in self.zones],
            "subnets": [dict(row) for row in self.subnets],
            "defaults": dict(self.defaults),
            "costs": dict(self.costs),
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
        }


class WorkspaceFacet(ABC):
    """The provider creates ``flavor == "workspace"`` nodes and can list the ones it runs.

    A workspace is created through ``NodeProvider.create_node`` (the request's ``flavor`` and
    ``workspace`` say so) and answered with ``services`` in
    :class:`~tlc_plugin_sdk.infrastructure.CreateNodeResponse`. The SDK lists
    :data:`OPERATION_WORKSPACE_CREATE` for a class with this facet, and each id in
    :attr:`private_operations` the class names.

    Attributes:
        private_operations: The ids of the routes the plugin mounts itself, from
            :data:`OPERATION_WORKSPACE_NAME_CHECK` and :data:`OPERATION_WORKSPACE_SWEEP`.
    """

    private_operations: ClassVar[tuple[str, ...]] = ()

    def workspace_retain_failed(self, provider_id: str) -> NodeStateResponse:
        """Remove public routing from a failed deployment while preserving its machine for bounded retention."""
        msg = "This provider does not support isolating retained failed deployments."
        raise NotSupported(msg)

    def workspace_diagnostics(self, provider_id: str) -> NodeStateResponse:
        """Collect optional detailed evidence under an explicit diagnostics grant, never a describe grant."""
        msg = "This provider does not support detailed deployment diagnostics."
        raise NotSupported(msg)

    def workspace_placement(self, *, region: str = "", vpc_id: str = "") -> WorkspacePlacement:
        """Discover placement and shared costs in the current Connection (unsupported by default)."""
        msg = "This provider does not support deployment placement discovery."
        raise NotSupported(msg)

    @abstractmethod
    def list_workspaces(self, *, owner: str = "") -> WorkspaceListing:
        """``GET /infra/workspaces?owner=``: every workspace instance the account runs.

        Args:
            owner: Scope to one owner (a tag), or ``""`` for all.

        Returns:
            The instances, tracked by the host or not.
        """
