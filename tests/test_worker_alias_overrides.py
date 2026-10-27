# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The worker and a run body's ``_alias_overrides``.

The host stages data and says which alias now points where. A plugin that declares
``applies_alias_overrides`` applies them itself (through 3LC) and the worker leaves them alone;
for any other plugin the worker registers them before ``run_job`` and restores them after,
whether the job succeeds or fails, and says in the job log that SDK 0.6 stops doing it. Either
way the overrides are process-wide, so a job that needs an alias elsewhere than a live job is
refused before it starts.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import pytest
from litestar.testing import TestClient

from tlc_plugin_sdk.asgi_app import build_plugin_app
from tlc_plugin_sdk.contract import ComputePlugin, HubPlugin
from tlc_plugin_sdk.shared import aliases
from tlc_plugin_sdk.worker import AliasOverrideConflict, _control_handlers, _Worker

if TYPE_CHECKING:
    from pathlib import Path

    from tlc_plugin_sdk.job_context import JobContext

_OVERRIDES = {"enabled": True, "overrides": [{"token": "DATA", "path": "/staged/data"}]}


def _staged(path: str, token: str = "DATA") -> dict[str, Any]:
    return {"_alias_overrides": {"enabled": True, "overrides": [{"token": token, "path": path}]}}


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

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


class _Plugin(ComputePlugin):
    def __init__(self, recorder: _Recorder, *, fail: bool = False) -> None:
        self._recorder = recorder
        self._fail = fail
        self.seen: tuple[Any, ...] | None = None

    def get_ui_fragment(self) -> str:
        return ""

    def run_job(self, ctx: JobContext) -> None:
        self._recorder.calls.append(("run", None))
        if self._fail:
            msg = "boom"
            raise RuntimeError(msg)


class _Declared(_Plugin):
    applies_alias_overrides = True

    def __init__(self, recorder: _Recorder, *, read: bool = True, fail: bool = False) -> None:
        super().__init__(recorder, fail=fail)
        self._read = read

    def run_job(self, ctx: JobContext) -> None:
        if self._read:
            self.seen = ctx.alias_overrides  # what tlc.integration.compute.job_scope(ctx) reads
        super().run_job(ctx)


class _Blocking(_Plugin):
    """Runs until released, so a second job can overlap it."""

    def __init__(self, recorder: _Recorder) -> None:
        super().__init__(recorder)
        self.started = threading.Event()
        self.release = threading.Event()

    def run_job(self, ctx: JobContext) -> None:
        super().run_job(ctx)
        self.started.set()
        self.release.wait(5)


def _run(tmp_path: Path, plugin: ComputePlugin, params: dict[str, Any]) -> list[dict[str, Any]]:
    worker = _Worker(plugin, "p", tmp_path / "state")
    job = worker.start_job("j1", params)
    assert job.wait(5)
    events = []
    while not job.events.empty():
        events.append(job.events.get_nowait())
    return events


def _logs(events: list[dict[str, Any]]) -> list[str]:
    return [e["message"] for e in events if e["event"] == "log"]


# ── a plugin that does not declare it: the worker applies them, and says SDK 0.6 stops ─────────


def test_overrides_are_applied_before_run_job_and_restored_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder(monkeypatch)
    events = _run(tmp_path, _Plugin(rec), {"_alias_overrides": _OVERRIDES})
    assert events[-1]["event"] == "done"
    assert rec.kinds() == ["apply", "run", "restore"]
    assert rec.calls[0][1] == _OVERRIDES["overrides"]
    assert rec.calls[2][1] == [{"token": "DATA", "original_path": "/original"}]


def test_the_job_log_says_sdk_06_stops_applying_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(monkeypatch)
    plugin = _Plugin(rec)
    plugin.id = "yolo"
    logs = _logs(_run(tmp_path, plugin, {"_alias_overrides": _OVERRIDES}))
    assert logs[0] == "Applied 1 alias override(s)"
    assert len(logs) == 2
    assert "(<DATA>)" in logs[1]
    assert "plugin yolo" in logs[1]
    assert "SDK 0.6" in logs[1]
    assert "applies_alias_overrides = True" in logs[1]
    assert "tlc.integration.compute.job_scope(ctx)" in logs[1]


def test_overrides_are_restored_when_run_job_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(monkeypatch)
    events = _run(tmp_path, _Plugin(rec, fail=True), {"_alias_overrides": _OVERRIDES})
    assert events[-1]["event"] == "error"
    assert rec.kinds() == ["apply", "run", "restore"]


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
    events = _run(tmp_path, _Plugin(rec), params)
    assert events[-1]["event"] == "done"
    assert rec.kinds() == ["run"]
    assert _logs(events) == []


