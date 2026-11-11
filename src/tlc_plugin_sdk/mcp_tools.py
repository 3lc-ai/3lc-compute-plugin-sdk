# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""A plugin's MCP tools: what an agent (or any MCP client of the deployment) may ask the plugin to do.

A plugin declares its tools in its manifest, under ``[mcp]``, and the host's MCP server lists them the moment
the plugin is installed — no MCP code in the plugin, no restart. Each tool names what it does, takes a JSON
Schema for its arguments, and is one of two kinds:

- a **job** tool starts ``run_job`` with a body built from ``[mcp.tools.job] body``, where any string that is
  ``"{{argument}}"`` is replaced by that argument's value (its type kept) and a string containing
  ``{{argument}}`` has the value spelled into it; ``outputs`` names the job outputs the caller may wait for
  (``ctx.output(name, value)``, ``ctx.result(url)`` is always ``run_url``);
- a **route** tool calls one of the plugin's own routes (``[mcp.tools.route] method`` and ``path``) with the
  arguments as the JSON body (``POST``) or as query parameters (``GET``) and returns the route's answer.

A plugin whose tools depend on runtime state (a model list, a catalogue) also overrides
:meth:`~tlc_plugin_sdk.contract.HubPlugin.get_mcp_tools` and returns the same shapes; the worker serves them at
the reserved route ``GET /mcp/tools``. Published manifest contracts remain authoritative; runtime-only tools
are scoped to the caller and node. Prefer stable tools that return changing values as data.

.. code-block:: toml

    [mcp]
    agent = false                      # true only for the plugin that is the deployment's chat agent
    knowledge = ["docs/mcp/*.md"]      # pages the host serves as MCP resources

    [[mcp.tools]]
    name = "export"
    description = "Export a table to YOLO, COCO, CSV or XLSX"
    input_schema = { type = "object", required = ["table_url", "format", "output_path"], properties = {
        table_url = { type = "string" }, format = { type = "string", enum = ["yolo", "coco", "csv", "xlsx"] },
        output_path = { type = "string" } } }
    [mcp.tools.job]
    body = { table_url = "{{table_url}}", format = "{{format}}", output_path = "{{output_path}}" }
    outputs = ["output_path"]
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

__all__ = [
    "McpTool",
    "load_mcp_section",
    "mcp_tools_as_wire",
    "packaged_knowledge",
    "parse_mcp_section",
    "parse_mcp_tools",
    "render_job_body",
    "render_route_request",
]

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_WHOLE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")
_PART = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_METHODS = ("GET", "POST")


