# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Project identity for forms, taken from an opened 3LC table."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from tlc import Table


class TableProjectContext(TypedDict):
    """The source project, including its storage location."""

    project_name: str
    project_url: str
    project_root_url: str


def table_project_context(table: Table) -> TableProjectContext:
    """Describe the opened table's project using 3LC's layout and URL handling.

    Unscoped tables have no project identity; callers must not infer one from a
    display name or a browser-parsed URL. No rows or images are read here.
    """
    from tlc.helpers.project_layout import ProjectLayout

    url = table.url.to_absolute()
    parts = ProjectLayout.table_url_parts(url) or ProjectLayout.run_url_parts(url)
    if parts is None:
        return {"project_name": "", "project_url": "", "project_root_url": ""}
    root, name = parts[:2]
    return {
        "project_name": name,
        "project_url": str(ProjectLayout.project_url(project_name=name, root_url=root)),
        "project_root_url": root,
    }
