# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Worker adapter for the shared read-only project catalogue."""

from __future__ import annotations

from typing import Any

from tlc_plugin_sdk.project_catalog import MAX_CELL as MAX_CELL
from tlc_plugin_sdk.project_catalog import MAX_ROWS as MAX_ROWS
from tlc_plugin_sdk.project_catalog import ProjectCatalog
from tlc_plugin_sdk.project_catalog import _canonical as _canonical
from tlc_plugin_sdk.project_catalog import _project_name as _project_name
from tlc_plugin_sdk.project_catalog import epoch_output_summary as epoch_output_summary
from tlc_plugin_sdk.project_catalog import run_blocking as run_blocking


def _root(root_url: str | None) -> str:
    import tlc

    return str(root_url or tlc.config.project_root_url or "")


def _roots() -> list[str]:
    from tlc_plugin_sdk.data_broker import configured_roots

    return configured_roots()


def _catalog() -> ProjectCatalog:
    return ProjectCatalog(lambda: _root(None), _roots)


def checked_url(url: str, *, exact: bool = False) -> str:
    return _catalog().checked_url(url, exact=exact)


def list_projects(root_url: str | None = None) -> dict[str, Any]:
    return _catalog().list_projects(root_url)


def list_datasets(project: str, root_url: str | None = None) -> dict[str, Any]:
    return _catalog().list_datasets(project, root_url)


def list_runs(project: str, root_url: str | None = None) -> dict[str, Any]:
    return _catalog().list_runs(project, root_url)


def run_info(url: str) -> dict[str, Any]:
    return _catalog().run_info(url)


def table_info(url: str) -> dict[str, Any]:
    return _catalog().table_info(url)


def table_weight_summary(url: str) -> dict[str, object]:
    return _catalog().table_weight_summary(url)


def table_rows(url: str, offset: int = 0, limit: int = 20, columns: list[str] | None = None) -> dict[str, Any]:
    return _catalog().table_rows(url, offset, limit, columns)
