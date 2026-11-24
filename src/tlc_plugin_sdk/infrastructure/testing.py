# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The conformance kit: drive a provider through its routes and say what disagrees with the contract.

:func:`check_provider` opens a :class:`~tlc_plugin_sdk.harness.PluginHarness` around the plugin
and runs one group of checks per group of operations the plugin lists; :func:`assert_conformant`
raises ``AssertionError`` with the report when any check fails. In a provider repo's CI::

    def test_conformance(tmp_path):
        with patch_provider_seams():  # the repo's own fake for its cloud's client
            assert_conformant(MyPlugin(), plugin_id="mycloud", config_root=tmp_path)

By default the kit runs the checks that create nothing: capabilities, the connect group, node
checks, catalogs, deployment options and checks, errors, settings and routes. ``create_nodes=True``
adds the node lifecycle (create twice for idempotency, state, diagnostics, delete twice);
``create_deployments=True`` the deployment lifecycle (prepare, create twice, state, list, retain is
not called, delete twice, sweep); ``dns_zone=DnsZone(...)`` the DNS checks, which write and remove
records under ``_3lc-conformance.<zone>``; ``live_storage=True`` adds ``list_objects`` and a
transfer dry-run against the first listed storage. Settings are read and written under
``config_root``, a temporary directory when none is given, so a run never touches
``~/.3lc-plugin-configs``.

Every call that acts on an account carries a Connection. Pass ``headers`` (built with
:func:`connection_headers`) to choose it; without them the kit sends an ``AMBIENT`` binding for
the plugin's provider, and none at all to a provider that takes no Connection.

Also a CLI: ``python -m tlc_plugin_sdk.infrastructure.testing <plugin_dir> [--config-root DIR]
[--create-nodes] [--create-deployments] [--dns-zone ID=NAME] [--live-storage] [--node-type T]
[--skip GROUP]... [--header N=V]...``.

:class:`FakeProvider` is the SDK's reference implementation of every operation, in memory: nodes,
deployments, a DNS zone, buckets and catalogs. It is what a host is tested against in process, and
what emits the golden fixtures of the wire types.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tlc_plugin_sdk.connections import (
    AMBIENT_ONLY_PATHS,
    AMBIENT_ONLY_PREFIXES,
    BOUND_CREDENTIAL_HEADER,
    CONNECTION_HEADER,
    CONNECTION_USE_HEADER,
    Ambient,
    ConnectionBinding,
    ConnectionUse,
    CredentialUnavailable,
    ResolveRequest,
    SecretToken,
    current_connection,
    current_credential,
    current_use,
    encode_binding,
    encode_credential,
    encode_use,
    register_resolver,
)
from tlc_plugin_sdk.infrastructure.errors import ERROR_CODES, Conflict, InvalidRequest, NotFound
from tlc_plugin_sdk.infrastructure.facets import CatalogFacet, DeploymentProvider, DnsFacet, NodeProvider, StorageFacet
from tlc_plugin_sdk.infrastructure.plugin import InfrastructurePlugin, capabilities_problems, provided_operations
from tlc_plugin_sdk.infrastructure.transitional import WorkspaceFacet, WorkspaceInstance, WorkspaceListing
from tlc_plugin_sdk.infrastructure.types import (
    CONTRACT_GENERATION,
    AccessMethod,
    AccountSchema,
    Address,
    CapabilitiesResponse,
    Catalog,
    CatalogRequest,
    Choice,
    ConnectionNodeAccess,
    ConnectionPermissions,
    ConnectionVerification,
    CostNote,
    CreateNodeRequest,
    CreateNodeResponse,
    CreateStorageRequest,
    DeleteObjectsRequest,
    DeploymentBootstrap,
    DeploymentCapabilities,
    DeploymentCheck,
    DeploymentCheckRequest,
    DeploymentInstance,
    DeploymentListing,
    DeploymentOptions,
    DeploymentPrepared,
    DeploymentPrepareRequest,
    DeploymentRequest,
    DeploymentResponse,
    DnsChange,
    DnsRecord,
    DnsRecords,
    DnsZone,
    FormField,
    Grant,
    GrantFile,
    GrantObservation,
    GrantRequest,
    GrantUpdate,
    GrantUpdateRequest,
    HostIdentity,
    Location,
    Locations,
    NodeAccessRequest,
    NodeCapabilities,
    NodeCheckRequest,
    NodeStateResponse,
    ObjectListing,
    Offer,
    PermissionOption,
    PermissionSchema,
    PlacementChoice,
    PlacementField,
    PreflightCheck,
    PreflightResponse,
    PresignRequest,
    PresignResponse,
    Region,
    StorageCandidate,
    StorageCandidates,
    StorageCapabilities,
    StorageDeleted,
    StorageItem,
    StorageListing,
    StorageLocationSchema,
    StorageRule,
    SweepReport,
    SweepRequest,
    VerifyRequest,
    is_node_state,
)
from tlc_plugin_sdk.shared.settings import PluginSettings, option, secret

if TYPE_CHECKING:
    from tlc_plugin_sdk.harness import HarnessResponse, PluginHarness
    from tlc_plugin_sdk.shared.storage_bundle import BundleRegistry
    from tlc_plugin_sdk.shared.storage_transfer import TransferRegistry

__all__ = [
    "GROUPS",
    "Check",
    "ConformanceReport",
    "FakeCredential",
    "FakeProvider",
    "FakeSettings",
    "assert_conformant",
    "check_provider",
    "connection_headers",
    "main",
]

#: The check groups, each skippable by name.
GROUPS: tuple[str, ...] = (
    "shape",
    "connect",
    "lifecycle",
    "check",
    "errors",
    "settings",
    "storage",
    "catalog",
    "deployments",
    "dns",
    "workspaces",
    "routes",
)

_MADE_UP_ID = "deadbeef0000"
_ERROR_KEYS = {"detail", "code", "remedy", "retry_after_s"}
_DNS_PAGE = 300


def connection_headers(
    binding: ConnectionBinding, *, use: ConnectionUse | None = None, credential: SecretToken | None = None
) -> dict[str, str]:
    """The host-owned headers a host sends for a call that acts on ``binding``.

    Args:
        binding: The Connection's binding.
        use: What the Connection is used for, when the host says.
        credential: The value of a ``SECRET`` Connection.

    Returns:
        The headers, for :meth:`~tlc_plugin_sdk.harness.PluginHarness.call` or :func:`check_provider`.
    """
    headers = {CONNECTION_HEADER: encode_binding(binding)}
    if use is not None:
        headers[CONNECTION_USE_HEADER] = encode_use(use)
    if credential is not None:
        headers[BOUND_CREDENTIAL_HEADER] = encode_credential(credential)
    return headers


def _ambient_only(path: str) -> bool:
    bare = path.split("?", 1)[0]
    return (
        bare.rstrip("/") in AMBIENT_ONLY_PATHS
        or any(bare.startswith(prefix) for prefix in AMBIENT_ONLY_PREFIXES)
        or bare.rstrip("/") == "/settings"
    )


# ── The report ─────────────────────────────────────────────────────────────────


