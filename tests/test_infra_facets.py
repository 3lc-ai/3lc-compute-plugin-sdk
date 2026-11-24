# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The reference provider through the real worker app: every group of operations, as a host drives it."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk.connections import ConnectionBinding, ConnectionPermissions, ConnectionUse, SecretToken, StorageRule
from tlc_plugin_sdk.harness import PluginHarness
from tlc_plugin_sdk.infrastructure import (
    CapabilitiesResponse,
    Catalog,
    ConnectionNodeAccess,
    ConnectionVerification,
    CreateNodeRequest,
    CreateNodeResponse,
    DeploymentBootstrap,
    DeploymentCheck,
    DeploymentCheckRequest,
    DeploymentHostnames,
    DeploymentListing,
    DeploymentOptions,
    DeploymentPrepared,
    DeploymentPrepareRequest,
    DeploymentRequest,
    DeploymentResponse,
    DeploymentStorage,
    DnsChange,
    DnsRecord,
    DnsRecords,
    DnsZone,
    Grant,
    GrantObservation,
    GrantRequest,
    GrantUpdate,
    GrantUpdateRequest,
    InfrastructurePlugin,
    Locations,
    NodeAccessRequest,
    NodeStateResponse,
    StorageCandidates,
    StorageCapabilities,
    StorageFacet,
    StorageListing,
    StorageRoot,
    SweepItem,
    SweepReport,
    SweepRequest,
    VerifyRequest,
)
from tlc_plugin_sdk.infrastructure.testing import FakeProvider, connection_headers

_ZONE = DnsZone(id="Z1", name="deploy.example.com")
_PERMISSIONS = ConnectionPermissions(
    deployments=True, nodes=True, storage=[StorageRule("fake://fake-data/projects/", "read_write")]
)


def _binding(kind: Any = "KEYLESS", **access: str) -> ConnectionBinding:
    return ConnectionBinding(
        "conn-1",
        "fake",
        kind,
        account={"account_id": "123456789012"},
        access={"role_name": "3lc-fake", "node_identity": "3lc-fake-node", **access},
        defaults={"location": "fake-2"},
        permissions=_PERMISSIONS,
    )


_KEYLESS = connection_headers(_binding(), use=ConnectionUse("node-1", "ada"))
_AMBIENT = connection_headers(_binding("AMBIENT"))
_SECRET = connection_headers(_binding("SECRET"), credential=SecretToken("fake", "fake-api-key-1", "conn-1"))


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[tuple[FakeProvider, PluginHarness]]:
    plugin = FakeProvider()
    with PluginHarness(plugin, plugin_id="fake", config_root=tmp_path) as h:
        h.post("/settings", json_body={"api_key": "fake-settings-key"})
        yield plugin, h


# ── Connect an account ───────────────────────────────────────────────────────


def test_capabilities_declare_the_account_the_methods_and_what_is_offered(fake: Any) -> None:
    _, h = fake
    caps = CapabilitiesResponse.from_dict(h.get("/infra/capabilities").json())
    assert caps.provider == "fake" and caps.contract == 2 and caps.ready
    assert caps.account is not None and [f.key for f in caps.account.fields] == ["account_id"]
    assert [(m.kind, m.preferred, m.grants) for m in caps.access_methods] == [
        ("KEYLESS", True, True),
        ("AMBIENT", False, False),
        ("SECRET", False, False),
    ]
    assert caps.access_methods[1].purposes == ["nodes", "storage"], "an own sign-in cannot carry deployments"
    assert caps.host is not None and caps.host.matches == {"setup_identity": "fake-host-principal"}
    assert caps.nodes is not None and caps.deployments is not None and caps.storage is not None
    assert caps.storage == StorageCapabilities(kind="bucket", label="Fake bucket", default_id="fake-data")


