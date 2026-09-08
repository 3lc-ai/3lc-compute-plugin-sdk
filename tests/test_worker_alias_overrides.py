# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The worker applies the run body's ``_alias_overrides`` around ``run_job``.

One canonical place: the host stages data and says which alias now points where; the worker
registers those aliases before the plugin opens a table and restores them after, whether the
job succeeds or fails. A plugin never has to read the key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tlc_plugin_sdk.contract import ComputePlugin
from tlc_plugin_sdk.shared import aliases
from tlc_plugin_sdk.worker import _Worker

if TYPE_CHECKING:
    from pathlib import Path

    from tlc_plugin_sdk.job_context import JobContext

_OVERRIDES = {"enabled": True, "overrides": [{"token": "DATA", "path": "/staged/data"}]}


class _Recorder:
    """Stand-in for the alias registry: records the order of apply / run / restore."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, Any]] = []
        monkeypatch.setattr(aliases, "apply_alias_overrides", self._apply)
        monkeypatch.setattr(aliases, "restore_aliases", self._restore)

    def _apply(self, overrides: list[dict[str, str]]) -> list[dict[str, str]]:
        self.calls.append(("apply", overrides))
        return [{"token": o["token"], "original_path": "/original"} for o in overrides]

    def _restore(self, originals: list[dict[str, str]]) -> None:
        self.calls.append(("restore", originals))


class _Plugin(ComputePlugin):
    def __init__(self, recorder: _Recorder, *, fail: bool = False) -> None:
        self._recorder = recorder
        self._fail = fail

    def get_ui_fragment(self) -> str:
        return ""

    def run_job(self, ctx: JobContext) -> None:
        self._recorder.calls.append(("run", None))
        if self._fail:
            msg = "boom"
            raise RuntimeError(msg)


def _run(tmp_path: Path, plugin: ComputePlugin, params: dict[str, Any]) -> dict[str, Any]:
    worker = _Worker(plugin, "p", tmp_path / "state")
    job = worker.start_job("j1", params)
    assert job.wait(5)
    events = []
    while not job.events.empty():
        events.append(job.events.get_nowait())
    return events[-1]


def test_overrides_are_applied_before_run_job_and_restored_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder(monkeypatch)
    terminal = _run(tmp_path, _Plugin(rec), {"_alias_overrides": _OVERRIDES})
    assert terminal["event"] == "done"
    assert [c[0] for c in rec.calls] == ["apply", "run", "restore"]
    assert rec.calls[0][1] == _OVERRIDES["overrides"]
    assert rec.calls[2][1] == [{"token": "DATA", "original_path": "/original"}]


def test_overrides_are_restored_when_run_job_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(monkeypatch)
    terminal = _run(tmp_path, _Plugin(rec, fail=True), {"_alias_overrides": _OVERRIDES})
    assert terminal["event"] == "error"
    assert [c[0] for c in rec.calls] == ["apply", "run", "restore"]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"_alias_overrides": None},
        {"_alias_overrides": {"enabled": False, "overrides": [{"token": "DATA", "path": "/x"}]}},
        {"_alias_overrides": {"enabled": True, "overrides": []}},
        {"_alias_overrides": {"enabled": True, "overrides": "not-a-list"}},
        {"_alias_overrides": "garbage"},
    ],
)
def test_absent_disabled_or_malformed_overrides_touch_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, params: dict[str, Any]
) -> None:
    rec = _Recorder(monkeypatch)
    terminal = _run(tmp_path, _Plugin(rec), params)
    assert terminal["event"] == "done"
    assert [c[0] for c in rec.calls] == ["run"]