def test_overlapping_jobs_with_the_same_staged_data_share_the_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Applied by the first job, restored after the last: never under a job still reading through it."""
    rec = _Recorder(monkeypatch)
    plugin = _Blocking(rec)
    worker = _Worker(plugin, "p", tmp_path / "state")
    first = worker.start_job("j1", _staged("/staged/data"))
    assert plugin.started.wait(5)
    plugin.started.clear()
    second = worker.start_job("j2", _staged("/staged/data/"))  # a trailing slash is the same place
    assert plugin.started.wait(5)
    assert rec.kinds() == ["apply", "run", "run"]  # applied once

    plugin.release.set()
    assert first.wait(5)
    assert second.wait(5)
    assert rec.kinds() == ["apply", "run", "run", "restore"]  # restored once, after both


# ── a plugin that declares it: the worker leaves them to the plugin ─────────────────────────────


def test_a_declared_plugin_gets_the_overrides_and_the_worker_applies_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder(monkeypatch)
    plugin = _Declared(rec)
    events = _run(tmp_path, plugin, {"_alias_overrides": _OVERRIDES})
    assert events[-1]["event"] == "done"
    assert rec.kinds() == ["run"]
    assert plugin.seen is not None
    assert [(o.token, o.path) for o in plugin.seen] == [("DATA", "/staged/data")]
    assert _logs(events) == []


def test_a_declared_plugin_that_never_reads_them_is_warned_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rec = _Recorder(monkeypatch)
    plugin = _Declared(rec, read=False)
    plugin.id = "yolo"
    with caplog.at_level("WARNING", logger="tlc_plugin_sdk.worker"):
        events = _run(tmp_path, plugin, {"_alias_overrides": _OVERRIDES})
    assert events[-1]["event"] == "done"  # the result stands: a plugin bug, not a failed job
    assert rec.kinds() == ["run"]
    [log] = _logs(events)
    assert "Plugin yolo declares applies_alias_overrides" in log
    assert "never read ctx.alias_overrides" in log
    assert "<DATA>" in log
    assert log in caplog.text


def test_a_declared_plugin_is_warned_about_even_when_its_job_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder(monkeypatch)
    events = _run(tmp_path, _Declared(rec, read=False, fail=True), {"_alias_overrides": _OVERRIDES})
    assert events[-1]["event"] == "error"
    assert len(_logs(events)) == 1


def test_a_declared_plugin_without_staged_data_is_not_warned_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder(monkeypatch)
    assert _logs(_run(tmp_path, _Declared(rec, read=False), {})) == []


# ── the overlap guard ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("declared", [False, True])
def test_a_job_staging_an_alias_elsewhere_than_a_live_job_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declared: bool
) -> None:
    rec = _Recorder(monkeypatch)
    plugin = _Blocking(rec)
    plugin.applies_alias_overrides = declared  # type: ignore[misc]
    worker = _Worker(plugin, "p", tmp_path / "state")
    first = worker.start_job("j1", _staged("/staged/one"))
    assert plugin.started.wait(5)
    with pytest.raises(AliasOverrideConflict) as excinfo:
        worker.start_job("j2", _staged("/staged/two"))
    message = str(excinfo.value)
    assert "<DATA>" in message
    assert "/staged/two" in message
    assert "/staged/one" in message
    assert "j1" in message
    assert [j.job_id for j in worker.live_jobs()] == ["j1"]  # the refused job never started

    # Other aliases, or none at all, may run beside it.
    beside = [worker.start_job("j3", _staged("/staged/other", token="OTHER")), worker.start_job("j4", {})]
    plugin.release.set()
    assert all(job.wait(5) for job in [first, *beside])
    # Once the first has ended, the second's data can be staged.
    assert worker.start_job("j2", _staged("/staged/two")).wait(5)


def test_a_finished_job_does_not_hold_its_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(monkeypatch)
    worker = _Worker(_Plugin(rec), "p", tmp_path / "state")
    assert worker.start_job("j1", _staged("/staged/one")).wait(5)
    assert worker.start_job("j2", _staged("/staged/two")).wait(5)


def test_the_run_route_answers_a_conflict_with_409_and_the_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder(monkeypatch)
    plugin = _Blocking(rec)
    plugin.id = "p"
    worker = _Worker(plugin, "p", tmp_path / "state")
    app = build_plugin_app(plugin, extra_handlers=_control_handlers(worker))
    with TestClient(app=app) as client:
        first = worker.start_job("j1", _staged("/staged/one"))
        assert plugin.started.wait(5)
        resp = client.post("/jobs/j2/run", json=_staged("/staged/two"))
        assert resp.status_code == 409, resp.text
        assert "one worker resolves an alias one way at a time" in resp.json()["detail"]
        plugin.release.set()
        assert first.wait(5)


# ── /health says whether the plugin applies them ───────────────────────────────────────────


class _Hub(HubPlugin):
    def get_ui_fragment(self) -> str:
        return ""


@pytest.mark.parametrize(
    ("plugin", "expected"),
    [(_Plugin.__new__(_Plugin), False), (_Declared.__new__(_Declared), True), (_Hub(), False)],
)
def test_health_reports_whether_the_plugin_applies_alias_overrides(plugin: HubPlugin, expected: bool) -> None:
    plugin.id = "p"
    with TestClient(app=build_plugin_app(plugin)) as client:
        assert client.get("/health").json()["applies_alias_overrides"] is expected


def test_compute_plugins_do_not_apply_alias_overrides_by_default() -> None:
    assert ComputePlugin.applies_alias_overrides is False