@dataclass
class Check:
    """One conformance check."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class ConformanceReport:
    """What :func:`check_provider` found."""

    checks: list[Check]
    operations: list[str]

    @property
    def ok(self) -> bool:
        """Whether every check passed."""
        return all(c.ok for c in self.checks)

    def failures(self) -> list[Check]:
        """The checks that failed."""
        return [c for c in self.checks if not c.ok]

    def text(self) -> str:
        """One line per check, for the CLI and assertion messages."""
        lines = [f"operations: {', '.join(self.operations) or 'none'}"]
        for c in self.checks:
            mark = "ok  " if c.ok else "FAIL"
            lines.append(f"{mark} {c.name}" + (f" — {c.detail}" if c.detail else ""))
        failed = len(self.failures())
        lines.append(f"{len(self.checks) - failed} passed, {failed} failed")
        return "\n".join(lines)


# ── The run ────────────────────────────────────────────────────────────────────


class _Run:
    """One conformance run: the harness, the checks so far, and every body seen (for the secret scan)."""

    def __init__(
        self,
        plugin: InfrastructurePlugin,
        harness: PluginHarness,
        *,
        plugin_id: str,
        headers: dict[str, str] | None,
        config_root: Path,
        node_type: str,
        create_nodes: bool,
        create_deployments: bool,
        dns_zone: DnsZone | None,
        live_storage: bool,
    ) -> None:
        self.plugin = plugin
        self.h = harness
        self.plugin_id = plugin_id
        self.given_headers = headers
        self.headers: dict[str, str] | None = headers
        self.config_root = config_root
        self.node_type = node_type
        self.create_nodes = create_nodes
        self.create_deployments = create_deployments
        self.dns_zone = dns_zone
        self.live_storage = live_storage
        self.checks: list[Check] = []
        self.responses: list[tuple[str, str, int, str]] = []
        self.caps = CapabilitiesResponse(provider="", display_name="")
        self.operations: list[str] = []

    # ── plumbing ──

    def call(self, method: str, path: str, *, json_body: Any = None, bare: bool = False) -> HarnessResponse:
        """One request. The Connection headers go on every route that may carry them, and on no other."""
        headers = None if bare or _ambient_only(path) else self.headers
        response = self.h.call(method, path, json_body=json_body, headers=headers)
        self.responses.append((method, path, response.status_code, response.text))
        return response

    def check(self, name: str, ok: bool, detail: str = "", *, note: str = "") -> bool:
        """Record a check; ``detail`` is kept for a failure, ``note`` for a pass (a skip's reason)."""
        self.checks.append(Check(name=name, ok=bool(ok), detail=detail if not ok else note))
        return bool(ok)

    @staticmethod
    def body(response: HarnessResponse) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def status_detail(self, response: HarnessResponse) -> str:
        return f"HTTP {response.status_code}: {response.text[:200]}"

    def listed(self, operation: str) -> bool:
        return operation in self.operations

    def round_trips(self, name: str, response: HarnessResponse, cls: Any, *, status: int = 200) -> Any:
        """Check ``response`` answers ``status`` with a body that reads as ``cls`` and writes back the same."""
        if response.status_code != status:
            self.check(name, False, self.status_detail(response))
            return None
        message = cls.from_dict(self.body(response))
        again = cls.from_dict(message.to_dict())
        self.check(name, again == message, f"{cls.__name__} does not round-trip: {response.text[:200]}")
        return message

    def load_capabilities(self) -> bool:
        r = self.call("GET", "/infra/capabilities")
        if r.status_code != 200:
            return False
        self.caps = CapabilitiesResponse.from_dict(self.body(r))
        self.operations = list(self.caps.operations)
        if self.given_headers is None and self.caps.account is not None:
            self.headers = connection_headers(
                ConnectionBinding(id="conformance", provider=self.caps.provider or self.plugin_id, kind="AMBIENT")
            )
        return True

    def default_node_type(self) -> str:
        types_ = self.caps.nodes.types if self.caps.nodes is not None else []
        return self.node_type or (types_[0] if types_ else "")

    # ── groups ──

    def shape(self) -> None:
        plugin = self.plugin
        r = self.call("GET", "/infra/capabilities")
        if not self.check("shape: GET /infra/capabilities answers 200", r.status_code == 200, self.status_detail(r)):
            return
        d = self.body(r)
        caps = CapabilitiesResponse.from_dict(d)
        self.check(
            "shape: capabilities round-trip through CapabilitiesResponse",
            CapabilitiesResponse.from_dict(caps.to_dict()) == caps,
        )
        self.check(
            f"shape: contract is {CONTRACT_GENERATION}", caps.contract == CONTRACT_GENERATION, f"{d.get('contract')!r}"
        )
        self.check(
            "shape: provider equals the plugin id",
            caps.provider == self.plugin_id,
            f"{caps.provider!r} != {self.plugin_id!r}",
        )
        problems = capabilities_problems(plugin, caps)
        self.check("shape: the declaration follows the contract's rules", not problems, "; ".join(problems))
        self.check(
            "shape: operations lists exactly what the class provides",
            caps.operations == provided_operations(plugin),
            f"{caps.operations!r} != {provided_operations(plugin)!r}",
        )
        self.check(
            "shape: storage present exactly when the class has a StorageFacet",
            ("storage" in d) == isinstance(plugin, StorageFacet),
        )
        self.check(
            "shape: requires_connection needs an account and an access method",
            not caps.requires_connection or (caps.account is not None and bool(caps.access_methods)),
        )
        grants = [m.kind for m in caps.access_methods if m.grants]
        self.check(
            "shape: a method that grants has connection.grant listed",
            not grants or self.listed("connection.grant"),
            f"methods {grants} say grants and connection.grant is not listed",
        )
        binding = ConnectionBinding(id="conformance", provider=caps.provider or self.plugin_id, kind="AMBIENT")
        r = self.h.call("GET", "/infra/capabilities", headers=connection_headers(binding))
        self.responses.append(("GET", "/infra/capabilities", r.status_code, r.text))
        self.check(
            "shape: GET /infra/capabilities refuses a Connection header with 400",
            r.status_code == 400,
            self.status_detail(r),
        )

    def connect(self) -> None:
        caps = self.caps
        if self.listed("connection.grant"):
            method = next((m for m in caps.access_methods if m.grants), None)
            if self.check("connect: a listed connection.grant has a method that grants", method is not None):
                assert method is not None
                account = {
                    f.key: f.example or f.default or "conformance"
                    for f in (caps.account.fields if caps.account else [])
                }
                access = {f.key: f.example or f.default or "conformance" for f in method.fields if not f.secret}
                request = GrantRequest(
                    connection_id="conformance",
                    kind=method.kind,
                    account=account,
                    access=access,
                    permissions=ConnectionPermissions(nodes=True),
                )
                r = self.call("POST", "/infra/connection/grant", json_body=request.to_dict())
                grant = self.round_trips("connect: POST /infra/connection/grant answers a Grant", r, Grant)
                if grant is not None:
                    self.check("connect: a grant says what it is", bool(grant.summary), r.text[:200])
                    self.check(
                        "connect: a grant's link is https",
                        not grant.link or grant.link.startswith("https://"),
                        grant.link[:120],
                    )
                    self.check("connect: a create grant answers action create", grant.action == "create", grant.action)
                r = self.h.call(
                    "POST",
                    "/infra/connection/grant",
                    json_body=request.to_dict(),
                    headers=connection_headers(
                        ConnectionBinding(id="conformance", provider=caps.provider, kind="AMBIENT")
                    ),
                )
                self.responses.append(("POST", "/infra/connection/grant", r.status_code, r.text))
                self.check(
                    "connect: POST /infra/connection/grant refuses a Connection header with 400",
                    r.status_code == 400,
                    self.status_detail(r),
                )
        for operation, method_, path in (
            ("connection.grant_update", "POST", "/infra/connection/grant/update"),
            ("connection.verify", "POST", "/infra/connection/verify"),
            ("connection.node_access", "GET", "/infra/connection/node-access"),
            ("storage.candidates", "GET", "/infra/storage/candidates"),
        ):
            if not self.listed(operation):
                continue
            r = self.call(method_, path, json_body={} if method_ == "POST" else None, bare=True)
            self.check(
                f"connect: {method_} {path} without a Connection answers 400",
                r.status_code == 400,
                self.status_detail(r),
            )
        if self.headers is None:
            return
        if self.listed("connection.verify"):
            r = self.call("POST", "/infra/connection/verify", json_body=VerifyRequest().to_dict())
            if r.status_code == 200:
                found = self.round_trips(
                    "connect: POST /infra/connection/verify answers a ConnectionVerification", r, ConnectionVerification
                )
                if found is not None:
                    self.check("connect: a verification says who the Connection is", bool(found.identity), r.text[:200])
                    declared = {f.key for f in (caps.account.fields if caps.account else [])}
                    self.check(
                        "connect: the verified account uses the declared account fields",
                        set(found.account) <= declared,
                        f"{sorted(found.account)} not within {sorted(declared)}",
                    )
            else:
                self.check(
                    "connect: a verification that cannot pass answers a worded 4xx",
                    400 <= r.status_code < 500,
                    self.status_detail(r),
                )
        if self.listed("connection.node_access"):
            r = self.call("GET", "/infra/connection/node-access")
            if r.status_code == 200:
                self.round_trips(
                    "connect: GET /infra/connection/node-access answers ConnectionNodeAccess", r, ConnectionNodeAccess
                )
            else:
                self.check(
                    "connect: node access that cannot be read answers a worded 4xx",
                    400 <= r.status_code < 500,
                    self.status_detail(r),
                )
        if self.listed("storage.candidates"):
            r = self.call("GET", "/infra/storage/candidates")
            if r.status_code == 200:
                found = self.round_trips(
                    "connect: GET /infra/storage/candidates answers StorageCandidates", r, StorageCandidates
                )
                if found is not None:
                    self.check(
                        "connect: at most 2000 storage candidates", len(found.items) <= 2000, str(len(found.items))
                    )
            else:
                self.check(
                    "connect: candidates that cannot be listed answer a worded 4xx",
                    400 <= r.status_code < 500,
                    self.status_detail(r),
                )

    def lifecycle(self) -> None:
        if not self.listed("node.create"):
            return
        node_type = self.default_node_type()
        r = self.call("POST", "/infra/nodes", json_body={"node_type": node_type})
        self.check(
            "lifecycle: POST /infra/nodes without node_id/token answers 400",
            r.status_code == 400,
            self.status_detail(r),
        )
        body = CreateNodeRequest(
            node_id="conformance-1",
            node_type=node_type,
            token="conformance-token",
            owner="conformance",
            host_id="conformance",
        ).to_dict()
        r = self.call("POST", "/infra/nodes", json_body=body)
        created = CreateNodeResponse.from_dict(self.body(r))
        if not self.check("lifecycle: POST /infra/nodes answers 201", r.status_code == 201, self.status_detail(r)):
            return
        self.check("lifecycle: create answers provider_id", bool(created.provider_id), r.text[:200])
        self.check("lifecycle: create answers agent_url", bool(created.agent_url), r.text[:200])
        self.check(
            "lifecycle: create answers a storage_access the capabilities declare",
            created.storage_access == "none"
            or (self.caps.nodes is not None and created.storage_access in self.caps.nodes.storage_access),
            created.storage_access,
        )
        pid = created.provider_id
        r = self.call("POST", "/infra/nodes", json_body=body)
        again = CreateNodeResponse.from_dict(self.body(r))
        self.check(
            "lifecycle: a repeated create with the same node_id returns the same node (idempotent)",
            200 <= r.status_code < 300 and again.provider_id == pid,
            self.status_detail(r),
        )
        r = self.call("GET", f"/infra/nodes/{pid}")
        self.check(
            "lifecycle: GET /infra/nodes/{id} answers 200 with a valid state",
            r.status_code == 200 and is_node_state(self.body(r).get("state")),
            self.status_detail(r),
        )
        r = self.call("GET", f"/infra/nodes/{pid}?diagnostics=true")
        self.check(
            "lifecycle: GET /infra/nodes/{id}?diagnostics=true answers 200", r.status_code == 200, self.status_detail(r)
        )
        r1 = self.call("DELETE", f"/infra/nodes/{pid}")
        r2 = self.call("DELETE", f"/infra/nodes/{pid}")
        self.check(
            "lifecycle: DELETE /infra/nodes/{id} answers 200 twice (idempotent)",
            r1.status_code == 200 and r2.status_code == 200,
            f"first {self.status_detail(r1)}; second {self.status_detail(r2)}",
        )
        r = self.call("GET", f"/infra/nodes/{pid}")
        after = self.body(r).get("state")
        self.check(
            "lifecycle: a deleted node reads terminated or gone (or 404)",
            r.status_code == 404 or (r.status_code == 200 and after in ("terminated", "gone")),
            self.status_detail(r),
        )

    def node_check(self) -> None:
        if not self.listed("node.check"):
            return
        request = NodeCheckRequest(node_type=self.default_node_type())
        r = self.call("POST", "/infra/nodes/check", json_body=request.to_dict())
        if not self.check("check: POST /infra/nodes/check answers 200", r.status_code == 200, self.status_detail(r)):
            return
        resp = PreflightResponse.from_dict(self.body(r))
        self.check("check: round-trip through PreflightResponse", PreflightResponse.from_dict(resp.to_dict()) == resp)
        failed_errors = [c for c in resp.checks if not c.ok and c.level == "error"]
        agrees = (not resp.ok) if failed_errors else True
        if resp.ok is False and resp.checks and all(c.ok for c in resp.checks):
            agrees = False
        self.check(
            "check: ok agrees with the checks", agrees, f"ok={resp.ok} checks={[c.to_dict() for c in resp.checks]}"
        )

    def errors(self) -> None:
        if self.listed("node.state"):
            r = self.call("GET", "/infra/nodes/conformance-missing")
            self.check(
                "errors: an unknown node id answers 200 (gone/unknown) or 404, never 5xx",
                (r.status_code == 200 and self.body(r).get("state") in ("gone", "unknown", "terminated"))
                or r.status_code == 404,
                self.status_detail(r),
            )
        bad = [
            f"{m} {p} → {s}: {t[:80]}"
            for m, p, s, t in self.responses
            if not (200 <= s < 300) and not _is_error_body(t)
        ]
        self.check(
            "errors: every non-2xx body is {detail, code, remedy, retry_after_s} with a known code",
            not bad,
            "; ".join(bad[:5]),
        )
        secrets = [v for v in self.plugin.secret_values() if v]
        leaked = [f"{m} {p}" for m, p, _s, t in self.responses if any(v in t for v in secrets)]
        self.check("errors: no secret value appears in any body", not leaked, ", ".join(leaked[:5]))

    def settings(self) -> None:
        settings = self.plugin.settings
        if settings is None:
            return
        r = self.call("GET", "/settings")
        if not self.check("settings: GET /settings answers 200", r.status_code == 200, self.status_detail(r)):
            return
        view = self.body(r)
        names = settings.secret_fields()
        self.check(
            "settings: GET /settings carries <secret>_set for every secret field",
            all(f"{n}_set" in view for n in names),
            f"missing: {[n for n in names if f'{n}_set' not in view]}",
        )
        self.check(
            "settings: GET /settings carries no secret field by name",
            not any(n in view for n in names),
            f"present: {[n for n in names if n in view]}",
        )
        if not names:
            return
        name = names[0]
        r = self.call("POST", "/settings", json_body={name: "conformance-secret-value"})
        set_ok = _ok(r) and self.body(r).get(f"{name}_set") is True
        self.check(f"settings: POST {{{name}: value}} sets it", set_ok, self.status_detail(r))
        r = self.call("POST", "/settings", json_body={name: ""})
        self.check(
            f"settings: POST {{{name}: ''}} keeps it",
            _ok(r) and self.body(r).get(f"{name}_set") is True,
            self.status_detail(r),
        )
        r = self.call("POST", "/settings", json_body={name: "-"})
        self.check(
            f"settings: POST {{{name}: '-'}} clears it",
            _ok(r) and self.body(r).get(f"{name}_set") is False,
            self.status_detail(r),
        )
        r = self.call("POST", "/settings", json_body={"conformance_unknown_key": 1})
        self.check("settings: unknown keys are ignored", _ok(r), self.status_detail(r))
        path = self.config_root / settings.plugin_id / f"{settings.record_id}.json"
        previous = path.read_bytes() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        try:
            r = self.call("GET", "/settings")
            self.check("settings: an unreadable settings file answers 409", r.status_code == 409, self.status_detail(r))
        finally:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)

    def storage(self) -> None:
        plugin = self.plugin
        if not isinstance(plugin, StorageFacet):
            return
        r = self.call("GET", "/infra/storage")
        listing: StorageListing | None = None
        if self.check("storage: GET /infra/storage answers 200", r.status_code == 200, self.status_detail(r)):
            d = self.body(r)
            listing = StorageListing.from_dict(d)
            header = {
                "kind",
                "label",
                "creatable",
                "upload",
                "browse",
                "download",
                "bundle",
                "delete",
                "transfer",
                "rename",
            }
            self.check(
                "storage: the listing flattens to the StorageListing shape",
                header <= set(d) and isinstance(d.get("storage"), list) and isinstance(d.get("regions"), list),
                f"keys={sorted(d)}",
            )
        flags = plugin.storage_capabilities()
        cls = type(plugin)
        overridden = {
            "creatable": cls.create_storage is not StorageFacet.create_storage,
            "upload": cls.presign is not StorageFacet.presign,
            "download": cls.presign is not StorageFacet.presign,
            "browse": cls.list_objects is not StorageFacet.list_objects,
            "delete": cls.delete_objects is not StorageFacet.delete_objects,
            "transfer": cls.transfer_registry is not StorageFacet.transfer_registry,
            "rename": cls.transfer_registry is not StorageFacet.transfer_registry,
            "bundle": cls.bundle_registry is not StorageFacet.bundle_registry,
        }
        unbacked = [flag for flag, done in overridden.items() if getattr(flags, flag) and not done]
        self.check(
            "storage: every True flag in storage_capabilities() has its method overridden",
            not unbacked,
            f"flags without a method: {unbacked}",
        )
        probes = {
            "creatable": ("POST", "/infra/storage", {"name": "conformance-probe"}),
            "upload": (
                "POST",
                "/infra/storage/presign",
                {"url": "x://probe", "files": [{"path": "a"}], "mode": "upload"},
            ),
            "download": (
                "POST",
                "/infra/storage/presign",
                {"url": "x://probe", "files": [{"path": "a"}], "mode": "download"},
            ),
            "browse": ("GET", "/infra/storage/list?url=x://probe", None),
            "delete": ("POST", "/infra/storage/delete", {"url": "x://probe", "paths": ["a"], "dry_run": True}),
            "transfer": (
                "POST",
                "/infra/storage/transfer",
                {"src_url": "x://probe", "dst_url": "x://probe", "items": ["a"], "dry_run": True},
            ),
            "bundle": ("POST", "/infra/storage/bundle", {"url": "x://probe"}),
        }
        for flag, (method, path, body) in probes.items():
            if getattr(flags, flag) or overridden[flag]:
                continue
            r = self.call(method, path, json_body=body)
            self.check(
                f"storage: the {flag} route answers 501 while the flag is False",
                r.status_code == 501,
                self.status_detail(r),
            )
        r = self.call("GET", f"/infra/storage/transfer/{_MADE_UP_ID}")
        self.check("storage: a made-up transfer id answers 404", r.status_code == 404, self.status_detail(r))
        r = self.call("GET", f"/infra/storage/bundle/{_MADE_UP_ID}")
        self.check("storage: a made-up bundle id answers 404", r.status_code == 404, self.status_detail(r))
        r = self.call("GET", "/infra/storage/transfer/not-an-id")
        self.check("storage: a malformed transfer id answers 400", r.status_code == 400, self.status_detail(r))
        if not self.live_storage or listing is None or not listing.storage:
            return
        first = listing.storage[0].url
        if flags.browse:
            r = self.call("GET", f"/infra/storage/list?url={first}")
            ok = r.status_code == 200
            if ok:
                objects = ObjectListing.from_dict(self.body(r))
                ok = ObjectListing.from_dict(objects.to_dict()) == objects
            self.check("storage: list_objects on the first item round-trips ObjectListing", ok, self.status_detail(r))
        if flags.transfer:
            body = {"src_url": first, "dst_url": first, "items": ["conformance-missing"], "dry_run": True}
            r = self.call("POST", "/infra/storage/transfer", json_body=body)
            self.check(
                "storage: a transfer dry-run answers counts (2xx) or a sentence (400)",
                _ok(r) or r.status_code == 400,
                self.status_detail(r),
            )

    def catalog(self) -> None:
        if not self.listed("catalog"):
            return
        r = self.call("POST", "/infra/catalog", json_body={"purpose": "conformance"})
        self.check("catalog: an unknown purpose answers 400", r.status_code == 400, self.status_detail(r))
        purposes = ["gpu_node", "cpu_node"] if self.caps.nodes is not None else []
        if self.caps.deployments is not None:
            purposes.append("deployment")
        for purpose in purposes:
            r = self.call("POST", "/infra/catalog", json_body={"purpose": purpose})
            found = self.round_trips(f"catalog: POST /infra/catalog ({purpose}) answers a Catalog", r, Catalog)
            if found is not None:
                self.check(f"catalog: at most 500 offers ({purpose})", len(found.offers) <= 500, str(len(found.offers)))
                self.check(f"catalog: every offer has an id ({purpose})", all(o.id for o in found.offers))
        if self.listed("catalog.locations"):
            r = self.call("GET", "/infra/catalog/locations")
            found = self.round_trips("catalog: GET /infra/catalog/locations answers Locations", r, Locations)
            if found is not None:
                self.check("catalog: every location has an id", all(x.id for x in found.locations))

    def deployments(self) -> None:
        if not self.listed("deployment.create"):
            return
        r = self.call("POST", "/infra/deployments", json_body={"deployment_id": "conformance"}, bare=True)
        self.check(
            "deployments: POST /infra/deployments without a Connection answers 400",
            r.status_code == 400,
            self.status_detail(r),
        )
        if self.headers is None:
            return
        placement: dict[str, str] = {}
        if self.listed("deployment.options"):
            for _ in range(8):
                r = self.call("POST", "/infra/deployments/options", json_body={"selected": placement})
                options = self.round_trips(
                    "deployments: POST /infra/deployments/options answers DeploymentOptions", r, DeploymentOptions
                )
                if options is None:
                    break
                chosen = dict(placement)
                for f in options.placement:
                    value = f.default or (f.choices[0].id if f.choices else "")
                    if value and f.key not in chosen:
                        chosen[f.key] = value
                        break
                if chosen == placement:
                    break
                placement = chosen
        ingress_key = ""
        if self.listed("deployment.check"):
            request = DeploymentCheckRequest(label="conformance", placement=placement, zone=self.dns_zone)
            r = self.call("POST", "/infra/deployments/check", json_body=request.to_dict())
            checked = self.round_trips(
                "deployments: POST /infra/deployments/check answers a DeploymentCheck", r, DeploymentCheck
            )
            if checked is not None:
                ingress_key = checked.ingress_key
                shared = self.caps.deployments is not None and self.caps.deployments.shared_ingress
                self.check(
                    "deployments: an ingress_key only with shared_ingress", shared or not ingress_key, ingress_key
                )
                self.check("deployments: a refused check says why", checked.ok or bool(checked.reason), r.text[:200])
        if not self.create_deployments:
            return
        if self.listed("deployment.prepare"):
            request_p = DeploymentPrepareRequest(
                deployment_id="conformance-1", placement=placement, ingress_key=ingress_key
            )
            r = self.call("POST", "/infra/deployments/prepare", json_body=request_p.to_dict())
            prepared = self.round_trips(
                "deployments: POST /infra/deployments/prepare answers DeploymentPrepared", r, DeploymentPrepared
            )
            if prepared is not None:
                self.check(
                    "deployments: prepare answers records only under the namespace it was given",
                    not prepared.dns_records or bool(request_p.namespace),
                    f"{[x.name for x in prepared.dns_records]} with no namespace",
                )
        request_c = DeploymentRequest(
            deployment_id="conformance-1",
            name="Conformance",
            bootstrap=DeploymentBootstrap(
                record_id="conformance-record",
                config_service_url="https://config.invalid",
                token="conformance-bootstrap-token",
            ),
            ingress_key="",
            placement=placement,
            owner="conformance",
        )
        r = self.call("POST", "/infra/deployments", json_body=request_c.to_dict())
        created = self.round_trips(
            "deployments: POST /infra/deployments answers a DeploymentResponse (201)", r, DeploymentResponse, status=201
        )
        if created is None:
            return
        pid = created.provider_id
        self.check("deployments: create answers provider_id", bool(pid), r.text[:200])
        self.check(
            "deployments: create answers addresses or a failure",
            bool(created.addresses) or bool(created.failure),
            r.text[:200],
        )
        endpoints_ok = bool(created.failure) or (
            {"object_service_url", "compute_service_url"} <= set(created.endpoints)
            and all(v.startswith("https://") for v in created.endpoints.values())
        )
        self.check("deployments: endpoints are the two https service urls", endpoints_ok, f"{created.endpoints!r}")
        self.check(
            "deployments: a deployment named by address needs deployments.sslip",
            self.caps.deployments is not None and self.caps.deployments.sslip,
        )
        self.check("deployments: the bootstrap token is in no answer", "conformance-bootstrap-token" not in r.text)
        r = self.call("POST", "/infra/deployments", json_body=request_c.to_dict())
        again = DeploymentResponse.from_dict(self.body(r))
        self.check(
            "deployments: a repeated create with the same deployment_id returns the same deployment (idempotent)",
            200 <= r.status_code < 300 and again.provider_id == pid,
            self.status_detail(r),
        )
        for diagnostics in ("", "baseline"):
            r = self.call("GET", f"/infra/deployments/{pid}?diagnostics={diagnostics}")
            self.check(
                f"deployments: GET /infra/deployments/{{id}}?diagnostics={diagnostics} answers a valid state",
                r.status_code == 200 and is_node_state(self.body(r).get("state")),
                self.status_detail(r),
            )
        r = self.call("GET", f"/infra/deployments/{pid}?diagnostics=conformance")
        self.check("deployments: an unknown diagnostics level answers 400", r.status_code == 400, self.status_detail(r))
        if self.listed("deployment.list"):
            r = self.call("GET", "/infra/deployments")
            listing = self.round_trips(
                "deployments: GET /infra/deployments answers a DeploymentListing", r, DeploymentListing
            )
            if listing is not None:
                self.check(
                    "deployments: the listing holds the created deployment",
                    any(x.provider_id == pid for x in listing.deployments),
                )
                self.check("deployments: at most 500 listed", len(listing.deployments) <= 500)
        r1 = self.call("DELETE", f"/infra/deployments/{pid}")
        r2 = self.call("DELETE", f"/infra/deployments/{pid}")
        self.check(
            "deployments: DELETE /infra/deployments/{id} answers 200 twice (idempotent)",
            r1.status_code == 200 and r2.status_code == 200,
            f"first {self.status_detail(r1)}; second {self.status_detail(r2)}",
        )
        if self.listed("deployment.sweep"):
            r = self.call("POST", "/infra/deployments/sweep", json_body=SweepRequest(grace_s=3600.0).to_dict())
            self.round_trips("deployments: POST /infra/deployments/sweep answers a SweepReport", r, SweepReport)

    def dns(self) -> None:
        if not self.listed("dns"):
            return
        r = self.h.call(
            "GET",
            "/infra/dns/records?zone_id=conformance&zone_name=conformance.invalid",
            headers=connection_headers(ConnectionBinding(id="conformance", provider=self.plugin_id, kind="AMBIENT")),
        )
        self.responses.append(("GET", "/infra/dns/records", r.status_code, r.text))
        self.check(
            "dns: the DNS routes refuse a Connection header with 400", r.status_code == 400, self.status_detail(r)
        )
        r = self.call(
            "POST",
            "/infra/dns/changes",
            json_body={
                "zone": {"id": "z", "name": "z.invalid"},
                "create": [{"name": "a.z.invalid", "type": "MX", "values": ["x"]}],
            },
        )
        self.check(
            "dns: a record type other than A, AAAA, CNAME, TXT answers 400", r.status_code == 400, self.status_detail(r)
        )
        zone = self.dns_zone
        if zone is None:
            return
        base = f"_3lc-conformance.{zone.name}"
        one = DnsRecord(name=f"a.{base}", type="TXT", values=["one"])
        other = DnsRecord(name=f"b.{base}", type="TXT", values=["other"])
        clash = DnsRecord(name=one.name, type="TXT", values=["two"])

        def change(create: list[DnsRecord], delete: list[DnsRecord]) -> HarnessResponse:
            return self.call(
                "POST", "/infra/dns/changes", json_body=DnsChange(zone=zone, create=create, delete=delete).to_dict()
            )

        def names() -> set[tuple[str, tuple[str, ...]]]:
            found: set[tuple[str, tuple[str, ...]]] = set()
            cursor = ""
            for _ in range(50):
                r = self.call(
                    "GET", f"/infra/dns/records?zone_id={zone.id}&zone_name={zone.name}&under={base}&cursor={cursor}"
                )
                page = DnsRecords.from_dict(self.body(r))
                found |= {(x.name, tuple(x.values)) for x in page.records}
                cursor = page.next_cursor
                if not cursor:
                    break
            return found

        change([], [one, other, clash])
        r = change([one], [])
        self.check("dns: a create answers 200", r.status_code == 200, self.status_detail(r))
        r = change([one], [])
        self.check(
            "dns: a create of the same record with the same values succeeds",
            r.status_code == 200,
            self.status_detail(r),
        )
        r = change([other, clash], [])
        self.check(
            "dns: a create of an existing name with other values answers 409 conflict",
            r.status_code == 409 and self.body(r).get("code") == "conflict",
            self.status_detail(r),
        )
        listed = names()
        self.check("dns: a refused change applied nothing", (other.name, ("other",)) not in listed, f"{sorted(listed)}")
        self.check(
            "dns: the listing under a name holds what was created", (one.name, ("one",)) in listed, f"{sorted(listed)}"
        )
        r = change([], [clash])
        self.check(
            "dns: a delete that does not match the record exactly removes nothing",
            r.status_code == 200 and (one.name, ("one",)) in names(),
            self.status_detail(r),
        )
        r1 = change([], [one])
        r2 = change([], [one])
        self.check(
            "dns: a delete answers 200, and again when the record is gone",
            r1.status_code == 200 and r2.status_code == 200 and (one.name, ("one",)) not in names(),
            f"first {self.status_detail(r1)}; second {self.status_detail(r2)}",
        )

    # ── transitional ──

    def workspaces(self) -> None:
        if not isinstance(self.plugin, WorkspaceFacet):
            return
        r = self.call("GET", "/infra/workspaces?owner=conformance")
        ok = r.status_code == 200 and isinstance(self.body(r).get("workspaces"), list)
        if ok:
            listing = WorkspaceListing.from_dict(self.body(r))
            ok = WorkspaceListing.from_dict(listing.to_dict()) == listing
        self.check("workspaces: GET /infra/workspaces round-trips WorkspaceListing", ok, self.status_detail(r))

    # ── end transitional ──


