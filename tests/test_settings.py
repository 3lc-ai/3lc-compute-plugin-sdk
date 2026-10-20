# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The settings layer: prompts from metadata, the merge rules, redaction, and re-hydrated nested rows."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk.infrastructure import PluginSettings, SettingsField, SettingsUnreadable, option, secret
from tlc_plugin_sdk.shared import config_store


def _check_region(value: Any) -> str:
    text = str(value or "").strip()
    if text not in ("us-east-1", "eu-north-1"):
        msg = f"'{text}' is not a region"
        raise ValueError(msg)
    return text


@dataclass
class Machine:
    name: str = ""
    address: str = ""
    agent_port: int = 8800
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Settings:
    id: str = "default"
    created: str = ""
    last_run: str | None = None
    api_key: str = secret(label="API key", help="Where it is", href="https://x", placeholder="key-…")
    aws_secret_access_key: str = secret(label="AWS secret", required=False)
    region: str = option("us-east-1", validate=_check_region)
    note: str = option("", validate=lambda v: str(v).upper(), clearable=True)
    placement: str = option("auto", coerce=lambda v: str(v or "").strip() or "auto")
    volume_gb: int = 100
    idle_ttl_s: int = 1800
    tls: bool = True
    ratio: float = 0.5
    node_types: list[str] = option(default_factory=lambda: ["g5.xlarge"], label="Instance types", required=True)
    extra_env: dict[str, str] = field(default_factory=dict)
    machines: list[Machine] = field(default_factory=list)
    name: str = ""

    def normalise(self) -> None:
        self.name = self.name.strip()


@pytest.fixture(autouse=True)
def _root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config_store, "CONFIG_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def settings() -> PluginSettings[Settings]:
    return PluginSettings(Settings, "probe")


def test_needs_a_dataclass() -> None:
    not_a_dataclass: Any = dict
    with pytest.raises(TypeError):
        PluginSettings(not_a_dataclass, "probe")


def test_load_defaults_when_never_saved_and_409_material_when_unreadable(
    settings: PluginSettings[Settings], _root: Path
) -> None:
    assert settings.load() == Settings()
    path = _root / "probe" / "default.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    with pytest.raises(SettingsUnreadable, match="fix or delete it"):
        settings.load()
    with pytest.raises(SettingsUnreadable):
        settings.save({"volume_gb": 1})


def test_prompts_readiness_and_field_view(settings: PluginSettings[Settings]) -> None:
    current = settings.load()
    assert settings.missing_fields(current) == [
        SettingsField(
            key="api_key", label="API key", secret=True, help="Where it is", href="https://x", placeholder="key-…"
        ),
        SettingsField(key="aws_secret_access_key", label="AWS secret", required=False, secret=True),
    ]
    assert settings.readiness(current) == {
        "missing_fields": settings.missing_fields(current),
        "missing": ["api_key"],
        "ready": False,
    }
    current.api_key = "k"
    current.node_types = []
    ready = settings.readiness(current)
    assert ready["missing"] == ["node_types"], "an empty described list is missing"
    assert [f["key"] for f in settings.field_view()] == ["api_key", "aws_secret_access_key", "node_types"]
    assert settings.secret_fields() == ["api_key", "aws_secret_access_key"]


def test_secret_merge_rules(settings: PluginSettings[Settings]) -> None:
    assert settings.save({"api_key": "one"}).api_key == "one"
    assert settings.save({"api_key": ""}).api_key == "one", "empty keeps"
    assert settings.save({"api_key": None}).api_key == "one"
    assert settings.save({"api_key": "-"}).api_key == "", "dash clears"


def test_annotation_driven_merge(settings: PluginSettings[Settings]) -> None:
    saved = settings.save({
        "volume_gb": "250",
        "idle_ttl_s": "not a number",
        "tls": "no",
        "ratio": "0.25",
        "node_types": [" a ", "", None, "b"],
        "extra_env": {"K": "v", "N": None},
        "name": "  spaced  ",
        "id": "spoof",
        "created": "spoof",
        "unknown": 1,
    })
    assert saved.volume_gb == 250
    assert saved.idle_ttl_s == 1800, "a failed int conversion keeps the current value"
    assert saved.tls is False
    assert saved.ratio == 0.25
    assert saved.node_types == ["a", "b"]
    assert saved.extra_env == {"K": "v"}
    assert saved.name == "spaced", "the normalise hook ran"
    assert saved.id == "default"
    assert saved.created, "the store stamped it"
    assert settings.save({"tls": "on"}).tls is True


