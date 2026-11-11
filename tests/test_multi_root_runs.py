# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
import sys
from types import SimpleNamespace

import pytest

from tlc_plugin_sdk import data_tools as data
from tlc_plugin_sdk import project_catalog


def setup(monkeypatch, fail=False):
    roots = ["s3://bucket/projects", "/local/projects"]
    monkeypatch.setattr(data, "_roots", lambda: roots)
    monkeypatch.setattr(project_catalog, "_project_created", lambda *a: None)
    monkeypatch.setattr(project_catalog, "_project_modified", lambda *a: None)
    monkeypatch.setattr(project_catalog, "_dated", lambda name, url: {"name": name, "url": url})

    def names(**kw):
        if fail and kw["root_url"] == roots[0]:
            msg = "private diagnostic"
            raise PermissionError(msg)
        return ["nano"] if kw["root_url"] == roots[0] else ["nano", "best-nano"]

    layout = SimpleNamespace(
        list_project_names=lambda **kw: ["Fire"],
        list_run_names=names,
        run_url=lambda **kw: kw["root_url"] + "/Fire/runs/" + kw["run_name"],
    )
    monkeypatch.setitem(sys.modules, "tlc.helpers.project_layout", SimpleNamespace(ProjectLayout=layout))
    return roots


def test_same_named_projects_and_runs_at_two_roots_are_not_hidden(monkeypatch):
    roots = setup(monkeypatch)
    assert [p["root_url"] for p in data.list_projects()["projects"]] == roots
    result = data.list_runs("Fire")
    assert len(result["runs"]) == 3 and result["errors"] == []
    assert {r["root_url"] for r in result["runs"]} == set(roots)
    assert len(data.list_runs("Fire", roots[1])["runs"]) == 2


def test_partial_root_failure_is_reported_without_losing_accessible_runs(monkeypatch):
    roots = setup(monkeypatch, fail=True)
    result = data.list_runs("Fire")
    assert len(result["runs"]) == 2
    assert result["errors"] == [{"root_url": roots[0], "error": "Could not list this project location"}]
    with pytest.raises(PermissionError):
        data.list_runs("Fire", roots[0])
    with pytest.raises(ValueError, match="outside"):
        data.list_runs("Fire", "s3://untrusted/projects")