def test_a_grant_is_described_without_a_connection_and_in_canonical_form(fake: Any) -> None:
    _, h = fake
    request = GrantRequest(
        connection_id="conn-1",
        kind="KEYLESS",
        account={"account_id": "123456789012"},
        access={"role_name": "team-role"},
        permissions=ConnectionPermissions(
            nodes=True,
            storage=[
                StorageRule("fake://b/z/", "read"),
                StorageRule("fake://b/a", "read_write"),
                StorageRule("fake://b/z", "read"),
            ],
        ),
    )
    grant = Grant.from_dict(h.post("/infra/connection/grant", json_body=request.to_dict()).json())
    assert grant.action == "create" and grant.link.startswith("https://") and "create" in grant.link
    assert [r.location for r in grant.permissions.storage] == ["fake://b/a", "fake://b/z"], (
        "normalised and deduplicated"
    )
    assert grant.access == {
        "role_name": "team-role",
        "node_identity": "team-role-node",
        "setup_identity": "fake-host-principal",
    }
    assert set(grant.creates) == {"nodes", "storage"} and grant.files and grant.command and grant.steps
    update = Grant.from_dict(
        h.post(
            "/infra/connection/grant",
            json_body={**request.to_dict(), "action": "update", "installed_ref": "fake:grant/conn-1"},
        ).json()
    )
    assert update.action == "update" and "update" in update.link and "create" not in update.link

    refused = h.post("/infra/connection/grant", json_body=request.to_dict(), headers=_KEYLESS)
    assert refused.status_code == 400, "a grant is described from the host's own identity, never under a Connection"
    assert h.post("/infra/connection/grant", json_body={**request.to_dict(), "kind": "WISHFUL"}).status_code == 400
    wrong = h.post("/infra/connection/grant", json_body={**request.to_dict(), "kind": "AMBIENT"})
    assert wrong.status_code == 400 and wrong.json()["remedy"]


def test_verification_says_who_the_connection_is_and_what_is_installed(fake: Any) -> None:
    _, h = fake
    body = VerifyRequest(
        storage_roots=[StorageRoot("fake://fake-data/p"), StorageRoot("fake://missing/p", "read", "scan")]
    )
    found = ConnectionVerification.from_dict(
        h.post("/infra/connection/verify", json_body=body.to_dict(), headers=_KEYLESS).json()
    )
    assert found.identity == "fake:keyless:123456789012" and found.account == {"account_id": "123456789012"}
    assert found.checked == ["resolve", "whoami"]
    assert [(c.ok, c.level) for c in found.checks] == [(True, "info"), (False, "warn")]
    assert found.observation is not None and found.observation.status == "complete"
    assert found.observation.permissions == _PERMISSIONS and found.observation.resource_ref == "fake:grant/conn-1"
    assert found.facts == {"Grant version": "1"}
    ambient = ConnectionVerification.from_dict(
        h.post("/infra/connection/verify", json_body={}, headers=_AMBIENT).json()
    )
    assert ambient.observation is None and ambient.identity.startswith("fake:ambient")
    assert h.post("/infra/connection/verify", json_body={}).status_code == 400, "there is nothing to verify without one"
    refused = h.post(
        "/infra/connection/verify", json_body={}, headers=connection_headers(_binding(role_name="refused"))
    )
    assert (
        refused.status_code == 424 and refused.json()["code"] == "credential_unavailable" and refused.json()["remedy"]
    )


def test_a_grant_update_is_prepared_under_the_connection(fake: Any) -> None:
    _, h = fake
    installed = GrantObservation(
        permissions=_PERMISSIONS, account={"account_id": "123456789012"}, resource_ref="r", status="complete"
    )
    same = GrantUpdateRequest(installed=installed, pending=_PERMISSIONS, revision=3, pending_revision=3)
    answer = GrantUpdate.from_dict(
        h.post("/infra/connection/grant/update", json_body=same.to_dict(), headers=_KEYLESS).json()
    )
    assert (answer.status, answer.review_url) == ("no_changes", "")
    wider = GrantUpdateRequest(
        installed=installed, pending=ConnectionPermissions(nodes=True), revision=3, pending_revision=4
    )
    answer = GrantUpdate.from_dict(
        h.post("/infra/connection/grant/update", json_body=wider.to_dict(), headers=_KEYLESS).json()
    )
    assert answer.status == "ready" and answer.review_url.endswith("/conn-1/changes/4") and answer.pending_revision == 4
    assert h.post("/infra/connection/grant/update", json_body=same.to_dict()).status_code == 400


