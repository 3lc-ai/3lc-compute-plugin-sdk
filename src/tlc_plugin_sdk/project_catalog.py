# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared project/table/run reads for deployment and worker adapters."""

from __future__ import annotations

import asyncio
import contextvars
import datetime
import json
import math
import posixpath
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

MAX_ROWS = 200
MAX_CELL = 400


def _canonical(url: str) -> tuple[str, str, str]:
    parts = urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment or "\\" in url:
        msg = "Invalid data URL"
        raise ValueError(msg)
    if parts.scheme in ("", "file"):
        if parts.netloc and parts.netloc != "localhost":
            msg = "Remote file authorities are not supported"
            raise ValueError(msg)
        return "file", "", str(Path(unquote(parts.path)).resolve())
    if parts.scheme not in {"s3", "gs", "az", "azure", "abfs", "abfss"}:
        msg = "Data reads require a configured storage root"
        raise ValueError(msg)
    return parts.scheme, parts.netloc, posixpath.normpath(unquote(parts.path))


def _project_name(project: str) -> None:
    if not project or project in {".", ".."} or any(c in project for c in "/\\%"):
        msg = "Invalid project name"
        raise ValueError(msg)


def _dated(name: str, url: str) -> dict[str, Any]:
    """A table or run entry with ``modified`` (ISO 8601, UTC) when its files are on this disk."""
    import os

    entry: dict[str, Any] = {"name": name, "url": url}
    path = url.removeprefix("file://")
    if path.startswith("/"):
        latest = 0.0
        try:
            for root_dir, _dirs, files in os.walk(path):
                for f in files:
                    try:
                        latest = max(latest, os.path.getmtime(os.path.join(root_dir, f)))
                    except OSError:
                        continue
        except OSError:
            latest = 0.0
        if latest:
            entry["modified"] = _iso(latest)
    return entry


def _run_summary(entry: dict[str, Any]) -> dict[str, Any]:
    """Small current run metadata; no metric tables, images or analysis jobs.

    Read stored fields rather than Run.created defaults (a legacy missing date
    must not become 'now'). last_modified is not a completion timestamp.
    """
    from tlcurl.url import Url
    from tlcurl.url_adapters._registry import UrlAdapterRegistry

    try:
        record = json.loads(UrlAdapterRegistry.read_string_content_from_url(Url(entry["url"]) / "object.3lc.json"))
        parameters = (record.get("constants") or {}).get("parameters") or {}
        return {
            **entry,
            "created": record.get("created"),
            "last_modified": record.get("last_modified"),
            "modified": record.get("last_modified"),
            "status": record.get("status"),
            "producer": parameters.get("3LC/plugin_id"),
            "metadata_available": True,
        }
    except Exception:
        return {**entry, "created": None, "last_modified": None, "status": None, "metadata_available": False}


def _table_parents(table: Any) -> list[str]:
    """Include native procedural dependencies on cores predating automatic lineage.

    Older EditedTable/Split revisions can have an empty input_tables metadata
    list while still declaring their real input_table_url dependency.
    """
    parents = list(table.input_tables)
    single = getattr(table, "input_table_url", None)
    if single is not None:
        parents.append(single)
    parents.extend(getattr(table, "input_table_urls", []) or [])
    return sorted({
        str(parent.absolute_url if hasattr(parent, "absolute_url") else parent.to_absolute(table.url))
        for parent in parents
    })


def _project_created(project: str, root: str) -> str | None:
    """When the project folder was made, where the file system records it (macOS, Windows); else ``None``."""
    import os

    from tlc.helpers.project_layout import ProjectLayout

    if not root.startswith(("/", "file:")):
        return None
    try:
        path = str(ProjectLayout.project_url(project_name=project, root_url=root)).removeprefix("file://")
        stat = os.stat(path)
    except Exception:  # a project that cannot be found simply has no date
        return None
    born = getattr(stat, "st_birthtime", None)
    return _iso(born) if born else None


