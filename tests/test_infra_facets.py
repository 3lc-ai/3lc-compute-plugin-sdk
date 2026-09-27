# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Facets through the real worker app: what subclassing mounts, what the base merges, how errors answer."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from litestar.exceptions import HTTPException

from tlc_plugin_sdk.harness import PluginHarness
from tlc_plugin_sdk.infrastructure import (
    CapabilitiesResponse,
    Conflict,
    CreateNodeRequest,
    CreateNodeResponse,
    CreateStorageRequest,
    GpuCatalog,
    InfrastructurePlugin,
    InvalidRequest,
    LegacyOwnerCredentialsFacet,
    LoginDescriptor,
    NodeStateResponse,
    NotConfigured,
    NotFound,
    NotSupported,
    OwnerCredentialsDescriptor,
    ProviderError,
    StorageCapabilities,
    StorageFacet,
    StorageItem,
    StorageListing,
    legacy,
)
from tlc_plugin_sdk.infrastructure.routes import scrub
from tlc_plugin_sdk.infrastructure.testing import FakeProvider


def _paths(plugin: InfrastructurePlugin) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for handler in plugin.get_route_handlers():
        for path in handler.paths:
            for method in handler.http_methods:
                out.add((str(method).upper(), "/" + str(path).strip("/")))
    return out


@pytest.fixture
def fake(tmp_path: Path) -> Any:
    plugin = FakeProvider()
    with PluginHarness(plugin, plugin_id="fake", config_root=tmp_path) as h:
        yield h


# ── What subclassing mounts ─────────────────────────────────────────────────


def test_every_facet_mounts_its_routes_and_settings_mounts_two() -> None:
    paths = _paths(FakeProvider())
    assert len(paths) == 5 + 12 + 3 + 1 + 2
    assert ("GET", "/infra/storage/list") in paths
    assert ("DELETE", "/infra/storage/bundle/{bundle_id:str}") in paths
    assert ("GET", "/infra/cpu-catalog") in paths
    assert ("GET", "/infra/workspaces") in paths
    assert ("POST", "/settings") in paths
    assert ("POST", "/infra/nodes/{provider_id:str}/terminate") not in paths, "no legacy facet, no legacy route"


def test_capabilities_carry_facets_storage_and_lists_workspaces(fake: PluginHarness) -> None:
    d = fake.get("/infra/capabilities").json()
    assert d["facets"] == ["storage", "catalog", "workspaces"]
    assert d["storage"]["kind"] == "bucket"
    assert d["storage"]["default_id"] == "fake-data"
    assert d["lists_workspaces"] is True
    assert d["gpu_types"] == d["node_types"] == ["fake-gpu"]
    assert d["missing"] == ["api_key"]
    assert [f["key"] for f in d["missing_fields"]] == ["api_key"]
    assert CapabilitiesResponse.from_dict(d).facets_reported is True


def test_an_author_set_facets_or_storage_is_overwritten(tmp_path: Path) -> None:
    class Spoof(FakeProvider):
        def capabilities(self) -> CapabilitiesResponse:
            caps = super().capabilities()
            caps.facets = ["catalog"]
            caps.storage = StorageCapabilities(kind="volume", label="wrong")
            return caps

    with PluginHarness(Spoof(), plugin_id="fake", config_root=tmp_path) as h:
        d = h.get("/infra/capabilities").json()
    assert d["facets"] == ["storage", "catalog", "workspaces"]
    assert d["storage"]["label"] == "Fake bucket"


# ── Core routes ──────────────────────────────────────────────────────────────


