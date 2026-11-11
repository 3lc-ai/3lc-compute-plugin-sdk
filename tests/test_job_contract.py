# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Native form submissions and MCP calls must describe the same operation."""

import pytest
from jsonschema import Draft202012Validator

from tlc_plugin_sdk.job_contract import JobContract, form_field_schema, write_tools
from tlc_plugin_sdk.mcp_tools import render_job_body


@pytest.fixture
def contract():
    return JobContract(
        {
            "source": {"type": "string", "minLength": 1},
            "mode": {"enum": ["train", "collect"], "default": "train"},
            "params.epochs": form_field_schema({"type": "number", "default": 20, "min": 1, "max": 1000}),
            "params.workers": form_field_schema({"type": "number", "default": 8, "min": 0, "max": 64}),
            "params.enabled": {"type": "boolean", "default": True},
        },
        required=("source",),
    )


def test_native_and_mcp_round_trip_preserves_exact_values(contract):
    native = {"source": "s3://bucket/table", "params": {"epochs": "30", "workers": 0, "enabled": False}}
    original = repr(native)
    tool = contract.tool("train", "Train", "Train", constants={"mode": "train"})
    args = contract.arguments(native)
    args.pop("mode")
    Draft202012Validator(tool["input_schema"]).validate(args)
    body = render_job_body(tool["job"]["body"], args)
    assert contract.normalize(body["project_config"]) == contract.normalize(native)
    assert body["project_config"]["params"]["workers"] == 0
    assert body["project_config"]["params"]["enabled"] is False
    assert repr(native) == original


@pytest.mark.parametrize("bad", [0, -1, 1001, True, 1.5, "nan", "invalid"])
def test_invalid_numbers_fail_before_execution(contract, bad):
    with pytest.raises(ValueError, match=r"params\.epochs"):
        contract.normalize({"source": "table", "params": {"epochs": bad}})


def test_typo_and_missing_input_are_not_silently_ignored(contract):
    with pytest.raises(ValueError):
        contract.normalize({"source": "table", "params": {"epohcs": 10}})
    with pytest.raises(ValueError):
        contract.normalize({"params": {}})


def test_published_tools_cannot_drift(contract, tmp_path):
    path = tmp_path / "mcp-tools.json"
    tools = [contract.tool("train", "Train", "Train")]
    write_tools(path, tools)
    write_tools(path, tools, check=True)
    path.write_text("[]")
    with pytest.raises(ValueError, match="Regenerate"):
        write_tools(path, tools, check=True)


def test_numeric_select_and_optional_blank():
    integer = form_field_schema({"type": "number", "step": 32, "min": 32, "default": 224})
    assert integer["type"] == "integer"
    schema = form_field_schema({"type": "number", "step": 1, "min": 1, "default": ""})
    native = JobContract({"start": schema})
    assert native.normalize({"start": ""})["start"] == ""
    assert native.normalize({"start": "2"})["start"] == 2


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_native_numbers_must_be_json_finite(value):
    schema = JobContract({"rate": {"type": "number", "minimum": 0}})
    with pytest.raises(ValueError, match="finite"):
        schema.normalize({"rate": value})
