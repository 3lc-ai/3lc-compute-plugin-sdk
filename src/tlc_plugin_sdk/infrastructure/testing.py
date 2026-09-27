# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The conformance kit: drive a provider through its routes and say what disagrees with the contract.

:func:`check_provider` opens a :class:`~tlc_plugin_sdk.harness.PluginHarness` around the plugin
and runs one group of checks per surface the plugin implements; :func:`assert_conformant` raises
``AssertionError`` with the report when any check fails. In a provider repo's CI::

    def test_conformance(tmp_path):
        with patch_provider_seams():  # the repo's own fake for boto3 / runpod / az clients
            assert_conformant(AwsPlugin(), plugin_id="aws", config_root=tmp_path)

By default (``create_nodes=False``, ``live_storage=False``) the kit runs the shape, preflight,
errors, settings, routes, catalog-envelope and workspaces-envelope checks — nothing that needs
cloud credentials or a patched provider seam. ``create_nodes=True`` adds the node lifecycle
(create, state, diagnostics, delete twice); ``live_storage=True`` adds ``list_objects`` and a
transfer dry-run against the first listed storage. Settings are read and written under
``config_root`` — a temporary directory when none is given — so a run never touches
``~/.3lc-plugin-configs``.

Also a CLI: ``python -m tlc_plugin_sdk.infrastructure.testing <plugin_dir> [--config-root DIR]
[--create-nodes] [--live-storage] [--node-type T] [--skip GROUP]... [--header N=V]...``.

:class:`FakeProvider` is the SDK's reference implementation of every facet — in-memory nodes,
buckets, catalogs and workspaces — and the object the SDK's own lockstep tests drive.
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

from tlc_plugin_sdk.infrastructure.errors import InvalidRequest, NotFound
from tlc_plugin_sdk.infrastructure.facets import (
    CatalogFacet,
    LegacyOwnerCredentialsFacet,
    StorageFacet,
    WorkspaceFacet,
)
from tlc_plugin_sdk.infrastructure.plugin import InfrastructurePlugin
from tlc_plugin_sdk.infrastructure.types import (
    CapabilitiesResponse,
    CpuCatalog,
    CreateNodeRequest,
    CreateNodeResponse,
    CreateStorageRequest,
    Datacenters,
    DeleteObjectsRequest,
    GpuCatalog,
    NodeStateResponse,
    ObjectListing,
    PreflightResponse,
    PresignRequest,
    PresignResponse,
    Region,
    StorageCapabilities,
    StorageDeleted,
    StorageItem,
    StorageListing,
    WorkspaceInstance,
    WorkspaceListing,
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
    "FakeProvider",
    "FakeSettings",
    "assert_conformant",
    "check_provider",
    "main",
]

#: The check groups, each skippable by name.
GROUPS: tuple[str, ...] = (
    "shape",
    "lifecycle",
    "preflight",
    "errors",
    "settings",
    "storage",
    "catalog",
    "workspaces",
    "legacy",
    "routes",
)