def test_create_answers_201_and_diagnostics_are_a_query_flag(fake: PluginHarness) -> None:
    body = CreateNodeRequest(node_id="n1", node_type="fake-gpu", token="tok").to_dict()
    r = fake.post("/infra/nodes", json_body=body)
    assert r.status_code == 201, r.text
    created = CreateNodeResponse.from_dict(r.json())
    assert created.agent_url == "http://fake:8800"
    assert created.hourly_rate == 1.5
    plain = fake.get(f"/infra/nodes/{created.provider_id}").json()
    assert plain == {"state": "running"}
    diag = NodeStateResponse.from_dict(fake.get(f"/infra/nodes/{created.provider_id}?diagnostics=true").json())
    assert diag.bootstrap_history == ["Starting node agent"]
    assert diag.bootstrap_failed is False
    assert fake.call("DELETE", f"/infra/nodes/{created.provider_id}").json()["state"] == "terminated"


def test_a_workspace_create_answers_services_and_managed_by(fake: PluginHarness) -> None:
    body = CreateNodeRequest(node_id="ws", node_type="fake-gpu", token="tok", flavor="workspace", owner="me").to_dict()
    d = fake.post("/infra/nodes", json_body=body).json()
    assert "agent_url" not in d
    assert set(d["services"]) == {"object_service_url", "compute_service_url"}
    assert d["managed_by"] == "creator"
    listed = fake.get("/infra/workspaces?owner=me").json()["workspaces"]
    assert [w["provider_id"] for w in listed] == ["fake-ws"]
    assert fake.get("/infra/workspaces?owner=someone-else").json()["workspaces"] == []


def test_preflight_and_catalogs_accept_the_gpu_type_alias(fake: PluginHarness) -> None:
    assert fake.get("/infra/preflight?gpu_type=fake-gpu").json()["ok"] is True
    assert fake.get("/infra/gpu-catalog?gpu_type=x&region=eu").json()["gpus"][0]["region"] == "eu"
    d = fake.get("/infra/datacenters?gpu_type=fake-gpu").json()
    assert d["node_type"] == d["gpu_type"] == "fake-gpu"
    assert fake.get("/infra/cpu-catalog").json()["region"] == "fake-1"


def test_credentials_on_create_are_refused_without_the_legacy_facet(fake: PluginHarness) -> None:
    body = {
        **CreateNodeRequest(node_id="n", node_type="fake-gpu", token="t").to_dict(),
        "credentials": {"api_key": "k"},
    }
    r = fake.post("/infra/nodes", json_body=body)
    assert r.status_code == 400
    assert "Connection" in r.json()["detail"]
    nested = CreateNodeRequest(node_id="n", node_type="fake-gpu", token="t").to_dict()
    nested["workspace"] = {"provider_configs": {"aws": {}}}
    assert fake.post("/infra/nodes", json_body=nested).status_code == 400
    r = fake.post("/infra/storage", json_body={"name": "b", "credentials": {"api_key": "k"}})
    assert r.status_code == 400
    malformed = {**CreateNodeRequest(node_id="n", node_type="fake-gpu", token="t").to_dict(), "credentials": "x"}
    r = fake.post("/infra/nodes", json_body=malformed)
    assert r.status_code == 400, "a malformed credentials value is refused for every provider"
    assert "object" in r.json()["detail"]


# ── Storage routes ───────────────────────────────────────────────────────────


def _wait(h: PluginHarness, path: str) -> dict[str, Any]:
    for _ in range(200):
        status = h.get(path).json()
        if status["state"] in ("done", "failed", "cancelled"):
            return dict(status)
        time.sleep(0.01)
    msg = f"{path} never finished"
    raise AssertionError(msg)


def test_storage_listing_create_and_delete(fake: PluginHarness) -> None:
    listing = StorageListing.from_dict(fake.get("/infra/storage").json())
    assert listing.capabilities.label == "Fake bucket"
    assert [i.id for i in listing.storage] == ["fake-data"]
    assert listing.storage[0].default is True
    r = fake.post("/infra/storage", json_body={"name": "new"})
    assert r.status_code == 201
    assert r.json()["url"] == "fake://new"
    assert fake.post("/infra/storage", json_body={"name": "new"}).status_code == 400, "exists already"
    assert fake.post("/infra/storage", json_body={}).status_code == 400
    assert fake.call("DELETE", "/infra/storage/new").json() == {"deleted": True, "id": "new"}
    assert fake.call("DELETE", "/infra/storage/new").status_code == 404


