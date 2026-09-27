# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The typed wire: every dataclass round-trips, emits a frozen key set, and re-emits the providers' recorded answers.

The golden fixtures under ``tests/fixtures/wire/`` are answers recorded from the runpod, aws and
azure plugins' route code. Each must parse with ``from_dict`` and re-emit through ``to_dict``
with exactly the recorded top-level key set, minus the keys the fixture marks as provider
passthrough (``extra``-style keys the typed layer never fills) and plus the keys the typed layer
adds (``facets``; the ``node_types`` side of the ``gpu_types`` alias). Nested items must keep
every recorded key (an item may gain a typed key such as ``required``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk.infrastructure import (
    BundleRequest,
    CapabilitiesResponse,
    CpuCatalog,
    CreateNodeRequest,
    CreateNodeResponse,
    CreateStorageRequest,
    Datacenters,
    DeleteObjectsRequest,
    GpuCatalog,
    LoginDescriptor,
    NodeStateResponse,
    ObjectListing,
    OwnerCredentialsDescriptor,
    PreflightCheck,
    PreflightResponse,
    PresignRequest,
    PresignResponse,
    ProjectStorage,
    Region,
    RoleDescriptor,
    SettingsField,
    StorageCapabilities,
    StorageDeleted,
    StorageItem,
    StorageListing,
    TransferRequest,
    WorkspaceInstance,
    WorkspaceListing,
    WorkspaceRequest,
    preflight_query,
)
from tlc_plugin_sdk.infrastructure.legacy import strip_request_credentials

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "wire").glob("*.json"))

# ── Round trips and frozen key sets ──────────────────────────────────────────

_FIELD = SettingsField(key="api_key", label="API key", secret=True, help="h", href="u", placeholder="p")
_CAPS = StorageCapabilities(kind="bucket", label="Bucket", upload=False, default_id="d", upload_hint="add keys")

ROUND_TRIPS: list[tuple[Any, set[str]]] = [
    (_FIELD, {"key", "label", "required", "secret", "help", "href", "placeholder"}),
    (ProjectStorage("s3://root", ["s3://a"]), {"project_root_url", "project_scan_urls"}),
    (
        WorkspaceRequest("s3://root", "full", True, "ws", ["src"], ["b"], True, True),
        {
            "project_root_url",
            "mode",
            "public",
            "name",
            "bootstrap_plugins",
            "data_buckets",
            "create_bucket",
            "seed_provider_configs",
        },
    ),
    (
        CreateNodeRequest(
            node_id="n1",
            node_type="A100",
            token="tok",
            env={"K": "V"},
            agent_port=9900,
            ports=[8888],
            idle_ttl_s=600.0,
            flavor="workspace",
            owner="me",
            pricing="spot",
            compute_spec="3lc-compute==1.2",
            wheelhouse="/w",
            project_storage=ProjectStorage("s3://root", []),
            storage_id="vol",
            workspace=WorkspaceRequest(project_root_url="s3://root", name="ws"),
        ),
        {
            "node_id",
            "node_type",
            "gpu_type",
            "token",
            "env",
            "agent_port",
            "ports",
            "idle_ttl_s",
            "flavor",
            "workspace",
            "owner",
            "storage_id",
            "project_storage",
            "compute_spec",
            "wheelhouse",
            "pricing",
        },
    ),
    (
        CreateNodeResponse("p", "http://a", "t", "spot", "d", 1.5, {"object_service_url": "u"}, "owner"),
        {"provider_id", "agent_url", "token", "pricing", "detail", "hourly_rate", "services", "managed_by"},
    ),
    (
        NodeStateResponse("pending", "d", ["a"], "b", False),
        {"state", "detail", "bootstrap_history", "bootstrap_detail", "bootstrap_failed"},
    ),
    (PreflightResponse(False, [PreflightCheck("k", False, "error", "d")], "s"), {"ok", "checks", "summary"}),
    (
        _CAPS,
        {
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
            "default_id",
            "upload_hint",
        },
    ),
    (
        CapabilitiesResponse(
            "p",
            ["a"],
            ["gpu", "workspace"],
            True,
            ["k"],
            [_FIELD],
            ["on_demand"],
            "Machine",
            "eu",
            [_FIELD],
            _CAPS,
            ["storage"],
            True,
        ),
        {
            "provider",
            "node_types",
            "gpu_types",
            "flavors",
            "ready",
            "missing",
            "missing_fields",
            "pricing",
            "node_type_label",
            "region",
            "workspace_fields",
            "storage",
            "facets",
            "lists_workspaces",
        },
    ),
    (
        StorageItem("b", "b", "s3://b", "bucket", "eu", 10, True, "owner"),
        {"id", "name", "url", "kind", "region", "size_gb", "default", "managed_by"},
    ),
    (Region("r", "R", "Loc"), {"id", "name", "location"}),
    (
        StorageListing(_CAPS, [StorageItem("b")], [Region("r")], "acct", "eu"),
        {*_CAPS.to_dict(), "storage", "regions", "account", "region"},
    ),
    (CreateStorageRequest("n", "r", 5, True, "me"), {"name", "region", "size_gb", "make_default", "owner"}),
    (StorageDeleted(True, "b"), {"deleted", "id"}),
    (PresignRequest("s3://b", [{"path": "a"}], "download"), {"url", "files", "mode"}),
    (PresignResponse(60, [{"k": 1}], [], [{"r": 1}], "eu"), {"expires_s", "uploads", "downloads", "refused", "region"}),
    (PresignResponse(60, [], [{"k": 1}], [], "eu"), {"expires_s", "uploads", "downloads", "refused", "region"}),
    (
        ObjectListing("s3://b/", [{"name": "d"}], [{"key": "k"}], True, "tok"),
        {"url", "prefixes", "objects", "truncated", "next_token"},
    ),
    (DeleteObjectsRequest("s3://b", ["a"], True), {"url", "paths", "dry_run"}),
    (
        TransferRequest("s3://a", "s3://b", ["x"], "move", "y", True),
        {"src_url", "dst_url", "items", "mode", "rename_to", "dry_run"},
    ),
    (BundleRequest("s3://b/d", "d"), {"url", "name"}),
    (GpuCatalog([{"id": "g"}], {"dc": "x"}, "e"), {"gpus", "placement", "error"}),
    (CpuCatalog([{"id": "c"}], "eu", "e"), {"sizes", "region", "error"}),
    (
        Datacenters([{"id": "d"}], "A100", "auto", {"dataCenterIds": ["d"]}, "e"),
        {"datacenters", "node_type", "gpu_type", "placement", "placement_effective", "error"},
    ),
    (
        WorkspaceInstance("p", "n", "o", "t", "running", "1.2.3.4"),
        {"provider_id", "name", "owner", "instance_type", "state", "public_ip"},
    ),
    (WorkspaceListing([WorkspaceInstance("p")]), {"workspaces"}),
    (
        OwnerCredentialsDescriptor(["k"], [_FIELD], LoginDescriptor("k", "l"), RoleDescriptor("k", "l")),
        {"credential_keys", "workspace_credentials", "workspace_login", "workspace_role"},
    ),
]