def _route_checks(plugin: InfrastructurePlugin) -> tuple[list[Check], bool]:
    """The routes group, from the handler list alone; also whether a duplicate would stop the app from building."""
    from tlc_plugin_sdk.asgi_app import RESERVED_WORKER_PATHS, RESERVED_WORKER_PREFIXES

    seen: dict[tuple[str, str], int] = {}
    reserved: list[str] = []
    for handler in plugin.get_route_handlers():
        paths = {"/" + str(p).strip("/") for p in (getattr(handler, "paths", None) or ())}
        methods = {str(m).upper() for m in (getattr(handler, "http_methods", None) or ())}
        for p in paths:
            if p in RESERVED_WORKER_PATHS or any(p == r or p.startswith(r + "/") for r in RESERVED_WORKER_PREFIXES):
                reserved.append(p)
            for m in methods:
                seen[(m, p)] = seen.get((m, p), 0) + 1
    duplicates = sorted(f"{m} {p}" for (m, p), n in seen.items() if n > 1)
    checks = [
        Check("routes: no handler on a reserved worker path", not reserved, ", ".join(sorted(reserved))),
        Check(
            "routes: no duplicate method+path (a settings layer next to hand-rolled /settings)",
            not duplicates,
            ", ".join(duplicates),
        ),
    ]
    return checks, bool(duplicates)