def test_browse_presign_and_delete_objects(fake: PluginHarness) -> None:
    top = fake.get("/infra/storage/list?url=fake://fake-data").json()
    assert [p["name"] for p in top["prefixes"]] == ["train"]
    assert [o["name"] for o in top["objects"]] == ["readme.txt"]
    assert "next_token" not in top
    assert fake.get("/infra/storage/list").status_code == 400
    r = fake.post("/infra/storage/presign", json_body={"url": "fake://fake-data/up", "files": [{"path": "x.jpg"}]})
    assert r.json()["uploads"][0]["method"] == "PUT"
    assert r.json()["downloads"] == []
    r = fake.post(
        "/infra/storage/presign",
        json_body={"url": "fake://fake-data", "files": [{"path": "readme.txt"}], "mode": "download"},
    )
    assert r.json()["downloads"][0]["method"] == "GET"
    assert (
        fake.post("/infra/storage/presign", json_body={"url": "fake://fake-data", "files": [], "mode": "x"}).status_code
        == 400
    )
    dry = fake.post(
        "/infra/storage/delete", json_body={"url": "fake://fake-data", "paths": ["train/"], "dry_run": True}
    ).json()
    assert dry["count"] == 2
    assert fake.get("/infra/storage/list?url=fake://fake-data/train").json()["objects"], "dry run deleted nothing"
    fake.post("/infra/storage/delete", json_body={"url": "fake://fake-data", "paths": ["train/"]})
    assert fake.get("/infra/storage/list?url=fake://fake-data/train").json()["objects"] == []


def test_transfers_run_over_the_plugins_registry(fake: PluginHarness) -> None:
    body = {"src_url": "fake://fake-data", "dst_url": "fake://fake-data/copy", "items": ["train/"], "dry_run": True}
    plan = fake.post("/infra/storage/transfer", json_body=body).json()
    assert plan["objects"] == 2
    started = fake.post("/infra/storage/transfer", json_body={**body, "dry_run": False}).json()
    tid = started["transfer_id"]
    done = _wait(fake, f"/infra/storage/transfer/{tid}")
    assert done["state"] == "done", done
    copied = fake.get("/infra/storage/list?url=fake://fake-data/copy/train").json()["objects"]
    assert sorted(o["name"] for o in copied) == ["a.jpg", "b.jpg"]
    assert fake.call("DELETE", f"/infra/storage/transfer/{tid}").json() == {"cancelled": False}
    assert fake.get("/infra/storage/transfer/deadbeef0000").status_code == 404
    assert fake.get("/infra/storage/transfer/NOT-AN-ID").status_code == 400
    assert fake.call("DELETE", "/infra/storage/transfer/zz").status_code == 400
    across = {"src_url": "fake://fake-data", "dst_url": "fake://other", "items": ["readme.txt"]}
    fake.post("/infra/storage", json_body={"name": "other"})
    r = fake.post("/infra/storage/transfer", json_body=across)
    assert r.status_code == 400
    assert "one storage" in r.json()["detail"]
    r = fake.post("/infra/storage/transfer", json_body={**body, "dry_run": False, "items": ["../x"]})
    assert r.status_code == 400, "the engine's TransferError is a 400"


def test_bundles_run_over_the_plugins_registry(fake: PluginHarness) -> None:
    started = fake.post("/infra/storage/bundle", json_body={"url": "fake://fake-data/train"}).json()
    assert started["name"] == "train"
    done = _wait(fake, f"/infra/storage/bundle/{started['bundle_id']}")
    assert done["state"] == "done", done
    assert done["download_url"] == "fake://fake-data/.downloads/train.zip"
    assert done["files_done"] == 2
    assert fake.get("/infra/storage/bundle/deadbeef0000").status_code == 404
    assert fake.post("/infra/storage/bundle", json_body={}).status_code == 400