@dataclass(frozen=True)
class McpTool:
    """One tool a plugin offers over MCP.

    Attributes:
        name: The tool's name within the plugin (``[a-z][a-z0-9_]*``); the host prefixes the plugin id.
        description: What the tool does, for the model and the person reading the catalogue.
        input_schema: The JSON Schema of the arguments (an ``object`` schema).
        job: For a job tool: ``{"body": <template>, "outputs": [...], "wait": bool}``.
        route: For a route tool: ``{"method": "GET" | "POST", "path": "/..."}``.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    job: dict[str, Any] | None = None
    route: dict[str, Any] | None = None
    title: str = ""
    annotations: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None

    @property
    def kind(self) -> str:
        """``"job"`` or ``"route"``."""
        return "job" if self.job is not None else "route"


def parse_mcp_tools(raw: object) -> tuple[McpTool, ...]:
    """Validate a manifest's ``[[mcp.tools]]`` list.

    Args:
        raw: The ``tools`` value of the ``[mcp]`` table, or ``None``.

    Returns:
        The tools, in manifest order.

    Raises:
        ValueError: When an entry is not a table, lacks a name, description or object ``input_schema``, names
            a tool twice, declares both or neither of ``job`` and ``route``, or its ``route`` has a method
            other than GET/POST or a path not starting with ``/``.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        msg = "[mcp] tools is a list of [[mcp.tools]] tables"
        raise ValueError(msg)
    tools: list[McpTool] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            msg = f"[[mcp.tools]] entries are tables, got {entry!r}"
            raise ValueError(msg)
        name = str(entry.get("name", "") or "")
        if not _NAME.match(name):
            msg = f"[[mcp.tools]] name {name!r} is not a lower-case identifier"
            raise ValueError(msg)
        if name in seen:
            msg = f"[[mcp.tools]] names {name!r} twice"
            raise ValueError(msg)
        seen.add(name)
        description = str(entry.get("description", "") or "").strip()
        if not description:
            msg = f"[[mcp.tools]] {name!r} needs a description"
            raise ValueError(msg)
        schema = entry.get("input_schema")
        if not isinstance(schema, dict) or schema.get("type", "object") != "object":
            msg = f'[[mcp.tools]] {name!r}: input_schema is a JSON Schema object (type = "object")'
            raise ValueError(msg)
        schema = {"type": "object", **schema}
        job, route = entry.get("job"), entry.get("route")
        if (job is None) == (route is None):
            msg = f"[[mcp.tools]] {name!r} declares exactly one of [mcp.tools.job] and [mcp.tools.route]"
            raise ValueError(msg)
        if job is not None:
            if not isinstance(job, dict) or not isinstance(job.get("body", {}), dict):
                msg = f"[[mcp.tools]] {name!r}: [mcp.tools.job] body is a table"
                raise ValueError(msg)
            outputs = job.get("outputs", [])
            if not isinstance(outputs, list) or not all(isinstance(o, str) and o for o in outputs):
                msg = f"[[mcp.tools]] {name!r}: [mcp.tools.job] outputs is a list of names"
                raise ValueError(msg)
            prepared = {}
            if "prepare" in job or "cancel" in job:
                if "prepare" not in job or "cancel" not in job:
                    msg = "Prepared job tools require both prepare and cancel routes"
                    raise ValueError(msg)
                for key in ("prepare", "cancel"):
                    candidate = job[key]
                    if not isinstance(candidate, dict) or candidate.get("method") != "POST":
                        msg = "Prepared job routes must explicitly use POST"
                        raise ValueError(msg)
                    fields = dict(schema.get("properties") or {})
                    if key == "cancel":
                        fields["run_id"] = {"type": "string"}
                    checked = parse_mcp_tools([
                        {
                            "name": name,
                            "description": description,
                            "input_schema": {**schema, "properties": fields},
                            "route": candidate,
                        }
                    ])[0]
                    prepared[key] = checked.route
            job = {
                "body": dict(job.get("body", {})),
                "outputs": list(outputs),
                "wait": bool(job.get("wait", False)),
                **prepared,
            }
        if route is not None:
            if not isinstance(route, dict):
                msg = f"[[mcp.tools]] {name!r}: [mcp.tools.route] is a table"
                raise ValueError(msg)
            method = str(route.get("method", "POST") or "POST").upper()
            path = str(route.get("path", "") or "")
            if method not in _METHODS:
                msg = f"[[mcp.tools]] {name!r}: route method is GET or POST"
                raise ValueError(msg)
            if (
                not path.startswith("/")
                or path.startswith("//")
                or unquote(path) != path
                or "\\" in path
                or any(part in (".", "..") for part in path.split("/"))
                or urlsplit(path).query
                or urlsplit(path).fragment
                or any(ord(ch) < 32 for ch in path)
                or path.split("/")[1] in {"mcp", "jobs", "health", "ui", "run", "compute", "_host"}
            ):
                msg = f'[[mcp.tools]] {name!r}: route path is one of the plugin\'s own routes ("/formats")'
                raise ValueError(msg)
            path_keys = re.findall(r"{([a-z][a-z0-9_]*)}", path)
            properties = schema.get("properties") or {}
            for segment in path.split("/"):
                if ("{" in segment or "}" in segment) and not re.fullmatch(r"{[a-z][a-z0-9_]*}", segment):
                    msg = "Route placeholders must occupy a complete path segment"
                    raise ValueError(msg)
            if any(key not in properties for key in path_keys) or path.split("/")[1].startswith("{"):
                msg = "Route placeholders must name declared arguments below a fixed plugin route"
                raise ValueError(msg)
            query = route.get("query", [])
            if not isinstance(query, list) or any(
                not isinstance(key, str) or key not in properties or key in {"run_target", "plugin_session", "node_id"}
                for key in query
            ):
                msg = "Route query names must be declared plugin arguments, never host routing"
                raise ValueError(msg)
            route = {"method": method, "path": path, **({"query": list(query)} if query else {})}
        annotations = entry.get("annotations", {})
        if not isinstance(annotations, dict):
            msg_0 = "MCP annotations must be an object"
            raise ValueError(msg_0)
        for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            if hint in annotations and not isinstance(annotations[hint], bool):
                msg_0 = f"MCP annotation {hint} must be a boolean"
                raise ValueError(msg_0)
        output_schema = entry.get("output_schema")
        if output_schema is not None and (not isinstance(output_schema, dict) or output_schema.get("type") != "object"):
            msg_0 = "MCP output_schema must have type object"
            raise ValueError(msg_0)
        tools.append(
            McpTool(
                name=name,
                description=description,
                input_schema=schema,
                job=job,
                route=route,
                title=str(entry.get("title", "") or ""),
                annotations=dict(annotations),
                output_schema=output_schema,
            )
        )
    return tuple(tools)


def parse_mcp_section(raw: object) -> dict[str, Any]:
    """Validate a manifest's whole ``[mcp]`` table.

    Returns:
        ``{"tools": tuple[McpTool, ...], "agent": bool, "knowledge": list[str]}``.

    Raises:
        ValueError: As :func:`parse_mcp_tools`, or when ``knowledge`` is not a list of relative paths.
    """
    if raw is None:
        return {"tools": (), "agent": False, "knowledge": []}
    if not isinstance(raw, dict):
        msg = "[mcp] is a table"
        raise ValueError(msg)
    knowledge = raw.get("knowledge", [])
    if not isinstance(knowledge, list) or not all(
        isinstance(k, str) and k and not k.startswith("/") and ".." not in k for k in knowledge
    ):
        msg = '[mcp] knowledge is a list of relative paths or globs ("docs/mcp/*.md")'
        raise ValueError(msg)
    return {"tools": parse_mcp_tools(raw.get("tools")), "agent": raw.get("agent") is True, "knowledge": list(knowledge)}


