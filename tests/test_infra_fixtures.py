# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The golden fixtures of the contract stay in step with the wire types."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import contract_fixtures
import pytest

from tlc_plugin_sdk import connections
from tlc_plugin_sdk import infrastructure as infra
from tlc_plugin_sdk.infrastructure import CapabilitiesResponse, capabilities_problems

_EXAMPLES = contract_fixtures.build_examples()

#: A fixture whose stem is not its type's name: the same type in another role.
_VARIANTS = {"CreateNodeRequest.workspace": "CreateNodeRequest", "CreateNodeResponse.workspace": "CreateNodeResponse"}


def _wire_types() -> dict[str, Any]:
    types = {
        name: getattr(infra, name)
        for name in infra.__all__
        if dataclasses.is_dataclass(getattr(infra, name)) and hasattr(getattr(infra, name), "to_dict")
    }
    types["ConnectionBinding"] = connections.ConnectionBinding
    types["ConnectionUse"] = connections.ConnectionUse
    del types["SettingsField"]  # the settings layer's own description, never sent to a host
    return types


def _parse(cls: Any, example: Any) -> Any:
    if cls is connections.ConnectionBinding or cls is connections.ConnectionUse:
        return cls.from_json(json.dumps(example))
    return cls.from_dict(example)


@pytest.mark.parametrize("stem", sorted(_EXAMPLES))
def test_a_fixture_is_what_the_fake_provider_emits_now(stem: str) -> None:
    path = contract_fixtures.FIXTURES / f"{stem}.json"
    assert path.exists(), f"no fixture {path.name}: run `python tests/contract_fixtures.py --write`"
    assert path.read_text(encoding="utf-8") == contract_fixtures.render(_EXAMPLES[stem]), (
        f"{path.name} and its type have drifted: run `python tests/contract_fixtures.py --write` and review the diff"
    )


def test_no_fixture_is_left_without_a_type() -> None:
    assert sorted(p.stem for p in contract_fixtures.FIXTURES.glob("*.json")) == sorted(_EXAMPLES)


@pytest.mark.parametrize("name", sorted(_wire_types()))
def test_every_wire_type_has_a_fixture_that_carries_each_of_its_fields(name: str) -> None:
    assert name in _EXAMPLES, f"{name} has no fixture: add it to tests/contract_fixtures.py"
    cls = _wire_types()[name]
    example = _EXAMPLES[name]["example"]
    message = _parse(cls, example)
    again = message.to_dict()
    assert again == example, "the type reads its own fixture and writes it back unchanged"
    optional = {
        f.name
        for f in dataclasses.fields(cls)
        if f.name not in again
        and f.name not in ("flavor", "workspace", "services", "placement", "ingress", "configuration")
    }
    carried = {stem for stem, kind in _VARIANTS.items() if kind == name}
    for stem in carried:
        optional -= set(_EXAMPLES[stem]["example"])
    allowed = _MAY_BE_ABSENT.get(name, set())
    assert optional <= allowed, f"{name}: the fixture shows no {sorted(optional - allowed)}"


#: Fields a fixture may leave out because the key is omitted when unset and the fake has nothing to say.
_MAY_BE_ABSENT: dict[str, set[str]] = {
    "CreateNodeResponse": {"token", "detail"},
    "NodeStateResponse": {"detail", "bootstrap_stage", "diagnostics"},
    "ObjectListing": {"next_token"},
    "PresignResponse": {"cors"},
    "Region": {"location"},
    "StorageItem": {"managed_by"},
    "StorageListing": {"capabilities", "account", "region"},
    "StorageCapabilities": {"upload_hint"},
    "ErrorAnswer": set(),
    "Offer": set(),
    "CostNote": {"reused"},
    "WorkspaceInstance": {"extra"},
    "CreateStorageRequest": {"size_gb"},
}


@pytest.mark.parametrize("stem", sorted(_VARIANTS))
def test_a_variant_fixture_reads_as_its_type(stem: str) -> None:
    cls = _wire_types()[_VARIANTS[stem]]
    example = _EXAMPLES[stem]["example"]
    assert cls.from_dict(example).to_dict() == example


@pytest.mark.parametrize("stem", sorted(s for s in _EXAMPLES if s.startswith("capabilities.")))
def test_the_declared_capabilities_are_valid_and_say_what_their_provider_is(stem: str) -> None:
    example = _EXAMPLES[stem]["example"]
    caps = CapabilitiesResponse.from_dict(example)
    assert caps.to_dict() == example
    assert caps.contract == 2
    plugin = next(cls for name, cls in contract_fixtures._DECLARED if f"capabilities.{name}" == stem)()
    assert capabilities_problems(plugin, caps) == []
    kinds = [m.kind for m in caps.access_methods]
    preferred = [m.kind for m in caps.access_methods if m.preferred]
    if caps.provider == "aws":
        assert kinds == ["KEYLESS", "AMBIENT"] and preferred == ["KEYLESS"]
        assert [f.key for f in caps.account.fields] == ["account_id"] if caps.account else False
        assert caps.host is not None and set(caps.host.matches) == {"setup_identity"}
        assert (caps.deployments is not None) == ("deployment.create" in caps.operations)
        assert ("workspace.create" in caps.operations) != ("deployment.create" in caps.operations)
    if caps.provider == "azure":
        assert (
            [f.key for f in caps.account.fields] == ["tenant_id", "subscription_id", "resource_group"]
            if caps.account
            else False
        )
        assert caps.host is not None and caps.host.matches == {}
        assert caps.permissions.storage is not None and caps.permissions.storage.access_modes == ["read", "read_write"]
        assert [m.available for m in caps.access_methods] == [True, False]
    if caps.provider == "runpod":
        assert caps.account is not None and caps.account.fields == []
        assert kinds == ["SECRET"] and all(f.secret for f in caps.access_methods[0].fields)
        assert caps.nodes is not None and caps.nodes.storage_access == []
        assert caps.deployments is None and caps.permissions.storage is None
    if caps.provider == "machines":
        assert caps.account is None and kinds == [] and not caps.requires_connection
        assert caps.nodes is not None and caps.nodes.type_connections
        assert caps.operations == ["node.create", "node.state", "node.delete", "node.check"]