def test_node_access_is_inspected_by_get_and_applied_only_by_post(fake: Any) -> None:
    plugin, h = fake
    roots = json.dumps([StorageRoot("fake://fake-data/p").to_dict()])
    seen = ConnectionNodeAccess.from_dict(
        h.get("/infra/connection/node-access", params={"storage_roots": roots}, headers=_AMBIENT).json()
    )
    assert (
        seen.supported
        and not seen.ready
        and seen.action_label
        and [c.name for c in seen.checks] == ["fake://fake-data/p"]
    )
    assert plugin.node_access["conn-1"]["ready"] is False, "inspection changes nothing"
    request = NodeAccessRequest(
        storage_roots=[StorageRoot("fake://fake-data/p")],
        storage_access={"rules": [{"location": "fake://x"}]},
        expected_revision="0",
    )
    applied = ConnectionNodeAccess.from_dict(
        h.post("/infra/connection/node-access", json_body=request.to_dict(), headers=_AMBIENT).json()
    )
    assert applied.ready and applied.revision == "1" and applied.storage_access == {"rules": [{"location": "fake://x"}]}
    stale = h.post("/infra/connection/node-access", json_body=request.to_dict(), headers=_AMBIENT)
    assert stale.status_code == 409 and stale.json()["code"] == "conflict"
    assert h.get("/infra/connection/node-access").status_code == 400
    assert h.get("/infra/connection/node-access", params={"storage_roots": "{}"}, headers=_AMBIENT).status_code == 400
    assert h.post("/infra/connection/node-access", json_body={}, headers=_AMBIENT).status_code == 400


def test_storage_candidates_are_names_under_the_connection(fake: Any) -> None:
    _, h = fake
    found = StorageCandidates.from_dict(h.get("/infra/storage/candidates", headers=_KEYLESS).json())
    assert [(c.location, c.label) for c in found.items] == [("fake://fake-data", "fake-data")] and not found.truncated
    assert h.get("/infra/storage/candidates").status_code == 400


# ── Nodes ────────────────────────────────────────────────────────────────────


def _node(node_id: str = "n1", **more: Any) -> dict[str, Any]:
    return CreateNodeRequest(
        node_id=node_id, node_type="fake-gpu", token="node-token-1", owner="ada", host_id="svc-1", **more
    ).to_dict()


def test_a_node_with_an_unknown_pricing_is_refused_not_launched(fake: Any) -> None:
    plugin, h = fake
    r = h.post("/infra/nodes", json_body={**_node(), "pricing": "reserved"}, headers=_KEYLESS)
    assert r.status_code == 400 and r.json()["code"] == "invalid_request" and "on_demand" in r.json()["remedy"]
    assert plugin.nodes == {}


@pytest.mark.parametrize(
    ("headers", "credential", "storage_access", "identity"),
    [
        (_KEYLESS, "FakeCredential", "identity", "3lc-fake-node"),
        (_AMBIENT, "Ambient", "ambient", ""),
        (_SECRET, "SecretToken", "none", ""),
        (None, "", "none", ""),
    ],
)
def test_a_node_is_created_described_and_deleted_with_each_kind_of_connection(
    fake: Any, headers: dict[str, str] | None, credential: str, storage_access: str, identity: str
) -> None:
    plugin, h = fake
    r = h.post("/infra/nodes", json_body=_node(storage_roots=[StorageRoot("fake://fake-data/p")]), headers=headers)
    assert r.status_code == 201, r.text
    created = CreateNodeResponse.from_dict(r.json())
    assert created.provider_id == "fake-n1" and created.agent_url == "http://fake:8800"
    assert (created.storage_access, created.identity) == (storage_access, identity)
    record = plugin.nodes["fake-n1"]
    assert record["credential"] == credential and record["connection"] == ("conn-1" if headers else "")
    assert record["kind"] == "gpu", "the node kind is not overwritten by the Connection's kind"
    kinds = {"FakeCredential": "KEYLESS", "Ambient": "AMBIENT", "SecretToken": "SECRET"}
    assert record["connection_kind"] == kinds.get(credential, "")
    assert record["tags"] == {"3lc": "node", "3lc-id": "n1", "3lc-owner": "ada", "3lc-host": "svc-1"}
    assert record["location"] == ("fake-2" if headers else "fake-1"), (
        "the Connection's default location, else the plugin's"
    )
    assert h.get("/infra/nodes/fake-n1", headers=headers).json() == {"state": "running"}
    diagnosed = NodeStateResponse.from_dict(h.get("/infra/nodes/fake-n1?diagnostics=true", headers=headers).json())
    assert diagnosed.bootstrap_history == ["Starting node agent"] and diagnosed.bootstrap_failed is False
    assert h.call("DELETE", "/infra/nodes/fake-n1", headers=headers).json() == {"state": "terminated"}
    assert h.call("DELETE", "/infra/nodes/fake-n1", headers=headers).json() == {"state": "terminated"}
    assert h.get("/infra/nodes/never-made", headers=headers).json()["state"] == "gone"