class _ListOnly(InfrastructurePlugin, StorageFacet):
    """A storage facet that overrides only the abstract methods: everything else must answer 501."""

    def get_ui_fragment(self) -> str:
        return "<div/>"

    def capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse(provider="listonly", node_types=["t"], ready=True)

    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
        return CreateNodeResponse(provider_id="p", agent_url="http://x")

    def node_state(self, provider_id: str) -> NodeStateResponse:
        return NodeStateResponse(state="running")

    def delete_node(self, provider_id: str) -> NodeStateResponse:
        return NodeStateResponse(state="terminated")

    def storage_capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(kind="bucket", label="B", upload=False, browse=False, transfer=False, bundle=False)

    def list_storage(self, *, fallback_url: str = "") -> StorageListing:
        return StorageListing(capabilities=self.storage_capabilities())


def test_a_default_facet_method_answers_501(tmp_path: Path) -> None:
    with PluginHarness(_ListOnly(), plugin_id="listonly", config_root=tmp_path) as h:
        assert h.get("/infra/storage").json()["storage"] == []
        assert h.get("/infra/storage/list?url=x://b").status_code == 501
        assert h.post("/infra/storage", json_body={"name": "b"}).status_code == 501
        r = h.post("/infra/storage/transfer", json_body={"src_url": "x://a", "dst_url": "x://a", "items": ["f"]})
        assert r.status_code == 501
        assert "does not support" in r.json()["detail"]
        assert h.get("/infra/storage/transfer/deadbeef0000").status_code == 404, "status needs no registry"


# ── Errors ───────────────────────────────────────────────────────────────────


class _Raising(InfrastructurePlugin):
    exc: BaseException | None = None

    def get_ui_fragment(self) -> str:
        return "<div/>"

    def capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse(provider="raising", node_types=["t"], ready=True)

    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
        if self.exc is not None:
            raise self.exc
        return CreateNodeResponse(provider_id="p", agent_url="http://x")

    def node_state(self, provider_id: str) -> NodeStateResponse:
        if self.exc is not None:
            raise self.exc
        return NodeStateResponse(state="running")

    def delete_node(self, provider_id: str) -> NodeStateResponse:
        return NodeStateResponse(state="terminated")

    def secret_values(self) -> list[str]:
        return ["hunter2", ""]


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (ProviderError("upstream said no"), 502),
        (InvalidRequest("bad id"), 400),
        (NotConfigured("no key"), 409),
        (Conflict("bucket not empty"), 409),
        (NotFound("no such node"), 404),
        (NotSupported("nope"), 501),
        (ValueError("not a region"), 400),
        (TypeError("wrong type"), 400),
        (NotImplementedError("later"), 501),
        (RuntimeError("boto exploded"), 502),
        (KeyError("k"), 502),
        (HTTPException(status_code=418, detail="teapot"), 418),
    ],
)
def test_what_a_method_raises_maps_to_a_status_with_the_sentence(exc: BaseException, status: int) -> None:
    plugin = _Raising()
    plugin.exc = exc
    with PluginHarness(plugin, plugin_id="raising") as h:
        r = h.get("/infra/nodes/x")
    assert r.status_code == status, r.text
    assert isinstance(r.json()["detail"], str)
    assert r.json()["detail"] != "Internal Server Error"


def test_a_plugins_own_http_exception_keeps_its_status_but_is_scrubbed() -> None:
    plugin = _Raising()
    plugin.exc = HTTPException(status_code=418, detail="raw hunter2 in a teapot")
    with PluginHarness(plugin, plugin_id="raising") as h:
        r = h.get("/infra/nodes/x")
    assert r.status_code == 418
    assert r.json()["detail"] == "raw *** in a teapot"


