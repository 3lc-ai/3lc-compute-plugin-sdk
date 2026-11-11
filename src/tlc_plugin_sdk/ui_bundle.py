# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Small, deterministic packaging for plugin HTML, CSS and JavaScript sources.

The served fragment remains self-contained for the isolated frame runtime. Source
assets have their own files; no bundler, remote script or extra browser request is
introduced. Asset markers are resolved only inside the plugin's source directory.
"""

from __future__ import annotations

import re
from html import escape
from pathlib import Path
from typing import Any

_ASSET = re.compile(r"\{\{asset:([a-zA-Z0-9_./-]+)\}\}")


def form_values(fields: list[dict[str, Any]]) -> dict[str, str]:
    """Render existing input attributes/options from the same metadata as validation."""
    result = {}
    for field in fields:
        prefix = "field:" + field["id"] + "."
        for attribute in ("default", "min", "max", "step"):
            if attribute in field:
                result[prefix + attribute] = escape(str(field[attribute]), quote=True)
        result[prefix + "checked"] = "checked" if field.get("default") is True else ""
        if "options" in field:
            options = []
            for option in field["options"]:
                selected = " selected" if option["value"] == field.get("default") else ""
                title = ' title="' + escape(str(option["title"]), quote=True) + '"' if option.get("title") else ""
                options.append(
                    '<option value="'
                    + escape(str(option["value"]), quote=True)
                    + '"'
                    + title
                    + selected
                    + ">"
                    + escape(str(option["label"]))
                    + "</option>"
                )
            result[prefix + "options"] = "\n".join(options)
    return result


def render_fragment(directory: Path, *, values: dict[str, str] | None = None) -> str:
    """Inline source assets and explicitly supplied template values."""
    root = directory.resolve()
    template = (root / "index.html").read_text(encoding="utf-8")

    def include(match: re.Match[str]) -> str:
        path = (root / match.group(1)).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            message = "UI asset must be a file inside the UI source directory"
            raise ValueError(message)
        return path.read_text(encoding="utf-8")

    result = _ASSET.sub(include, template)
    for key, value in (values or {}).items():
        result = result.replace("{{" + key + "}}", value)
    if re.search(r"\{\{(?:asset:|field:)[^}]+\}\}", result):
        message = "Unresolved UI source value"
        raise ValueError(message)
    return result


def build_fragment(
    directory: Path, destination: Path, *, check: bool = False, values: dict[str, str] | None = None
) -> None:
    """Build the packaged fragment, or verify it exactly matches the source assets."""
    text = render_fragment(directory, values=values)
    if check:
        if not destination.exists() or destination.read_text(encoding="utf-8") != text:
            message = f"Regenerate the UI fragment: {destination.name}"
            raise ValueError(message)
    else:
        destination.write_text(text, encoding="utf-8")
