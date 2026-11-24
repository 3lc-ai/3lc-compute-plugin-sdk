# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""A request's Connection is resolved by the worker's middleware and visible to its handler only."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from litestar import get

from tlc_plugin_sdk import connections
from tlc_plugin_sdk.connections import (
    BOUND_CREDENTIAL_HEADER,
    CONNECTION_HEADER,
    CONNECTION_USE_HEADER,
    MAX_BINDING_BYTES,
    MAX_CREDENTIAL_BYTES,
    Ambient,
    ConnectionBinding,
    ConnectionPermissions,
    ConnectionUse,
    CredentialUnavailable,
    ProviderCredential,
    ResolveRequest,
    SecretToken,
    StorageRule,
    credential_as,
    encode_binding,
    encode_credential,
    encode_use,
    register_resolver,
)
from tlc_plugin_sdk.contract import HubPlugin
from tlc_plugin_sdk.harness import PluginHarness
from tlc_plugin_sdk.infrastructure import RateLimited

_ERROR_KEYS = {"detail", "code", "remedy", "retry_after_s"}


@dataclass(frozen=True)
class _Session:
    """A plugin's own credential class: what its resolver returns."""

    token: str
    expires_at: str = ""

    def secret_values(self) -> list[str]:
        return [self.token]


def _describe() -> dict[str, Any]:
    binding = connections.current_connection()
    credential = connections.current_credential()
    use = connections.current_use()
    return {
        "connection": binding.id if binding else None,
        "use": [use.resource_id, use.source_identity] if use else None,
        "credential": type(credential).__name__ if credential else None,
        "account": getattr(credential, "account", None),
        "token": getattr(credential, "token", None),
    }


_NOTHING = {"connection": None, "use": None, "credential": None, "account": None, "token": None}


@get("/threaded", sync_to_thread=True)
def _threaded() -> dict[str, Any]:
    return _describe()


@get("/async")
async def _async() -> dict[str, Any]:
    return _describe()


@get("/infra/capabilities", sync_to_thread=True)
def _capabilities() -> dict[str, Any]:
    return _describe()


@get("/infra/dns/records", sync_to_thread=True)
def _dns() -> dict[str, Any]:
    return _describe()


class _Probe(HubPlugin):
    def get_ui_fragment(self) -> str:
        return ""

    def get_route_handlers(self) -> list[Any]:
        return [_threaded, _async, _capabilities, _dns]


@pytest.fixture
def harness(tmp_path: Any) -> Iterator[PluginHarness]:
    with PluginHarness(_Probe(), plugin_id="probe", config_root=tmp_path) as h:
        yield h


@pytest.fixture
def clean_resolvers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connections, "_RESOLVERS", {})


def _binding(kind: Any = "AMBIENT", **access: str) -> ConnectionBinding:
    return ConnectionBinding(
        "conn-1", "aws", kind, account={"account_id": "123456789012"}, access=access, defaults={"location": "eu-west-1"}
    )


def _header(kind: Any = "AMBIENT", **access: str) -> dict[str, str]:
    return {CONNECTION_HEADER: encode_binding(_binding(kind, **access))}


# ── the binding ──────────────────────────────────────────────────────────────


def test_a_binding_round_trips_and_names_no_vendor_concept() -> None:
    binding = ConnectionBinding(
        "conn-1",
        "azure",
        "KEYLESS",
        account={"tenant_id": "t", "subscription_id": "s", "resource_group": "rg"},
        access={"identity_name": "3lc"},
        defaults={"location": "westeurope"},
        permissions=ConnectionPermissions(
            nodes=True, storage=[StorageRule("abfss://c@a.dfs.core.windows.net", "read")]
        ),
        grant_ref="/subscriptions/s/deployments/3lc",
    )
    wire = json.loads(encode_binding(binding))
    assert set(wire) == {"id", "provider", "kind", "account", "access", "defaults", "permissions", "grant_ref"}
    assert ConnectionBinding.from_json(encode_binding(binding)) == binding
    assert ConnectionBinding.from_json('{"id":"c","provider":"x","kind":"ambient"}') == ConnectionBinding(
        "c", "x", "AMBIENT"
    )
    assert (
        ConnectionBinding.from_json('{"id":"c","provider":"x","kind":"SECRET","permissions":null}').permissions is None
    )