def _ok(response: HarnessResponse) -> bool:
    return 200 <= response.status_code < 300


def _is_error_body(text: str) -> bool:
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return (
        isinstance(data, dict)
        and set(data) == _ERROR_KEYS
        and isinstance(data["detail"], str)
        and data["code"] in ERROR_CODES
        and isinstance(data["remedy"], str)
        and (data["retry_after_s"] is None or isinstance(data["retry_after_s"], (int, float)))
    )


def check_provider(
    plugin: InfrastructurePlugin,
    *,
    plugin_id: str,
    config_root: str | Path | None = None,
    node_type: str = "",
    create_nodes: bool = False,
    create_deployments: bool = False,
    dns_zone: DnsZone | None = None,
    live_storage: bool = False,
    skip: Iterable[str] = (),
    headers: dict[str, str] | None = None,
) -> ConformanceReport:
    """Drive ``plugin`` through its routes and report every check.

    Args:
        plugin: The provider.
        plugin_id: The manifest id a host would hydrate onto it.
        config_root: A scratch settings root (see :class:`~tlc_plugin_sdk.harness.PluginHarness`);
            a temporary directory when ``None``. The real ``~/.3lc-plugin-configs`` is never used.
        node_type: The node type to create and check (default: the first in ``nodes.types``).
        create_nodes: Run the node lifecycle checks (create, state, diagnostics, delete).
        create_deployments: Run the deployment lifecycle checks (prepare, create, state, list,
            delete, sweep), naming the deployment by its address.
        dns_zone: A zone the DNS checks may write under ``_3lc-conformance.<name>``; ``None``
            runs only the DNS checks that write nothing.
        live_storage: Run ``list_objects`` and a transfer dry-run against the first listed storage.
        skip: Group names (:data:`GROUPS`) not to run.
        headers: The Connection headers to send on every call that acts on an account
            (:func:`connection_headers`). Default: an ``AMBIENT`` binding for the provider, and
            none for a provider whose capabilities declare no account.

    Returns:
        The report.
    """
    skipped = set(skip)
    route_checks, duplicates = _route_checks(plugin) if "routes" not in skipped else ([], False)
    if duplicates:
        # Litestar refuses to build an app with two handlers on one method and path: nothing else can run.
        return ConformanceReport(checks=route_checks, operations=provided_operations(plugin))
    with tempfile.TemporaryDirectory(prefix="tlc-conformance-") as scratch:
        root = Path(config_root) if config_root is not None else Path(scratch)
        from tlc_plugin_sdk.harness import PluginHarness

        with PluginHarness(plugin, plugin_id=plugin_id, config_root=root) as h:
            run = _Run(
                plugin,
                h,
                plugin_id=plugin_id,
                headers=headers,
                config_root=root,
                node_type=node_type,
                create_nodes=create_nodes,
                create_deployments=create_deployments,
                dns_zone=dns_zone,
                live_storage=live_storage,
            )
            loaded = run.load_capabilities()
            if "shape" not in skipped:
                run.shape()
            if loaded:
                groups: tuple[tuple[str, Any], ...] = (
                    ("connect", run.connect),
                    ("lifecycle", run.lifecycle if create_nodes else None),
                    ("check", run.node_check),
                    ("settings", run.settings),
                    ("storage", run.storage),
                    ("catalog", run.catalog),
                    ("deployments", run.deployments),
                    ("dns", run.dns),
                    ("workspaces", run.workspaces),
                    ("errors", run.errors),
                )
                for group, fn in groups:
                    if group not in skipped and fn is not None:
                        fn()
    return ConformanceReport(checks=[*run.checks, *route_checks], operations=provided_operations(plugin))


