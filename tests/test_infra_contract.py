# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The provider contract: tolerant wire types, operations that follow the class, and typed errors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from litestar.exceptions import HTTPException

from tlc_plugin_sdk.connections import ConnectionBinding, CredentialUnavailable
from tlc_plugin_sdk.contract import HubPlugin
from tlc_plugin_sdk.harness import PluginHarness
from tlc_plugin_sdk.infrastructure import (
    CONTRACT_GENERATION,
    OPERATIONS,
    TRANSITIONAL_OPERATIONS,
    AccessMethod,
    AccountSchema,
    Address,
    CapabilitiesResponse,
    Catalog,
    CatalogFacet,
    CatalogRequest,
    Conflict,
    ConnectionVerification,
    CreateNodeRequest,
    CreateNodeResponse,
    CredentialExpired,
    DeploymentCapabilities,
    DeploymentPrepared,
    DeploymentProvider,
    DeploymentRequest,
    DnsFacet,
    DnsRecord,
    ErrorAnswer,
    FormField,
    GrantObservation,
    GrantUpdate,
    InfrastructurePlugin,
    InvalidRequest,
    NodeCapabilities,
    NodeProvider,
    NodeStateResponse,
    NotConfigured,
    NotFound,
    NotSupported,
    Offer,
    PermissionDenied,
    PermissionOption,
    PermissionSchema,
    PluginSettings,
    ProviderError,
    RateLimited,
    SettingsField,
    StorageFacet,
    VerifyRequest,
    capabilities_problems,
    provided_operations,
)
from tlc_plugin_sdk.infrastructure.errors import ERROR_CODES, scrub
from tlc_plugin_sdk.infrastructure.routes import OPERATION_HANDLERS, answer, error_handlers
from tlc_plugin_sdk.infrastructure.testing import FakeProvider, FakeSettings, connection_headers

_ERROR_KEYS = {"detail", "code", "remedy", "retry_after_s"}
_AMBIENT = connection_headers(ConnectionBinding("conn-1", "nodes", "AMBIENT"))


def _paths(plugin: InfrastructurePlugin) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for handler in plugin.get_route_handlers():
        for path in handler.paths:
            for method in handler.http_methods:
                if str(method).upper() != "OPTIONS":
                    out.add((str(method).upper(), "/" + str(path).strip("/")))
    return out


# ── Wire types ───────────────────────────────────────────────────────────────


def test_a_peers_shape_never_raises_and_unknown_keys_are_dropped() -> None:
    caps = CapabilitiesResponse.from_dict({
        "provider": 7,
        "display_name": None,
        "contract": "2",
        "ready": "yes",
        "missing_fields": "nope",
        "account": {"noun": "A", "fields": [{"key": "k", "label": "L", "kind": "weird", "max_length": "x"}, 3]},
        "access_methods": [{"kind": "SECRET", "label": "S", "purposes": ["nodes", "moon"]}],
        "nodes": {"kinds": ["gpu", "quantum"], "pricing": "spot", "type_connections": {"a": None, "b": "c"}},
        "deployments": "no",
        "added_in_a_later_minor": {"x": 1},
    })
    assert caps.provider == "7" and caps.display_name == "" and caps.contract == 2 and caps.ready is True
    assert caps.missing_fields == [] and caps.deployments is None
    assert caps.account is not None and caps.account.fields == [FormField(key="k", label="L")]
    assert caps.access_methods == [AccessMethod(kind="SECRET", label="S", purposes=["nodes"])]
    assert caps.nodes == NodeCapabilities(kinds=["gpu"], type_connections={"b": "c"})
    assert "added_in_a_later_minor" not in caps.to_dict()
    for cls in (CreateNodeRequest, CreateNodeResponse, DeploymentRequest, ConnectionVerification, Catalog):
        assert cls.from_dict(None) == cls.from_dict({}) == cls.from_dict([1, 2])


