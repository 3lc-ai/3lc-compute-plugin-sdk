# Copyright 2026 3LC Inc.
import pytest

from tlc_plugin_sdk.ai.catalog import MODELS, cost_usd, pricing_snapshot, public
from tlc_plugin_sdk.ai.settings import parse_route, parse_settings


def test_saved_rate_snapshot_cannot_change_during_an_inflight_call(monkeypatch):
    price = pricing_snapshot("gpt-6.1-sol")
    monkeypatch.setitem(MODELS, "gpt-6.1-sol", {**MODELS["gpt-6.1-sol"], "input_rate": 1000})
    assert cost_usd("gpt-6.1-sol", {"input": 1000, "output": 100}, price=price) == 0.003


def test_effort_controls_and_validator_use_model_capabilities():
    raw = {"service": "openai", "model": "gpt-6.1-sol", "connection_id": "connection", "effort": "none"}
    with pytest.raises(ValueError, match="effort"):
        parse_route("reasoning", raw)
    assert parse_route("reasoning", {**raw, "model": "gpt-6.1-terra"}).effort == "none"
    models = {m["id"]: m for p in public()["providers"] for m in p["models"]}
    assert "none" not in models["gpt-6.1-sol"]["efforts"]
    assert "none" in models["gpt-6-luna"]["efforts"]
    with pytest.raises(ValueError):
        parse_settings({"limits": {"per_day_usd": True}})