def test_create_is_idempotent_on_the_node_id(fake: Any) -> None:
    plugin, h = fake
    first = h.post("/infra/nodes", json_body=_node(pricing="spot", location="fake-1"), headers=_KEYLESS).json()
    again = h.post("/infra/nodes", json_body=_node(pricing="on_demand", location="fake-2"), headers=_KEYLESS).json()
    assert again == first and len(plugin.nodes) == 1, "a repeat returns what the first call created"
    assert first["pricing"] == "spot" and first["hourly_rate"] == 0.5 and first["location"] == "fake-1"
    assert plugin.nodes["fake-n1"]["resource_id"] == "node-1" and plugin.nodes["fake-n1"]["source_identity"] == "ada"


def test_a_node_request_names_one_location_and_bad_input_says_what_to_change(fake: Any) -> None:
    _, h = fake
    r = h.post("/infra/nodes", json_body=_node(location="mars-1"))
    assert r.status_code == 400 and r.json()["code"] == "invalid_request" and "fake-1" in r.json()["remedy"]
    r = h.post("/infra/nodes", json_body={**_node(), "node_type": "node-token-1 is not a type"})
    assert r.status_code == 400 and "node-token-1" not in r.text, "the node token is scrubbed from an error"
    check = h.post("/infra/nodes/check", json_body={"node_type": "fake-gpu", "location": "mars-1"}).json()
    assert check["ok"] is False and [c["ok"] for c in check["checks"]] == [True, False]
    assert h.post("/infra/nodes/check", json_body={}).json()["ok"] is True


# ── Catalog ──────────────────────────────────────────────────────────────────


def test_one_catalog_answers_every_purpose_with_typed_offers(fake: Any) -> None:
    _, h = fake
    gpu = Catalog.from_dict(h.post("/infra/catalog", json_body={"purpose": "gpu_node"}, headers=_KEYLESS).json())
    assert gpu.location == "fake-2", "priced in the Connection's default location"
    assert [(o.id, o.available) for o in gpu.offers] == [("fake-gpu", True), ("fake-gpu-xl", False)]
    assert gpu.offers[1].unavailable_reason and gpu.offers[0].hourly_spot == 0.5
    sizes = Catalog.from_dict(
        h.post("/infra/catalog", json_body={"purpose": "deployment", "location": "fake-1"}).json()
    )
    assert [o.id for o in sizes.offers] == ["fake-small", "fake-large"] and sizes.location == "fake-1"
    failed = Catalog.from_dict(h.post("/infra/catalog", json_body={"purpose": "cpu_node", "location": "mars-1"}).json())
    assert failed.offers == [] and failed.error, "a failed lookup is an empty list with the reason"
    assert h.post("/infra/catalog", json_body={"purpose": "tapes"}).status_code == 400
    places = Locations.from_dict(h.get("/infra/catalog/locations?node_type=fake-gpu").json())
    assert [p.id for p in places.locations] == ["fake-1", "fake-2"] and places.node_type == "fake-gpu"


# ── Storage ──────────────────────────────────────────────────────────────────


def test_every_storage_route_acts_through_a_connection(fake: Any) -> None:
    _, h = fake
    for method, path, body in (
        ("GET", "/infra/storage", None),
        ("POST", "/infra/storage", {"name": "x"}),
        ("DELETE", "/infra/storage/x", None),
        ("POST", "/infra/storage/presign", {"url": "fake://fake-data", "files": [{"path": "a"}]}),
        ("GET", "/infra/storage/list?url=fake://fake-data", None),
        ("POST", "/infra/storage/delete", {"url": "fake://fake-data", "paths": ["a"]}),
        (
            "POST",
            "/infra/storage/transfer",
            {"src_url": "fake://fake-data", "dst_url": "fake://fake-data", "items": ["a"]},
        ),
        ("GET", "/infra/storage/transfer/deadbeef0000", None),
        ("DELETE", "/infra/storage/transfer/deadbeef0000", None),
        ("POST", "/infra/storage/bundle", {"url": "fake://fake-data"}),
        ("GET", "/infra/storage/bundle/deadbeef0000", None),
        ("DELETE", "/infra/storage/bundle/deadbeef0000", None),
    ):
        r = h.call(method, path, json_body=body)
        assert r.status_code == 400 and r.json()["code"] == "invalid_request", (method, path, r.text)