def test_a_provider_that_says_no_contract_reads_as_generation_zero() -> None:
    assert CONTRACT_GENERATION == 2
    assert CapabilitiesResponse(provider="p", display_name="P").to_dict()["contract"] == 2
    assert CapabilitiesResponse.from_dict({"provider": "p", "node_types": ["a"], "facets": []}).contract == 0


def test_a_key_whose_presence_carries_meaning_is_omitted_when_unset() -> None:
    caps = CapabilitiesResponse(provider="p", display_name="P").to_dict()
    assert {"account", "host", "nodes", "deployments", "storage"}.isdisjoint(caps)
    assert caps["permissions"] == {"options": []}, "no storage schema: storage rules are not a thing here"
    offer = Offer(id="a").to_dict()
    assert "hourly" not in offer and "available" not in offer and offer["gpu_count"] == 0
    assert CreateNodeResponse(provider_id="p").to_dict() == {"provider_id": "p", "storage_access": "none"}
    assert ErrorAnswer(detail="d").to_dict() == {
        "detail": "d",
        "code": "provider_error",
        "remedy": "",
        "retry_after_s": None,
    }


def test_an_unreadable_status_reads_as_the_safe_one() -> None:
    assert GrantObservation.from_dict({"status": "COMPLETE!"}).status == "failed"
    assert GrantUpdate.from_dict({}).status == "failed"
    assert DeploymentPrepared.from_dict({"state": "done"}).state == "failed"
    assert DnsRecord.from_dict({"name": "a", "type": "MX"}).type == "TXT"
    assert Address.from_dict({"value": "x"}).type == "hostname"
    assert CatalogRequest.from_dict({}).purpose == "gpu_node"
    assert NodeStateResponse.from_dict({"state": "booting"}).state == "unknown"


def test_a_create_request_carries_kind_location_and_storage_roots() -> None:
    body = {
        "node_id": " n1 ",
        "node_type": "t",
        "token": "tok",
        "kind": "cpu",
        "location": "eu-west-1",
        "pricing": "spot",
        "host_id": "svc-1",
        "idle_ttl_s": "inf",
        "storage_roots": [{"url": "s3://b/p", "access": "read", "role": "scan"}, "junk"],
        "gpu_type": "ignored",
        "flavor": "gpu",
    }
    req = CreateNodeRequest.from_dict(body)
    assert (req.node_id, req.kind, req.location, req.pricing, req.host_id) == (
        "n1",
        "cpu",
        "eu-west-1",
        "spot",
        "svc-1",
    )
    assert req.idle_ttl_s == 1800.0 and req.flavor == ""
    assert [(r.url, r.access, r.role) for r in req.storage_roots] == [("s3://b/p", "read", "scan")]
    wire = req.to_dict()
    assert "flavor" not in wire and "workspace" not in wire and "gpu_type" not in wire
    assert CreateNodeRequest.from_dict(wire) == req
    assert CreateNodeRequest.from_dict({"kind": "quantum", "pricing": "free"}).kind == "gpu"
    assert CreateNodeRequest.from_dict({"pricing": "free"}).pricing == ""
    assert CreateNodeRequest.from_dict({"idle_ttl_s": 0}).idle_ttl_s == 0.0, "zero or less means never"


def test_a_bootstrap_token_is_never_in_a_repr() -> None:
    request = DeploymentRequest.from_dict({
        "deployment_id": "d",
        "name": "D",
        "bootstrap": {"record_id": "r", "token": "tok-1"},
    })
    assert request.bootstrap.token == "tok-1" and "tok-1" not in repr(request)


def test_a_settings_field_becomes_a_form_field_prompt() -> None:
    described = SettingsField(key="api_key", label="API key", secret=True, help="Where it is", placeholder="rp_…")
    field = FormField.from_settings(described)
    assert (field.kind, field.secret, field.example, field.help) == ("secret", True, "rp_…", "Where it is")
    assert FormField.from_settings(SettingsField(key="region", label="Region")).kind == "text"


# ── Operations follow the class ──────────────────────────────────────────────


class _Bare(InfrastructurePlugin):
    def get_ui_fragment(self) -> str:
        return ""

    def capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse(provider="bare", display_name="Bare", ready=True)