def _project_modified(project: str, root: str) -> str | None:
    """When the project last changed (ISO 8601, UTC): the newest table or run file under a local root."""
    import os

    from tlc.helpers.project_layout import ProjectLayout

    if not root.startswith(("/", "file:")):
        return None  # a bucket: not scanned here
    latest = 0.0
    try:
        urls = [
            str(ProjectLayout.run_url(run_name=n, project_name=project, root_url=root))
            for n in ProjectLayout.list_run_names(project_name=project, root_url=root)
        ]
        for dataset in ProjectLayout.list_dataset_names(project_name=project, root_url=root):
            urls += [
                str(ProjectLayout.table_url(table_name=n, dataset_name=dataset, project_name=project, root_url=root))
                for n in ProjectLayout.list_table_names(dataset_name=dataset, project_name=project, root_url=root)
            ]
        for url in urls:
            path = url.removeprefix("file://")
            try:
                latest = max(latest, os.path.getmtime(path))
            except OSError:
                continue
    except Exception:  # a project that cannot be listed simply has no date
        return None
    return _iso(latest) if latest else None


def _iso(seconds: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(seconds, datetime.UTC).replace(microsecond=0).isoformat()


def epoch_output_summary(outputs: object) -> dict[str, Any] | None:
    """Compute independent extrema for an unambiguous numeric epoch series.

    A checkpoint selected by one metric is not the peak of every other metric.
    Preserve each metric's own epoch and last recorded value. Repeated epochs may
    represent different phases, so leave those records unaggregated.
    """
    if not isinstance(outputs, list) or not outputs or not all(isinstance(row, dict) for row in outputs):
        return None
    rows = [row for row in outputs if isinstance(row, dict)]
    epochs = [row.get("epoch") for row in rows]
    if any(
        isinstance(epoch, bool) or not isinstance(epoch, int | float) or not math.isfinite(epoch) for epoch in epochs
    ):
        return None
    if len(set(epochs)) != len(epochs):
        return None
    rows.sort(key=lambda row: row["epoch"])
    keys = sorted({key for row in rows for key in row if isinstance(key, str) and key != "epoch"})
    metrics = {}
    for key in keys[:64]:
        values = [(row["epoch"], row[key]) for row in rows if type(row.get(key)) in (int, float)]
        values = [(epoch, value) for epoch, value in values if math.isfinite(value)]
        if not values:
            continue
        low, high, final = min(values, key=lambda pair: pair[1]), max(values, key=lambda pair: pair[1]), values[-1]
        metrics[key] = {
            "minimum": {"value": low[1], "epoch": low[0]},
            "maximum": {"value": high[1], "epoch": high[0]},
            "last_recorded": {"value": final[1], "epoch": final[0]},
        }
    return {"source": "constants.outputs", "recorded_epochs": len(rows), "metrics": metrics} if metrics else None


def _column_kind(schema: Any) -> str:
    for attr in ("sample_type", "number_role", "string_role"):
        value = getattr(schema, attr, None)
        if value:
            return str(value)
    return type(getattr(schema, "value", schema)).__name__


def _cell(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_CELL else value[:MAX_CELL] + "…"
    if isinstance(value, dict):
        text = str(dict(list(value.items())[:8]))
    elif isinstance(value, (list, tuple)):
        text = str(list(value)[:8]) + (" …" if len(value) > 8 else "")
    else:
        text = str(value)
    return text if len(text) <= MAX_CELL else text[:MAX_CELL] + "…"


async def run_blocking(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Table reads block on I/O: off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


class ProjectCatalog:
    """Read-only project operations bound to an explicit set of storage roots.

    Each host/node supplies its own root resolvers. No process-global mutation or
    transport-specific authorization is used; callers enforce their role first.
    """

    def __init__(self, default_root: Callable[[], str], configured_roots: Callable[[], list[str]]) -> None:
        self._default_root = default_root
        self._configured_roots = configured_roots

    def checked_url(self, url: str, *, exact: bool = False) -> str:
        wanted = _canonical(url)
        for root in self._configured_roots():
            base = _canonical(root)
            if wanted[:2] == base[:2] and (
                wanted[2] == base[2] or (not exact and wanted[2].startswith(base[2].rstrip("/") + "/"))
            ):
                return url
        msg = "This data URL is outside the deployment's configured project roots"
        raise ValueError(msg)

    def _root_of(self, project: str, root_url: str | None) -> str:
        """The root that holds ``project``: the one named, else the first known root where it exists."""
        from tlc.helpers.project_layout import ProjectLayout

        _project_name(project)
        if root_url:
            return self.checked_url(root_url, exact=True)
        for root in self._configured_roots():
            try:
                if ProjectLayout.project_exists(project_name=project, root_url=root):
                    return root
            except Exception:  # a root that cannot be read holds nothing
                continue
        return self._default_root()

    def list_projects(self, root_url: str | None = None) -> dict[str, Any]:
        """Every project of every root this deployment knows (or of ``root_url``), newest change first.

        Each project names its ``root_url``. ``created`` (the project folder's birth time) and ``modified`` (its newest
        table or run file) are known for a root on this machine's disk, ISO 8601 in UTC; a bucket's projects carry
        neither.
        """
        from tlc.helpers.project_layout import ProjectLayout

        roots = [self.checked_url(root_url, exact=True)] if root_url else self._configured_roots()
        projects: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for root in roots:
            try:
                names = sorted(ProjectLayout.list_project_names(root_url=root, include_scan_urls=False))
            except Exception:  # a root that cannot be listed
                continue
            for name in names:
                if (root, name) in seen:
                    continue
                seen.add((root, name))
                projects.append({
                    "name": name,
                    "root_url": root,
                    "created": _project_created(name, root),
                    "modified": _project_modified(name, root),
                })
        projects.sort(key=lambda p: (p["modified"] or "", p["name"]), reverse=True)
        return {"roots": roots, "projects": projects}

    def list_datasets(self, project: str, root_url: str | None = None) -> dict[str, Any]:
        from tlc.helpers.project_layout import ProjectLayout

        root = self._root_of(project, root_url)
        out = []
        for dataset in sorted(ProjectLayout.list_dataset_names(project_name=project, root_url=root)):
            tables = sorted(ProjectLayout.list_table_names(dataset_name=dataset, project_name=project, root_url=root))
            out.append({
                "dataset": dataset,
                "tables": [
                    _dated(
                        name,
                        str(
                            ProjectLayout.table_url(
                                table_name=name, dataset_name=dataset, project_name=project, root_url=root
                            )
                        ),
                    )
                    for name in tables
                ],
            })
        return {"project": project, "root_url": root, "datasets": out}

    def list_runs(self, project: str, root_url: str | None = None) -> dict[str, Any]:
        from concurrent.futures import ThreadPoolExecutor

        from tlc.helpers.project_layout import ProjectLayout

        _project_name(project)
        roots = [self.checked_url(root_url, exact=True)] if root_url else self._configured_roots()
        runs: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for root in roots:
            try:
                names = sorted(ProjectLayout.list_run_names(project_name=project, root_url=root))
                runs.extend(
                    {
                        "name": n,
                        "url": str(ProjectLayout.run_url(run_name=n, project_name=project, root_url=root)),
                        "root_url": root,
                    }
                    for n in names
                )
            except Exception:
                if root_url:
                    raise
                errors.append({"root_url": root, "error": "Could not list this project location"})
        if runs:
            with ThreadPoolExecutor(max_workers=min(8, len(runs))) as pool:
                pending = [pool.submit(contextvars.copy_context().run, _run_summary, run) for run in runs]
                runs = [task.result() for task in pending]

            def chronological(run: dict[str, Any]) -> tuple[float, str]:
                try:
                    date = datetime.datetime.fromisoformat(str(run.get("created") or ""))
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=datetime.UTC)
                    stamp = date.timestamp()
                except ValueError:
                    stamp = float("-inf")
                return stamp, str(run["url"])

            runs.sort(key=chronological, reverse=True)
        return {
            "project": project,
            "roots": roots,
            "runs": runs,
            "errors": errors,
            "order": "created_descending",
            "undated_runs": sum(not run.get("created") for run in runs),
        }

    def run_info(self, url: str) -> dict[str, Any]:
        """A run as its record states it: the tables it trained on, its parameters, its outputs, its metrics tables.

        ``form`` is the plugin form the run was started from (``3LC/form`` in its parameters, recorded by the
        plugin), in the shape the page's set_form action takes; ``None`` when the run has none.
        """
        import tlc
        from tlcurl.url_adapters._registry import UrlAdapterRegistry

        from tlc_plugin_sdk.shared.run_inputs import run_inputs

        run = tlc.Run.from_url(self.checked_url(url))
        # Runs are mutable and workers write them in another process. Run.from_url
        # caches its first snapshot; read the current record without flushing the
        # shared registry or mutating an object another request may be using.
        record = json.loads(UrlAdapterRegistry.read_string_content_from_url(run.url / "object.3lc.json"))
        constants = record.get("constants") or {}
        inputs = run_inputs(run, record)
        parameters = {k: v for k, v in (constants.get("parameters") or {}).items() if k != "3LC/form"}
        form = (constants.get("parameters") or {}).get("3LC/form")
        return {
            "url": str(run.url),
            "name": run.name,
            "project": getattr(run, "project_name", "") or "",
            "description": str(record.get("description") or ""),
            "status": record.get("status"),
            "created": record.get("created"),
            "last_modified": record.get("last_modified"),
            "inputs": inputs,
            "parameters": parameters,
            "form": form if isinstance(form, dict) else None,
            "output_series_summary": epoch_output_summary(constants.get("outputs")),
            "outputs": constants.get("outputs") or {},
            "metrics_tables": len(record.get("metrics") or []),
            "metric_table_details": [
                {"url": str(tlc.Url(item["url"]).to_absolute(run.url)), "rows": item.get("row_count")}
                for item in record.get("metrics") or []
                if isinstance(item, dict) and item.get("url")
            ],
        }

    def table_info(self, url: str) -> dict[str, Any]:
        import tlc

        table = tlc.Table.from_url(self.checked_url(url))
        columns = []
        for name, schema in table.rows_schema.values.items():
            columns.append({"name": name, "kind": _column_kind(schema)})
        info: dict[str, Any] = {
            "url": str(table.url),
            "project": table.project_name,
            "dataset": table.dataset_name,
            "name": str(table.url).rstrip("/").rsplit("/", 1)[-1],
            "rows": len(table),
            "columns": columns,
            "parent_table_urls": _table_parents(table),
            "is_original": not bool(_table_parents(table)),
        }
        try:
            latest = table.latest()
            info["latest_revision_url"] = str(latest.url)
        except Exception:
            info["latest_revision_url"] = str(table.url)
        return info

    def table_weight_summary(self, url: str) -> dict[str, object]:
        """Count eligible samples on the exact revision, reading only its weight column."""
        import tlc

        from tlc_plugin_sdk.shared.weights import weight_summary

        return weight_summary(tlc.Table.from_url(self.checked_url(url)))

    def table_rows(
        self, url: str, offset: int = 0, limit: int = 20, columns: list[str] | None = None
    ) -> dict[str, Any]:
        import tlc

        table = tlc.Table.from_url(self.checked_url(url))
        total = len(table)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), MAX_ROWS))
        wanted = [c for c in (columns or []) if c in table.rows_schema.values] or list(table.rows_schema.values)
        rows = []
        for index in range(offset, min(total, offset + limit)):
            row = table.table_rows[index]
            rows.append({"index": index, **{c: _cell(row.get(c)) for c in wanted}})
        return {"url": str(table.url), "total": total, "offset": offset, "columns": wanted, "rows": rows}
