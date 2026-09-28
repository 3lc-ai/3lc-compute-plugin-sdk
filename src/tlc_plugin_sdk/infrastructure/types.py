# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The typed wire between the host's InfraManager and an infrastructure plugin.

Every message the host sends a provider, and every answer it reads, is a dataclass here. The
rules every one of them follows:

- ``from_dict(data)`` reads only known keys; unknown keys are dropped; ``None`` or a missing key
  is the field default; scalars are coerced (``str()``/``int()``/``float()``/``bool()``) and lists
  filtered to their item type, so an older or newer peer never raises here.
- ``to_dict()`` emits every field the wire reads. A key whose presence carries meaning
  (``token``, ``pricing``, ``detail``, ``hourly_rate``, ``services``, ``managed_by``, the
  ``bootstrap_*`` trio, ``region``, ``storage`` …) is omitted when unset, so a reader's
  ``"hourly_rate" in answer`` keeps its meaning.
- The aliases the wire carries are kept on both sides: ``node_type``/``gpu_type`` on a create
  body and a preflight query, ``node_types``/``gpu_types`` on capabilities. ``from_dict`` prefers
  the ``node_*`` name; ``to_dict`` emits both.
- An ``extra`` field is emit-only: ``to_dict`` merges it first and a typed key that is set wins; ``from_dict``
  never fills it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "FACETS",
    "FACET_CATALOG",
    "FACET_LEGACY_OWNER_CREDENTIALS",
    "FACET_STORAGE",
    "FACET_WORKSPACES",
    "BundleRequest",
    "CapabilitiesResponse",
    "CpuCatalog",
    "CreateNodeRequest",
    "CreateNodeResponse",
    "CreateStorageRequest",
    "Datacenters",
    "DeleteObjectsRequest",
    "GpuCatalog",
    "LoginDescriptor",
    "NodeProviderState",
    "NodeStateResponse",
    "ObjectListing",
    "OwnerCredentialsDescriptor",
    "PreflightCheck",
    "PreflightResponse",
    "PresignRequest",
    "PresignResponse",
    "ProjectStorage",
    "Region",
    "RoleDescriptor",
    "SettingsField",
    "StorageCapabilities",
    "StorageDeleted",
    "StorageItem",
    "StorageListing",
    "TransferRequest",
    "WorkspaceInstance",
    "WorkspaceListing",
    "WorkspaceRequest",
    "is_node_state",
    "preflight_query",
]

# ── Facet ids (the strings on the wire in ``capabilities.facets``) ──────────────

FACET_STORAGE = "storage"
FACET_CATALOG = "catalog"
FACET_WORKSPACES = "workspaces"
FACET_LEGACY_OWNER_CREDENTIALS = "legacy-owner-credentials"
#: Every facet id, in the order ``implemented_facets()`` reports them.
FACETS: tuple[str, ...] = (FACET_STORAGE, FACET_CATALOG, FACET_WORKSPACES, FACET_LEGACY_OWNER_CREDENTIALS)


# ── Coercion helpers (a peer never makes these raise) ──────────────────────────


def _str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _stripped(value: Any) -> str:
    return _str(value).strip()


def _int(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _opt_bool(value: Any) -> bool | None:
    return None if value is None else _bool(value)


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v) for v in value if v is not None and str(v).strip()]


def _int_list(value: Any) -> list[int]:
    out: list[int] = []
    if isinstance(value, (list, tuple)):
        for v in value:
            parsed = _opt_int(v)
            if parsed is not None:
                out.append(parsed)
    return out


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(v) for v in value if isinstance(v, Mapping)]


def _str_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): str(v) for k, v in value.items() if v is not None}


