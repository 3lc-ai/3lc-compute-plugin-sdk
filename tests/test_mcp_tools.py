# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""A plugin's MCP tools: declared in the manifest, served by the worker, outputs kept in the job record."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from litestar.testing import TestClient

from tlc_plugin_sdk import ComputePlugin
from tlc_plugin_sdk.asgi_app import RESERVED_WORKER_PREFIXES, build_plugin_app
from tlc_plugin_sdk.harness import read_manifest
from tlc_plugin_sdk.job_context import JobContext
from tlc_plugin_sdk.mcp_tools import mcp_tools_as_wire, parse_mcp_section, parse_mcp_tools, render_job_body

MANIFEST = """
id = "exporter"
name = "Exporter"
version = "1.0.0"

[runtime]
isolation = "venv"
entrypoint = "tlc_plugin_exporter:ExporterPlugin"

[mcp]
knowledge = ["docs/mcp/*.md"]

[[mcp.tools]]
name = "export"
description = "Export a table"
[mcp.tools.input_schema]
required = ["table_url", "format"]
[mcp.tools.input_schema.properties]
table_url = { type = "string" }
format = { type = "string" }
columns = { type = "array" }
[mcp.tools.job]
outputs = ["output_path"]
[mcp.tools.job.body]
table_url = "{{table_url}}"
format = "{{format}}"
columns = "{{columns}}"
output_path = "exports/{{format}}/{{table_url}}"

[[mcp.tools]]
name = "formats"
description = "The export formats"
input_schema = { properties = {} }
[mcp.tools.route]
method = "GET"
path = "/formats"
"""


def test_manifest_tools_are_read_validated_and_rendered(tmp_path: Path) -> None:
    (tmp_path / "plugin.toml").write_text(MANIFEST)
    manifest = read_manifest(tmp_path)
    assert manifest.mcp["knowledge"] == ["docs/mcp/*.md"] and manifest.mcp["agent"] is False
    export, formats = manifest.mcp["tools"]
    assert export.kind == "job" and export.job == {
        "body": {
            "table_url": "{{table_url}}",
            "format": "{{format}}",
            "columns": "{{columns}}",
            "output_path": "exports/{{format}}/{{table_url}}",
        },
        "outputs": ["output_path"],
        "wait": False,
    }
    assert export.input_schema["type"] == "object" and "required" in export.input_schema
    assert formats.kind == "route" and formats.route == {"method": "GET", "path": "/formats"}
    body = render_job_body(export.job["body"], {"table_url": "s3://b/t", "format": "yolo", "columns": ["a", 1]})
    assert body == {
        "table_url": "s3://b/t",
        "format": "yolo",
        "columns": ["a", 1],
        "output_path": "exports/yolo/s3://b/t",
    }
    # An argument not given: dropped as a whole value, blank inside a longer string.
    assert render_job_body(export.job["body"], {"format": "coco"}) == {"format": "coco", "output_path": "exports/coco/"}
    wire = mcp_tools_as_wire(manifest.mcp["tools"])
    assert [t["name"] for t in wire] == ["export", "formats"] and wire[0]["job"]["outputs"] == ["output_path"]
    assert json.dumps(wire)  # plain JSON, nothing exotic


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"name": "Bad Name", "description": "x", "input_schema": {}, "route": {"path": "/x"}}, "lower-case"),
        ({"name": "a", "description": "", "input_schema": {}, "route": {"path": "/x"}}, "description"),
        ({"name": "a", "description": "x", "input_schema": {"type": "array"}, "route": {"path": "/x"}}, "object"),
        ({"name": "a", "description": "x", "input_schema": {}}, "exactly one"),
        ({"name": "a", "description": "x", "input_schema": {}, "job": {}, "route": {"path": "/x"}}, "exactly one"),
        (
            {"name": "a", "description": "x", "input_schema": {}, "route": {"method": "DELETE", "path": "/x"}},
            "GET or POST",
        ),
        ({"name": "a", "description": "x", "input_schema": {}, "route": {"path": "/jobs/1/run"}}, "own routes"),
        ({"name": "a", "description": "x", "input_schema": {}, "route": {"path": "../x"}}, "own routes"),
        ({"name": "a", "description": "x", "input_schema": {}, "job": {"outputs": "run_url"}}, "list of names"),
    ],
)
def test_a_tool_declaration_of_the_wrong_shape_is_refused(entry: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_mcp_tools([entry])
    with pytest.raises(ValueError, match="twice"):
        parse_mcp_tools([{"name": "a", "description": "x", "input_schema": {}, "route": {"path": "/x"}}] * 2)
    with pytest.raises(ValueError, match="relative"):
        parse_mcp_section({"knowledge": ["/etc/passwd"]})
    assert parse_mcp_section(None) == {"tools": (), "agent": False, "knowledge": []}


class _Plugin(ComputePlugin):
    id = "dyn"
    models = ("yolo11n", "yolo11s")

    def get_ui_fragment(self) -> str:
        return "<div></div>"

    def get_mcp_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "train",
                "description": "Train one of " + ", ".join(self.models),
                "input_schema": {
                    "type": "object",
                    "properties": {"model": {"type": "string", "enum": list(self.models)}},
                },
                "job": {"body": {"model": "{{model}}"}, "outputs": ["run_url"]},
            },
            "not a tool",
        ]


