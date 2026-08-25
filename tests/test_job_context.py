# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Golden wire-shape tests for ``JobContext`` and the worker's terminal events.

These pin the exact event dicts a plugin's ``run_job`` emits — the contract the
host's ``JobManager._apply`` folds into the generic job record. If a shape changes
here, the host relay changes with it.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tlc_plugin_sdk import JobContext, JobFailed


def _ctx(events: list[dict]) -> JobContext:
    return JobContext(
        "job-1",
        {"a": 1},
        Path("/tmp"),
        sink=events.append,
        cancel_event=threading.Event(),
    )


def test_progress_wire_shape() -> None:
    events: list[dict] = []
    _ctx(events).progress(percent=42.0, label="half", timing={"eta_s": 3})
    assert events == [
        {"event": "progress", "percent": 42.0, "label": "half", "timing": {"eta_s": 3}, "job_id": "job-1"}
    ]


def test_progress_indeterminate_passes_minus_one() -> None:
    events: list[dict] = []
    _ctx(events).progress(percent=-1)
    assert events == [{"event": "progress", "percent": -1, "label": "", "timing": None, "job_id": "job-1"}]


def test_metric_wire_shape() -> None:
    events: list[dict] = []
    _ctx(events).metric("loss", 0.04)
    assert events == [{"event": "metric", "label": "loss", "value": 0.04, "job_id": "job-1"}]


def test_log_wire_shape() -> None:
    events: list[dict] = []
    _ctx(events).log("hello")
    assert events == [{"event": "log", "message": "hello", "job_id": "job-1"}]


def test_result_is_positional_and_emits_run_url() -> None:
    events: list[dict] = []
    _ctx(events).result("s3://bucket/run")  # positional url; the run_url= keyword is gone
    assert events == [{"event": "result", "run_url": "s3://bucket/run", "job_id": "job-1"}]


def test_result_rejects_run_url_keyword() -> None:
    with pytest.raises(TypeError):
        _ctx([]).result(run_url="s3://bucket/run")  # type: ignore[call-arg]


def test_emit_custom_wire_shape() -> None:
    events: list[dict] = []
    _ctx(events).emit("epoch_metrics", {"epoch": 3, "loss": 0.04})
    assert events == [
        {"event": "custom", "name": "epoch_metrics", "payload": {"epoch": 3, "loss": 0.04}, "job_id": "job-1"}
    ]


def test_emit_reserved_name_rejected() -> None:
    with pytest.raises(ValueError, match="reserved"):
        _ctx([]).emit("job_update", {})


def test_fail_raises_jobfailed_with_bare_message() -> None:
    with pytest.raises(JobFailed) as excinfo:
        _ctx([]).fail("bad input")
    assert str(excinfo.value) == "bad input"


def test_job_id_stamped_on_every_event() -> None:
    events: list[dict] = []
    ctx = _ctx(events)
    ctx.progress(percent=1)
    ctx.metric("m", 1)
    ctx.log("l")
    ctx.result("u")
    ctx.emit("e", {})
    assert [e["job_id"] for e in events] == ["job-1"] * 5


# ── worker terminal-event formatting (A2) ─────────────────────────────────────


class _FakePlugin:
    def __init__(self, run_job):  # type: ignore[no-untyped-def]
        self._run_job = run_job

    def run_job(self, ctx: JobContext) -> None:
        self._run_job(ctx)


def _terminal_event(run_job) -> dict:  # type: ignore[no-untyped-def]
    from tlc_plugin_sdk.worker import _Job

    job = _Job("j", {}, Path("/tmp"), _FakePlugin(run_job))
    job._run()  # run synchronously on this thread
    events: list[dict] = []
    while not job.events.empty():
        events.append(job.events.get())
    return events[-1]


def test_worker_jobfailed_is_reported_as_bare_message() -> None:
    def run_job(ctx: JobContext) -> None:
        ctx.fail("nothing selected")

    assert _terminal_event(run_job) == {"event": "error", "message": "nothing selected", "job_id": "j"}


def test_worker_other_exception_keeps_type_prefix() -> None:
    def run_job(ctx: JobContext) -> None:
        msg = "boom"
        raise ValueError(msg)

    term = _terminal_event(run_job)
    assert term == {"event": "error", "message": "ValueError: boom", "job_id": "j"}


def test_worker_success_is_done_completed() -> None:
    def run_job(ctx: JobContext) -> None:
        ctx.progress(percent=100)

    assert _terminal_event(run_job) == {"event": "done", "status": "completed", "job_id": "j"}