def test_storage_listing_browse_transfer_and_bundle(fake: Any) -> None:
    plugin, h = fake
    listing = StorageListing.from_dict(h.get("/infra/storage", headers=_KEYLESS).json())
    assert [(i.id, i.url, i.default) for i in listing.storage] == [("fake-data", "fake://fake-data", True)]
    assert h.post("/infra/storage", json_body={"name": "fresh"}, headers=_KEYLESS).status_code == 201
    assert h.call("DELETE", "/infra/storage/fresh", headers=_KEYLESS).json() == {"deleted": True, "id": "fresh"}
    assert h.call("DELETE", "/infra/storage/fresh", headers=_KEYLESS).status_code == 404
    level = h.get("/infra/storage/list?url=fake://fake-data", headers=_KEYLESS).json()
    assert [p["name"] for p in level["prefixes"]] == ["train"] and [o["name"] for o in level["objects"]] == [
        "readme.txt"
    ]
    body = {"src_url": "fake://fake-data/train", "dst_url": "fake://fake-data/copy", "items": ["a.jpg"]}
    started = h.post("/infra/storage/transfer", json_body=body, headers=_KEYLESS).json()
    deadline = time.time() + 5
    while h.get(f"/infra/storage/transfer/{started['transfer_id']}", headers=_KEYLESS).json()["state"] not in (
        "done",
        "failed",
    ):
        assert time.time() < deadline
        time.sleep(0.01)
    assert plugin.buckets["fake-data"]["copy/a.jpg"] == b"a" * 10
    bundle = h.post("/infra/storage/bundle", json_body={"url": "fake://fake-data/train"}, headers=_KEYLESS).json()
    assert h.get(f"/infra/storage/bundle/{bundle['bundle_id']}", headers=_KEYLESS).status_code == 200
    assert h.get("/infra/storage/transfer/not-an-id", headers=_KEYLESS).status_code == 400


def test_a_storage_method_left_at_its_default_answers_501(tmp_path: Path) -> None:
    class ListOnly(InfrastructurePlugin, StorageFacet):
        def get_ui_fragment(self) -> str:
            return ""

        def capabilities(self) -> CapabilitiesResponse:
            return CapabilitiesResponse(provider="listonly", display_name="List only", ready=True)

        def storage_capabilities(self) -> StorageCapabilities:
            return StorageCapabilities(kind="bucket", label="B", creatable=False, transfer=False)

        def list_storage(self, *, fallback_url: str = "") -> StorageListing:
            return StorageListing(capabilities=self.storage_capabilities())

    headers = connection_headers(ConnectionBinding("c", "listonly", "AMBIENT"))
    with PluginHarness(ListOnly(), plugin_id="listonly", config_root=tmp_path) as h:
        assert h.get("/infra/capabilities").json()["operations"] == ["storage"]
        r = h.post("/infra/storage", json_body={"name": "x"}, headers=headers)
        assert r.status_code == 501 and r.json()["code"] == "not_supported"


# ── Deployments and the launcher's zone ──────────────────────────────────────


def _records(h: PluginHarness, under: str = "") -> list[DnsRecord]:
    found: list[DnsRecord] = []
    cursor = ""
    while True:
        page = DnsRecords.from_dict(
            h.get(
                "/infra/dns/records",
                params={"zone_id": _ZONE.id, "zone_name": _ZONE.name, "under": under, "cursor": cursor},
            ).json()
        )
        found += page.records
        cursor = page.next_cursor
        if not cursor:
            return found


def _change(h: PluginHarness, create: list[DnsRecord], delete: list[DnsRecord] | None = None) -> Any:
    return h.post("/infra/dns/changes", json_body=DnsChange(zone=_ZONE, create=create, delete=delete or []).to_dict())


