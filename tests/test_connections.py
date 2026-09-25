# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""A request's Connection is resolved by the worker's middleware and visible to its handler only."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from litestar import get

from tlc_plugin_sdk import connections
from tlc_plugin_sdk.connections import (
    CONNECTION_HEADER,
    Ambient,
    AwsSession,
    ConnectionBinding,
    CredentialUnavailable,
    encode_binding,
    register_resolver,
)
from tlc_plugin_sdk.contract import HubPlugin
from tlc_plugin_sdk.harness import PluginHarness


def _describe() -> dict[str, Any]:
    binding = connections.current_connection()
    credential = connections.current_credential()
    return {
        "connection": binding.id if binding else None,
        "credential": type(credential).__name__ if credential else None,
        "region": getattr(credential, "region", None),
        "session_token": getattr(credential, "session_token", None),
    }


@get("/threaded", sync_to_thread=True)
def _threaded() -> dict[str, Any]:
    return _describe()


@get("/async")
async def _async() -> dict[str, Any]:
    return _describe()


class _Probe(HubPlugin):
    def get_ui_fragment(self) -> str:
        return ""

    def get_route_handlers(self) -> list[Any]:
        return [_threaded, _async]


@pytest.fixture
def harness(tmp_path: Any) -> Iterator[PluginHarness]:
    with PluginHarness(_Probe(), plugin_id="probe", config_root=tmp_path) as h:
        yield h


@pytest.fixture
def clean_resolvers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connections, "_RESOLVERS", {})


def _header(kind: str, **metadata: Any) -> dict[str, str]:
    return {CONNECTION_HEADER: encode_binding(ConnectionBinding("conn-1", "aws", kind, metadata))}


def test_no_header_means_no_connection(harness: PluginHarness) -> None:
    assert harness.get("/threaded").json() == {
        "connection": None,
        "credential": None,
        "region": None,
        "session_token": None,
    }


@pytest.mark.parametrize("route", ["/threaded", "/async"])
def test_ambient_resolves_in_the_sdk_for_sync_and_async_handlers(harness: PluginHarness, route: str) -> None:
    body = harness.get(route, headers=_header("ambient", region="eu-north-1")).json()
    assert body == {"connection": "conn-1", "credential": "Ambient", "region": "eu-north-1", "session_token": None}


def test_nothing_leaks_into_the_next_request(harness: PluginHarness) -> None:
    harness.get("/threaded", headers=_header("AMBIENT"))
    assert harness.get("/threaded").json()["connection"] is None


def test_a_registered_resolver_handles_its_kind(harness: PluginHarness, clean_resolvers: None) -> None:
    seen: list[ConnectionBinding] = []

    def assume(binding: ConnectionBinding) -> AwsSession:
        seen.append(binding)
        return AwsSession("AKIA123", "secret", "token", region="us-east-1")

    register_resolver("aws", "keyless", assume)
    body = harness.get("/async", headers=_header("KEYLESS", role_arn="arn:aws:iam::1:role/x")).json()
    assert body["credential"] == "AwsSession"
    assert body["session_token"] == "token"
    assert seen[0].metadata == {"role_arn": "arn:aws:iam::1:role/x"}


def test_an_unresolvable_kind_is_424(harness: PluginHarness, clean_resolvers: None) -> None:
    response = harness.get("/threaded", headers=_header("KEYLESS"))
    assert response.status_code == 424
    assert "cannot use KEYLESS Connections for aws" in response.json()["detail"]


def test_a_refusing_resolver_is_424(harness: PluginHarness, clean_resolvers: None) -> None:
    def refuse(binding: ConnectionBinding) -> AwsSession:
        msg = "role not assumable"
        raise CredentialUnavailable(msg)

    register_resolver("aws", "KEYLESS", refuse)
    response = harness.get("/threaded", headers=_header("KEYLESS"))
    assert (response.status_code, response.json()["detail"]) == (424, "role not assumable")


@pytest.mark.parametrize(
    "value",
    ["not json", "[]", '{"id": "c", "provider": "aws"}', '{"id":"c","provider":"aws","kind":"AMBIENT","metadata":[]}'],
)
def test_a_malformed_header_is_400(harness: PluginHarness, value: str) -> None:
    response = harness.get("/threaded", headers={CONNECTION_HEADER: value})
    assert response.status_code == 400


def test_ambient_cannot_be_replaced() -> None:
    with pytest.raises(ValueError, match="AMBIENT"):
        register_resolver("aws", "ambient", lambda b: Ambient("aws"))


def test_session_credentials_do_not_print_their_secret() -> None:
    text = repr(AwsSession("AKIAABCDEFGH", "very-secret", "tok", region="us-east-1"))
    assert "very-secret" not in text
    assert "'tok'" not in text
    assert "AKIAABCDEFGH" not in text