def test_details_are_scrubbed_of_the_plugins_secrets_and_the_request_token() -> None:
    plugin = _Raising()
    plugin.exc = RuntimeError("key hunter2 and token tok-123 rejected")
    with PluginHarness(plugin, plugin_id="raising") as h:
        r = h.post("/infra/nodes", json_body={"node_id": "n", "node_type": "t", "token": "tok-123"})
    assert r.status_code == 502
    assert r.json()["detail"] == "key *** and token *** rejected"
    assert scrub("a bbbb", ["", "bbbb"]) == "a ***"
    assert scrub("ghost t", ["t"]) == "ghost t", "a value too short to be a secret is left alone"


_ARN = "arn:aws:iam::123456789012:role/x"


def _plain(exc: Exception) -> str:
    return "AWS refused the call." if _ARN in str(exc) else ""


class _Describing(_Raising):
    def describe_error(self, exc: Exception) -> str:
        return _plain(exc)


def test_a_conflict_answers_409_with_its_sentence() -> None:
    plugin = _Raising()
    plugin.exc = Conflict("The bucket is not empty. Delete its objects first.")
    with PluginHarness(plugin, plugin_id="raising") as h:
        r = h.get("/infra/nodes/x")
    assert r.status_code == 409
    assert r.json()["detail"] == "The bucket is not empty. Delete its objects first."


def test_describe_error_words_an_unexpected_exception_and_is_still_scrubbed() -> None:
    plugin = _Describing()
    plugin.exc = RuntimeError(f"AccessDenied for {_ARN} (request id 42)")
    with PluginHarness(plugin, plugin_id="raising") as h:
        assert h.get("/infra/nodes/x").json()["detail"] == "AWS refused the call."
        plugin.exc = RuntimeError("hunter2 is wrong")
        assert h.get("/infra/nodes/x").json()["detail"] == "*** is wrong", "an empty answer falls back, scrubbed"
        plugin.exc = ProviderError(f"worded by the provider: {_ARN}")
        r = h.get("/infra/nodes/x")
        assert r.status_code == 502 and _ARN in r.json()["detail"], "a ProviderError keeps its own sentence"


def test_a_describe_error_that_leaks_a_secret_or_raises_is_contained() -> None:
    class Leaky(_Raising):
        def describe_error(self, exc: Exception) -> str:
            if "boom" in str(exc):
                msg = "hook failed"
                raise RuntimeError(msg)
            return "the key hunter2 was refused"

    plugin = Leaky()
    plugin.exc = RuntimeError("anything")
    with PluginHarness(plugin, plugin_id="raising") as h:
        assert h.get("/infra/nodes/x").json()["detail"] == "the key *** was refused"
        plugin.exc = RuntimeError("boom")
        assert h.get("/infra/nodes/x").json()["detail"] == "boom"


def test_the_default_describe_error_is_the_scrubbed_text() -> None:
    assert _Raising().describe_error(RuntimeError("key hunter2")) == "key ***"
    assert _Raising().describe_error(KeyError()) == "KeyError"


class _DescribingFake(FakeProvider):
    """A fake whose catalog and storage calls fail the way a cloud SDK does."""

    fail_catalog = False

    def describe_error(self, exc: Exception) -> str:
        return _plain(exc)

    def gpu_catalog(self, *, node_type: str = "", region: str = "") -> GpuCatalog:
        if self.fail_catalog:
            msg = f"DescribeInstanceTypes failed for {_ARN}"
            raise RuntimeError(msg)
        return super().gpu_catalog(node_type=node_type, region=region)

    def transfer_registry(self, url: str) -> Any:
        registry = super().transfer_registry(url)

        def copy(src: str, dst: str) -> None:
            msg = f"CopyObject denied for {_ARN}"
            raise RuntimeError(msg)

        registry._copy = copy
        return registry

    def bundle_registry(self, url: str) -> Any:
        registry = super().bundle_registry(url)

        def store(path: Path, name: str) -> str:
            msg = f"PutObject denied for {_ARN}"
            raise RuntimeError(msg)

        registry._store = store
        return registry