def assert_conformant(plugin: InfrastructurePlugin, *, plugin_id: str, **options: Any) -> None:
    """Run :func:`check_provider` and raise ``AssertionError`` with the report when a check fails.

    Args:
        plugin: The provider.
        plugin_id: The manifest id.
        **options: As :func:`check_provider`.

    Raises:
        AssertionError: With ``report.text()``.
    """
    report = check_provider(plugin, plugin_id=plugin_id, **options)
    if not report.ok:
        raise AssertionError(report.text())


# ── The reference provider ─────────────────────────────────────────────────────


@dataclass
class FakeSettings:
    """The settings of :class:`FakeProvider`: a secret, a node-type list and an env dict."""

    id: str = "default"
    created: str = ""
    last_run: str | None = None
    api_key: str = secret(
        label="Fake API key", help="Any value works; the provider is in memory.", placeholder="fake-…"
    )
    node_types: list[str] = option(default_factory=lambda: ["fake-gpu", "fake-cpu"], label="Node types")
    default_storage: str = "fake-data"
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, repr=False)
class FakeCredential:
    """What :class:`FakeProvider`'s resolver returns for a ``KEYLESS`` Connection: its own credential class.

    Attributes:
        connection_id: The Connection it was exchanged for.
        access_token: The short-lived value. Never in a repr; scrubbed from error answers.
        account: The account it is in.
        expires_at: ISO-8601 expiry.
    """

    connection_id: str
    access_token: str
    account: dict[str, str] = field(default_factory=dict)
    expires_at: str = "2099-01-01T00:00:00+00:00"

    def __repr__(self) -> str:
        return f"FakeCredential(connection_id={self.connection_id!r}, access_token='***')"

    def secret_values(self) -> list[str]:
        """The token."""
        return [self.access_token]


_FAKE_URL = re.compile(r"^fake://([^/]+)/?(.*)$")
_FAKE_ACCOUNT = {"account_id": "000000000000"}
_FAKE_LOCATIONS = ("fake-1", "fake-2")
_FAKE_HOST = "fake-host-principal"


def _split(url: str) -> tuple[str, str]:
    """``("bucket", "prefix")`` of a ``fake://bucket/prefix`` URL."""
    m = _FAKE_URL.match(str(url or ""))
    if not m:
        msg = f"'{str(url)[:60]}' is not a fake:// URL"
        raise InvalidRequest(msg)
    return m.group(1), m.group(2).strip("/")


def _canonical(permissions: ConnectionPermissions) -> ConnectionPermissions:
    """The permissions as the fake stores them: locations without a trailing slash, sorted, no duplicates."""
    rules = sorted({(rule.location.rstrip("/"), rule.access) for rule in permissions.storage})
    return ConnectionPermissions(
        deployments=permissions.deployments,
        nodes=permissions.nodes,
        storage=[StorageRule(location=location, access=access) for location, access in rules],
        options={key: permissions.options[key] for key in sorted(permissions.options)},
    )