def test_a_binding_reads_scalar_field_values_as_text_and_ignores_keys_it_does_not_know() -> None:
    raw = (
        '{"id":"c","provider":"aws","kind":"KEYLESS","access":{"session_duration_s":3600,"x":null,"n":{"a":1}},"new":1}'
    )
    assert ConnectionBinding.from_json(raw).access == {"session_duration_s": "3600"}


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        "[]",
        '{"id":"c","provider":"aws"}',
        '{"id":"c","provider":"aws","kind":"WISHFUL"}',
        '{"id":"c","provider":"aws","kind":"AMBIENT","access":[]}',
        '{"id":"c","provider":"aws","kind":"AMBIENT","permissions":[]}',
    ],
)
def test_a_malformed_header_is_400(harness: PluginHarness, value: str) -> None:
    response = harness.get("/threaded", headers={CONNECTION_HEADER: value})
    assert response.status_code == 400
    assert set(response.json()) == _ERROR_KEYS and response.json()["code"] == "invalid_request"


def test_the_transitional_configuration_travels_beside_the_permissions() -> None:
    binding = ConnectionBinding(
        "conn-1", "aws", "KEYLESS", access={"role_arn": "role"}, configuration={"node_storage": True}
    )
    assert ConnectionBinding.from_json(encode_binding(binding)) == binding
    assert "configuration" not in json.loads(encode_binding(_binding()))
    with pytest.raises(ValueError, match="configuration must be an object"):
        ConnectionBinding.from_json('{"id":"c","provider":"aws","kind":"KEYLESS","configuration":[]}')


def test_a_binding_over_8_kib_is_refused_with_a_sentence_naming_the_connection(harness: PluginHarness) -> None:
    rules = [StorageRule(f"s3://bucket-{i}/" + "p" * 240, "read") for i in range(40)]
    big = ConnectionBinding("conn-big", "aws", "AMBIENT", permissions=ConnectionPermissions(storage=rules))
    assert len(encode_binding(big)) > MAX_BINDING_BYTES
    response = harness.get("/threaded", headers={CONNECTION_HEADER: encode_binding(big)})
    assert response.status_code == 400 and response.json()["code"] == "invalid_request"
    assert "conn-big" in response.json()["detail"] and "storage rules" in response.json()["detail"]
    twenty = [StorageRule("s3://" + "b" * 251, "read_write_no_delete") for _ in range(20)]
    full = ConnectionBinding("conn-full", "aws", "AMBIENT", permissions=ConnectionPermissions(storage=twenty))
    assert len(encode_binding(full)) < MAX_BINDING_BYTES, "twenty rules of the longest length fit"
    assert harness.get("/threaded", headers={CONNECTION_HEADER: encode_binding(full)}).status_code == 200


# ── resolution ───────────────────────────────────────────────────────────────


def test_no_header_means_no_connection(harness: PluginHarness) -> None:
    assert harness.get("/threaded").json() == _NOTHING


@pytest.mark.parametrize("route", ["/threaded", "/async"])
def test_ambient_resolves_in_the_sdk_with_the_account_and_defaults(harness: PluginHarness, route: str) -> None:
    body = harness.get(route, headers=_header()).json()
    assert body["connection"] == "conn-1" and body["credential"] == "Ambient"
    assert body["account"] == {"account_id": "123456789012"}
    assert connections.resolve(_binding()) == Ambient("aws", {"account_id": "123456789012"}, {"location": "eu-west-1"})


def test_nothing_leaks_into_the_next_request(harness: PluginHarness) -> None:
    harness.get("/threaded", headers=_header())
    assert harness.get("/threaded").json() == _NOTHING


def test_a_keyless_binding_goes_to_the_plugins_resolver_with_the_use(
    harness: PluginHarness, clean_resolvers: None
) -> None:
    seen: list[ResolveRequest] = []

    def resolver(request: ResolveRequest) -> _Session:
        seen.append(request)
        return _Session(token="session-1")

    register_resolver("aws", resolver)
    headers = {
        **_header("KEYLESS", role_arn="role"),
        CONNECTION_USE_HEADER: encode_use(ConnectionUse("node-1", "alice")),
    }
    body = harness.get("/threaded", headers=headers).json()
    assert body["credential"] == "_Session" and body["token"] == "session-1"
    assert body["use"] == ["node-1", "alice"]
    assert seen == [ResolveRequest(_binding("KEYLESS", role_arn="role"), ConnectionUse("node-1", "alice"))]
    assert isinstance(_Session("t"), ProviderCredential)