def test_describe_error_covers_the_routes_and_jobs_the_sdk_drives(tmp_path: Path) -> None:
    plugin = _DescribingFake()
    plugin.fail_catalog = True
    with PluginHarness(plugin, plugin_id="fake", config_root=tmp_path) as h:
        r = h.get("/infra/gpu-catalog")
        assert (r.status_code, r.json()["detail"]) == (502, "AWS refused the call.")
        body = {"src_url": "fake://fake-data", "dst_url": "fake://fake-data/copy", "items": ["train/"]}
        tid = h.post("/infra/storage/transfer", json_body=body).json()["transfer_id"]
        transfer = _wait(h, f"/infra/storage/transfer/{tid}")
        assert transfer["state"] == "failed"
        assert {f["reason"] for f in transfer["failures"]} == {"AWS refused the call."}
        bid = h.post("/infra/storage/bundle", json_body={"url": "fake://fake-data/train"}).json()["bundle_id"]
        bundle = _wait(h, f"/infra/storage/bundle/{bid}")
        assert (bundle["state"], bundle["error"]) == ("failed", "AWS refused the call.")


def test_a_registry_built_with_its_own_describer_keeps_it() -> None:
    from tlc_plugin_sdk.shared.storage_bundle import BundleRegistry

    def mine(exc: Exception) -> str:
        return "mine"

    registry = BundleRegistry(
        list_objects=lambda u: [], open_object=lambda k: None, store_bundle=lambda p, n: "", describe_error=mine
    )

    class Own(FakeProvider):
        def bundle_registry(self, url: str) -> Any:
            return registry

    with PluginHarness(Own(), plugin_id="fake") as h:
        h.post("/infra/storage/bundle", json_body={"url": "fake://fake-data/train"})
    assert registry.describe_error is mine


# ── Legacy owner-credentials facet ───────────────────────────────────────────


class _Legacy(InfrastructurePlugin, LegacyOwnerCredentialsFacet, StorageFacet):
    def __init__(self) -> None:
        self.seen: list[tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]] = []
        self.owners: list[tuple[str, str]] = []
        self.storage_seen: list[tuple[str, str, dict[str, Any] | None]] = []

    def storage_capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(kind="bucket", label="B")

    def list_storage(self, *, fallback_url: str = "") -> StorageListing:
        return StorageListing(capabilities=self.storage_capabilities())

    def create_storage(self, request: CreateStorageRequest) -> StorageItem:
        self.storage_seen.append((request.owner, legacy.current_request_owner(), legacy.current_request_credentials()))
        return StorageItem(id=request.name, name=request.name, url=f"x://{request.name}")

    def get_ui_fragment(self) -> str:
        return "<div/>"

    def capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse(provider="legacy", node_types=["t"], ready=True)

    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
        self.seen.append((legacy.current_request_credentials(), legacy.current_provider_configs()))
        self.owners.append((request.owner, legacy.current_request_owner()))
        return CreateNodeResponse(provider_id="p", agent_url="http://x")

    def node_state(self, provider_id: str) -> NodeStateResponse:
        return NodeStateResponse(state="running")

    def delete_node(self, provider_id: str) -> NodeStateResponse:
        return NodeStateResponse(state="terminated")

    def credential_descriptor(self) -> OwnerCredentialsDescriptor:
        return OwnerCredentialsDescriptor(credential_keys=["api_key"], login=LoginDescriptor(kind="k", label="Sign in"))

    def terminate_with_credentials(
        self, provider_id: str, *, credentials: dict[str, Any], owner: str = ""
    ) -> NodeStateResponse:
        if credentials.get("api_key") == "sekrit":
            msg = f"{owner} used {credentials['api_key']}: the provider refused it"
            raise ProviderError(msg)
        return NodeStateResponse(state="terminated", detail=f"terminated for {owner}")

    def discover_storage(self, *, credentials: dict[str, Any], owner: str = "") -> StorageListing:
        return StorageListing(capabilities=StorageCapabilities(kind="bucket", label="B"), account=owner)


