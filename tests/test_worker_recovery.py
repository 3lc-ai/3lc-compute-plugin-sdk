# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""A disconnected controller can recover progress and the actual terminal outcome."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from litestar.testing import TestClient

from tlc_plugin_sdk.asgi_app import build_plugin_app
from tlc_plugin_sdk.contract import ComputePlugin
from tlc_plugin_sdk.job_context import JobContext
from tlc_plugin_sdk.worker import _control_handlers, _Worker


class Training(ComputePlugin):
    def __init__(self) -> None:
        self.finish = threading.Event()
        self.started = threading.Event()
        self.calls = 0

    def get_ui_fragment(self) -> str:
        return ""

    def run_job(self, ctx: JobContext) -> None:
        self.calls += 1
        ctx.progress(percent=20, label="Training")
        self.started.set()
        assert self.finish.wait(5)
        ctx.result("s3://bucket/project/runs/trained")


def test_disconnect_does_not_cancel_and_outcome_survives_worker_restart(tmp_path: Path) -> None:
    plugin = Training()
    worker = _Worker(plugin, "train", tmp_path)
    job = worker.start_job("training-1", {})
    assert plugin.started.wait(2)
    job.abandon()  # HTTP stream closed while the laptop slept
    try:
        app = build_plugin_app(plugin, extra_handlers=_control_handlers(worker), token="private")
        with TestClient(app) as client:
            assert client.get("/jobs/training-1/status").status_code == 401
            response = client.get("/jobs/training-1/status", headers={"Authorization": "Bearer private"})
            assert response.status_code == 200
            assert response.json()["state"] == "running"
            assert not job.ctx.cancelled
    finally:
        plugin.finish.set()
    assert job.wait(3)
    restarted = _Worker(plugin, "train", tmp_path)
    saved = restarted.job_status("training-1")
    assert saved is not None and saved["state"] == "completed"
    assert any(e.get("run_url") == "s3://bucket/project/runs/trained" for e in saved["events"])
    assert plugin.calls == 1
    assert job.events.empty(), "disconnected progress must not accumulate in the old stream queue"


def test_worker_restart_reports_interrupted_job_instead_of_running_forever(tmp_path: Path) -> None:
    plugin = Training()
    worker = _Worker(plugin, "train", tmp_path)
    job = worker.start_job("oom", {})
    try:
        assert plugin.started.wait(2)
        # Only the initial record survived a process kill.
        restarted = _Worker(plugin, "train", tmp_path)
        status = restarted.job_status("oom")
        assert status is not None and status["state"] == "failed"
        assert "worker exited" in status["events"][0]["message"]
        assert restarted.job_status("../oom") is None
        assert restarted.job_status("unknown") is None
    finally:
        plugin.finish.set()
        assert job.wait(3)


def test_detached_progress_is_bounded_and_preserves_terminal_event(tmp_path: Path) -> None:
    plugin = Training()
    worker = _Worker(plugin, "train", tmp_path)
    job = worker.start_job("many-events", {})
    try:
        assert plugin.started.wait(2)
        job.abandon()
        for i in range(1000):
            job._put_event({"event": "metric", "label": str(i), "value": i})
        assert len(job.snapshot()["events"]) <= 256
    finally:
        plugin.finish.set()
        assert job.wait(3)
    status: dict[str, Any] = job.snapshot()
    assert status["state"] == "completed"


def test_status_masks_tokens_in_memory_and_on_disk(tmp_path: Path) -> None:
    from tlc_plugin_sdk.connections import CREDENTIAL_KEY

    class QuotesToken(Training):
        def run_job(self, ctx: JobContext) -> None:
            assert ctx.credential is not None
            ctx.fail(f"rejected {ctx.credential.secret}")

    secret = 'quoted"secret\\with-suffix'
    worker = _Worker(QuotesToken(), "train", tmp_path)
    job = worker.start_job("redacted", {CREDENTIAL_KEY: {"secret": secret}})
    assert job.wait(3)
    status = worker.job_status("redacted")
    assert status is not None
    assert status["events"][-1]["message"] == "rejected ***"
    assert secret not in (tmp_path / "redacted" / "worker-status.json").read_text()


def test_full_disk_does_not_hide_completion_or_keep_worker_busy(tmp_path: Path, monkeypatch: Any) -> None:
    plugin = Training()
    worker = _Worker(plugin, "train", tmp_path)
    job = worker.start_job("full-disk", {})
    assert plugin.started.wait(2)

    def fail_save() -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(job, "_save_snapshot", fail_save)
    plugin.finish.set()
    assert job.wait(3)
    assert worker.active_jobs() == 0
    status = worker.job_status("full-disk")
    assert status is not None and status["state"] == "completed"
