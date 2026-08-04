# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""A worker must not keep a finished job's GPU memory cached.

PyTorch's caching allocator hands freed blocks back to itself, not to the driver, so a
long-lived worker stays charged for a finished job's peak allocation. The host serializes
GPU jobs and releases the queue slot as soon as the terminal event closes the ``/run``
stream — so the reclaim has to happen *before* that event is emitted, or the next job is
handed a card whose memory the previous worker is still holding.

``torch`` is not an SDK dependency, so these tests inject a fake into ``sys.modules``:
that is also exactly the condition the code under test keys on (never import torch —
use it only if the plugin already loaded it)."""

from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

from tlc_plugin_sdk.contract import ComputePlugin
from tlc_plugin_sdk.worker import _Job, release_gpu_memory

if TYPE_CHECKING:
    from collections.abc import Callable

    from tlc_plugin_sdk.job_context import JobContext


class _FakeCuda:
    """Stand-in for ``torch.cuda`` recording whether the cache was flushed."""

    def __init__(self, *, available: bool = True, raises: bool = False) -> None:
        self._available = available
        self._raises = raises
        self.empty_cache_calls = 0

    def is_available(self) -> bool:
        if self._raises:
            msg = "CUDA driver exploded"
            raise RuntimeError(msg)
        return self._available

    def empty_cache(self) -> None:
        self.empty_cache_calls += 1


def _fake_torch(cuda: _FakeCuda) -> ModuleType:
    module = ModuleType("torch")
    module.cuda = cuda
    return module


def test_no_torch_loaded_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin that never loaded torch has nothing to reclaim — and must not import it."""
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    assert release_gpu_memory() is False
    # The whole point of keying on sys.modules: reclaim must not drag torch into a
    # worker that does not use it (the SDK's import-light invariant).
    assert "torch" not in sys.modules


def test_releases_when_cuda_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    cuda = _FakeCuda(available=True)
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda))
    assert release_gpu_memory() is True
    assert cuda.empty_cache_calls == 1


def test_skips_when_cuda_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CPU-only worker that imported torch should not pay for a cache flush."""
    cuda = _FakeCuda(available=False)
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda))
    assert release_gpu_memory() is False
    assert cuda.empty_cache_calls == 0


def test_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reclaim runs on the path that reports a job's outcome; it must never raise there."""
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(_FakeCuda(raises=True)))
    assert release_gpu_memory() is False


class _Plugin(ComputePlugin):
    """Minimal concrete plugin; only ``run_job`` is exercised here."""

    def __init__(self, work: Callable[[JobContext], None]) -> None:
        self._work = work

    def get_ui_fragment(self) -> str:
        return ""

    def compute(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def run_job(self, ctx: JobContext) -> None:
        self._work(ctx)


def _drain(events: queue.Queue[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    while not events.empty():
        out.append(str(events.get_nowait()["event"]))
    return out


@pytest.mark.parametrize("fail", [False, True])
def test_reclaim_precedes_the_terminal_event(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail: bool) -> None:
    """The queue slot is freed on the terminal event, so reclaim must already have run.

    Asserted by capturing the event queue's depth at reclaim time — the ordering
    guarantee itself, rather than a call count a later refactor could satisfy while
    reintroducing the race. Both outcomes are covered: a job that OOMs takes the failure
    path and is precisely the one holding the most memory.
    """
    depth_at_reclaim: list[int] = []

    def work(ctx: JobContext) -> None:
        if fail:
            msg = "CUDA out of memory"
            raise RuntimeError(msg)

    job = _Job("job-1", {}, tmp_path, _Plugin(work))

    def record() -> bool:
        depth_at_reclaim.append(job.events.qsize())
        return True

    monkeypatch.setattr("tlc_plugin_sdk.worker.release_gpu_memory", record)

    job._run()

    assert depth_at_reclaim == [0], "reclaim ran after the terminal event was queued"
    assert _drain(job.events) == ["error" if fail else "done"]


def test_reclaim_on_the_real_thread_path(tmp_path: Path) -> None:
    """Sanity: the real (torch-free) reclaim on the actual job thread is harmless."""
    done = threading.Event()
    job = _Job("job-2", {}, tmp_path, _Plugin(lambda ctx: done.set()))
    job.start()
    job._thread.join(timeout=5)
    assert done.is_set()
    assert _drain(job.events) == ["done"]