def test_a_launch_runs_options_check_prepare_create_with_the_launcher_writing_the_zone(fake: Any) -> None:
    plugin, h = fake
    first = DeploymentOptions.from_dict(
        h.post("/infra/deployments/options", json_body={"selected": {}}, headers=_KEYLESS).json()
    )
    assert [f.key for f in first.placement] == ["location"] and first.placement[0].default == "fake-2"
    narrowed = DeploymentOptions.from_dict(
        h.post("/infra/deployments/options", json_body={"selected": {"location": "fake-1"}}, headers=_KEYLESS).json()
    )
    assert [f.key for f in narrowed.placement] == ["location", "network"], "choices follow what is selected so far"
    assert [c.id for c in narrowed.placement[1].choices] == ["net-fake-1-a", "net-fake-1-b"] and narrowed.costs
    placement = {"location": "fake-1", "network": "net-fake-1-a"}

    storage = DeploymentStorage(root="fake://fake-data/projects/ada")
    request = DeploymentCheckRequest(label="ada", placement=placement, storage=storage, zone=_ZONE)
    check = DeploymentCheck.from_dict(
        h.post("/infra/deployments/check", json_body=request.to_dict(), headers=_KEYLESS).json()
    )
    assert check.ok and check.ingress_key == "fake-1-z1", "opaque to the launcher; it carries the zone it was asked in"
    outside = DeploymentCheckRequest(
        label="ada", placement=placement, storage=DeploymentStorage(root="fake://other/x"), zone=_ZONE
    )
    refused = DeploymentCheck.from_dict(
        h.post("/infra/deployments/check", json_body=outside.to_dict(), headers=_KEYLESS).json()
    )
    assert not refused.ok and "project root" in refused.reason, "the Connection's storage rules bound the root"
    by_address = DeploymentCheckRequest(label="ada", placement=placement)
    assert (
        DeploymentCheck.from_dict(
            h.post("/infra/deployments/check", json_body=by_address.to_dict(), headers=_KEYLESS).json()
        ).ingress_key
        == ""
    )

    namespace = f"i-{check.ingress_key}.{_ZONE.name}"
    prepare = DeploymentPrepareRequest(
        deployment_id="dep-1", placement=placement, namespace=namespace, ingress_key=check.ingress_key
    )
    pending = DeploymentPrepared.from_dict(
        h.post("/infra/deployments/prepare", json_body=prepare.to_dict(), headers=_KEYLESS).json()
    )
    assert pending.state == "pending" and [r.name for r in pending.dns_records] == [f"_validate.{namespace}"]
    assert all(r.name.endswith("." + namespace) for r in pending.dns_records)
    assert _change(h, pending.dns_records).status_code == 200, "the launcher writes the records with its own identity"
    ready = DeploymentPrepared.from_dict(
        h.post("/infra/deployments/prepare", json_body=prepare.to_dict(), headers=_KEYLESS).json()
    )
    assert ready.state == "ready"

    hostnames = DeploymentHostnames(
        hostname="",
        object_service=f"os-ada.{namespace}",
        compute_service=f"compute-ada.{namespace}",
        namespace=namespace,
    )
    create = DeploymentRequest(
        deployment_id="dep-1",
        name="Ada",
        bootstrap=DeploymentBootstrap(
            record_id="rec-1", config_service_url="https://cs.example", token="bootstrap-secret-1"
        ),
        hostnames=hostnames,
        ingress_key=check.ingress_key,
        placement=placement,
        storage=storage,
        owner="ada",
    )
    r = h.post("/infra/deployments", json_body=create.to_dict(), headers=_KEYLESS)
    assert r.status_code == 201 and "bootstrap-secret-1" not in r.text
    created = DeploymentResponse.from_dict(r.json())
    assert (
        created.provider_id == "fake-dep-dep-1"
        and created.resource_ref == "fake:deployment/dep-1"
        and not created.failure
    )
    assert created.endpoints == {
        "object_service_url": f"https://os-ada.{namespace}",
        "compute_service_url": f"https://compute-ada.{namespace}",
    }
    assert created.location == "fake-1" and [a.type for a in created.addresses] == ["ipv4"]
    again = DeploymentResponse.from_dict(
        h.post("/infra/deployments", json_body=create.to_dict(), headers=_KEYLESS).json()
    )
    assert again == created and len(plugin.deployments) == 1, "create is idempotent on the deployment id"
    assert plugin.deployments["fake-dep-dep-1"]["tags"]["3lc-id"] == "dep-1"
    assert plugin.deployments["fake-dep-dep-1"]["credential"] == "FakeCredential"
    assert plugin.deployments["fake-dep-dep-1"]["connection_kind"] == "KEYLESS"

    address = created.addresses[0].value
    names = [
        DnsRecord(name=host, type="A", values=[address])
        for host in (hostnames.object_service, hostnames.compute_service)
    ]
    owner = DnsRecord(name=f"_owner-ada.{namespace}", type="TXT", values=["digest-of-launcher-and-dep-1"])
    assert _change(h, [*names, owner]).status_code == 200
    assert {r.name for r in _records(h, under=namespace)} == {n.name for n in [*names, owner, *pending.dns_records]}

    state = NodeStateResponse.from_dict(
        h.get("/infra/deployments/fake-dep-dep-1?diagnostics=baseline", headers=_KEYLESS).json()
    )
    assert state.state == "running" and state.bootstrap_history and not state.diagnostics
    detailed = h.get("/infra/deployments/fake-dep-dep-1?diagnostics=detailed", headers=_KEYLESS).json()
    assert detailed["diagnostics"], "the separately granted collector"
    assert h.get("/infra/deployments/fake-dep-dep-1?diagnostics=everything", headers=_KEYLESS).status_code == 400
    listing = DeploymentListing.from_dict(h.get("/infra/deployments?owner=ada").json())
    assert [(d.provider_id, d.deployment_id, d.state) for d in listing.deployments] == [
        ("fake-dep-dep-1", "dep-1", "running")
    ]
    assert h.get("/infra/deployments?owner=someone-else").json() == {"deployments": []}

    plugin.leftovers.append("fake:address/203.0.113.250")
    kept = SweepRequest(keep=[SweepItem("dep-1", "fake-dep-dep-1")])
    report = SweepReport.from_dict(h.post("/infra/deployments/sweep", json_body=kept.to_dict()).json())
    assert report.complete and report.held_addresses == [address] and report.released == []
    assert report.pending == ["fake:address/203.0.113.250"], "inside the grace period"

    assert _change(h, [], [*names, owner]).status_code == 200, "the launcher removes its records first"
    assert h.call("DELETE", "/infra/deployments/fake-dep-dep-1", headers=_KEYLESS).json() == {"state": "terminated"}
    assert h.call("DELETE", "/infra/deployments/fake-dep-dep-1", headers=_KEYLESS).json() == {"state": "terminated"}
    assert [r.name for r in _records(h, under=namespace)] == [f"_validate.{namespace}"], "shared records stay"
    gone = SweepRequest(gone=[SweepItem("dep-1")], grace_s=0)
    report = SweepReport.from_dict(h.post("/infra/deployments/sweep", json_body=gone.to_dict()).json())
    assert report.released == ["fake:address/203.0.113.250"] and report.held_addresses == []


