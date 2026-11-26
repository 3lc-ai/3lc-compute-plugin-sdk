# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Table metadata delegates identity to the core layout, without reading rows."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from tlc_plugin_sdk.shared.table_context import table_project_context


@pytest.mark.parametrize("kind", ["dataset", "run", "unscoped"])
def test_context_uses_opened_table_url_and_core_layout(monkeypatch, kind):
    opened_url = object()  # Could differ from the supplied URL after core resolution.
    table = SimpleNamespace(url=SimpleNamespace(to_absolute=lambda: opened_url))
    root, name = "s3://bucket/projects", "Fire AWS"
    layout = SimpleNamespace(
        table_url_parts=Mock(return_value=(root, name, "train", "revision") if kind == "dataset" else None),
        run_url_parts=Mock(return_value=(root, name, "training") if kind == "run" else None),
        project_url=Mock(return_value=f"{root}/{name}"),
    )
    module = ModuleType("tlc.helpers.project_layout")
    module.ProjectLayout = layout
    monkeypatch.setitem(sys.modules, "tlc.helpers.project_layout", module)

    result = table_project_context(table)

    layout.table_url_parts.assert_called_once_with(opened_url)
    if kind == "unscoped":
        assert result == {"project_name": "", "project_url": "", "project_root_url": ""}
        layout.project_url.assert_not_called()
    else:
        assert result == {"project_name": name, "project_url": f"{root}/{name}", "project_root_url": root}
        layout.project_url.assert_called_once_with(project_name=name, root_url=root)
    if kind != "dataset":
        layout.run_url_parts.assert_called_once_with(opened_url)