class FakeProvider(
    InfrastructurePlugin, NodeProvider, DeploymentProvider, StorageFacet, CatalogFacet, DnsFacet, WorkspaceFacet
):
    """Every operation of the contract, in memory: the SDK's reference provider.

    Nodes and deployments live in dicts keyed by provider id and are idempotent on the host's id;
    storage is one ``{key: bytes}`` per bucket under ``fake://<bucket>/``; transfers and bundles
    run the SDK engines over that dict; the DNS zone is a dict per zone id with the atomic-change
    semantics of the contract; catalogs answer static offers. It declares all three access
    methods and resolves ``KEYLESS`` itself, to a :class:`FakeCredential`: a binding whose
    ``access`` says ``role_name = "refused"`` is refused.

    Attributes:
        nodes: ``{provider_id: record}``; a record says what the create call carried, including
            the ``connection`` id and the ``credential`` class the call acted with.
        deployments: ``{provider_id: record}``, likewise.
        zones: ``{zone_id: {(name, type): DnsRecord}}``.
        buckets: ``{bucket: {key: bytes}}``.
        leftovers: Resource names a sweep finds unowned and releases.
        shared_ingress: Whether ``check_deployment`` answers an ``ingress_key`` in a zone, so
            that ``prepare_deployment`` has a validation record to ask for.
        requires_connection: What capabilities say.
    """

    settings = PluginSettings(FakeSettings, "fake")

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.deployments: dict[str, dict[str, Any]] = {}
        self.zones: dict[str, dict[tuple[str, str], DnsRecord]] = {}
        self.buckets: dict[str, dict[str, bytes]] = {
            "fake-data": {"train/a.jpg": b"a" * 10, "train/b.jpg": b"b" * 20, "readme.txt": b"hello"}
        }
        self.leftovers: list[str] = []
        self.shared_ingress = True
        self.requires_connection = False
        self.node_access: dict[str, dict[str, Any]] = {}
        self._transfers: dict[str, TransferRegistry] = {}
        self._bundles: dict[str, BundleRegistry] = {}
        register_resolver("fake", self.resolve_keyless)

    def get_ui_fragment(self) -> str:
        return "<div>fake provider</div>"

    # ── the credential seam ──

    @staticmethod
    def resolve_keyless(request: ResolveRequest) -> Ambient | FakeCredential:
        """The fake's ``KEYLESS`` resolver, registered for provider ``fake`` when a fake is built."""
        binding = request.binding
        if binding.access.get("role_name") == "refused":
            msg = "The fake account refused to let this host in"
            raise CredentialUnavailable(msg, remedy="Use a role_name other than 'refused'.")
        if binding.access.get("deployment_of") == _FAKE_HOST:
            return Ambient(provider=binding.provider, account=dict(binding.account), defaults=dict(binding.defaults))
        return FakeCredential(
            connection_id=binding.id,
            access_token=f"fake-session-{binding.id}",
            account=dict(binding.account) or dict(_FAKE_ACCOUNT),
        )

    @staticmethod
    def _acting() -> dict[str, Any]:
        """Which Connection, credential and use the current call acts with, for a record."""
        binding = current_connection()
        credential = current_credential()
        use = current_use()
        return {
            "connection": binding.id if binding is not None else "",
            "kind": binding.kind if binding is not None else "",
            "credential": type(credential).__name__ if credential is not None else "",
            "resource_id": use.resource_id if use is not None else "",
            "source_identity": use.source_identity if use is not None else "",
        }

    @staticmethod
    def _account() -> dict[str, str]:
        binding = current_connection()
        return dict(binding.account) if binding is not None and binding.account else dict(_FAKE_ACCOUNT)

    @staticmethod
    def _default_location() -> str:
        binding = current_connection()
        return (binding.defaults.get("location") if binding is not None else "") or _FAKE_LOCATIONS[0]

    # ── capabilities ──

    def capabilities(self) -> CapabilitiesResponse:
        s = self.settings.load()
        return CapabilitiesResponse(
            provider="fake",
            display_name="Fake cloud",
            requires_connection=self.requires_connection,
            account=AccountSchema(
                noun="Fake account",
                fields=[
                    FormField(
                        key="account_id",
                        label="Account id",
                        help="The twelve digits of the fake account.",
                        example="000000000000",
                        pattern=r"^\d{12}$",
                        max_length=12,
                    )
                ],
                defaults=[
                    FormField(
                        key="location",
                        label="Default location",
                        kind="select",
                        required=False,
                        choices=[Choice(id=loc, label=f"Fake region {loc[-1]}") for loc in _FAKE_LOCATIONS],
                        default=_FAKE_LOCATIONS[0],
                    )
                ],
                help="Any twelve digits work; the provider is in memory.",
            ),
            access_methods=[
                AccessMethod(
                    kind="KEYLESS",
                    label="Uses access you grant to 3LC",
                    summary="You create a role in the fake account that trusts this host.",
                    preferred=True,
                    grants=True,
                    fields=[
                        FormField(
                            key="role_name", label="Role name", example="3lc-fake", default="3lc-fake", max_length=64
                        )
                    ],
                ),
                AccessMethod(
                    kind="AMBIENT",
                    label="Uses this machine's own sign-in",
                    summary="The Compute Service acts as itself.",
                    purposes=["nodes", "storage"],
                ),
                AccessMethod(
                    kind="SECRET",
                    label="Uses an API key you store",
                    purposes=["nodes", "storage"],
                    fields=[FormField(key="api_key", label="API key", kind="secret", secret=True, example="fake-…")],
                ),
            ],
            host=HostIdentity(display="Fake host", matches={"setup_identity": _FAKE_HOST}),
            permissions=PermissionSchema(
                options=[
                    PermissionOption(
                        key="deployment_nodes",
                        label="Deployments may start their own nodes",
                        purpose="deployments",
                        default=True,
                        operations=["node.create", "node.describe", "node.terminate"],
                    )
                ],
                storage=StorageLocationSchema(
                    scheme="fake",
                    example="fake://fake-data/projects",
                    pattern=r"^fake://[a-z0-9*-]+(/.*)?$",
                    access_modes=["read", "read_write_no_delete", "read_write"],
                    help="A bucket, or a folder in one. * matches any run of characters.",
                ),
            ),
            nodes=NodeCapabilities(
                kinds=["gpu", "cpu"],
                types=list(s.node_types),
                type_label="Fake machine",
                pricing=["on_demand", "spot"],
                storage_access=["identity", "ambient"],
            ),
            deployments=DeploymentCapabilities(certificate="provider", shared_ingress=True, sslip=True),
            **self.readiness(),
        )

    # ── connect an account ──

    def describe_grant(self, request: GrantRequest) -> Grant:
        if request.kind != "KEYLESS":
            msg = "Only the granted access method has a grant to describe"
            raise InvalidRequest(msg, remedy="Ask for the grant of a KEYLESS Connection.")
        role = request.access.get("role_name") or "3lc-fake"
        account = request.account.get("account_id") or _FAKE_ACCOUNT["account_id"]
        permissions = _canonical(request.permissions)
        update = request.action == "update"
        creates: dict[str, list[str]] = {}
        if permissions.deployments:
            creates["deployments"] = [f"A role {role}-deployment that a managed deployment runs as."]
        if permissions.nodes:
            creates["nodes"] = [f"A role {role}-node that a node runs as."]
        if permissions.storage:
            creates["storage"] = [f"{rule.access} access to {rule.location}" for rule in permissions.storage]
        document = {"role": role, "trusts": _FAKE_HOST, "permissions": permissions.to_dict()}
        return Grant(
            summary=f"{'Updates' if update else 'Creates'} the role {role} in fake account {account}.",
            action=request.action,
            link=f"https://console.fake.invalid/grants/{request.action}?connection={request.connection_id}",
            link_label="Open the fake console",
            steps=["Open the link.", "Review what is created.", "Choose Apply and wait for it to complete."],
            creates=creates,
            trusts=f"The fake principal {_FAKE_HOST}",
            files=[GrantFile(name="3lc-connection.json", content=json.dumps(document, sort_keys=True))],
            command="fake grants apply --file 3lc-connection.json",
            permissions=permissions,
            access={"role_name": role, "node_identity": f"{role}-node", "setup_identity": _FAKE_HOST},
            version="1",
            console_url=f"https://console.fake.invalid/grants/{request.connection_id}",
            notes=["Delete the role to revoke the access."],
        )

    def prepare_grant_update(self, request: GrantUpdateRequest) -> GrantUpdate:
        if _canonical(request.installed.permissions) == _canonical(request.pending):
            return GrantUpdate(
                status="no_changes",
                detail="The installed grant already allows this.",
                revision=request.revision,
                pending_revision=request.pending_revision,
            )
        binding = current_connection()
        name = binding.id if binding is not None else ""
        return GrantUpdate(
            status="ready",
            detail="A change is ready for review.",
            review_url=f"https://console.fake.invalid/grants/{name}/changes/{request.pending_revision}",
            revision=request.revision,
            pending_revision=request.pending_revision,
        )

    def verify_connection(self, request: VerifyRequest) -> ConnectionVerification:
        binding = current_connection()
        kind = binding.kind if binding is not None else "AMBIENT"
        account = self._account()
        observation = None
        if binding is not None and kind == "KEYLESS":
            observation = GrantObservation(
                permissions=binding.permissions or ConnectionPermissions(nodes=True),
                account=account,
                resource_ref=binding.grant_ref or f"fake:grant/{binding.id}",
                status="complete",
                version="1",
            )
        checks = []
        for root in request.storage_roots:
            try:
                reachable = _split(root.url)[0] in self.buckets
            except InvalidRequest:
                reachable = False
            checks.append(
                PreflightCheck(
                    name=root.url,
                    ok=reachable,
                    level="info" if reachable else "warn",
                    detail="" if reachable else "Not found.",
                )
            )
        return ConnectionVerification(
            identity=f"fake:{kind.lower()}:{account.get('account_id', '')}",
            account=account,
            account_label=f"Fake account {account.get('account_id', '')}",
            checked=["resolve", "whoami"],
            checks=checks,
            observation=observation,
            facts={"Grant version": "1"} if observation is not None else {},
        )

    def connection_node_access(self, request: NodeAccessRequest, *, apply: bool = False) -> ConnectionNodeAccess:
        binding = current_connection()
        key = binding.id if binding is not None else ""
        state = self.node_access.setdefault(key, {"ready": False, "rules": [], "revision": "0"})
        if apply:
            if request.storage_access is not None:
                if request.expected_revision and request.expected_revision != state["revision"]:
                    msg = "The node access setup changed since it was reviewed"
                    raise Conflict(msg, remedy="Review the current setup and apply again.")
                state["rules"] = list(request.storage_access.get("rules") or [])
            state["ready"] = True
            state["revision"] = str(int(state["revision"]) + 1)
        return ConnectionNodeAccess(
            supported=True,
            ready=bool(state["ready"]),
            detail="Nodes reach storage as the fake node identity."
            if state["ready"]
            else "The fake node identity is not set up.",
            action_label="" if state["ready"] else "Set up node access",
            checks=[PreflightCheck(name=root.url, ok=True, detail=root.access) for root in request.storage_roots],
            storage_access={"rules": list(state["rules"])},
            revision=str(state["revision"]),
        )

    def storage_candidates(self) -> StorageCandidates:
        return StorageCandidates(
            items=[StorageCandidate(location=f"fake://{b}", label=b) for b in sorted(self.buckets)][:2000]
        )

    # ── nodes ──

    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
        s = self.settings.load()
        if request.node_type not in s.node_types:
            msg = f"'{request.node_type[:40]}' is not a configured node type"
            raise InvalidRequest(msg, remedy="Choose one of the configured node types.")
        location = request.location or self._default_location()
        if location not in _FAKE_LOCATIONS:
            msg = f"'{location[:40]}' is not a fake location"
            raise InvalidRequest(msg, remedy=f"Choose one of {', '.join(_FAKE_LOCATIONS)}.")
        provider_id = f"fake-{request.node_id}"
        existing = self.nodes.get(provider_id)
        if existing is not None and existing["state"] != "terminated":
            response: CreateNodeResponse = existing["response"]
            return response
        acting = self._acting()
        access = {"KEYLESS": "identity", "AMBIENT": "ambient"}.get(acting["kind"], "none")
        node: dict[str, Any] = {
            "state": "running",
            "kind": request.kind,
            "flavor": request.flavor,
            "owner": request.owner,
            "host_id": request.host_id,
            "type": request.node_type,
            "location": location,
            "pricing": request.pricing or "on_demand",
            "storage_roots": [root.to_dict() for root in request.storage_roots],
            "tags": {"3lc": "node", "3lc-id": request.node_id, "3lc-owner": request.owner, "3lc-host": request.host_id},
            **acting,
        }
        # ── transitional ──
        if request.flavor == "workspace":
            node["tags"]["3lc"] = "deployment"
            node["services"] = {
                "object_service_url": f"http://{provider_id}:5015",
                "compute_service_url": f"http://{provider_id}:5020",
            }
            node["response"] = CreateNodeResponse(
                provider_id=provider_id, services=node["services"], hourly_rate=0.1, location=location
            )
            self.nodes[provider_id] = node
            workspace_response: CreateNodeResponse = node["response"]
            return workspace_response
        # ── end transitional ──
        binding = current_connection()
        identity = (
            (binding.access.get("node_identity", "") if binding is not None else "") if access == "identity" else ""
        )
        node["response"] = CreateNodeResponse(
            provider_id=provider_id,
            agent_url=f"http://fake:{request.agent_port}",
            pricing=node["pricing"],
            hourly_rate=0.5 if node["pricing"] == "spot" else 1.5,
            location=location,
            identity=identity or ("fake-node" if access == "identity" else ""),
            storage_access="identity" if access == "identity" else "ambient" if access == "ambient" else "none",
        )
        self.nodes[provider_id] = node
        created: CreateNodeResponse = node["response"]
        return created

    def node_state(self, provider_id: str) -> NodeStateResponse:
        node = self.nodes.get(provider_id)
        if node is None:
            return NodeStateResponse(state="gone", detail="no such node")
        return NodeStateResponse(state=node["state"])

    def node_diagnostics(self, provider_id: str) -> NodeStateResponse:
        state = self.node_state(provider_id)
        if state.state == "gone":
            return state
        state.bootstrap_history = ["Starting node agent"]
        state.bootstrap_detail = "Starting node agent"
        state.bootstrap_failed = False
        return state

    def delete_node(self, provider_id: str) -> NodeStateResponse:
        node = self.nodes.get(provider_id)
        if node is None:
            return NodeStateResponse(state="gone", detail="no such node")
        node["state"] = "terminated"
        return NodeStateResponse(state="terminated")

    def check_node(self, request: NodeCheckRequest) -> PreflightResponse:
        s = self.settings.load()
        node_type = request.node_type or (s.node_types[0] if s.node_types else "")
        location = request.location or self._default_location()
        checks = [
            PreflightCheck(
                name="node type",
                ok=node_type in s.node_types,
                level="info" if node_type in s.node_types else "error",
                detail=node_type,
            ),
            PreflightCheck(
                name="location",
                ok=location in _FAKE_LOCATIONS,
                level="info" if location in _FAKE_LOCATIONS else "error",
                detail=location,
            ),
        ]
        ok = all(c.ok for c in checks)
        return PreflightResponse(ok=ok, checks=checks, summary="ready" if ok else "not ready")

    # ── storage ──

    def storage_capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(kind="bucket", label="Fake bucket", default_id=self.settings.load().default_storage)

    def list_storage(self, *, fallback_url: str = "") -> StorageListing:
        default = self.settings.load().default_storage
        return StorageListing(
            capabilities=self.storage_capabilities(),
            storage=[
                StorageItem(id=b, name=b, url=f"fake://{b}", kind="bucket", region="fake-1", default=b == default)
                for b in sorted(self.buckets)
            ],
            regions=[Region(id="fake-1", name="Fake region 1")],
        )

    def create_storage(self, request: CreateStorageRequest) -> StorageItem:
        if request.name in self.buckets:
            msg = f"A bucket called '{request.name}' exists already"
            raise InvalidRequest(msg)
        self.buckets[request.name] = {}
        return StorageItem(
            id=request.name, name=request.name, url=f"fake://{request.name}", kind="bucket", region="fake-1"
        )

    def delete_storage(self, storage_id: str) -> StorageDeleted:
        if storage_id not in self.buckets:
            msg = f"No bucket called '{storage_id}'"
            raise NotFound(msg)
        del self.buckets[storage_id]
        return StorageDeleted(deleted=True, id=storage_id)

    def _bucket(self, url: str) -> tuple[str, str, dict[str, bytes]]:
        bucket, prefix = _split(url)
        objects = self.buckets.get(bucket)
        if objects is None:
            msg = f"No bucket called '{bucket}'"
            raise NotFound(msg)
        return bucket, prefix, objects

    def presign(self, request: PresignRequest) -> PresignResponse:
        bucket, prefix, _ = self._bucket(request.url)
        items = [
            {
                "path": str(f.get("path", "")),
                "key": f"{prefix}/{f.get('path', '')}".strip("/"),
                "url": f"fake://{bucket}/{prefix}/{f.get('path', '')}".replace("//", "/").replace("fake:/", "fake://"),
                "method": "PUT" if request.mode == "upload" else "GET",
            }
            for f in request.files
        ]
        if request.mode == "download":
            return PresignResponse(expires_s=3600, downloads=items, region="fake-1")
        return PresignResponse(expires_s=3600, uploads=items, region="fake-1")

    def list_objects(self, url: str, *, next_token: str = "") -> ObjectListing:
        bucket, prefix, objects = self._bucket(url)
        base = f"{prefix}/" if prefix else ""
        prefixes: dict[str, str] = {}
        files: list[dict[str, Any]] = []
        for key in sorted(objects):
            if not key.startswith(base):
                continue
            rest = key[len(base) :]
            if "/" in rest:
                folder = rest.split("/", 1)[0]
                prefixes.setdefault(folder, f"fake://{bucket}/{base}{folder}/")
            else:
                files.append({"key": key, "name": rest, "size": len(objects[key]), "url": f"fake://{bucket}/{key}"})
        return ObjectListing(
            url=f"fake://{bucket}/{base}",
            prefixes=[{"name": n, "url": u} for n, u in prefixes.items()],
            objects=files,
        )

    def delete_objects(self, request: DeleteObjectsRequest) -> dict[str, Any]:
        _, prefix, objects = self._bucket(request.url)
        base = f"{prefix}/" if prefix else ""
        doomed: list[str] = []
        for raw in request.paths:
            rel = f"{base}{raw.strip('/')}"
            if raw.endswith("/"):
                doomed.extend(k for k in objects if k.startswith(rel + "/"))
            elif rel in objects:
                doomed.append(rel)
        size = sum(len(objects[k]) for k in doomed)
        if not request.dry_run:
            for k in doomed:
                del objects[k]
        return {
            "deleted": 0 if request.dry_run else len(doomed),
            "count": len(doomed),
            "bytes": size,
            "dry_run": request.dry_run,
        }

    def _walk(self, url: str) -> Iterator[tuple[str, int]]:
        bucket, prefix, objects = self._bucket(url)
        base = f"{prefix}/" if prefix else ""
        for key in sorted(objects):
            if key.startswith(base):
                yield f"fake://{bucket}/{key}", len(objects[key])

    def transfer_registry(self, url: str) -> TransferRegistry:
        from tlc_plugin_sdk.shared.storage_transfer import TransferRegistry

        bucket, _, objects = self._bucket(url)
        registry = self._transfers.get(bucket)
        if registry is None:

            def head(u: str) -> int | None:
                _, key, _ = self._bucket(u)
                data = objects.get(key)
                return None if data is None else len(data)

            def copy(src: str, dst: str) -> None:
                _, src_key, _ = self._bucket(src)
                _, dst_key, dst_objects = self._bucket(dst)
                dst_objects[dst_key] = objects[src_key]

            def remove(u: str) -> None:
                _, key, _ = self._bucket(u)
                objects.pop(key, None)

            registry = TransferRegistry(
                list_objects=self._walk,
                head_object=head,
                copy_object=copy,
                delete_object=remove,
                notify_change=lambda _url, _op: None,
            )
            self._transfers[bucket] = registry
        return registry

    def bundle_registry(self, url: str) -> BundleRegistry:
        from tlc_plugin_sdk.shared.storage_bundle import BundleRegistry

        bucket, _, objects = self._bucket(url)
        registry = self._bundles.get(bucket)
        if registry is None:

            def listing(u: str) -> Iterator[tuple[str, int]]:
                for full, size in self._walk(u):
                    yield _split(full)[1], size

            registry = BundleRegistry(
                list_objects=listing,
                open_object=lambda key: io.BytesIO(objects[key]),
                store_bundle=lambda _path, name: f"fake://{bucket}/.downloads/{name}.zip",
            )
            self._bundles[bucket] = registry
        return registry

    # ── catalog ──

    def catalog(self, request: CatalogRequest) -> Catalog:
        location = request.location or self._default_location()
        if location not in _FAKE_LOCATIONS:
            return Catalog(location=location, error=f"'{location[:40]}' is not a fake location")
        offers = {
            "gpu_node": [
                Offer(
                    id="fake-gpu",
                    label="Fake GPU",
                    description="One fake GPU.",
                    vcpus=8,
                    memory_gb=32.0,
                    gpu_name="Fake T1000",
                    gpu_count=1,
                    gpu_memory_gb=24.0,
                    hourly=1.5,
                    hourly_spot=0.5,
                    available=True,
                    capacity="High",
                    recommended=True,
                ),
                Offer(
                    id="fake-gpu-xl",
                    label="Fake GPU XL",
                    vcpus=32,
                    memory_gb=128.0,
                    gpu_name="Fake T1000",
                    gpu_count=4,
                    gpu_memory_gb=24.0,
                    hourly=6.0,
                    available=False,
                    unavailable_reason="Your fake quota for this family is zero.",
                ),
            ],
            "cpu_node": [Offer(id="fake-cpu", label="Fake CPU", vcpus=4, memory_gb=16.0, hourly=0.2, available=True)],
            "deployment": [
                Offer(
                    id="fake-small", label="Small", vcpus=2, memory_gb=8.0, hourly=0.1, available=True, recommended=True
                ),
                Offer(id="fake-large", label="Large", vcpus=8, memory_gb=32.0, hourly=0.4, available=True),
            ],
        }
        return Catalog(offers=offers[request.purpose], location=location)

    def locations(self, *, node_type: str = "") -> Locations:
        return Locations(
            locations=[
                Location(id="fake-1", label="Fake region 1", available=True, capacity="High"),
                Location(id="fake-2", label="Fake region 2", available=True, capacity="Low"),
            ],
            node_type=node_type,
            default=self._default_location(),
        )

    # ── deployments ──

    @staticmethod
    def _address(deployment_id: str) -> str:
        return f"203.0.113.{sum(deployment_id.encode()) % 250 + 1}"

    def deployment_options(self, selected: dict[str, str]) -> DeploymentOptions:
        location = selected.get("location") or self._default_location()
        fields = [
            PlacementField(
                key="location",
                label="Location",
                choices=[PlacementChoice(id=loc, label=f"Fake region {loc[-1]}") for loc in _FAKE_LOCATIONS],
                default=self._default_location(),
            )
        ]
        blockers: list[str] = []
        if location not in _FAKE_LOCATIONS:
            blockers.append(f"'{location[:40]}' is not a fake location.")
        elif "location" in selected:
            fields.append(
                PlacementField(
                    key="network",
                    label="Network",
                    choices=[
                        PlacementChoice(id=f"net-{location}-a", label=f"Default network of {location}"),
                        PlacementChoice(id=f"net-{location}-b", label=f"Second network of {location}"),
                    ],
                    default=f"net-{location}-a",
                )
            )
        return DeploymentOptions(
            placement=fields,
            costs=[
                CostNote(label="Machine", description="Billed while the deployment runs.", hourly=0.1),
                CostNote(label="Shared ingress", description="One per network.", hourly=0.03, reused=False),
            ],
            warnings=[] if location != "fake-2" else ["Fake region 2 has low stock."],
            blockers=blockers,
        )

    def check_deployment(self, request: DeploymentCheckRequest) -> DeploymentCheck:
        location = request.placement.get("location") or self._default_location()
        checks = [PreflightCheck(name="quota", ok=True, detail="1 of 10 fake machines in use")]
        if len(request.label) > 40:
            return DeploymentCheck(
                ok=False, reason="The name is too long for the fake ingress (40 characters).", checks=checks
            )
        binding = current_connection()
        allowed = binding.permissions.storage if binding is not None and binding.permissions is not None else None
        root = request.storage.root.rstrip("/")
        if allowed is not None and root and not any(root.startswith(rule.location.rstrip("*")) for rule in allowed):
            checks.append(PreflightCheck(name="storage", ok=False, level="error", detail=root))
            return DeploymentCheck(ok=False, reason="The Connection may not reach the project root.", checks=checks)
        key = f"{location}-{request.zone.id}".lower() if self.shared_ingress and request.zone is not None else ""
        return DeploymentCheck(ok=True, ingress_key=key, checks=checks)

    def prepare_deployment(self, request: DeploymentPrepareRequest) -> DeploymentPrepared:
        if not request.namespace:
            return DeploymentPrepared(state="ready", detail="Nothing is shared outside a zone.")
        record = DnsRecord(name=f"_validate.{request.namespace}", type="TXT", values=[f"fake-{request.ingress_key}"])
        present = any(
            zone.get((record.name, record.type)) is not None
            and zone[(record.name, record.type)].values == record.values
            for zone in self.zones.values()
        )
        if present:
            return DeploymentPrepared(state="ready", detail="The shared certificate is issued.", dns_records=[record])
        return DeploymentPrepared(
            state="pending",
            detail="Waiting for the certificate's validation record.",
            dns_records=[record],
            retry_after_s=0.0,
        )

    def create_deployment(self, request: DeploymentRequest) -> DeploymentResponse:
        provider_id = f"fake-dep-{request.deployment_id}"
        existing = self.deployments.get(provider_id)
        if existing is not None and existing["state"] != "terminated":
            response: DeploymentResponse = existing["response"]
            return response
        location = request.placement.get("location") or self._default_location()
        if location not in _FAKE_LOCATIONS:
            msg = f"'{location[:40]}' is not a fake location"
            raise InvalidRequest(msg, remedy=f"Choose one of {', '.join(_FAKE_LOCATIONS)}.")
        address = self._address(request.deployment_id)
        if request.hostnames is not None:
            endpoints = {
                "object_service_url": f"https://{request.hostnames.object_service}",
                "compute_service_url": f"https://{request.hostnames.compute_service}",
            }
        else:
            endpoints = {
                "object_service_url": f"https://os.{address}.sslip.io",
                "compute_service_url": f"https://compute.{address}.sslip.io",
            }
        failure = "The fake machine did not start." if request.env.get("FAKE_FAIL") else ""
        response = DeploymentResponse(
            provider_id=provider_id,
            addresses=[Address(type="ipv4", value=address)],
            endpoints=endpoints,
            location=location,
            placement={**request.placement, "location": location},
            hourly_rate=0.4 if request.size == "fake-large" else 0.1,
            resource_ref=f"fake:deployment/{request.deployment_id}",
            failure=failure,
        )
        self.deployments[provider_id] = {
            "state": "exited" if failure else "running",
            "deployment_id": request.deployment_id,
            "name": request.name,
            "owner": request.owner,
            "size": request.size or "fake-small",
            "location": location,
            "address": address,
            "routed": True,
            "record_id": request.bootstrap.record_id,
            "hostnames": request.hostnames.to_dict() if request.hostnames is not None else None,
            "ingress_key": request.ingress_key,
            "storage": request.storage.to_dict(),
            "mode": request.mode,
            "tags": {"3lc": "deployment", "3lc-id": request.deployment_id, "3lc-owner": request.owner},
            "response": response,
            **self._acting(),
        }
        return response

    def deployment_state(self, provider_id: str, *, diagnostics: str = "") -> NodeStateResponse:
        record = self.deployments.get(provider_id)
        if record is None:
            return NodeStateResponse(state="gone", detail="no such deployment")
        state = NodeStateResponse(state=record["state"])
        if diagnostics:
            state.bootstrap_history = ["Machine started", "Services installed"]
            state.bootstrap_detail = "Services installed"
            state.bootstrap_stage = "services"
            state.bootstrap_failed = record["state"] == "exited"
        if diagnostics == "detailed":
            state.diagnostics = [{"source": "boot-log", "available": True, "lines": ["fake boot line"]}]
        return state

    def delete_deployment(self, provider_id: str) -> NodeStateResponse:
        record = self.deployments.get(provider_id)
        if record is None:
            return NodeStateResponse(state="gone", detail="no such deployment")
        record["state"] = "terminated"
        record["routed"] = False
        return NodeStateResponse(state="terminated")

    def retain_deployment(self, provider_id: str) -> NodeStateResponse:
        record = self.deployments.get(provider_id)
        if record is None:
            msg = f"No deployment {provider_id[:60]}"
            raise NotFound(msg)
        record["routed"] = False
        return NodeStateResponse(state=record["state"], detail="Traffic is no longer routed to the machine.")

    def list_deployments(self, *, owner: str = "") -> DeploymentListing:
        return DeploymentListing(
            deployments=[
                DeploymentInstance(
                    provider_id=pid,
                    deployment_id=d["deployment_id"],
                    name=d["name"],
                    owner=d["owner"],
                    size=d["size"],
                    state=d["state"],
                    location=d["location"],
                    addresses=[Address(type="ipv4", value=d["address"])],
                )
                for pid, d in self.deployments.items()
                if d["state"] != "terminated" and (not owner or d["owner"] == owner)
            ][:500]
        )

    def sweep_deployments(self, request: SweepRequest) -> SweepReport:
        keep = {item.deployment_id for item in request.keep}
        gone = {item.deployment_id for item in request.gone}
        released: list[str] = []
        held: list[str] = []
        pending: list[str] = []
        for pid, d in self.deployments.items():
            if d["state"] == "terminated":
                continue
            if d["deployment_id"] in keep:
                held.append(d["address"])
            elif d["deployment_id"] in gone or request.grace_s <= 0:
                d["state"] = "terminated"
                d["routed"] = False
                released.append(pid)
            else:
                pending.append(pid)
        if request.grace_s <= 0:
            released += self.leftovers
            self.leftovers = []
        else:
            pending += self.leftovers
        return SweepReport(complete=True, released=released, held_addresses=sorted(held), pending=pending)

    # ── DNS ──

    def list_records(self, zone: DnsZone, *, under: str = "", cursor: str = "") -> DnsRecords:
        suffix = under.lower().rstrip(".")
        records = [
            record
            for (name, _type), record in sorted(self.zones.get(zone.id, {}).items())
            if not suffix or name == suffix or name.endswith("." + suffix)
        ]
        try:
            start = int(cursor) if cursor else 0
        except ValueError:
            msg = "That cursor did not come from this listing"
            raise InvalidRequest(msg) from None
        page = records[start : start + _DNS_PAGE]
        more = start + _DNS_PAGE < len(records)
        return DnsRecords(records=page, next_cursor=str(start + _DNS_PAGE) if more else "")

    def change_records(self, change: DnsChange) -> None:
        zone = self.zones.setdefault(change.zone.id, {})
        suffix = change.zone.name.lower().rstrip(".")
        for record in [*change.create, *change.delete]:
            name = record.name.lower().rstrip(".")
            if suffix and name != suffix and not name.endswith("." + suffix):
                msg = f"{record.name[:80]} is not in the zone {change.zone.name[:80]}"
                raise InvalidRequest(msg)
        for record in change.create:
            existing = zone.get((record.name.lower().rstrip("."), record.type))
            if existing is not None and (existing.values != record.values or existing.ttl != record.ttl):
                msg = f"{record.type} {record.name[:80]} exists with other values"
                raise Conflict(msg, remedy="Choose another name, or remove the record that holds it.")
        for record in change.delete:
            key = (record.name.lower().rstrip("."), record.type)
            existing = zone.get(key)
            if existing is not None and existing.values == record.values and existing.ttl == record.ttl:
                del zone[key]
        for record in change.create:
            name = record.name.lower().rstrip(".")
            zone[(name, record.type)] = DnsRecord(
                name=name, type=record.type, values=list(record.values), ttl=record.ttl
            )

    # ── transitional: workspaces ──

    def list_workspaces(self, *, owner: str = "") -> WorkspaceListing:
        return WorkspaceListing(
            workspaces=[
                WorkspaceInstance(
                    provider_id=pid, name=pid, owner=n["owner"], instance_type=n["type"], state=n["state"]
                )
                for pid, n in self.nodes.items()
                if n["flavor"] == "workspace" and (not owner or n["owner"] == owner)
            ]
        )

    # ── end transitional ──