def test_validate_coerce_and_clearable(settings: PluginSettings[Settings]) -> None:
    assert settings.save({"region": "eu-north-1"}).region == "eu-north-1"
    with pytest.raises(ValueError, match="not a region"):
        settings.save({"region": "mars"})
    assert settings.save({"region": ""}).region == "eu-north-1", "empty keeps a validated field"
    assert settings.save({"note": "hi"}).note == "HI", "the validator's return is stored"
    assert settings.save({"note": ""}).note == "", "clearable: empty clears"
    assert settings.save({"placement": "  "}).placement == "auto", "coerce runs first"
    assert settings.save({"placement": " dc:US-NC-1 "}).placement == "dc:US-NC-1"


def test_nested_rows_are_merged_from_known_keys_and_rehydrated_on_load(
    settings: PluginSettings[Settings], _root: Path
) -> None:
    saved = settings.save({
        "machines": [
            {"name": "box", "address": "10.0.0.1", "agent_port": "9000", "env": {"A": "1"}, "bogus": True},
            "not a row",
        ]
    })
    assert saved.machines == [Machine(name="box", address="10.0.0.1", agent_port=9000, env={"A": "1"})]
    loaded = settings.load()
    assert loaded.machines == saved.machines, "rows come back as dataclasses, not dicts"
    assert isinstance(loaded.machines[0], Machine)
    # A row written by an older version that predates ``agent_port``: the key is defaulted, unknown keys dropped.
    path = _root / "probe" / "default.json"
    data = json.loads(path.read_text())
    data["machines"] = [{"name": "old", "address": "h", "env": {}, "gone_field": 1}]
    path.write_text(json.dumps(data))
    old = settings.load().machines
    assert old == [Machine(name="old", address="h", agent_port=8800)]


def test_redacted_and_secret_values(settings: PluginSettings[Settings]) -> None:
    current = settings.save({
        "api_key": "k",
        "extra_env": {"TOKEN": "t"},
        "machines": [{"name": "box", "env": {"PW": "p"}}],
    })
    view = settings.redacted(current)
    assert view["api_key_set"] is True
    assert view["aws_secret_access_key_set"] is False
    assert "api_key" not in view
    assert view["extra_env_keys"] == ["TOKEN"]
    assert "extra_env" not in view
    assert view["machines"] == [{"name": "box", "address": "", "agent_port": 8800, "env_keys": ["PW"]}]
    assert view["id"] == "default"
    assert "created" not in view
    assert "last_run" not in view
    assert sorted(settings.secret_values(current)) == ["k", "p", "t"]
    assert json.dumps(view)


def test_option_takes_a_default_factory() -> None:
    @dataclass
    class WithFactory:
        id: str = "default"
        created: str = ""
        last_run: str | None = None
        sizes: list[int] = option(default_factory=lambda: [5], label="Sizes")

    assert WithFactory().sizes == [5]
    assert WithFactory().sizes is not WithFactory().sizes
    assert PluginSettings(WithFactory, "probe").field_view() == [
        {
            "key": "sizes",
            "label": "Sizes",
            "required": False,
            "secret": False,
            "help": "",
            "href": "",
            "placeholder": "",
        }
    ]


def test_an_unlabelled_secret_is_never_prompted_but_still_secret() -> None:
    @dataclass
    class Unlabelled:
        id: str = "default"
        created: str = ""
        last_run: str | None = None
        client_secret: str = secret()
        tlc_api_key: str = secret(label="3LC API key")
        subscription_id: str = option("", label="Subscription id", required=True)

    layer = PluginSettings(Unlabelled, "probe")
    current = layer.load()
    assert [f.key for f in layer.missing_fields(current)] == ["tlc_api_key", "subscription_id"], "field order"
    assert layer.readiness(current)["missing"] == ["tlc_api_key", "subscription_id"]
    assert [f["key"] for f in layer.field_view()] == ["tlc_api_key", "subscription_id"]
    saved = layer.save({"client_secret": "s3cr3t-value"})
    assert layer.secret_fields() == ["client_secret", "tlc_api_key"]
    assert layer.secret_values(saved) == ["s3cr3t-value"]
    assert "client_secret" not in layer.redacted(saved) and layer.redacted(saved)["client_secret_set"] is True
