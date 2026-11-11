# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""One configuration contract for native jobs, form metadata and published MCP tools.

This module is import-light. JSON Schema validation is loaded only when a request
is normalized. Tool declarations are ordinary SDK manifest data: generate them at
development/build time, so discovery never starts a worker or imports ML libraries.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def form_field_schema(field: dict[str, Any]) -> dict[str, Any]:
    """Translate existing form metadata without changing its labels or controls."""
    kind = field.get("type", "text")
    default = field.get("default")
    schema: dict[str, Any] = {}
    if kind in {"checkbox", "boolean"}:
        schema["type"] = "boolean"
    elif kind == "number":
        schema["type"] = "integer" if float(field.get("step", 1)).is_integer() else "number"
        for source, target in (("min", "minimum"), ("max", "maximum")):
            if source in field:
                schema[target] = field[source]
    elif kind == "select":
        values = [option["value"] for option in field.get("options", [])]
        schema["type"] = "integer" if isinstance(default, int) and not isinstance(default, bool) else "string"
        if values:
            schema["enum"] = [int(value) for value in values] if schema["type"] == "integer" else values
    else:
        schema["type"] = "string"
    if "default" in field:
        schema["default"] = default
    if field.get("help") or field.get("label"):
        schema["description"] = field.get("help") or field["label"]
    # Empty optional numeric controls mean "use the engine's default", not zero.
    if kind == "number" and default == "":
        schema = {"anyOf": [schema, {"const": ""}], "default": ""}
        schema["anyOf"][0].pop("default", None)
    return schema


def _normalize(value: Any, schema: dict[str, Any]) -> Any:
    """Normalize native HTML form values before applying the shared JSON Schema."""
    if isinstance(value, float) and not math.isfinite(value):
        message = "Configuration numbers must be finite"
        raise ValueError(message)
    if schema.get("type") == "array" and isinstance(value, list):
        return [_normalize(item, schema.get("items", {})) for item in value]
    if schema.get("type") == "object" and isinstance(value, dict):
        result = copy.deepcopy(value)
        for key, child in schema.get("properties", {}).items():
            if key not in result and "default" in child:
                result[key] = copy.deepcopy(child["default"])
            if key in result:
                result[key] = _normalize(result[key], child)
        return result
    if "anyOf" in schema:
        if any(branch.get("const", object()) == value for branch in schema["anyOf"]):
            return value
        return _normalize(value, schema["anyOf"][0])
    if isinstance(value, str):
        if schema.get("type") == "boolean" and value in {"true", "false"}:
            return value == "true"
        if schema.get("type") in {"integer", "number"} and value.strip():
            try:
                number = float(value)
                if math.isfinite(number):
                    if schema["type"] == "integer" and number.is_integer():
                        return int(number)
                    if schema["type"] == "number":
                        return number
            except ValueError:
                pass
    return value


def normalize_configuration(value: object, schema: dict[str, Any]) -> dict[str, Any]:
    """Return an independent normalized configuration or a safe field-level error."""
    from jsonschema import Draft202012Validator

    result = _normalize(value, schema)
    error = next(Draft202012Validator(schema).iter_errors(result), None)
    if error is not None:
        location = ".".join(str(part) for part in error.absolute_path) or "configuration"
        message = f"Invalid {location}: failed {error.validator} constraint"
        raise ValueError(message)
    if not isinstance(result, dict):
        message = "Configuration must be an object"
        raise ValueError(message)
    return result


@dataclass(frozen=True)
class JobContract:
    """Flat public arguments mapped to one native configuration schema.

    ``fields`` uses dotted native paths (for example ``params.epochs``). ``aliases``
    changes only public argument names; the native worker always receives the same
    object as its UI. Metadata keys are accepted only by the native schema.
    """

    fields: dict[str, dict[str, Any]]
    required: tuple[str, ...] = ()
    aliases: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    body_key: str | None = "project_config"

    def native_schema(self) -> dict[str, Any]:
        """Build the authoritative nested schema for a worker request."""
        root: dict[str, Any] = {
            "type": "object",
            "properties": copy.deepcopy(self.metadata),
            "additionalProperties": False,
        }
        for path, value in self.fields.items():
            current = root
            parts = path.split(".")
            for part in parts[:-1]:
                current = current["properties"].setdefault(
                    part,
                    {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                        "default": {},
                    },
                )
            current["properties"][parts[-1]] = copy.deepcopy(value)
            if path in self.required:
                current.setdefault("required", []).append(parts[-1])
        return root

    def normalize(self, value: object) -> dict[str, Any]:
        """Validate UI and MCP configurations with identical defaults and constraints."""
        return normalize_configuration(value, self.native_schema())

    def arguments(self, value: object) -> dict[str, Any]:
        """Convert a native configuration into public arguments without losing false/zero."""
        normalized = self.normalize(value)
        result: dict[str, Any] = {}
        for path in self.fields:
            current: Any = normalized
            for part in path.split("."):
                if not isinstance(current, dict) or part not in current:
                    break
                current = current[part]
            else:
                result[self.aliases.get(path, path.split(".")[-1])] = copy.deepcopy(current)
        return result

    def tool(
        self,
        name: str,
        title: str,
        description: str,
        *,
        constants: dict[str, Any] | None = None,
        required: tuple[str, ...] | None = None,
        outputs: tuple[str, ...] = ("run_url",),
    ) -> dict[str, Any]:
        """Publish a stable manifest job tool; no separate MCP execution function exists."""
        constants = constants or {}
        properties: dict[str, Any] = {}
        body: dict[str, Any] = {}
        for path, schema in self.fields.items():
            argument = self.aliases.get(path, path.split(".")[-1])
            current = body
            parts = path.split(".")
            for part in parts[:-1]:
                current = current.setdefault(part, {})
            if path in constants:
                current[parts[-1]] = constants[path]
            else:
                if argument in properties:
                    message = f"Duplicate public argument: {argument}"
                    raise ValueError(message)
                properties[argument] = copy.deepcopy(schema)
                current[parts[-1]] = "{{" + argument + "}}"
        needed = self.required if required is None else required
        native_body: dict[str, Any] = {self.body_key: body} if self.body_key else body
        if self.body_key and "project_name" in properties:
            native_body["project_name"] = "{{project_name}}"
        return {
            "name": name,
            "title": title,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
                "required": [self.aliases.get(path, path.split(".")[-1]) for path in needed if path not in constants],
            },
            "job": {"body": native_body, "outputs": list(outputs), "wait": False},
        }


def write_tools(path: Path, tools: list[dict[str, Any]], *, check: bool = False) -> None:
    """Generate a packaged catalog, or fail CI when it differs from its source contract."""
    from tlc_plugin_sdk.mcp_tools import parse_mcp_tools

    parse_mcp_tools(tools)
    text = json.dumps(tools, indent=2, ensure_ascii=False) + "\n"
    if check:
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            message = f"Regenerate the MCP catalog: {path.name}"
            raise ValueError(message)
    else:
        path.write_text(text, encoding="utf-8")
