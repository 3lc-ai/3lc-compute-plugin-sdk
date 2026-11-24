# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The host-pushed identity token of a KEYLESS Connection: header rules, resolver input, conformance."""

from __future__ import annotations

from pathlib import Path

import pytest

from tlc_plugin_sdk.connections import (
    CONNECTION_TOKEN_HEADER,
    ConnectionBinding,
    IdentityToken,
    current_identity_token,
    encode_identity_token,
)
from tlc_plugin_sdk.harness import PluginHarness
from tlc_plugin_sdk.infrastructure import VerifyRequest
from tlc_plugin_sdk.infrastructure.testing import FakeProvider, acting_as, check_provider, connection_headers

AUDIENCE = "api://fake-token-exchange"
BINDING = ConnectionBinding(id="conn-1", provider="fake", kind="KEYLESS", account={"account_id": "000000000000"})
SECRET_JWT = "eyJhbGciOiJSUzI1NiJ9.secret-claims.secret-signature"


def _token(connection_id: str = "conn-1", audience: str = AUDIENCE) -> IdentityToken:
    return IdentityToken(
        connection_id=connection_id, audience=audience, token=SECRET_JWT, expires_at="2099-01-01T00:00:00Z"
    )


@pytest.fixture
def fake() -> FakeProvider:
    provider = FakeProvider()
    provider.require_identity_token(AUDIENCE)
    return provider


def _verify(h: PluginHarness, headers: dict[str, str]) -> tuple[int, str]:
    r = h.call("POST", "/infra/connection/verify", json_body=VerifyRequest().to_dict(), headers=headers)
    return r.status_code, r.text


def test_the_token_reaches_the_resolver(fake: FakeProvider, tmp_path: Path) -> None:
    with PluginHarness(fake, plugin_id="fake", config_root=tmp_path) as h:
        status, text = _verify(h, connection_headers(BINDING, token=_token()))
    assert status == 200, text
    assert fake.identity_tokens == ["conn-1"]


@pytest.mark.parametrize(
    ("token", "words"),
    [
        (None, "carried none"),
        (_token(connection_id="conn-2"), "another Connection"),
        (_token(audience="api://elsewhere"), "another audience"),
    ],
)
def test_a_missing_or_foreign_token_answers_424(
    fake: FakeProvider, tmp_path: Path, token: IdentityToken | None, words: str
) -> None:
    with PluginHarness(fake, plugin_id="fake", config_root=tmp_path) as h:
        status, text = _verify(h, connection_headers(BINDING, token=token))
    assert status == 424
    assert words in text
    assert SECRET_JWT not in text
    assert fake.identity_tokens == []


def test_a_token_is_refused_on_ambient_only_routes_and_without_a_connection(fake: FakeProvider, tmp_path: Path) -> None:
    header = {CONNECTION_TOKEN_HEADER: encode_identity_token(_token())}
    with PluginHarness(fake, plugin_id="fake", config_root=tmp_path) as h:
        assert h.call("GET", "/infra/capabilities", headers=header).status_code == 400
        status, text = _verify(h, header)
        assert status == 400
        assert SECRET_JWT not in text


def test_a_malformed_token_header_is_400_and_never_quoted(fake: FakeProvider, tmp_path: Path) -> None:
    headers = connection_headers(BINDING)
    headers[CONNECTION_TOKEN_HEADER] = '{"token": "' + SECRET_JWT + '"}'
    with PluginHarness(fake, plugin_id="fake", config_root=tmp_path) as h:
        status, text = _verify(h, headers)
    assert status == 400
    assert SECRET_JWT not in text


def test_a_resolver_error_never_echoes_the_token(fake: FakeProvider, tmp_path: Path) -> None:
    refused = ConnectionBinding(id="conn-1", provider="fake", kind="KEYLESS", access={"role_name": "refused"})
    with PluginHarness(fake, plugin_id="fake", config_root=tmp_path) as h:
        status, text = _verify(h, connection_headers(refused, token=_token()))
    assert status == 424
    assert SECRET_JWT not in text


def test_a_method_without_identity_token_ignores_none(tmp_path: Path) -> None:
    with PluginHarness(FakeProvider(), plugin_id="fake", config_root=tmp_path) as h:
        status, text = _verify(h, connection_headers(BINDING))
    assert status == 200, text


def test_the_token_never_shows_in_a_repr() -> None:
    assert SECRET_JWT not in repr(_token())


def test_acting_as_sets_the_token() -> None:
    with acting_as(BINDING, token=_token()):
        assert current_identity_token() == _token()
    assert current_identity_token() is None


def test_conformance_covers_a_token_method(fake: FakeProvider, tmp_path: Path) -> None:
    headers = connection_headers(BINDING, token=_token())
    report = check_provider(fake, plugin_id="fake", config_root=tmp_path, headers=headers)
    assert report.ok, report.text()
    names = {c.name for c in report.checks}
    assert "connect: a KEYLESS call without a token answers 424" in names
    assert "connect: the KEYLESS resolver is registered with the declared token audience" in names


def test_conformance_names_an_unregistered_audience(tmp_path: Path) -> None:
    provider = FakeProvider()
    provider.identity_token_audience = AUDIENCE  # declared, but the resolver was registered without it
    report = check_provider(provider, plugin_id="fake", config_root=tmp_path, skip=("storage", "routes", "dns"))
    failed = {c.name for c in report.checks if not c.ok}
    assert "connect: the KEYLESS resolver is registered with the declared token audience" in failed