@pytest.mark.parametrize("message", [m for m, _ in ROUND_TRIPS], ids=lambda m: type(m).__name__)
def test_to_dict_then_from_dict_is_identity(message: Any) -> None:
    assert type(message).from_dict(message.to_dict()) == message


@pytest.mark.parametrize(
    ("message", "keys"), ROUND_TRIPS, ids=lambda x: type(x).__name__ if not isinstance(x, set) else ""
)
def test_to_dict_emits_the_frozen_key_set(message: Any, keys: set[str]) -> None:
    assert set(message.to_dict()) == keys


def test_minimal_answers_omit_every_unset_optional_key() -> None:
    assert set(CreateNodeResponse("p").to_dict()) == {"provider_id"}
    assert set(NodeStateResponse("running").to_dict()) == {"state"}
    assert set(CapabilitiesResponse("p").to_dict()) == {
        "provider",
        "node_types",
        "gpu_types",
        "flavors",
        "ready",
        "missing",
        "missing_fields",
        "facets",
    }
    assert set(StorageItem("b").to_dict()) == {"id", "name", "url", "region", "size_gb"}
    assert set(GpuCatalog().to_dict()) == {"gpus"}
    assert set(Datacenters().to_dict()) == {"datacenters"}
    assert OwnerCredentialsDescriptor().to_dict() == {}


def test_an_empty_placement_means_anywhere_and_is_emitted() -> None:
    assert GpuCatalog(placement={}).to_dict() == {"gpus": [], "placement": {}}
    assert GpuCatalog.from_dict({"gpus": [], "placement": {}}).placement == {}
    assert GpuCatalog.from_dict({"gpus": []}).placement is None
    assert GpuCatalog.from_dict({"placement": "bogus"}).placement is None
    dcs = Datacenters(node_type="A100", placement="auto", placement_effective={})
    assert dcs.to_dict()["placement_effective"] == {}
    assert Datacenters.from_dict(dcs.to_dict()) == dcs
    assert "placement_effective" not in Datacenters(placement="auto").to_dict()


def test_a_gpu_create_body_sends_an_empty_workspace_and_no_pricing() -> None:
    d = CreateNodeRequest(node_id="n", node_type="t", token="k").to_dict()
    assert d["workspace"] == {}
    assert d["gpu_type"] == "t"
    assert "pricing" not in d


# ── Aliases, coercion and tolerance ──────────────────────────────────────────