class _Nodes(InfrastructurePlugin, NodeProvider):
    """The smallest node provider: the three abstract methods and nothing else."""

    exc: BaseException | None = None

    def get_ui_fragment(self) -> str:
        return ""

    def capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse(
            provider="nodes", display_name="Nodes", ready=True, nodes=NodeCapabilities(types=["t"])
        )

    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
        if self.exc is not None:
            raise self.exc
        return CreateNodeResponse(provider_id=f"p-{request.node_id}", agent_url="http://x:8800")

    def node_state(self, provider_id: str) -> NodeStateResponse:
        if self.exc is not None:
            raise self.exc
        return NodeStateResponse(state="running" if provider_id.startswith("p-") else "gone")

    def delete_node(self, provider_id: str) -> NodeStateResponse:
        return NodeStateResponse(state="terminated")

    def secret_values(self) -> list[str]:
        return ["hunter2", ""]


def test_a_plugin_with_no_group_serves_capabilities_alone() -> None:
    assert _paths(_Bare()) == {("GET", "/infra/capabilities")}
    assert provided_operations(_Bare()) == []
    with PluginHarness(_Bare(), plugin_id="bare") as h:
        caps = h.get("/infra/capabilities").json()
        assert caps["operations"] == [] and caps["contract"] == 2
        assert h.post("/infra/nodes", json_body={}).status_code == 404, "an operation not provided has no route"
        assert h.get("/infra/storage").status_code == 404
        assert h.post("/infra/connection/verify", json_body={}).status_code == 404


def test_a_node_provider_serves_its_three_routes_and_lists_only_what_it_wrote() -> None:
    assert provided_operations(_Nodes()) == ["node.create", "node.state", "node.delete"]
    assert _paths(_Nodes()) == {
        ("GET", "/infra/capabilities"),
        ("POST", "/infra/nodes"),
        ("GET", "/infra/nodes/{provider_id:str}"),
        ("DELETE", "/infra/nodes/{provider_id:str}"),
    }

    class Checking(_Nodes):
        def check_node(self, request: Any) -> Any:
            raise NotImplementedError

        def verify_connection(self, request: VerifyRequest) -> ConnectionVerification:
            return ConnectionVerification(identity="me", account={})

    assert provided_operations(Checking()) == [
        "connection.verify",
        "node.create",
        "node.state",
        "node.delete",
        "node.check",
    ]
    assert _paths(Checking()) - _paths(_Nodes()) == {
        ("POST", "/infra/connection/verify"),
        ("POST", "/infra/nodes/check"),
    }


def test_every_operation_has_a_route_builder_and_the_fake_provides_them_all() -> None:
    assert set(OPERATION_HANDLERS) == set(OPERATIONS)
    fake = FakeProvider()
    assert provided_operations(fake) == [*OPERATIONS, "workspace.create"]
    assert fake.operations() == provided_operations(fake)
    assert set(TRANSITIONAL_OPERATIONS).isdisjoint(OPERATIONS)
    for group in (NodeProvider, DeploymentProvider, StorageFacet, CatalogFacet, DnsFacet):
        assert isinstance(fake, group)
    paths = _paths(fake)
    assert len(paths) == len({(m, p) for m, p in paths})
    for expected in (
        ("POST", "/infra/connection/grant"),
        ("POST", "/infra/connection/grant/update"),
        ("POST", "/infra/connection/verify"),
        ("GET", "/infra/connection/node-access"),
        ("POST", "/infra/connection/node-access"),
        ("GET", "/infra/storage/candidates"),
        ("POST", "/infra/nodes/check"),
        ("POST", "/infra/catalog"),
        ("GET", "/infra/catalog/locations"),
        ("POST", "/infra/deployments/options"),
        ("POST", "/infra/deployments/check"),
        ("POST", "/infra/deployments/prepare"),
        ("POST", "/infra/deployments"),
        ("GET", "/infra/deployments"),
        ("GET", "/infra/deployments/{provider_id:str}"),
        ("DELETE", "/infra/deployments/{provider_id:str}"),
        ("POST", "/infra/deployments/{provider_id:str}/retain"),
        ("POST", "/infra/deployments/sweep"),
        ("GET", "/infra/dns/records"),
        ("POST", "/infra/dns/changes"),
        ("GET", "/settings"),
        ("POST", "/settings"),
    ):
        assert expected in paths, expected
    assert not {p for _, p in paths if "gpu-catalog" in p or "preflight" in p or "login" in p or "role-setup" in p}