_MADE_UP_ID = "deadbeef0000"


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
    facets: list[str]

    @property
    def ok(self) -> bool:
        """Whether every check passed."""
        return all(c.ok for c in self.checks)

    def failures(self) -> list[Check]:
        """The checks that failed."""
        return [c for c in self.checks if not c.ok]

    def text(self) -> str:
        """One line per check, for the CLI and assertion messages."""
        lines = [f"facets: {', '.join(self.facets) or 'none'}"]
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
        headers: dict[str, str] | None,
        config_root: Path,
        node_type: str,
        create_nodes: bool,
        live_storage: bool,
    ) -> None:
        self.plugin = plugin
        self.h = harness
        self.headers = headers
        self.config_root = config_root
        self.node_type = node_type
        self.create_nodes = create_nodes
        self.live_storage = live_storage
        self.checks: list[Check] = []
        self.responses: list[tuple[str, str, int, str]] = []
        self.caps: dict[str, Any] = {}

    # ── plumbing ──

    def call(self, method: str, path: str, *, json_body: Any = None) -> HarnessResponse:
        response = self.h.call(method, path, json_body=json_body, headers=self.headers)
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

    # ── groups ──

    def shape(self) -> None:
        plugin = self.plugin
        r = self.call("GET", "/infra/capabilities")
        if not self.check("shape: GET /infra/capabilities answers 200", r.status_code == 200, self.status_detail(r)):
            return
        d = self.body(r)
        self.caps = d
        caps = CapabilitiesResponse.from_dict(d)
        self.check(
            "shape: capabilities round-trip through CapabilitiesResponse",
            CapabilitiesResponse.from_dict(caps.to_dict()) == caps,
        )
        self.check(
            "shape: facets equals implemented_facets()",
            d.get("facets") == plugin.implemented_facets(),
            f"{d.get('facets')!r} != {plugin.implemented_facets()!r}",
        )
        self.check("shape: gpu_types equals node_types", d.get("gpu_types") == d.get("node_types"))
        self.check(
            "shape: flavors lists workspace iff WorkspaceFacet",
            ("workspace" in (d.get("flavors") or [])) == isinstance(plugin, WorkspaceFacet),
            f"flavors={d.get('flavors')!r}",
        )
        self.check(
            "shape: storage present iff StorageFacet",
            ("storage" in d) == isinstance(plugin, StorageFacet),
        )
        pricing = d.get("pricing")
        self.check(
            "shape: pricing, when present, is a non-empty list of strings",
            pricing is None
            or (isinstance(pricing, list) and bool(pricing) and all(isinstance(p, str) for p in pricing)),
            f"pricing={pricing!r}",
        )
        wanted = {"key", "label", "required", "secret", "help", "href", "placeholder"}
        fields_ok = all(isinstance(f, dict) and wanted <= set(f) for f in d.get("missing_fields") or [])
        self.check("shape: missing_fields entries carry the seven keys", fields_ok)

    def lifecycle(self) -> None:
        plugin = self.plugin
        types_ = self.caps.get("node_types") or []
        node_type = self.node_type or (str(types_[0]) if types_ else "")
        r = self.call("POST", "/infra/nodes", json_body={"node_type": node_type})
        self.check(
            "lifecycle: POST /infra/nodes without node_id/token answers 400",
            r.status_code == 400,
            self.status_detail(r),
        )
        body = CreateNodeRequest(node_id="conformance-1", node_type=node_type, token="conformance-token").to_dict()
        r = self.call("POST", "/infra/nodes", json_body=body)
        created = CreateNodeResponse.from_dict(self.body(r))
        if not self.check(
            "lifecycle: POST /infra/nodes answers 2xx", 200 <= r.status_code < 300, self.status_detail(r)
        ):
            return
        self.check("lifecycle: create answers provider_id", bool(created.provider_id), r.text[:200])
        self.check(
            "lifecycle: create answers agent_url or services",
            bool(created.agent_url or created.services),
            r.text[:200],
        )
        pid = created.provider_id
        r = self.call("GET", f"/infra/nodes/{pid}")
        state = NodeStateResponse.from_dict(self.body(r))
        self.check(
            "lifecycle: GET /infra/nodes/{id} answers 200 with a valid state",
            r.status_code == 200 and is_node_state(self.body(r).get("state")),
            self.status_detail(r),
        )
        del state
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
        with_creds = {**body, "node_id": "conformance-creds", "credentials": {"api_key": "conformance-secret-value"}}
        r = self.call("POST", "/infra/nodes", json_body=with_creds)
        if isinstance(plugin, LegacyOwnerCredentialsFacet):
            self.check("lifecycle: a credentials object on create is not a 5xx (legacy facet)", r.status_code < 500)
            if 200 <= r.status_code < 300:
                self.call("DELETE", f"/infra/nodes/{CreateNodeResponse.from_dict(self.body(r)).provider_id}")
        else:
            self.check(
                "lifecycle: a credentials object on create answers 400 without the legacy facet",
                r.status_code == 400,
                self.status_detail(r),
            )
        alias = {k: v for k, v in body.items() if k != "node_type"}
        alias["node_id"] = "conformance-2"
        r = self.call("POST", "/infra/nodes", json_body=alias)
        self.check(
            "lifecycle: POST /infra/nodes with gpu_type only creates as with node_type",
            200 <= r.status_code < 300,
            self.status_detail(r),
        )
        if 200 <= r.status_code < 300:
            self.call("DELETE", f"/infra/nodes/{CreateNodeResponse.from_dict(self.body(r)).provider_id}")

    def preflight(self) -> None:
        types_ = self.caps.get("node_types") or []
        node_type = self.node_type or (str(types_[0]) if types_ else "")
        if not node_type:
            self.check(
                "preflight: skipped",
                True,
                note="capabilities list no node_types and no --node-type was given; nothing to preflight",
            )
            return
        path = f"/infra/preflight?node_type={node_type}"
        r = self.call("GET", path)
        if not self.check("preflight: GET /infra/preflight answers 200", r.status_code == 200, self.status_detail(r)):
            return
        d = self.body(r)
        resp = PreflightResponse.from_dict(d)
        self.check(
            "preflight: round-trip through PreflightResponse", PreflightResponse.from_dict(resp.to_dict()) == resp
        )
        failed_errors = [c for c in resp.checks if not c.ok and c.level == "error"]
        agrees = (not resp.ok) if failed_errors else True
        if resp.ok is False and resp.checks and all(c.ok for c in resp.checks):
            agrees = False
        self.check(
            "preflight: ok agrees with the checks", agrees, f"ok={resp.ok} checks={[c.to_dict() for c in resp.checks]}"
        )

    def errors(self) -> None:
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
            if not (200 <= s < 300) and not _is_detail_body(t)
        ]
        self.check("errors: every non-2xx body is {detail: str}", not bad, "; ".join(bad[:5]))
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
        if not isinstance(self.plugin, CatalogFacet):
            return
        for path, key in (
            ("/infra/gpu-catalog", "gpus"),
            ("/infra/cpu-catalog", "sizes"),
            ("/infra/datacenters", "datacenters"),
        ):
            r = self.call("GET", path)
            d = self.body(r)
            self.check(
                f"catalog: GET {path} answers 200 with a {key} list and a string error",
                r.status_code == 200 and isinstance(d.get(key), list) and isinstance(d.get("error", ""), str),
                self.status_detail(r),
            )

    def workspaces(self) -> None:
        if not isinstance(self.plugin, WorkspaceFacet):
            return
        r = self.call("GET", "/infra/workspaces?owner=conformance")
        ok = r.status_code == 200 and isinstance(self.body(r).get("workspaces"), list)
        if ok:
            listing = WorkspaceListing.from_dict(self.body(r))
            ok = WorkspaceListing.from_dict(listing.to_dict()) == listing
        self.check("workspaces: GET /infra/workspaces round-trips WorkspaceListing", ok, self.status_detail(r))

    def legacy(self) -> None:
        if not isinstance(self.plugin, LegacyOwnerCredentialsFacet):
            return
        keys = self.caps.get("credential_keys")
        self.check("legacy: capabilities carry credential_keys", isinstance(keys, list) and bool(keys), f"{keys!r}")
        r = self.call("POST", "/infra/nodes/conformance-missing/terminate", json_body={})
        self.check(
            "legacy: POST /infra/nodes/{id}/terminate without credentials answers 400",
            r.status_code == 400,
            self.status_detail(r),
        )


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


