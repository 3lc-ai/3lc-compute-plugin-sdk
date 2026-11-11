# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
import pytest

from tlc_plugin_sdk.project_catalog import ProjectCatalog, epoch_output_summary


def test_catalog_instances_never_share_storage_roots():
    first = ProjectCatalog(lambda: "s3://one/projects", lambda: ["s3://one/projects"])
    second = ProjectCatalog(lambda: "s3://two/projects", lambda: ["s3://two/projects"])
    for own, other, url in [(first, second, "s3://one/projects/P"), (second, first, "s3://two/projects/P")]:
        assert own.checked_url(url) == url
        with pytest.raises(ValueError, match="outside"):
            other.checked_url(url)
    for url in ["s3://one/projects-other/P", "s3://one/projects/%2e%2e/secret", "s3://one/projects/P?token=x"]:
        with pytest.raises(ValueError):
            first.checked_url(url)


def test_worker_and_deployment_share_per_metric_extrema():
    summary = epoch_output_summary([
        {"epoch": 1, "precision": 0.9, "recall": 0.2},
        {"epoch": 2, "precision": 0.8, "recall": 0.7},
    ])
    assert summary["metrics"]["precision"]["maximum"] == {"value": 0.9, "epoch": 1}
    assert summary["metrics"]["recall"]["maximum"] == {"value": 0.7, "epoch": 2}
    assert epoch_output_summary([{"epoch": 1}, {"epoch": 1}]) is None


def test_run_summaries_expose_recorded_dates_without_inventing_completion(monkeypatch):
    import json

    from tlcurl.url_adapters._registry import UrlAdapterRegistry

    from tlc_plugin_sdk.project_catalog import _run_summary

    record = {"created": "2026-11-11T09:30:00+00:00", "last_modified": "2026-11-11T10:15:00+00:00", "status": 1.0}
    monkeypatch.setattr(UrlAdapterRegistry, "read_string_content_from_url", lambda _: json.dumps(record))
    result = _run_summary({"name": "undated-name", "url": "s3://bucket/projects/P/runs/run"})
    assert result["created"] == record["created"] and result["status"] == 1.0
    assert "completed_at" not in result
    record.pop("created")
    assert _run_summary({"url": "s3://bucket/run"})["created"] is None


def test_table_info_preserves_exact_revision_lineage(monkeypatch):
    import sys
    from pathlib import PurePosixPath
    from types import SimpleNamespace

    class Url:
        def __init__(self, value):
            self.value = value

        def __str__(self):
            return self.value

        def to_absolute(self, root):
            import posixpath

            return Url(posixpath.normpath(str(PurePosixPath(str(root)) / self.value)))

    class Table:
        url = Url("/projects/P/datasets/D/tables/child")
        project_name = "P"
        dataset_name = "D"
        rows_schema = SimpleNamespace(values={})

        def __init__(self):
            self.input_tables = [Url("../original")]

        def __len__(self):
            return 3

        def latest(self):
            return self

    table = Table()
    monkeypatch.setitem(sys.modules, "tlc", SimpleNamespace(Table=SimpleNamespace(from_url=lambda _: table)))
    catalog = ProjectCatalog(lambda: "/projects", lambda: ["/projects"])
    monkeypatch.setattr(catalog, "checked_url", lambda value: value)
    detail = catalog.table_info(str(table.url))
    assert detail["is_original"] is False
    assert detail["parent_table_urls"] == ["/projects/P/datasets/D/tables/original"]
    table.input_tables = []
    assert catalog.table_info(str(table.url))["is_original"] is True
    table.input_table_url = SimpleNamespace(absolute_url=Url("/projects/P/datasets/D/tables/original"))
    assert catalog.table_info(str(table.url))["is_original"] is False