def load_mcp_section(raw: object, directory: Path) -> dict[str, Any]:
    """Expand a packaged JSON tools file without importing plugin code.

    The file must stay inside the manifest directory, including through symlinks,
    and contain at most 4 MiB. Inline and file tools share one validation pass.
    """
    if raw is not None and not isinstance(raw, dict):
        msg = "[mcp] must be a table"
        raise ValueError(msg)
    section = dict(raw) if isinstance(raw, dict) else {}
    filename = section.pop("tools_file", None)
    if filename is None:
        return section
    if not isinstance(filename, str) or not filename or Path(filename).is_absolute():
        msg = "MCP tools_file must be a relative packaged JSON path"
        raise ValueError(msg)
    root = directory.resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root):
        msg = "MCP tools_file must stay inside the plugin package"
        raise ValueError(msg)
    with path.open("rb") as stream:
        content = stream.read(4 * 1024 * 1024 + 1)
    if len(content) > 4 * 1024 * 1024:
        msg = "MCP tools_file exceeds 4 MiB"
        raise ValueError(msg)
    tools = json.loads(content)
    if not isinstance(tools, list):
        msg = "MCP tools_file must contain a JSON array"
        raise ValueError(msg)
    section["tools"] = [*section.get("tools", []), *tools]
    parse_mcp_section(section)
    return section


def render_job_body(template: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """The run body for a job tool: ``template`` with its ``{{argument}}`` placeholders filled.

    A string that is exactly one placeholder takes the argument's value as is (a number stays a number, a list
    a list); other strings get the value spelled in. A placeholder for an argument the caller did not give is
    dropped from the body when it is the whole value, and left blank inside a longer string.
    """

    def fill(value: Any) -> tuple[bool, Any]:
        if isinstance(value, str):
            whole = _WHOLE.match(value)
            if whole:
                key = whole.group(1)
                return (key in arguments), arguments.get(key)
            return True, _PART.sub(lambda m: _spell(arguments.get(m.group(1))), value)
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                keep, filled = fill(v)
                if keep:
                    out[k] = filled
            return True, out
        if isinstance(value, list):
            return True, [filled for keep, filled in (fill(v) for v in value) if keep]
        return True, value

    _, body = fill(dict(template))
    return dict(body)


def _spell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def mcp_tools_as_wire(tools: tuple[McpTool, ...] | list[McpTool]) -> list[dict[str, Any]]:
    """The tools as plain dicts (what ``GET /mcp/tools`` answers and what a host reads)."""
    wire = []
    for tool in tools:
        entry: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        if tool.output_schema is not None:
            entry["output_schema"] = tool.output_schema
        if tool.title:
            entry["title"] = tool.title
        if tool.annotations:
            entry["annotations"] = tool.annotations
        if tool.job is not None:
            entry["job"] = tool.job
        else:
            entry["route"] = tool.route
        wire.append(entry)
    return wire


def packaged_knowledge(plugin: object) -> list[dict[str, str]]:
    """Read only bounded Markdown explicitly packaged beside the plugin manifest."""
    import inspect
    import tomllib
    from pathlib import Path

    root = Path(inspect.getfile(type(plugin))).resolve().parent
    manifest = root / "plugin.toml"
    if not manifest.is_file():
        return []
    raw = tomllib.loads(manifest.read_text(encoding="utf-8"))
    patterns = parse_mcp_section(raw.get("mcp"))["knowledge"]
    pages: list[dict[str, str]] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            resolved = path.resolve()
            if resolved in seen or path.is_symlink() or not resolved.is_relative_to(root):
                continue
            if not path.is_file() or path.suffix.lower() != ".md" or path.stat().st_size > 65536:
                continue
            seen.add(resolved)
            pages.append({"path": path.relative_to(root).as_posix(), "text": path.read_text(encoding="utf-8")})
            if len(pages) >= 16:
                return pages
    return pages


def render_route_request(
    route: dict[str, Any], arguments: dict[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Build a fixed plugin route with bounded identifier segments and explicit query arguments."""
    remaining = dict(arguments)

    def segment(match: re.Match[str]) -> str:
        value = remaining.pop(match[1], None)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}", value):
            msg = "A route path argument must be a bounded identifier"
            raise ValueError(msg)
        return value

    path = re.sub(r"{([a-z][a-z0-9_]*)}", segment, str(route["path"]))
    query = {}
    for key in route.get("query", []):
        if key in remaining:
            value = remaining.pop(key)
            if not isinstance(value, (str, int, float, bool)):
                msg = "Route query arguments must be scalar values"
                raise ValueError(msg)
            query[key] = value
    return path, query, remaining