def test_without_a_zone_a_deployment_is_named_by_its_address(fake: Any) -> None:
    _, h = fake
    create = DeploymentRequest(
        deployment_id="dep-2",
        name="B",
        bootstrap=DeploymentBootstrap(record_id="rec-2", config_service_url="https://cs"),
    )
    created = DeploymentResponse.from_dict(
        h.post("/infra/deployments", json_body=create.to_dict(), headers=_KEYLESS).json()
    )
    address = created.addresses[0].value
    assert created.endpoints == {
        "object_service_url": f"https://os.{address}.sslip.io",
        "compute_service_url": f"https://compute.{address}.sslip.io",
    }
    prepared = h.post("/infra/deployments/prepare", json_body={"deployment_id": "dep-2"}, headers=_KEYLESS).json()
    assert prepared["state"] == "ready" and prepared["dns_records"] == []


def test_a_failed_launch_reports_failure_and_can_be_retained(fake: Any) -> None:
    plugin, h = fake
    create = DeploymentRequest(
        deployment_id="dep-3",
        name="C",
        bootstrap=DeploymentBootstrap(record_id="rec-3", config_service_url="https://cs"),
        env={"FAKE_FAIL": "1"},
    )
    created = DeploymentResponse.from_dict(
        h.post("/infra/deployments", json_body=create.to_dict(), headers=_KEYLESS).json()
    )
    assert created.failure and created.provider_id, "resources exist and the launch did not complete"
    retained = h.post("/infra/deployments/fake-dep-dep-3/retain", headers=_KEYLESS)
    assert retained.status_code == 200 and plugin.deployments["fake-dep-dep-3"]["routed"] is False
    assert plugin.deployments["fake-dep-dep-3"]["state"] == "exited", "the machine is kept"
    missing = h.post("/infra/deployments/never-made/retain", headers=_KEYLESS)
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"