class _LegacyWithLogin(_Legacy):
    def login_start(self, body: dict[str, Any], *, owner: str = "") -> dict[str, Any]:
        return {"code": "ABCD", "owner": owner}

    def role_setup(self, *, owner: str = "", bucket_url: str = "") -> dict[str, Any]:
        return {"external_id": owner}


def test_legacy_routes_are_mounted_only_for_overridden_methods() -> None:
    base = {(m, p) for m, p in _paths(_Legacy()) if not p.startswith("/infra/storage") or p.endswith("/discover")}
    assert ("POST", "/infra/nodes/{provider_id:str}/terminate") in base
    assert ("POST", "/infra/storage/discover") in base
    assert not any(p.startswith("/infra/login") or p == "/infra/role-setup" for _, p in base)
    more = _paths(_LegacyWithLogin())
    assert ("POST", "/infra/login") in more
    assert ("GET", "/infra/role-setup") in more
    assert ("GET", "/infra/login/{login_id:str}") not in more, "login_poll was not overridden"


def test_legacy_credentials_reach_the_plugin_through_the_context_and_never_the_request() -> None:
    plugin = _Legacy()
    with PluginHarness(plugin, plugin_id="legacy") as h:
        caps = h.get("/infra/capabilities").json()
        assert caps["facets"] == ["storage", "legacy-owner-credentials"]
        assert caps["credential_keys"] == ["api_key"]
        assert caps["workspace_login"]["label"] == "Sign in"
        assert "workspace_role" not in caps
        body = {
            "node_id": "n",
            "node_type": "t",
            "token": "tok",
            "owner": "me@x",
            "workspace": {"name": "w", "credentials": {"api_key": "sekrit"}, "provider_configs": {"aws": {"r": 1}}},
        }
        assert h.post("/infra/nodes", json_body=body).status_code == 201
        assert h.post("/infra/nodes", json_body={"node_id": "n", "node_type": "t", "token": "tok"}).status_code == 201
        r = h.post("/infra/nodes/p/terminate", json_body={"credentials": {"api_key": "sekrit"}, "owner": "me"})
        assert r.status_code == 502
        assert r.json()["detail"] == "me used ***: the provider refused it", "request credentials are scrubbed"
        r = h.post("/infra/nodes/p/terminate", json_body={"credentials": {"api_key": "ok"}, "owner": "me"})
        assert r.status_code == 200
        assert r.json()["detail"] == "terminated for me"
        assert h.post("/infra/nodes/p/terminate", json_body={}).status_code == 400
        assert (
            h.post("/infra/storage/discover", json_body={"credentials": {"api_key": "k"}, "owner": "o"}).json()[
                "account"
            ]
            == "o"
        )
    assert plugin.seen == [({"api_key": "sekrit"}, {"aws": {"r": 1}}), (None, {})]
    assert plugin.owners == [("me@x", "me@x"), ("", "")]
    assert legacy.current_request_credentials() is None, "reset after the call"
    assert legacy.current_request_owner() == ""


def test_owner_reaches_create_storage_in_the_request_and_the_context() -> None:
    plugin = _Legacy()
    with PluginHarness(plugin, plugin_id="legacy") as h:
        r = h.post("/infra/storage", json_body={"name": "b", "owner": "me@x", "credentials": {"api_key": "k"}})
        assert r.status_code == 201, r.text
        assert h.post("/infra/storage", json_body={"name": "c", "owner": "you@x"}).status_code == 201
    assert plugin.storage_seen == [("me@x", "me@x", {"api_key": "k"}), ("you@x", "you@x", None)]


def test_login_and_role_routes_pass_the_owner_through() -> None:
    with PluginHarness(_LegacyWithLogin(), plugin_id="legacy") as h:
        assert h.post("/infra/login", json_body={"owner": "me", "start_url": "u"}).json() == {
            "code": "ABCD",
            "owner": "me",
        }
        assert h.get("/infra/role-setup?owner=me").json() == {"external_id": "me"}