def test_a_resolver_may_answer_ambient_for_a_machine_that_is_already_the_identity(
    harness: PluginHarness, clean_resolvers: None
) -> None:
    register_resolver("aws", lambda request: Ambient(request.binding.provider, dict(request.binding.account)))
    assert harness.get("/threaded", headers=_header("KEYLESS")).json()["credential"] == "Ambient"


def test_keyless_without_a_resolver_is_424(harness: PluginHarness, clean_resolvers: None) -> None:
    response = harness.get("/threaded", headers=_header("KEYLESS"))
    assert response.status_code == 424
    assert response.json()["code"] == "credential_unavailable" and "KEYLESS" in response.json()["detail"]


def test_what_a_resolver_raises_is_a_worded_answer_never_a_500(harness: PluginHarness, clean_resolvers: None) -> None:
    def refuse(request: ResolveRequest) -> _Session:
        msg = "The role refused this host"
        raise CredentialUnavailable(msg, remedy="Add this host to the role's trust policy.")

    register_resolver("aws", refuse)
    response = harness.get("/threaded", headers=_header("KEYLESS"))
    assert response.status_code == 424
    assert response.json() == {
        "detail": "The role refused this host",
        "code": "credential_unavailable",
        "remedy": "Add this host to the role's trust policy.",
        "retry_after_s": None,
    }

    def throttled(request: ResolveRequest) -> _Session:
        msg = "The token service is throttling"
        raise RateLimited(msg, retry_after_s=2.5)

    register_resolver("aws", throttled)
    response = harness.get("/threaded", headers=_header("KEYLESS"))
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited" and response.json()["retry_after_s"] == 2.5

    def broken(request: ResolveRequest) -> _Session:
        msg = "secret-looking internals"
        raise RuntimeError(msg)

    register_resolver("aws", broken)
    response = harness.get("/threaded", headers=_header("KEYLESS"))
    assert response.status_code == 424 and "RuntimeError" in response.json()["detail"]
    assert "internals" not in response.text


def test_credential_as_hands_back_the_plugins_own_class() -> None:
    with pytest.raises(CredentialUnavailable, match="needs a _Session and the request carries no credential"):
        credential_as(_Session)
    token = connections._CREDENTIAL.set(_Session("session-1"))
    try:
        assert credential_as(_Session).token == "session-1"
        with pytest.raises(CredentialUnavailable, match="carries a _Session"):
            credential_as(Ambient)
    finally:
        connections._CREDENTIAL.reset(token)


# ── the use ──────────────────────────────────────────────────────────────────


def test_a_use_without_a_connection_is_ignored(harness: PluginHarness) -> None:
    headers = {CONNECTION_USE_HEADER: encode_use(ConnectionUse("node-1", "alice"))}
    assert harness.get("/threaded", headers=headers).json()["use"] is None


@pytest.mark.parametrize("value", ["not json", "[]", '{"resource_id": 3}'])
def test_a_malformed_use_is_400(harness: PluginHarness, value: str) -> None:
    response = harness.get("/threaded", headers={**_header(), CONNECTION_USE_HEADER: value})
    assert response.status_code == 400
    assert CONNECTION_USE_HEADER in response.json()["detail"]


# ── routes that read the host's own identity ─────────────────────────────────


@pytest.mark.parametrize("route", ["/infra/capabilities", "/infra/dns/records"])
@pytest.mark.parametrize("header", [CONNECTION_HEADER, CONNECTION_USE_HEADER, BOUND_CREDENTIAL_HEADER])
def test_an_ambient_only_route_refuses_every_connection_header(
    harness: PluginHarness, clean_resolvers: None, route: str, header: str
) -> None:
    called: list[ResolveRequest] = []

    def resolver(request: ResolveRequest) -> _Session:
        called.append(request)
        return _Session("s")

    register_resolver("aws", resolver)
    values = {
        CONNECTION_HEADER: encode_binding(_binding("KEYLESS")),
        CONNECTION_USE_HEADER: encode_use(ConnectionUse("node-1", "alice")),
        BOUND_CREDENTIAL_HEADER: encode_credential(SecretToken("aws", "the-value", "conn-1")),
    }
    response = harness.get(route, headers={header: values[header]})
    assert response.status_code == 400 and response.json()["code"] == "invalid_request"
    assert "the-value" not in response.text
    assert not called, "nothing is resolved for a route that may not see a Connection"
    assert harness.get(route).json() == _NOTHING