class _Broken(_Plugin):
    def get_mcp_tools(self) -> list[dict[str, Any]]:
        msg = "no models yet"
        raise RuntimeError(msg)


def test_the_worker_serves_dynamic_tools_at_a_reserved_route() -> None:
    assert "/mcp" in RESERVED_WORKER_PREFIXES
    with TestClient(app=build_plugin_app(_Plugin())) as client:
        answer = client.get("/mcp/tools")
        assert answer.status_code == 200
        assert [t["name"] for t in answer.json()["tools"]] == ["train"], "only tables are tools"
    with TestClient(app=build_plugin_app(_Broken())) as client:
        answer = client.get("/mcp/tools").json()
        assert answer["tools"] == [] and "failed" in answer["error"]


def test_outputs_are_recorded_once_per_name_and_need_a_name(tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []
    ctx = JobContext("j1", {}, tmp_path, sink=events.append, cancel_event=threading.Event())
    ctx.output("table_url", "s3://b/t")
    ctx.output("rows", 12)
    assert [{k: v for k, v in e.items() if k != "job_id"} for e in events] == [
        {"event": "output", "name": "table_url", "value": "s3://b/t"},
        {"event": "output", "name": "rows", "value": 12},
    ]
    with pytest.raises(ValueError):
        ctx.output("", 1)


@pytest.mark.parametrize("path", ["/run", "/jobs/cancel", "/../escape", "//elsewhere", "/x/%2e%2e"])
def test_prepared_jobs_cannot_escape_native_plugin_routes(path):
    entry = {
        "name": "start",
        "description": "Prepare a job",
        "input_schema": {},
        "job": {
            "prepare": {"method": "POST", "path": path},
            "cancel": {"method": "POST", "path": "/history/{run_id}/cancel"},
        },
    }
    with pytest.raises(ValueError):
        parse_mcp_tools([entry])


def test_prepared_job_requires_explicit_cleanup_and_mutating_routes():
    job = {"prepare": {"method": "POST", "path": "/prepare"}}
    entry = {"name": "start", "description": "Prepare a job", "input_schema": {}, "job": job}
    with pytest.raises(ValueError, match="both"):
        parse_mcp_tools([entry])
    job["cancel"] = {"method": "GET", "path": "/history/{run_id}/cancel"}
    with pytest.raises(ValueError, match="POST"):
        parse_mcp_tools([entry])


def test_typed_metadata_and_packaged_file_are_validated(tmp_path: Path) -> None:
    from tlc_plugin_sdk.mcp_tools import load_mcp_section

    tool = {
        "name": "inspect",
        "description": "Inspect",
        "input_schema": {"type": "object"},
        "route": {"method": "GET", "path": "/inspect"},
        "title": "Inspect data",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "output_schema": {"type": "object", "required": ["count"], "properties": {"count": {"type": "integer"}}},
    }
    assert mcp_tools_as_wire(parse_mcp_tools([tool])) == [tool]
    for hint in tool["annotations"]:
        with pytest.raises(ValueError, match="boolean"):
            parse_mcp_tools([{**tool, "annotations": {hint: "true"}}])
    with pytest.raises(ValueError, match="output_schema"):
        parse_mcp_tools([{**tool, "output_schema": {"type": "array"}}])
    (tmp_path / "tools.json").write_text(json.dumps([tool]))
    assert load_mcp_section({"tools_file": "tools.json"}, tmp_path)["tools"] == [tool]
    with pytest.raises(ValueError, match="inside"):
        load_mcp_section({"tools_file": "../outside.json"}, tmp_path)
    (tmp_path / "outside.json").symlink_to(tmp_path.parent / "outside.json")
    with pytest.raises(ValueError, match="inside"):
        load_mcp_section({"tools_file": "outside.json"}, tmp_path)
    with pytest.raises(ValueError, match="twice"):
        load_mcp_section({"tools_file": "tools.json", "tools": [tool]}, tmp_path)
    with pytest.raises(ValueError, match="table"):
        load_mcp_section("wrong", tmp_path)