def test_the_sdk_fills_operations_and_storage_over_what_the_author_set(tmp_path: Path) -> None:
    class Overwriting(_Nodes):
        def capabilities(self) -> CapabilitiesResponse:
            caps = super().capabilities()
            caps.operations = ["deployment.create", "made.up"]
            return caps

    with PluginHarness(Overwriting(), plugin_id="nodes", config_root=tmp_path) as h:
        caps = h.get("/infra/capabilities").json()
    assert caps["operations"] == ["node.create", "node.state", "node.delete"]
    assert "storage" not in caps


def test_node_diagnostics_is_listed_only_when_written_and_the_plain_state_answers_otherwise() -> None:
    with PluginHarness(_Nodes(), plugin_id="nodes") as h:
        assert "node.diagnostics" not in h.get("/infra/capabilities").json()["operations"]
        assert h.get("/infra/nodes/p-1?diagnostics=true").json() == {"state": "running"}


# ── Capabilities are validated before they are answered ──────────────────────


def _valid() -> CapabilitiesResponse:
    return CapabilitiesResponse(
        provider="nodes",
        display_name="Nodes",
        account=AccountSchema(noun="A", fields=[FormField(key="account_id", label="Id")]),
        access_methods=[AccessMethod(kind="KEYLESS", label="K", fields=[FormField(key="role_name", label="R")])],
        nodes=NodeCapabilities(),
    )


def test_a_valid_declaration_has_no_problems() -> None:
    assert capabilities_problems(_Nodes(), _valid()) == []


@pytest.mark.parametrize(
    ("spoil", "says"),
    [
        (lambda c: setattr(c, "provider", "Not A Slug"), "provider"),
        (lambda c: setattr(c, "contract", 1), "contract"),
        (lambda c: c.account.fields.append(FormField(key="account_id", label="Again")), "listed twice"),
        (lambda c: c.account.fields.append(FormField(key="Bad-Key", label="B")), "does not match"),
        (lambda c: c.account.defaults.append(FormField(key="account_id", label="D")), "both a field and a default"),
        (lambda c: c.access_methods[0].fields.append(FormField(key="account_id", label="X")), "also an account field"),
        (
            lambda c: c.access_methods[0].fields.append(FormField(key="k", label="K", secret=True)),
            "secret true exactly",
        ),
        (
            lambda c: c.access_methods[0].fields.append(FormField(key="k", label="K", kind="secret")),
            "secret true exactly",
        ),
        (lambda c: c.access_methods[0].fields.append(FormField(key="k", label="K", max_length=0)), "max_length"),
        (lambda c: c.access_methods.append(AccessMethod(kind="KEYLESS", label="Again")), "more than one access method"),
        (
            lambda c: (
                setattr(c.access_methods[0], "preferred", True),
                c.access_methods.append(AccessMethod(kind="AMBIENT", label="A", preferred=True)),
            ),
            "more than one is preferred",
        ),
        (
            lambda c: setattr(
                c,
                "permissions",
                PermissionSchema(
                    options=[
                        PermissionOption(key="o", label="O", purpose="nodes"),
                        PermissionOption(key="o", label="O2", purpose="nodes"),
                    ]
                ),
            ),
            "listed twice",
        ),
        (lambda c: setattr(c, "nodes", None), "nodes must be set exactly"),
        (
            lambda c: setattr(c, "deployments", DeploymentCapabilities(certificate="machine")),
            "deployments must be set exactly",
        ),
    ],
)
def test_a_declaration_that_breaks_a_rule_is_named_and_never_answered(spoil: Any, says: str, tmp_path: Path) -> None:
    caps = _valid()
    spoil(caps)
    problems = capabilities_problems(_Nodes(), caps)
    assert any(says in p for p in problems), problems

    class Spoiled(_Nodes):
        def capabilities(self) -> CapabilitiesResponse:
            return caps

    with PluginHarness(Spoiled(), plugin_id="nodes", config_root=tmp_path) as h:
        r = h.get("/infra/capabilities")
    assert r.status_code == 502 and r.json()["code"] == "provider_error"
    assert says in r.json()["detail"] and r.json()["remedy"]