def test_node_types_alias_is_read_and_emitted() -> None:
    caps = CapabilitiesResponse.from_dict({"provider": "p", "gpu_types": ["a"]})
    assert caps.node_types == ["a"]
    assert CapabilitiesResponse.from_dict({"provider": "p", "node_types": ["n"], "gpu_types": ["g"]}).node_types == [
        "n"
    ]
    d = caps.to_dict()
    assert d["node_types"] == d["gpu_types"] == ["a"]


def test_facets_reported_tells_an_older_provider_from_one_with_no_facets() -> None:
    assert CapabilitiesResponse.from_dict({"provider": "p"}).facets_reported is False
    caps = CapabilitiesResponse.from_dict({"provider": "p", "facets": ["storage", "bogus"]})
    assert caps.facets_reported is True
    assert caps.facets == ["storage"]


def test_lists_workspaces_follows_the_workspaces_facet() -> None:
    assert "lists_workspaces" not in CapabilitiesResponse("p").to_dict()
    assert CapabilitiesResponse("p", facets=["workspaces"]).to_dict()["lists_workspaces"] is True
    assert (
        CapabilitiesResponse.from_dict({"provider": "p", "lists_workspaces": True}).to_dict()["lists_workspaces"]
        is True
    )


def test_extra_is_emit_only_and_typed_keys_win() -> None:
    caps = CapabilitiesResponse("p", extra={"machines": [1], "provider": "spoof", "region": "from-extra"})
    d = caps.to_dict()
    assert d["machines"] == [1]
    assert d["provider"] == "p"
    assert d["region"] == "from-extra", "an unset typed key does not blank a passthrough key"
    assert CapabilitiesResponse.from_dict(d).extra == {}


def test_presign_extra_carries_provider_keys_and_an_explicit_null() -> None:
    presign = PresignResponse(60, extra={"bucket": "b", "account": "acct", "volume": "v", "cors": None, "expires_s": 1})
    d = presign.to_dict()
    assert d["bucket"] == "b" and d["account"] == "acct" and d["volume"] == "v"
    assert "cors" in d and d["cors"] is None, "an explicit None in extra is emitted as null"
    assert d["expires_s"] == 60, "typed keys win"
    assert PresignResponse.from_dict(d).extra == {}


def test_region_extra_carries_a_country() -> None:
    d = Region("US-NC-1", "US-NC-1", extra={"country": "US", "location": "", "id": "spoof"}).to_dict()
    assert d == {"id": "US-NC-1", "name": "US-NC-1", "country": "US", "location": ""}
    assert Region.from_dict(d).extra == {}


def test_listing_extra_answers_an_empty_account_and_a_subscription() -> None:
    listing = StorageListing(_CAPS, extra={"account": "", "subscription": "sub-1", "kind": "spoof"})
    d = listing.to_dict()
    assert d["account"] == "" and d["subscription"] == "sub-1"
    assert d["kind"] == "bucket", "typed keys win"
    assert StorageListing(_CAPS, account="acct", extra={"account": ""}).to_dict()["account"] == "acct"
    assert StorageListing.from_dict(d).extra == {}


def test_unknown_keys_and_bad_values_never_raise() -> None:
    state = NodeStateResponse.from_dict({"state": "weird", "detail": None, "bootstrap_failed": "no", "extra": 1})
    assert state.state == "unknown"
    assert state.detail == ""
    assert state.bootstrap_failed is False
    req = CreateNodeRequest.from_dict({"node_id": None, "token": 5, "ports": ["8888", "x", None], "env": None})
    assert req.node_id == ""
    assert req.token == "5"
    assert req.ports == [8888]
    assert CreateNodeResponse.from_dict({"provider_id": "p", "hourly_rate": "n/a", "services": {"a": 1, "b": "u"}}) == (
        CreateNodeResponse("p", services={"b": "u"})
    )
    assert StorageItem.from_dict({"id": "b", "size_gb": "12", "default": "true"}).size_gb == 12
    assert PreflightResponse.from_dict({"ok": "yes", "checks": "nope"}).checks == []


def test_workspace_request_never_carries_transient_keys() -> None:
    ws = WorkspaceRequest.from_dict({"name": "w", "credentials": {"k": "v"}, "provider_configs": {"aws": {}}})
    assert ws.name == "w"
    assert "credentials" not in ws.to_dict()


def test_strip_request_credentials_finds_them_top_level_or_under_workspace() -> None:
    body, creds, configs = strip_request_credentials({
        "node_id": "n",
        "workspace": {"name": "w", "credentials": {"api_key": "k"}, "provider_configs": {"aws": {"region": "eu"}}},
    })
    assert creds == {"api_key": "k"}
    assert configs == {"aws": {"region": "eu"}}
    assert body["workspace"] == {"name": "w"}
    body, creds, configs = strip_request_credentials({"credentials": {"a": 1}, "workspace": {"credentials": {"a": 1}}})
    assert creds == {"a": 1}, "the same object in both places is one"
    assert configs == {}
    assert strip_request_credentials({"node_id": "n"}) == ({"node_id": "n"}, None, {})


