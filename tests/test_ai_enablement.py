# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
import pytest

from tlc_plugin_sdk.ai.settings import Settings, parse_settings


@pytest.mark.parametrize("assistant", [False, True])
@pytest.mark.parametrize("refine", [False, True])
@pytest.mark.parametrize("module", ["refine", "automate"])
def test_legacy_assistant_choice_becomes_shared_ai_policy(assistant, refine, module):
    parsed = parse_settings({"features": {"assistant": assistant, module: refine}})
    assert parsed.enabled is assistant
    assert parsed.as_dict()["enabled"] is assistant
    assert "features" not in parsed.as_dict()


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, {}, []])
def test_enablement_is_strictly_boolean(value):
    with pytest.raises(ValueError):
        parse_settings({"enabled": value})


def test_missing_and_ambiguous_policy_never_enable_ai():
    assert Settings().enabled is False
    assert parse_settings({}).enabled is False
    with pytest.raises(ValueError):
        parse_settings({"enabled": True, "features": {"assistant": False, "refine": True}})


@pytest.mark.parametrize("enabled", [False, True])
def test_shared_policy_round_trips_without_plugin_names(enabled):
    policy = parse_settings({"enabled": enabled})
    assert parse_settings(policy.as_dict()).enabled is enabled