# ── a SECRET Connection: the binding beside its value ────────────────────────


def _secret_headers(*, value_for: str = "conn-1", service: str = "ssh", use: bool = True) -> dict[str, str]:
    binding = ConnectionBinding("conn-1", "ssh", "SECRET", access={"endpoint_url": "https://api.example"})
    headers = {
        CONNECTION_HEADER: encode_binding(binding),
        BOUND_CREDENTIAL_HEADER: encode_credential(SecretToken(service, "the-value", value_for)),
    }
    if use:
        headers[CONNECTION_USE_HEADER] = encode_use(ConnectionUse("node-1", "alice"))
    return headers


@pytest.mark.parametrize("route", ["/threaded", "/async"])
def test_a_secret_connection_is_its_binding_its_use_and_its_value(harness: PluginHarness, route: str) -> None:
    """Like any other kind in the handler: the Connection, what it is used for, and a credential."""
    body = harness.get(route, headers=_secret_headers()).json()
    assert body["connection"] == "conn-1"
    assert body["use"] == ["node-1", "alice"]
    assert body["credential"] == "SecretToken"
    assert harness.get(route).json() == _NOTHING, "nothing of it is left for the next request"


def test_a_secret_binding_needs_no_resolver(harness: PluginHarness, clean_resolvers: None) -> None:
    assert harness.get("/threaded", headers=_secret_headers()).status_code == 200
    with pytest.raises(CredentialUnavailable, match="arrives with the request"):
        connections.resolve(ConnectionBinding("conn-1", "ssh", "SECRET"))


def test_a_secret_binding_without_its_value_is_424(harness: PluginHarness) -> None:
    headers = _secret_headers()
    del headers[BOUND_CREDENTIAL_HEADER]
    response = harness.get("/threaded", headers=headers)
    assert response.status_code == 424 and "carries no value" in response.json()["detail"]
    assert response.json()["code"] == "credential_unavailable"


@pytest.mark.parametrize("mismatch", [{"value_for": "conn-2"}, {"service": "huggingface"}])
def test_a_value_granted_for_another_connection_or_service_is_never_bound_under_this_one(
    harness: PluginHarness, mismatch: dict[str, str]
) -> None:
    response = harness.get("/threaded", headers=_secret_headers(**mismatch))
    assert response.status_code == 424 and "different Connections" in response.json()["detail"]
    assert "the-value" not in response.text


def test_a_malformed_value_beside_a_secret_binding_is_the_value_middlewares_400(harness: PluginHarness) -> None:
    headers = {**_secret_headers(), BOUND_CREDENTIAL_HEADER: "not json"}
    assert harness.get("/threaded", headers=headers).status_code == 400


def test_a_value_over_4_kib_is_refused(harness: PluginHarness) -> None:
    big = encode_credential(SecretToken("ssh", "k" * MAX_CREDENTIAL_BYTES, "conn-1"))
    response = harness.get("/threaded", headers={BOUND_CREDENTIAL_HEADER: big})
    assert response.status_code == 400 and "k" * 40 not in response.text


def test_a_value_of_several_fields_is_a_json_object_of_strings() -> None:
    token = SecretToken("runpod", json.dumps({"api_key": "rpa_1", "volume_access_key": "", "volume_secret_key": "s3s"}))
    assert token.fields() == {"api_key": "rpa_1", "volume_secret_key": "s3s"}
    assert set(token.secret_values()) == {token.secret, "rpa_1", "s3s"}
    for bad in ("rpa_plain", "[1]", '{"api_key": 3}'):
        with pytest.raises(CredentialUnavailable) as raised:
            SecretToken("runpod", bad).fields()
        assert bad not in str(raised.value)
    assert SecretToken("runpod", "rpa_plain").secret_values() == ["rpa_plain"]


def test_secret_token_repr_and_from_wire_hide_and_tolerate() -> None:
    token = SecretToken.from_wire({"connection_id": "c", "provider": "huggingface", "secret": "hf_x"})
    assert token is not None and "hf_x" not in repr(token)
    assert SecretToken.from_wire({"secret": ""}) is None
    assert SecretToken.from_wire("hf_x") is None