@pytest.mark.parametrize(
    ("body", "found"),
    [
        ({"credentials": {}, "workspace": {"credentials": {"api_key": "k"}}}, {"api_key": "k"}),
        (
            {"credentials": {"api_key": " ", "region": None}, "workspace": {"credentials": {"api_key": "k"}}},
            {"api_key": "k"},
        ),
        ({"credentials": {"api_key": "k"}, "workspace": {"credentials": {}}}, {"api_key": "k"}),
        (
            {"credentials": {"api_key": "k", "region": ""}, "workspace": {"credentials": {"api_key": "k"}}},
            {"api_key": "k", "region": ""},
        ),
        ({"credentials": {}, "workspace": {"credentials": {"region": ""}}}, None),
        ({"credentials": {}}, None),
    ],
)
def test_an_empty_credentials_object_is_absent_and_never_hides_a_filled_one(
    body: dict[str, Any], found: dict[str, Any] | None
) -> None:
    _, creds, _ = strip_request_credentials(body)
    assert creds == found


def test_two_filled_credentials_objects_that_disagree_are_refused() -> None:
    from tlc_plugin_sdk.infrastructure import InvalidRequest

    with pytest.raises(InvalidRequest, match="two different credentials"):
        strip_request_credentials({"credentials": {"a": 1}, "workspace": {"credentials": {"b": 2}}})


@pytest.mark.parametrize(
    "body",
    [
        {"credentials": "not-an-object"},
        {"credentials": ["a"]},
        {"workspace": {"credentials": 1}},
        {"workspace": {"provider_configs": "x"}},
        {"workspace": {"provider_configs": {"aws": "not-an-object"}}},
    ],
)
def test_a_malformed_transient_key_is_refused_not_dropped(body: dict[str, Any]) -> None:
    from tlc_plugin_sdk.infrastructure import InvalidRequest

    with pytest.raises(InvalidRequest):
        strip_request_credentials(body)


def test_preflight_query_emits_the_alias_once() -> None:
    assert preflight_query() == {}
    assert preflight_query("A100", "EU-RO-1") == {"node_type": "A100", "gpu_type": "A100", "datacenter": "EU-RO-1"}


# ── Golden lockstep fixtures ─────────────────────────────────────────────────


def _emit(message: str, answer: dict[str, Any]) -> dict[str, Any]:
    if message == "CapabilitiesResponse":
        core = CapabilitiesResponse.from_dict(answer).to_dict()
        return {**core, **OwnerCredentialsDescriptor.from_dict(answer).to_dict()}
    parsers: dict[str, Any] = {
        "CreateNodeResponse": CreateNodeResponse,
        "NodeStateResponse": NodeStateResponse,
        "StorageListing": StorageListing,
        "ObjectListing": ObjectListing,
        "WorkspaceListing": WorkspaceListing,
        "GpuCatalog": GpuCatalog,
        "Datacenters": Datacenters,
    }
    result: dict[str, Any] = parsers[message].from_dict(answer).to_dict()
    return result


def _assert_covers(emitted: Any, recorded: Any, passthrough: list[str], where: str) -> None:
    """Every recorded value is re-emitted; a nested item may gain typed keys, never lose recorded ones."""
    if isinstance(recorded, dict):
        assert isinstance(emitted, dict), where
        for key, value in recorded.items():
            if key in passthrough:
                continue
            assert key in emitted, f"{where}.{key} lost"
            _assert_covers(emitted[key], value, [], f"{where}.{key}")
    elif isinstance(recorded, list):
        assert isinstance(emitted, list) and len(emitted) == len(recorded), where
        for i, (e, r) in enumerate(zip(emitted, recorded, strict=True)):
            _assert_covers(e, r, passthrough, f"{where}[{i}]")
    else:
        assert emitted == recorded, f"{where}: {emitted!r} != {recorded!r}"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_golden_fixture_re_emits_the_recorded_key_set(path: Path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    answer: dict[str, Any] = doc["answer"]
    emitted = _emit(doc["message"], answer)
    expected = (set(answer) - set(doc["passthrough"])) | set(doc["additive"])
    assert set(emitted) == expected, f"emitted {sorted(set(emitted) ^ expected)} differ"
    for key in set(answer) - set(doc["passthrough"]):
        _assert_covers(emitted[key], answer[key], doc["item_passthrough"].get(key, []), key)


def test_every_fixture_names_a_parser() -> None:
    assert FIXTURES, "no fixtures recorded"
    for path in FIXTURES:
        _emit(json.loads(path.read_text())["message"], json.loads(path.read_text())["answer"])