def test_readiness_comes_from_the_settings_layer_as_form_fields(tmp_path: Path) -> None:
    class WithSettings(_Nodes):
        settings = PluginSettings(FakeSettings, "nodes")

        def capabilities(self) -> CapabilitiesResponse:
            return CapabilitiesResponse(
                provider="nodes", display_name="N", nodes=NodeCapabilities(), **self.readiness()
            )

    assert _Nodes().readiness() == {"ready": True, "missing_fields": []}
    with PluginHarness(WithSettings(), plugin_id="nodes", config_root=tmp_path) as h:
        caps = h.get("/infra/capabilities").json()
        assert caps["ready"] is False
        assert [(f["key"], f["kind"], f["secret"]) for f in caps["missing_fields"]] == [("api_key", "secret", True)]
        h.post("/settings", json_body={"api_key": "hunter2-key"})
        caps = h.get("/infra/capabilities").json()
        assert caps["ready"] is True and caps["missing_fields"] == []
        assert h.get("/settings", headers=_AMBIENT).status_code == 400, "settings are never sent a Connection"


# ── Typed errors ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (ProviderError("upstream said no"), 502, "provider_error"),
        (InvalidRequest("bad id"), 400, "invalid_request"),
        (PermissionDenied("not allowed to run instances"), 403, "permission_denied"),
        (NotFound("no such node"), 404, "not_found"),
        (NotConfigured("no key"), 409, "not_configured"),
        (Conflict("bucket not empty"), 409, "conflict"),
        (CredentialUnavailable("the role refused"), 424, "credential_unavailable"),
        (CredentialExpired("the session ended"), 424, "credential_expired"),
        (RateLimited("slow down", retry_after_s=3), 429, "rate_limited"),
        (NotSupported("nope"), 501, "not_supported"),
        (ValueError("not a region"), 400, "invalid_request"),
        (TypeError("wrong type"), 400, "invalid_request"),
        (NotImplementedError("later"), 501, "not_supported"),
        (RuntimeError("the cloud exploded"), 502, "provider_error"),
        (KeyError("k"), 502, "provider_error"),
        (HTTPException(status_code=418, detail="teapot"), 418, "provider_error"),
        (HTTPException(status_code=403, detail="no"), 403, "permission_denied"),
    ],
)
def test_what_a_method_raises_answers_its_status_code_and_sentence(exc: BaseException, status: int, code: str) -> None:
    plugin = _Nodes()
    plugin.exc = exc
    with PluginHarness(plugin, plugin_id="nodes") as h:
        r = h.get("/infra/nodes/x")
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == _ERROR_KEYS and body["code"] == code and code in ERROR_CODES
    assert isinstance(body["detail"], str) and body["detail"] != "Internal Server Error"
    assert body["retry_after_s"] == (3 if code == "rate_limited" else None)


def test_detail_and_remedy_are_scrubbed_of_the_plugins_secrets_and_the_requests() -> None:
    plugin = _Nodes()
    plugin.exc = PermissionDenied("key hunter2 may not start tok-123", remedy="Grant it to hunter2 in the console.")
    with PluginHarness(plugin, plugin_id="nodes") as h:
        r = h.post("/infra/nodes", json_body={"node_id": "n", "node_type": "t", "token": "tok-123"})
    assert r.status_code == 403
    assert r.json()["detail"] == "key *** may not start ***"
    assert r.json()["remedy"] == "Grant it to *** in the console."
    assert scrub("a bbbb", ["", "bbbb"]) == "a ***"
    assert scrub("ghost t", ["t"]) == "ghost t", "a value too short to be a secret is left alone"