def _any_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _opt_dict(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# ── Core ───────────────────────────────────────────────────────────────────────


@dataclass
class SettingsField:
    """One field a person is asked for: an entry of ``missing_fields``, ``workspace_fields`` or
    ``workspace_credentials``.

    Attributes:
        key: The settings key.
        label: What the prompt says.
        required: Whether a node cannot be created without it.
        secret: Whether the value is a secret (masked input, never echoed).
        help: Where the value is found, in a sentence.
        href: A link to where the value is found.
        placeholder: An example value.
    """

    key: str
    label: str
    required: bool = True
    secret: bool = False
    help: str = ""
    href: str = ""
    placeholder: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> SettingsField:
        """Parse one field description (anything else is an empty field)."""
        d = _mapping(data)
        return cls(
            key=_str(d.get("key")),
            label=_str(d.get("label")),
            required=_bool(d.get("required"), True),
            secret=_bool(d.get("secret")),
            help=_str(d.get("help")),
            href=_str(d.get("href")),
            placeholder=_str(d.get("placeholder")),
        )

    def to_dict(self) -> dict[str, Any]:
        """All seven keys, always."""
        return {
            "key": self.key,
            "label": self.label,
            "required": self.required,
            "secret": self.secret,
            "help": self.help,
            "href": self.href,
            "placeholder": self.placeholder,
        }


def _fields_from(value: Any) -> list[SettingsField]:
    return [SettingsField.from_dict(v) for v in value] if isinstance(value, (list, tuple)) else []


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
        d = _mapping(data)
        return cls(
            project_root_url=_stripped(d.get("project_root_url")),
            project_scan_urls=_str_list(d.get("project_scan_urls")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the object the host sends."""
        return {"project_root_url": self.project_root_url, "project_scan_urls": list(self.project_scan_urls)}


@dataclass
class WorkspaceRequest:
    """The ``workspace`` object of a create body for a ``flavor == "workspace"`` node.

    The transient ``credentials`` and ``provider_configs`` a demo request carries are not fields:
    the SDK strips them from the body and exposes them through
    :mod:`tlc_plugin_sdk.infrastructure.legacy` to a plugin with the legacy facet.
    """

    project_root_url: str = ""
    mode: str = "full"
    public: bool = False
    name: str = ""
    bootstrap_plugins: list[str] = field(default_factory=list)
    data_buckets: list[str] = field(default_factory=list)
    create_bucket: bool = False
    seed_provider_configs: bool = False

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
            create_bucket=_bool(d.get("create_bucket")),
            seed_provider_configs=_bool(d.get("seed_provider_configs")),
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
            "create_bucket": self.create_bucket,
            "seed_provider_configs": self.seed_provider_configs,
        }


def _idle_ttl(value: Any) -> float:
    """An idle TTL in seconds: ``0`` and negatives kept (never auto-off).

    Only a finite number is read: a missing or unreadable value, ``nan`` and ``inf`` (``"inf"``
    included) are the 1800 s default — never mapped to "never".
    """
    parsed = _opt_float(value)
    return parsed if parsed is not None and math.isfinite(parsed) else 1800.0


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

    ``idle_ttl_s`` is how long the node may sit idle before its agent turns it off; ``0`` (or
    less) means never. Only a finite number is read: a missing or unreadable value, and ``inf``,
    read as the 1800 s default.

    ``storage_id`` names the provider storage to attach (a network volume; ``""`` for the
    provider's default or none). ``workspace`` is filled for ``flavor == "workspace"`` and empty
    for a GPU node. ``pricing`` is ``""`` when the host leaves the choice to the provider's
    configured default.
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
    storage_id: str = ""
    workspace: WorkspaceRequest = field(default_factory=WorkspaceRequest)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CreateNodeRequest:
        """Parse from the JSON body the host sends."""
        return cls(
            node_id=_stripped(data.get("node_id")),
            node_type=_str(data.get("node_type") or data.get("gpu_type")),
            token=_str(data.get("token")),
            env=_str_dict(data.get("env")),
            agent_port=_int(data.get("agent_port"), 8800) or 8800,
            ports=_int_list(data.get("ports")),
            idle_ttl_s=_idle_ttl(data.get("idle_ttl_s")),
            flavor=_str(data.get("flavor")) or "gpu",
            owner=_str(data.get("owner")),
            pricing=_str(data.get("pricing")),
            compute_spec=_stripped(data.get("compute_spec")),
            wheelhouse=_stripped(data.get("wheelhouse")),
            project_storage=ProjectStorage.from_dict(data.get("project_storage")),
            storage_id=_str(data.get("storage_id")),
            workspace=WorkspaceRequest.from_dict(data.get("workspace")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the body the host sends: ``node_type`` and its ``gpu_type`` alias; ``pricing`` only when set."""
        d: dict[str, Any] = {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "gpu_type": self.node_type,
            "token": self.token,
            "env": dict(self.env),
            "agent_port": self.agent_port,
            "ports": list(self.ports),
            "idle_ttl_s": self.idle_ttl_s,
            "flavor": self.flavor,
            "workspace": self.workspace.to_dict() if self.flavor == "workspace" else {},
            "owner": self.owner,
            "storage_id": self.storage_id,
            "project_storage": self.project_storage.to_dict(),
            "compute_spec": self.compute_spec,
            "wheelhouse": self.wheelhouse,
        }
        if self.pricing:
            d["pricing"] = self.pricing
        return d


@dataclass
class CreateNodeResponse:
    """What a provider returns after successfully creating a node.

    ``agent_url`` is the one address the host needs for a GPU node: it talks to the node agent
    there and reaches the node's workers through the agent's proxy. A workspace has no agent:
    it answers ``services`` (``object_service_url``, ``compute_service_url``) instead.
    ``managed_by`` is ``"owner"`` when the node lives in the requester's own account (only the
    keys that created it can terminate it); ``""`` reads as ``"creator"`` to the host.
    """

    provider_id: str
    agent_url: str = ""
    token: str = ""
    pricing: str = ""
    detail: str = ""
    #: The provider's hourly quote for this node in USD, when it knows one (an on-demand list
    #: price, or the spot bid it placed). The host saves it on the node record and the Hub shows
    #: it as the node's cost; ``None`` means "no quote" and the Hub says so.
    hourly_rate: float | None = None
    services: dict[str, str] = field(default_factory=dict)
    managed_by: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CreateNodeResponse:
        """Parse a provider's answer (for the host)."""
        return cls(
            provider_id=_str(data.get("provider_id")),
            agent_url=_str(data.get("agent_url")),
            token=_str(data.get("token")),
            pricing=_str(data.get("pricing")),
            detail=_str(data.get("detail")),
            hourly_rate=_opt_float(data.get("hourly_rate")),
            services={k: v for k, v in _mapping(data.get("services")).items() if isinstance(v, str)},
            managed_by=_str(data.get("managed_by")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON body the host expects; optional keys only when set."""
        d: dict[str, Any] = {"provider_id": self.provider_id}
        if self.agent_url:
            d["agent_url"] = self.agent_url
        if self.token:
            d["token"] = self.token
        if self.pricing:
            d["pricing"] = self.pricing
        if self.detail:
            d["detail"] = self.detail
        if self.hourly_rate is not None:
            d["hourly_rate"] = float(self.hourly_rate)
        if self.services:
            d["services"] = dict(self.services)
        if self.managed_by:
            d["managed_by"] = self.managed_by
        return d


#: The states a provider can report for a node it manages. ``pending`` is a node the provider is
#: still starting; ``exited`` one that stopped on its own (the host reads it during boot).
NodeProviderState = Literal["pending", "running", "exited", "terminated", "gone", "unknown"]
_NODE_STATES: frozenset[str] = frozenset(("pending", "running", "exited", "terminated", "gone", "unknown"))


def _node_state(value: Any) -> NodeProviderState:
    text = _str(value).strip().lower()
    if text == "pending":
        return "pending"
    if text == "running":
        return "running"
    if text == "exited":
        return "exited"
    if text == "terminated":
        return "terminated"
    if text == "gone":
        return "gone"
    return "unknown"


@dataclass
class NodeStateResponse:
    """What a provider returns for a node-state query.

    The ``bootstrap_*`` trio is diagnostics a provider adds on ``GET /infra/nodes/{id}?diagnostics=true``
    (:meth:`~tlc_plugin_sdk.infrastructure.InfrastructurePlugin.node_diagnostics`): the startup
    milestones seen so far, the current one in a sentence, and whether startup failed —
    ``None`` when the provider cannot tell (a console it could not read).
    """

    state: NodeProviderState
    detail: str = ""
    bootstrap_history: list[str] = field(default_factory=list)
    bootstrap_detail: str = ""
    bootstrap_failed: bool | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NodeStateResponse:
        """Parse a provider's answer; an unknown state reads as ``unknown``."""
        return cls(
            state=_node_state(data.get("state")),
            detail=_str(data.get("detail")),
            bootstrap_history=_str_list(data.get("bootstrap_history")),
            bootstrap_detail=_str(data.get("bootstrap_detail")),
            bootstrap_failed=_opt_bool(data.get("bootstrap_failed")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON body the host expects; diagnostics keys only when set."""
        d: dict[str, Any] = {"state": self.state}
        if self.detail:
            d["detail"] = self.detail
        if self.bootstrap_history:
            d["bootstrap_history"] = list(self.bootstrap_history)
        if self.bootstrap_detail:
            d["bootstrap_detail"] = self.bootstrap_detail
        if self.bootstrap_failed is not None:
            d["bootstrap_failed"] = self.bootstrap_failed
        return d


@dataclass
class PreflightCheck:
    """One check in a preflight response."""

    name: str
    ok: bool
    level: str = "info"
    detail: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> PreflightCheck:
        """Parse one check."""
        d = _mapping(data)
        return cls(
            name=_str(d.get("name")),
            ok=_bool(d.get("ok")),
            level=_str(d.get("level")) or "info",
            detail=_str(d.get("detail")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON."""
        return {"name": self.name, "ok": self.ok, "level": self.level, "detail": self.detail}


@dataclass
class PreflightResponse:
    """The result of a preflight check on a provider."""

    ok: bool
    checks: list[PreflightCheck] = field(default_factory=list)
    summary: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PreflightResponse:
        """Parse a provider's answer."""
        checks = data.get("checks")
        return cls(
            ok=_bool(data.get("ok")),
            checks=[PreflightCheck.from_dict(c) for c in checks] if isinstance(checks, (list, tuple)) else [],
            summary=_str(data.get("summary")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON body the host passes through."""
        return {"ok": self.ok, "checks": [c.to_dict() for c in self.checks], "summary": self.summary}


def preflight_query(node_type: str = "", datacenter: str = "") -> dict[str, str]:
    """The query parameters of ``GET /infra/preflight`` (for the host): ``node_type`` with its alias.

    Args:
        node_type: The node type to check, or ``""`` for the provider's default.
        datacenter: A datacenter to check stock in, or ``""``.

    Returns:
        The parameters to send; empty ones are left out.
    """
    query: dict[str, str] = {}
    if node_type:
        query["node_type"] = node_type
        query["gpu_type"] = node_type
    if datacenter:
        query["datacenter"] = datacenter
    return query


@dataclass
class StorageCapabilities:
    """What the storage a provider offers can do: the ``storage`` object of capabilities, and the header of a listing.

    Attributes:
        kind: ``"bucket"`` or ``"volume"``.
        label: What the Hub calls one (``"S3 bucket"``, ``"RunPod network volume"``).
        creatable: ``POST /infra/storage`` works.
        upload: ``POST /infra/storage/presign`` (mode ``upload``) works.
        browse: ``GET /infra/storage/list`` works.
        download: ``POST /infra/storage/presign`` (mode ``download``) works.
        bundle: ``POST /infra/storage/bundle`` works.
        delete: ``POST /infra/storage/delete`` works.
        transfer: ``POST /infra/storage/transfer`` works.
        rename: A transfer of one item with ``rename_to`` works.
        default_id: The storage a node gets when the request names none (``""`` for none).
        upload_hint: Why the browser cannot fill this storage right now, in a sentence.
    """

    kind: str
    label: str
    creatable: bool = True
    upload: bool = True
    browse: bool = True
    download: bool = True
    bundle: bool = True
    delete: bool = True
    transfer: bool = True
    rename: bool = True
    default_id: str = ""
    upload_hint: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> StorageCapabilities:
        """Parse the ``storage`` object of capabilities or the header of a listing."""
        d = _mapping(data)
        return cls(
            kind=_str(d.get("kind")),
            label=_str(d.get("label")),
            creatable=_bool(d.get("creatable"), True),
            upload=_bool(d.get("upload"), True),
            browse=_bool(d.get("browse"), True),
            download=_bool(d.get("download"), True),
            bundle=_bool(d.get("bundle"), True),
            delete=_bool(d.get("delete"), True),
            transfer=_bool(d.get("transfer"), True),
            rename=_bool(d.get("rename"), True),
            default_id=_str(d.get("default_id")),
            upload_hint=_str(d.get("upload_hint")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Every flag; ``default_id`` and ``upload_hint`` only when non-empty."""
        d: dict[str, Any] = {
            "kind": self.kind,
            "label": self.label,
            "creatable": self.creatable,
            "upload": self.upload,
            "browse": self.browse,
            "download": self.download,
            "bundle": self.bundle,
            "delete": self.delete,
            "transfer": self.transfer,
            "rename": self.rename,
        }
        if self.default_id:
            d["default_id"] = self.default_id
        if self.upload_hint:
            d["upload_hint"] = self.upload_hint
        return d


@dataclass
class CapabilitiesResponse:
    """What a provider returns for a capabilities query.

    ``node_types`` lists the node kinds this provider can create — instance types for a cloud
    provider, machine names for a static-machine provider; the host's create-node dialog offers
    these. ``flavors`` is what it makes (``["gpu"]``, or ``["gpu", "workspace"]``); ``pricing``
    how a node may be paid for (``["on_demand", "spot"]``; the host assumes ``["on_demand"]``
    when absent). ``missing_fields`` describe what to ask a person for before a node can be
    created; ``workspace_fields`` what a workspace's own copy of this provider needs.

    ``storage`` and ``facets`` are filled by the SDK from the facets the plugin implements — an
    author-set value is overwritten. ``lists_workspaces`` reads ``True`` when the plugin has the
    workspaces facet. ``extra`` carries provider-specific data for the plugin's own fragment and
    is passed through unchanged.
    """

    provider: str
    node_types: list[str] = field(default_factory=list)
    flavors: list[str] = field(default_factory=lambda: ["gpu"])
    ready: bool = False
    missing: list[str] = field(default_factory=list)
    missing_fields: list[SettingsField] = field(default_factory=list)
    pricing: list[str] = field(default_factory=list)
    node_type_label: str = ""
    region: str = ""
    workspace_fields: list[SettingsField] = field(default_factory=list)
    storage: StorageCapabilities | None = None
    facets: list[str] = field(default_factory=list)
    lists_workspaces: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    #: Whether the answer carried a ``facets`` key at all (``from_dict`` sets it): a host tells
    #: "no facets" from "a provider that predates facets" by this.
    facets_reported: bool = field(default=True, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CapabilitiesResponse:
        """Parse a provider's answer (for the host); ``node_types`` or its ``gpu_types`` alias."""
        node_types = data.get("node_types")
        if node_types is None:
            node_types = data.get("gpu_types")
        storage = data.get("storage")
        return cls(
            provider=_str(data.get("provider")),
            node_types=_str_list(node_types),
            flavors=_str_list(data.get("flavors")) or ["gpu"],
            ready=_bool(data.get("ready")),
            missing=_str_list(data.get("missing")),
            missing_fields=_fields_from(data.get("missing_fields")),
            pricing=_str_list(data.get("pricing")),
            node_type_label=_str(data.get("node_type_label")),
            region=_str(data.get("region")),
            workspace_fields=_fields_from(data.get("workspace_fields")),
            storage=StorageCapabilities.from_dict(storage) if isinstance(storage, Mapping) else None,
            facets=[f for f in _str_list(data.get("facets")) if f in FACETS],
            lists_workspaces=_bool(data.get("lists_workspaces")),
            facets_reported="facets" in data,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON body the host expects.

        ``extra`` first, typed keys over it; ``facets`` always (``[]`` for none); ``pricing``,
        ``node_type_label``, ``region``, ``workspace_fields``, ``storage`` only when set;
        ``lists_workspaces`` only when true.
        """
        d: dict[str, Any] = dict(self.extra)
        d.update({
            "provider": self.provider,
            "node_types": list(self.node_types),
            "gpu_types": list(self.node_types),
            "flavors": list(self.flavors),
            "ready": self.ready,
            "missing": list(self.missing),
            "missing_fields": [f.to_dict() for f in self.missing_fields],
            "facets": list(self.facets),
        })
        if self.pricing:
            d["pricing"] = list(self.pricing)
        if self.node_type_label:
            d["node_type_label"] = self.node_type_label
        if self.region:
            d["region"] = self.region
        if self.workspace_fields:
            d["workspace_fields"] = [f.to_dict() for f in self.workspace_fields]
        if self.storage is not None:
            d["storage"] = self.storage.to_dict()
        if self.lists_workspaces or FACET_WORKSPACES in self.facets:
            d["lists_workspaces"] = True
        return d


# ── Storage facet ──────────────────────────────────────────────────────────────


@dataclass
class StorageItem:
    """One bucket, container or volume as the Data page lists it.

    ``url`` is what the Data page browses and uploads to (``s3://…``, ``abfs://…``,
    ``volume://<id>``); ``default`` is whether a node gets this storage when the request names
    none (``None`` when the provider has no such notion). ``extra`` is emit-only passthrough.
    """

    id: str
    name: str = ""
    url: str = ""
    kind: str = ""
    region: str = ""
    size_gb: int | None = None
    default: bool | None = None
    managed_by: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> StorageItem:
        """Parse one listed item."""
        d = _mapping(data)
        return cls(
            id=_str(d.get("id")),
            name=_str(d.get("name")),
            url=_str(d.get("url")),
            kind=_str(d.get("kind")),
            region=_str(d.get("region")),
            size_gb=_opt_int(d.get("size_gb")),
            default=_opt_bool(d.get("default")),
            managed_by=_str(d.get("managed_by")),
        )

    def to_dict(self) -> dict[str, Any]:
        """``id``, ``name``, ``url``, ``region``, ``size_gb`` always; ``kind``, ``default``, ``managed_by`` when set."""
        d: dict[str, Any] = dict(self.extra)
        d.update({"id": self.id, "name": self.name, "url": self.url, "region": self.region, "size_gb": self.size_gb})
        if self.kind:
            d["kind"] = self.kind
        if self.default is not None:
            d["default"] = self.default
        if self.managed_by:
            d["managed_by"] = self.managed_by
        return d


@dataclass
class Region:
    """A place storage can be created in. ``extra`` is emit-only passthrough (a ``country``, for example)."""

    id: str
    name: str = ""
    location: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> Region:
        """Parse one region."""
        d = _mapping(data)
        return cls(id=_str(d.get("id")), name=_str(d.get("name")), location=_str(d.get("location")))

    def to_dict(self) -> dict[str, Any]:
        """``extra`` first; ``id`` and ``name`` always; ``location`` when set."""
        d: dict[str, Any] = dict(self.extra)
        d.update({"id": self.id, "name": self.name})
        if self.location:
            d["location"] = self.location
        return d


@dataclass
class StorageListing:
    """The answer of ``GET /infra/storage`` (and of a legacy discover): the header flags plus the items.

    ``to_dict`` flattens: the :class:`StorageCapabilities` keys at the top level, then
    ``storage`` and ``regions``, then ``account`` and ``region`` when set — the shape the host
    splits into per-provider meta and items. ``extra`` is emit-only passthrough, merged first (an
    ``account`` answered even when empty, the ``subscription`` a discover listed).
    """

    capabilities: StorageCapabilities
    storage: list[StorageItem] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    account: str = ""
    region: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StorageListing:
        """Parse a provider's flattened answer."""
        items = data.get("storage")
        regions = data.get("regions")
        return cls(
            capabilities=StorageCapabilities.from_dict(data),
            storage=[StorageItem.from_dict(i) for i in items] if isinstance(items, (list, tuple)) else [],
            regions=[Region.from_dict(r) for r in regions] if isinstance(regions, (list, tuple)) else [],
            account=_str(data.get("account")),
            region=_str(data.get("region")),
        )

    def to_dict(self) -> dict[str, Any]:
        """The flattened answer; ``extra`` first, typed keys over it."""
        d: dict[str, Any] = dict(self.extra)
        d.update(self.capabilities.to_dict())
        d["storage"] = [i.to_dict() for i in self.storage]
        d["regions"] = [r.to_dict() for r in self.regions]
        if self.account:
            d["account"] = self.account
        if self.region:
            d["region"] = self.region
        return d


@dataclass
class CreateStorageRequest:
    """The body of ``POST /infra/storage``: ``{name, region?, size_gb?, make_default?, owner?}``.

    ``owner`` is the caller the host acts for (an identity, not a credential); a transient
    ``credentials`` object goes to the legacy context, not here.
    """

    name: str
    region: str = ""
    size_gb: int | None = None
    make_default: bool = False
    owner: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CreateStorageRequest:
        """Parse the body (``credentials`` goes to the legacy context, not here)."""
        return cls(
            name=_stripped(data.get("name")),
            region=_stripped(data.get("region")),
            size_gb=_opt_int(data.get("size_gb")),
            make_default=_bool(data.get("make_default")),
            owner=_str(data.get("owner")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the body."""
        d: dict[str, Any] = {
            "name": self.name,
            "region": self.region,
            "make_default": self.make_default,
            "owner": self.owner,
        }
        if self.size_gb is not None:
            d["size_gb"] = self.size_gb
        return d


@dataclass
class StorageDeleted:
    """The answer of ``DELETE /infra/storage/{id}``."""

    deleted: bool
    id: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StorageDeleted:
        """Parse the answer."""
        return cls(deleted=_bool(data.get("deleted")), id=_str(data.get("id")))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the answer."""
        return {"deleted": self.deleted, "id": self.id}


@dataclass
class PresignRequest:
    """The body of ``POST /infra/storage/presign``: ``{url, files: [{path, size?, content_type?}], mode?}``."""

    url: str
    files: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "upload"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PresignRequest:
        """Parse the body."""
        return cls(
            url=_str(data.get("url")), files=_dict_list(data.get("files")), mode=_str(data.get("mode")) or "upload"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the body."""
        return {"url": self.url, "files": [dict(f) for f in self.files], "mode": self.mode}


@dataclass
class PresignResponse:
    """Presigned URLs for the browser.

    The item dicts are browser-facing and provider-shaped; only the envelope is typed. ``extra`` is
    emit-only passthrough for the keys a provider's answer adds (``bucket``, ``account``, ``volume``,
    ``cors``); a ``None`` value in it is emitted as ``null``.
    """

    expires_s: int = 0
    uploads: list[dict[str, Any]] = field(default_factory=list)
    downloads: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    region: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PresignResponse:
        """Parse the answer."""
        return cls(
            expires_s=_int(data.get("expires_s"), 0),
            uploads=_dict_list(data.get("uploads")),
            downloads=_dict_list(data.get("downloads")),
            refused=_dict_list(data.get("refused")),
            region=_str(data.get("region")),
        )

    def to_dict(self) -> dict[str, Any]:
        """``extra`` first; ``expires_s``, ``uploads``, ``downloads`` and ``refused`` always; ``region`` when set."""
        d: dict[str, Any] = dict(self.extra)
        d.update({
            "expires_s": self.expires_s,
            "uploads": [dict(u) for u in self.uploads],
            "downloads": [dict(u) for u in self.downloads],
            "refused": [dict(r) for r in self.refused],
        })
        if self.region:
            d["region"] = self.region
        return d


@dataclass
class ObjectListing:
    """One level of a storage prefix: the answer of ``GET /infra/storage/list``.

    ``prefixes`` are ``{name, url}``; ``objects`` are ``{key, size, name, url, …}``. ``next_token``
    continues a ``truncated`` listing (emitted only when there is one).
    """

    url: str
    prefixes: list[dict[str, Any]] = field(default_factory=list)
    objects: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    next_token: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ObjectListing:
        """Parse the answer."""
        return cls(
            url=_str(data.get("url")),
            prefixes=_dict_list(data.get("prefixes")),
            objects=_dict_list(data.get("objects")),
            truncated=_bool(data.get("truncated")),
            next_token=_str(data.get("next_token")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the answer."""
        d: dict[str, Any] = {
            "url": self.url,
            "prefixes": [dict(p) for p in self.prefixes],
            "objects": [dict(o) for o in self.objects],
            "truncated": self.truncated,
        }
        if self.next_token:
            d["next_token"] = self.next_token
        return d


@dataclass
class DeleteObjectsRequest:
    """The body of ``POST /infra/storage/delete``: paths relative to ``url``; a trailing ``/`` names a folder."""

    url: str
    paths: list[str] = field(default_factory=list)
    dry_run: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeleteObjectsRequest:
        """Parse the body."""
        return cls(url=_str(data.get("url")), paths=_str_list(data.get("paths")), dry_run=_bool(data.get("dry_run")))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the body."""
        return {"url": self.url, "paths": list(self.paths), "dry_run": self.dry_run}


@dataclass
class TransferRequest:
    """The body of ``POST /infra/storage/transfer``: copy or move ``items`` from ``src_url`` under ``dst_url``."""

    src_url: str
    dst_url: str
    items: list[str] = field(default_factory=list)
    mode: str = "copy"
    rename_to: str = ""
    dry_run: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TransferRequest:
        """Parse the body."""
        return cls(
            src_url=_str(data.get("src_url")),
            dst_url=_str(data.get("dst_url")),
            items=_str_list(data.get("items")),
            mode=_str(data.get("mode")) or "copy",
            rename_to=_str(data.get("rename_to")),
            dry_run=_bool(data.get("dry_run")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the body."""
        return {
            "src_url": self.src_url,
            "dst_url": self.dst_url,
            "items": list(self.items),
            "mode": self.mode,
            "rename_to": self.rename_to,
            "dry_run": self.dry_run,
        }


@dataclass
class BundleRequest:
    """The body of ``POST /infra/storage/bundle``: the folder to zip and the archive's name."""

    url: str
    name: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BundleRequest:
        """Parse the body."""
        return cls(url=_str(data.get("url")), name=_stripped(data.get("name")))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the body."""
        return {"url": self.url, "name": self.name}


# ── Catalog facet ──────────────────────────────────────────────────────────────


@dataclass
class GpuCatalog:
    """The answer of ``GET /infra/gpu-catalog``: rows for the catalogue table; ``error`` says why a list is short.

    ``placement`` is where the provider looked for stock: ``{}`` means anywhere and is emitted;
    ``None`` (the default) means the provider did not say, and the key is left out.
    """

    gpus: list[dict[str, Any]] = field(default_factory=list)
    placement: dict[str, Any] | None = None
    error: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GpuCatalog:
        """Parse the answer."""
        return cls(
            gpus=_dict_list(data.get("gpus")), placement=_opt_dict(data.get("placement")), error=_str(data.get("error"))
        )

    def to_dict(self) -> dict[str, Any]:
        """``gpus`` always; ``placement`` when set (``{}`` included); ``error`` when non-empty."""
        d: dict[str, Any] = {"gpus": [dict(g) for g in self.gpus]}
        if self.placement is not None:
            d["placement"] = dict(self.placement)
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class CpuCatalog:
    """The answer of ``GET /infra/cpu-catalog``: workspace sizes with prices, for one region."""

    sizes: list[dict[str, Any]] = field(default_factory=list)
    region: str = ""
    error: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CpuCatalog:
        """Parse the answer."""
        return cls(sizes=_dict_list(data.get("sizes")), region=_str(data.get("region")), error=_str(data.get("error")))

    def to_dict(self) -> dict[str, Any]:
        """``sizes`` always; ``region`` and ``error`` when non-empty."""
        d: dict[str, Any] = {"sizes": [dict(s) for s in self.sizes]}
        if self.region:
            d["region"] = self.region
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class Datacenters:
    """The answer of ``GET /infra/datacenters``: sites with stock of ``node_type``, and the placement in force.

    ``placement`` is the setting as saved (``"auto"``, ``"dc:US-NC-1"``); ``placement_effective`` what
    it resolves to: ``{}`` means anywhere and is emitted; ``None`` (the default) leaves the key out.
    """

    datacenters: list[dict[str, Any]] = field(default_factory=list)
    node_type: str = ""
    placement: str = ""
    placement_effective: dict[str, Any] | None = None
    error: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Datacenters:
        """Parse the answer; ``node_type`` or its ``gpu_type`` alias."""
        return cls(
            datacenters=_dict_list(data.get("datacenters")),
            node_type=_str(data.get("node_type") or data.get("gpu_type")),
            placement=_str(data.get("placement")),
            placement_effective=_opt_dict(data.get("placement_effective")),
            error=_str(data.get("error")),
        )

    def to_dict(self) -> dict[str, Any]:
        """``datacenters`` always; ``placement_effective`` when set (``{}`` included); the rest when non-empty.

        ``node_type`` is emitted with its ``gpu_type`` alias.
        """
        d: dict[str, Any] = {"datacenters": [dict(x) for x in self.datacenters]}
        if self.node_type:
            d["node_type"] = self.node_type
            d["gpu_type"] = self.node_type
        if self.placement:
            d["placement"] = self.placement
        if self.placement_effective is not None:
            d["placement_effective"] = dict(self.placement_effective)
        if self.error:
            d["error"] = self.error
        return d


# ── Workspace facet ────────────────────────────────────────────────────────────


@dataclass
class WorkspaceInstance:
    """One workspace instance a provider reports (tracked by the host or not). ``extra`` is emit-only passthrough."""

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


# ── Legacy owner-credentials facet (LEGACY) ────────────────────────────────────


@dataclass
class LoginDescriptor:
    """A provider sign-in a person can use instead of pasting keys (``capabilities.workspace_login``)."""

    kind: str
    label: str
    help: str = ""
    fields: list[dict[str, Any]] = field(default_factory=list)
    credential_keys: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> LoginDescriptor:
        """Parse the descriptor."""
        d = _mapping(data)
        return cls(
            kind=_str(d.get("kind")),
            label=_str(d.get("label")),
            help=_str(d.get("help")),
            fields=_dict_list(d.get("fields")),
            credential_keys=_str_list(d.get("credential_keys")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the descriptor."""
        return {
            "kind": self.kind,
            "label": self.label,
            "help": self.help,
            "fields": [dict(f) for f in self.fields],
            "credential_keys": list(self.credential_keys),
        }


@dataclass
class RoleDescriptor:
    """A cross-account role a person can grant (``capabilities.workspace_role``)."""

    kind: str
    label: str
    help: str = ""
    setup: str = ""
    field: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> RoleDescriptor:
        """Parse the descriptor."""
        d = _mapping(data)
        return cls(
            kind=_str(d.get("kind")),
            label=_str(d.get("label")),
            help=_str(d.get("help")),
            setup=_str(d.get("setup")),
            field=_any_dict(d.get("field")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the descriptor."""
        return {
            "kind": self.kind,
            "label": self.label,
            "help": self.help,
            "setup": self.setup,
            "field": dict(self.field),
        }


@dataclass
class OwnerCredentialsDescriptor:
    """What the legacy facet adds to capabilities: the keys a request's ``credentials`` must carry, and how a
    person supplies them.

    Emitted into the capabilities answer as ``credential_keys``, ``workspace_credentials``,
    ``workspace_login`` and ``workspace_role``, each only when set.
    """

    credential_keys: list[str] = field(default_factory=list)
    workspace_credentials: list[SettingsField] = field(default_factory=list)
    login: LoginDescriptor | None = None
    role: RoleDescriptor | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OwnerCredentialsDescriptor:
        """Parse the descriptor keys out of a capabilities answer."""
        login = data.get("workspace_login")
        role = data.get("workspace_role")
        return cls(
            credential_keys=_str_list(data.get("credential_keys")),
            workspace_credentials=_fields_from(data.get("workspace_credentials")),
            login=LoginDescriptor.from_dict(login) if isinstance(login, Mapping) else None,
            role=RoleDescriptor.from_dict(role) if isinstance(role, Mapping) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """The capabilities keys, each only when set."""
        d: dict[str, Any] = {}
        if self.credential_keys:
            d["credential_keys"] = list(self.credential_keys)
        if self.workspace_credentials:
            d["workspace_credentials"] = [f.to_dict() for f in self.workspace_credentials]
        if self.login is not None:
            d["workspace_login"] = self.login.to_dict()
        if self.role is not None:
            d["workspace_role"] = self.role.to_dict()
        return d


def is_node_state(value: Any) -> bool:
    """Whether ``value`` is one of :data:`NodeProviderState` (for tests and the conformance kit)."""
    return isinstance(value, str) and value in _NODE_STATES
