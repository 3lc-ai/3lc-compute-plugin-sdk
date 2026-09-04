# Copyright (c) 2026 3LC Inc. All rights reserved.
"""A cancel must actually stop a job — whatever the host's stream did in between.

Found live 2026-09-04: the host restarted while a GPU training streamed; the worker dropped
the job from its registry when the *stream* ended, so every later cancel answered 404 while
the thread kept training for 40 minutes. Three guarantees are pinned here:

* the registry keeps a job for as long as its THREAD runs, not its stream;
* a cancel that the job ignores is escalated (the worker ends itself after the grace);
* ``cancel_all`` stops everything still running (the host asks after a restart).
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from tlc_plugin_sdk import worker as worker_mod
from tlc_plugin_sdk.contract import ComputePlugin
from tlc_plugin_sdk.worker import _cancel_grace_seconds, _Worker

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tlc_plugin_sdk.job_context import JobContext


class _Plugin(ComputePlugin):
    def __init__(self, work: Callable[[JobContext], None]) -> None:
        self._work = work

    def get_ui_fragment(self) -> str:
        return ""

    def compute(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def run_job(self, ctx: JobContext) -> None:
        self._work(ctx)


def _wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _cooperative(started: threading.Event) -> Callable[[JobContext], None]:
    def work(ctx: JobContext) -> None:
        started.set()
        while not ctx.cancelled:  # a well-behaved training loop
            time.sleep(0.01)

    return work


def test_job_stays_registered_after_the_stream_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLC_WORKER_CANCEL_GRACE_S", "0")
    started = threading.Event()
    worker = _Worker(_Plugin(_cooperative(started)), "tiny", tmp_path / "state")
    worker.start_job("j1", {})
    assert started.wait(5)

    worker.finish_job("j1")  # what the /run stream does when the host goes away
    assert worker.active_jobs() == 1, "a running thread must stay registered when its stream ends"

    assert worker.cancel_job("j1") is True  # ...so a later cancel still reaches it
    assert _wait_until(lambda: worker.active_jobs() == 0)
    worker.finish_job("j1")
    assert "j1" not in worker._jobs


def test_finished_job_is_forgotten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLC_WORKER_CANCEL_GRACE_S", "0")
    worker = _Worker(_Plugin(lambda ctx: None), "tiny", tmp_path / "state")
    job = worker.start_job("j2", {})
    assert job.wait(5)
    assert _wait_until(lambda: "j2" not in worker._jobs), "the thread's end drops the registry entry"
    assert worker.cancel_job("j2") is False


def test_cancel_all_stops_every_running_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLC_WORKER_CANCEL_GRACE_S", "0")
    a, b = threading.Event(), threading.Event()
    worker = _Worker(_Plugin(lambda ctx: None), "tiny", tmp_path / "state")
    worker.plugin = _Plugin(_cooperative(a))
    worker.start_job("a", {})
    worker.plugin = _Plugin(_cooperative(b))
    worker.start_job("b", {})
    assert a.wait(5) and b.wait(5)

    assert sorted(worker.cancel_all()) == ["a", "b"]
    assert _wait_until(lambda: worker.active_jobs() == 0)
    assert worker.cancel_all() == []


def test_ignored_cancel_ends_the_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A loop that never looks at ``ctx.cancelled`` is stopped by ending the process."""
    monkeypatch.setenv("TLC_WORKER_CANCEL_GRACE_S", "0.2")
    exited: list[int] = []
    release = threading.Event()
    monkeypatch.setattr(worker_mod.os, "_exit", lambda code: (exited.append(code), release.set()))
    monkeypatch.setattr(worker_mod.logging, "shutdown", lambda: None)

    started = threading.Event()

    def stubborn(ctx: JobContext) -> None:
        started.set()
        release.wait(10)  # ignores the cancel until the watchdog "exits"

    worker = _Worker(_Plugin(stubborn), "tiny", tmp_path / "state")
    worker.start_job("j3", {})
    assert started.wait(5)
    assert worker.cancel_job("j3") is True
    assert release.wait(5), "the watchdog must fire once the grace has passed"
    assert exited == [3]


def test_honoured_cancel_does_not_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLC_WORKER_CANCEL_GRACE_S", "0.3")
    exited: list[int] = []
    monkeypatch.setattr(worker_mod.os, "_exit", lambda code: exited.append(code))
    started = threading.Event()
    worker = _Worker(_Plugin(_cooperative(started)), "tiny", tmp_path / "state")
    worker.start_job("j4", {})
    assert started.wait(5)
    assert worker.cancel_job("j4") is True
    assert _wait_until(lambda: worker.active_jobs() == 0)
    time.sleep(0.5)  # past the grace
    assert exited == []


@pytest.mark.parametrize(("raw", "expected"), [("", 60.0), ("15", 15.0), ("0", 0.0), ("nonsense", 60.0)])
def test_cancel_grace_setting(monkeypatch: pytest.MonkeyPatch, raw: str, expected: float) -> None:
    monkeypatch.setenv("TLC_WORKER_CANCEL_GRACE_S", raw)
    assert _cancel_grace_seconds() == expected