def test_the_requests_own_credential_is_scrubbed_from_an_error(tmp_path: Path) -> None:
    class Leaky(FakeProvider):
        def node_state(self, provider_id: str) -> NodeStateResponse:
            from tlc_plugin_sdk.connections import current_credential

            msg = f"the cloud rejected {getattr(current_credential(), 'access_token', '')}"
            raise RuntimeError(msg)

    headers = connection_headers(ConnectionBinding("conn-9", "fake", "KEYLESS", access={"role_name": "r"}))
    with PluginHarness(Leaky(), plugin_id="fake", config_root=tmp_path) as h:
        r = h.get("/infra/nodes/x", headers=headers)
    assert r.status_code == 502 and r.json()["detail"] == "the cloud rejected ***"


def test_describe_error_words_an_unexpected_exception_and_is_still_scrubbed() -> None:
    class Describing(_Nodes):
        def describe_error(self, exc: Exception) -> str:
            if "boom" in str(exc):
                msg = "hook failed"
                raise RuntimeError(msg)
            return "The cloud refused the call with key hunter2." if "arn:" in str(exc) else ""

    plugin = Describing()
    with PluginHarness(plugin, plugin_id="nodes") as h:
        plugin.exc = RuntimeError("AccessDenied for arn:x (request id 42)")
        assert h.get("/infra/nodes/x").json()["detail"] == "The cloud refused the call with key ***."
        plugin.exc = RuntimeError("hunter2 is wrong")
        assert h.get("/infra/nodes/x").json()["detail"] == "*** is wrong", "an empty answer falls back, scrubbed"
        plugin.exc = RuntimeError("boom")
        assert h.get("/infra/nodes/x").json()["detail"] == "boom", "a hook that raises falls back too"
        plugin.exc = ProviderError("worded by the provider: arn:x")
        assert "arn:x" in h.get("/infra/nodes/x").json()["detail"], "a ProviderError keeps its own sentence"
    assert _Nodes().describe_error(RuntimeError("key hunter2")) == "key ***"
    assert _Nodes().describe_error(KeyError()) == "KeyError"


def test_a_malformed_body_is_a_contract_error_too() -> None:
    with PluginHarness(_Nodes(), plugin_id="nodes") as h:
        r = h.call("POST", "/infra/nodes", content=b"{not json", headers={"content-type": "application/json"})
        assert r.status_code == 400 and set(r.json()) == _ERROR_KEYS and r.json()["code"] == "invalid_request"
        r = h.post("/infra/nodes", json_body={"node_type": "t"})
        assert r.status_code == 400 and "node_id" in r.json()["detail"]


def test_a_private_route_answers_the_same_error_body_with_the_handlers() -> None:
    from litestar import get

    class Private(HubPlugin):
        def get_ui_fragment(self) -> str:
            return ""

        def get_route_handlers(self) -> list[Any]:
            plugin = _Nodes()

            def refuse() -> dict[str, Any]:
                msg = "no machine by that name"
                raise NotFound(msg)

            @get("/with", sync_to_thread=True, exception_handlers=error_handlers())
            def with_handlers() -> dict[str, Any]:
                return answer(plugin, refuse)

            @get("/without", sync_to_thread=True)
            def without_handlers() -> dict[str, Any]:
                return answer(plugin, refuse)

            return [with_handlers, without_handlers]

    with PluginHarness(Private(), plugin_id="private") as h:
        assert h.get("/with").json() == {
            "detail": "no machine by that name",
            "code": "not_found",
            "remedy": "",
            "retry_after_s": None,
        }
        plain = h.get("/without")
        assert plain.status_code == 404 and plain.json()["detail"] == "no machine by that name"
        assert plain.json()["extra"]["code"] == "not_found"