def _is_detail_body(text: str) -> bool:
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and isinstance(data.get("detail"), str)


def check_provider(
    plugin: InfrastructurePlugin,
    *,
    plugin_id: str,
    config_root: str | Path | None = None,
    node_type: str = "",
    create_nodes: bool = False,
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
        node_type: The node type to create and preflight (default: the first in ``node_types``).
        create_nodes: Run the lifecycle checks (create, state, diagnostics, delete).
        live_storage: Run ``list_objects`` and a transfer dry-run against the first listed storage.
        skip: Group names (:data:`GROUPS`) not to run.
        headers: Headers to send on every call (an ``x-3lc-connection`` binding, say).

    Returns:
        The report.
    """
    skipped = set(skip)
    route_checks, duplicates = _route_checks(plugin) if "routes" not in skipped else ([], False)
    if duplicates:
        # Litestar refuses to build an app with two handlers on one method and path: nothing else can run.
        return ConformanceReport(checks=route_checks, facets=plugin.implemented_facets())
    with tempfile.TemporaryDirectory(prefix="tlc-conformance-") as scratch:
        root = Path(config_root) if config_root is not None else Path(scratch)
        return _run_checks(
            plugin,
            plugin_id=plugin_id,
            root=root,
            node_type=node_type,
            create_nodes=create_nodes,
            live_storage=live_storage,
            skipped=skipped,
            headers=headers,
            route_checks=route_checks,
        )


def _run_checks(
    plugin: InfrastructurePlugin,
    *,
    plugin_id: str,
    root: Path,
    node_type: str,
    create_nodes: bool,
    live_storage: bool,
    skipped: set[str],
    headers: dict[str, str] | None,
    route_checks: list[Check],
) -> ConformanceReport:
    from tlc_plugin_sdk.harness import PluginHarness

    with PluginHarness(plugin, plugin_id=plugin_id, config_root=root) as h:
        run = _Run(
            plugin,
            h,
            headers=headers,
            config_root=root,
            node_type=node_type,
            create_nodes=create_nodes,
            live_storage=live_storage,
        )
        if "shape" not in skipped:
            run.shape()
        else:
            run.caps = run.body(run.call("GET", "/infra/capabilities"))
        if create_nodes and "lifecycle" not in skipped:
            run.lifecycle()
        for group in ("preflight", "settings", "storage", "catalog", "workspaces", "legacy"):
            if group not in skipped:
                getattr(run, group)()
        if "errors" not in skipped:
            run.errors()
    return ConformanceReport(checks=[*run.checks, *route_checks], facets=plugin.implemented_facets())


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
    node_types: list[str] = option(default_factory=lambda: ["fake-gpu"], label="Node types")
    default_storage: str = "fake-data"
    extra_env: dict[str, str] = field(default_factory=dict)


_FAKE_URL = re.compile(r"^fake://([^/]+)/?(.*)$")


def _split(url: str) -> tuple[str, str]:
    """``("bucket", "prefix")`` of a ``fake://bucket/prefix`` URL."""
    m = _FAKE_URL.match(str(url or ""))
    if not m:
        msg = f"'{str(url)[:60]}' is not a fake:// URL"
        raise InvalidRequest(msg)
    return m.group(1), m.group(2).strip("/")


class FakeProvider(InfrastructurePlugin, StorageFacet, CatalogFacet, WorkspaceFacet):
    """Every facet, in memory: the SDK's reference provider and the object its lockstep tests drive.

    Nodes live in a dict; storage is one ``{key: bytes}`` per bucket under ``fake://<bucket>/``;
    transfers and bundles run the SDK engines over that dict; catalogs answer one static row each.
    """

    settings = PluginSettings(FakeSettings, "fake")

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.buckets: dict[str, dict[str, bytes]] = {
            "fake-data": {"train/a.jpg": b"a" * 10, "train/b.jpg": b"b" * 20, "readme.txt": b"hello"}
        }
        self._transfers: dict[str, TransferRegistry] = {}
        self._bundles: dict[str, BundleRegistry] = {}

    def get_ui_fragment(self) -> str:
        return "<div>fake provider</div>"

    # ── core ──

    def capabilities(self) -> CapabilitiesResponse:
        s = self.settings.load()
        return CapabilitiesResponse(
            provider="fake",
            node_types=list(s.node_types),
            flavors=["gpu", "workspace"],
            pricing=["on_demand"],
            region="fake-1",
            **self.settings.readiness(s),
        )

    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
        s = self.settings.load()
        if request.node_type not in s.node_types:
            msg = f"'{request.node_type[:40]}' is not a configured node type"
            raise InvalidRequest(msg)
        provider_id = f"fake-{request.node_id}"
        node: dict[str, Any] = {
            "state": "running",
            "flavor": request.flavor,
            "owner": request.owner,
            "type": request.node_type,
        }
        if request.flavor == "workspace":
            node["services"] = {
                "object_service_url": f"http://{provider_id}:5015",
                "compute_service_url": f"http://{provider_id}:5020",
            }
            self.nodes[provider_id] = node
            return CreateNodeResponse(
                provider_id=provider_id, services=node["services"], managed_by="creator", hourly_rate=0.1
            )
        self.nodes[provider_id] = node
        return CreateNodeResponse(
            provider_id=provider_id, agent_url=f"http://fake:{request.agent_port}", hourly_rate=1.5
        )

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

    def gpu_catalog(self, *, node_type: str = "", region: str = "") -> GpuCatalog:
        return GpuCatalog(gpus=[{"id": "fake-gpu", "name": "Fake GPU", "price": 1.5, "region": region or "fake-1"}])

    def cpu_catalog(self, *, region: str = "") -> CpuCatalog:
        return CpuCatalog(sizes=[{"id": "fake-cpu", "name": "Fake CPU", "price": 0.1}], region=region or "fake-1")

    def datacenters(self, *, node_type: str = "") -> Datacenters:
        return Datacenters(datacenters=[{"id": "fake-1", "name": "Fake region 1", "stock": "ok"}], node_type=node_type)

    # ── workspaces ──

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
    parser.add_argument(
        "--live-storage", action="store_true", help="List objects and dry-run a transfer on real storage"
    )
    parser.add_argument("--node-type", default="", help="The node type to create and preflight")
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
    report = check_provider(
        plugin,
        plugin_id=loaded.plugin_id,
        config_root=args.config_root,
        node_type=args.node_type,
        create_nodes=args.create_nodes,
        live_storage=args.live_storage,
        skip=args.skip,
        headers=headers or None,
    )
    print(report.text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