def test_every_deployment_call_but_list_and_sweep_needs_a_connection(fake: Any) -> None:
    _, h = fake
    for method, path, body in (
        ("POST", "/infra/deployments/options", {}),
        ("POST", "/infra/deployments/check", {"label": "a"}),
        ("POST", "/infra/deployments/prepare", {"deployment_id": "d"}),
        ("POST", "/infra/deployments", {"deployment_id": "d", "bootstrap": {"record_id": "r"}}),
        ("GET", "/infra/deployments/x", None),
        ("DELETE", "/infra/deployments/x", None),
        ("POST", "/infra/deployments/x/retain", None),
    ):
        assert h.call(method, path, json_body=body).status_code == 400, (method, path)
    assert h.get("/infra/deployments").status_code == 200
    assert h.post("/infra/deployments/sweep", json_body={}).status_code == 200
    assert h.post("/infra/deployments", json_body={"name": "no id"}, headers=_KEYLESS).status_code == 400


def test_a_dns_change_is_atomic_and_two_launches_cannot_both_win_a_name(fake: Any) -> None:
    _, h = fake
    mine = DnsRecord(name="os.ada.deploy.example.com", type="A", values=["203.0.113.7"])
    assert _change(h, [mine]).json() == {}
    assert _change(h, [mine]).status_code == 200, "the same name with the same values succeeds"
    other = DnsRecord(name="compute.bob.deploy.example.com", type="A", values=["203.0.113.8"])
    theirs = DnsRecord(name=mine.name, type="A", values=["203.0.113.9"])
    lost = _change(h, [other, theirs])
    assert lost.status_code == 409 and lost.json()["code"] == "conflict" and lost.json()["remedy"]
    assert _records(h) == [mine], "nothing of a refused change is applied"
    assert _change(h, [], [theirs]).status_code == 200 and _records(h) == [mine], "a delete removes only an exact match"
    assert _change(h, [], [mine]).status_code == 200 and _records(h) == []
    assert _change(h, [], [mine]).status_code == 200, "a missing record is success"


def test_dns_listing_pages_and_changes_are_bounded_and_never_see_a_connection(fake: Any) -> None:
    _, h = fake
    many = [DnsRecord(name=f"n{i:03d}.many.deploy.example.com", type="TXT", values=[str(i)]) for i in range(350)]
    for start in range(0, 350, 100):
        assert _change(h, many[start : start + 100]).status_code == 200
    first = DnsRecords.from_dict(
        h.get("/infra/dns/records", params={"zone_id": _ZONE.id, "zone_name": _ZONE.name}).json()
    )
    assert len(first.records) == 300 and first.next_cursor
    assert len(_records(h, under="many.deploy.example.com")) == 350
    assert _records(h, under="other.deploy.example.com") == []
    assert _change(h, many[:101]).status_code == 400, "one change holds at most 100 records"
    mx = {"zone": _ZONE.to_dict(), "create": [{"name": "a.deploy.example.com", "type": "MX", "values": ["x"]}]}
    assert h.post("/infra/dns/changes", json_body=mx).status_code == 400
    elsewhere = DnsRecord(name="a.elsewhere.example.org", type="A", values=["203.0.113.1"])
    assert _change(h, [elsewhere]).status_code == 400
    assert h.get("/infra/dns/records").status_code == 400
    assert h.get("/infra/dns/records", params={"zone_id": "Z1"}, headers=_KEYLESS).status_code == 400
    assert h.post("/infra/dns/changes", json_body=DnsChange(zone=_ZONE).to_dict(), headers=_AMBIENT).status_code == 400