# ── CLI ────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """``python -m tlc_plugin_sdk.infrastructure.testing <plugin_dir> [options]``: print the report; exit 1 on failure.

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        0 when every check passed, 1 otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="python -m tlc_plugin_sdk.infrastructure.testing", description="Check a provider against the contract"
    )
    parser.add_argument("plugin_dir", help="Directory holding plugin.toml or pyproject.toml")
    parser.add_argument("--config-root", default=None, help="Settings root (default: a temporary directory)")
    parser.add_argument("--create-nodes", action="store_true", help="Run the node lifecycle checks")
    parser.add_argument("--create-deployments", action="store_true", help="Run the deployment lifecycle checks")
    parser.add_argument("--dns-zone", default="", metavar="ID=NAME", help="A zone the DNS checks may write under")
    parser.add_argument(
        "--live-storage", action="store_true", help="List objects and dry-run a transfer on real storage"
    )
    parser.add_argument("--node-type", default="", help="The node type to create and check")
    parser.add_argument(
        "--skip", action="append", default=[], metavar="GROUP", help=f"Skip a group: {', '.join(GROUPS)}"
    )
    parser.add_argument(
        "--header", action="append", default=[], metavar="NAME=VALUE", help="Request header (repeatable)"
    )
    args = parser.parse_args(argv)

    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    from tlc_plugin_sdk.harness import PluginHarness

    loaded = PluginHarness.from_manifest(args.plugin_dir)
    plugin = loaded.plugin
    if not isinstance(plugin, InfrastructurePlugin):
        print(f"{loaded.plugin_id} is not an InfrastructurePlugin", file=sys.stderr)
        return 1
    headers = dict(item.split("=", 1) for item in args.header)
    zone_id, _, zone_name = args.dns_zone.partition("=")
    report = check_provider(
        plugin,
        plugin_id=loaded.plugin_id,
        config_root=args.config_root,
        node_type=args.node_type,
        create_nodes=args.create_nodes,
        create_deployments=args.create_deployments,
        dns_zone=DnsZone(id=zone_id, name=zone_name) if zone_id and zone_name else None,
        live_storage=args.live_storage,
        skip=args.skip,
        headers=headers or None,
    )
    print(report.text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
