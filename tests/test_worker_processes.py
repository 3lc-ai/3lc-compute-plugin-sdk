# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Real spawned workers: overlapping jobs, credentials, aliases and cancellation."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tlc_plugin_sdk.connections import current_credential
from tlc_plugin_sdk.contract import ComputePlugin
from tlc_plugin_sdk.shared import aliases
from tlc_plugin_sdk.worker import _Worker

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process-group isolation uses POSIX")
_REGISTRY = {}


class IsolatedPlugin(ComputePlugin):
    def get_ui_fragment(self):
        return ""

    def initialise_runtime(self):
        def apply(entries):
            _REGISTRY.update({entry["token"]: entry["path"] for entry in entries})
            return []

        aliases.apply_alias_overrides = apply

    def prepare_job(self, ctx):
        ctx.params["worker_local_input"] = b"private local bytes"

    def run_job(self, ctx):
        assert ctx.params["worker_local_input"] == b"private local bytes"
        root = Path(ctx.params["root"])
        token = current_credential("huggingface")
        if token is not None:
            assert token.secret == ctx.job_id + "-secret"
        assert "_credentials" not in ctx.params and "_require_job_isolation" not in ctx.params
        assert "TLC_WORKER_TOKEN" not in os.environ
        assert sys.argv == ["plugin-job"]
        child = None
        if ctx.params.get("descendant"):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (root / (ctx.job_id + ".started")).write_text(
            json.dumps({"pid": os.getpid(), "child": child.pid if child else None})
        )
        if ctx.params.get("host_request"):
            try:
                response = ctx.request_host("renew", {"job": ctx.job_id}, 5)
            except RuntimeError:
                if ctx.cancelled:
                    return
                raise
            ctx.protect_secret(response["secret"])
            ctx.log(response["secret"])
            ctx.output("answer", response["answer"])
        if ctx.params.get("crash"):
            os._exit(7)
        if ctx.params.get("fail"):
            ctx.fail("Missing training column")
        deadline = time.monotonic() + 15
        while not (root / (ctx.job_id + ".release")).exists():
            if not ctx.params.get("stubborn") and ctx.cancelled:
                return
            if time.monotonic() > deadline:
                ctx.fail("test release did not arrive")
            time.sleep(0.01)
        assert _REGISTRY["DATA"] == ctx.job_id
        if token is not None:
            ctx.log(token.secret)  # the durable status must redact even a misbehaving plugin
        ctx.progress(percent=100, label=ctx.job_id)


def wait_for(predicate, timeout=8):
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "worker did not reach expected state"
        time.sleep(0.01)


def params(root, job_id, **extra):
    return {
        "root": str(root),
        "_require_job_isolation": True,
        "_alias_overrides": {"enabled": True, "overrides": [{"token": "DATA", "path": job_id}]},
        "_credentials": {
            "huggingface": {"provider": "huggingface", "connection_id": job_id, "secret": job_id + "-secret"}
        },
        **extra,
    }


def worker(root):
    return _Worker(IsolatedPlugin(), "isolation-test", root / "state", entry=__name__ + ":IsolatedPlugin")


def test_concurrent_processes_keep_aliases_and_credentials_private(tmp_path, monkeypatch):
    monkeypatch.setenv("TLC_WORKER_TOKEN", "control-token-not-for-plugin")
    w = worker(tmp_path)
    a = w.start_job("one", params(tmp_path, "one"))
    b = w.start_job("two", params(tmp_path, "two"))
    try:
        wait_for(lambda: all((tmp_path / (name + ".started")).exists() for name in ("one", "two")))
        assert w.active_jobs() == 2
        pids = [json.loads((tmp_path / (name + ".started")).read_text())["pid"] for name in ("one", "two")]
        assert len({*pids, os.getpid()}) == 3
        a.abandon()  # disconnected host can still poll and cancel its process
        assert w.job_status("one")["state"] == "running"
    finally:
        for name in ("one", "two"):
            (tmp_path / (name + ".release")).touch()
        assert a.wait(8) and b.wait(8)
    for name in ("one", "two"):
        assert w.job_status(name)["state"] == "completed"
        snapshot = (tmp_path / "state" / name / "worker-status.json").read_text()
        assert name + "-secret" not in snapshot
    assert not _REGISTRY


@pytest.mark.parametrize("cancel_first", [False, True])
def test_isolated_host_requests_survive_disconnect_and_stay_job_scoped(tmp_path, cancel_first):
    w = worker(tmp_path)
    a = w.start_job("one", params(tmp_path, "one", host_request=True))
    b = w.start_job("two", params(tmp_path, "two", host_request=True))
    try:
        wait_for(lambda: bool(a.host_requests.pending()) and bool(b.host_requests.pending()))
        a.abandon()
        first = a.host_requests.pending()[0]
        second = b.host_requests.pending()[0]
        assert first["payload"] == {"job": "one"}
        assert second["payload"] == {"job": "two"}
        assert any(e.get("request_id") == first["request_id"] for e in w.job_status("one")["events"])
        assert not a.host_requests.answer(second["request_id"], {"result": "wrong job"})
        assert b.host_requests.answer(second["request_id"], {"result": {"secret": "renewed-two", "answer": "two"}})
        if cancel_first:
            assert w.cancel_job("one")
            assert a.wait(8)
            assert w.job_status("one")["state"] == "cancelled"
        else:
            assert a.host_requests.answer(first["request_id"], {"result": {"secret": "renewed-one", "answer": "one"}})
            assert not a.host_requests.answer(first["request_id"], {"result": "duplicate"})
    except BaseException:
        w.cancel_all()
        raise
    finally:
        for name in ("one", "two"):
            (tmp_path / (name + ".release")).touch()
        assert a.wait(8) and b.wait(8)
    for name in ("one", "two"):
        snapshot = w.job_status(name)
        assert not any(e.get("event") == "host_request" for e in snapshot["events"])
        assert "renewed-" not in json.dumps(snapshot)
        assert "renewed-" not in (tmp_path / "state" / name / "worker-status.json").read_text()
        if name == "one" and cancel_first:
            continue
        assert snapshot["state"] == "completed"
        assert any(e.get("name") == "answer" and e.get("value") == name for e in snapshot["events"])


def test_stubborn_cancel_kills_only_that_job_and_its_children(tmp_path, monkeypatch):
    monkeypatch.setenv("TLC_WORKER_CANCEL_GRACE_S", "0.1")
    w = worker(tmp_path)
    a = w.start_job("one", params(tmp_path, "one", stubborn=True, descendant=True))
    b = w.start_job("two", params(tmp_path, "two"))
    try:
        wait_for(lambda: all((tmp_path / (name + ".started")).exists() for name in ("one", "two")))
        assert w.cancel_job("one")
        assert a.wait(8)
        assert w.job_status("one")["state"] == "cancelled"
        assert b.is_alive() and w.active_jobs() == 1
        pids = json.loads((tmp_path / "one.started").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pids["pid"], 0)
        # An orphan may briefly be a zombie awaiting init; it must not be executing.
        result = subprocess.run(["ps", "-o", "stat=", "-p", str(pids["child"])], capture_output=True, text=True)
        assert not result.stdout.strip() or result.stdout.strip().startswith("Z")
    finally:
        (tmp_path / "two.release").touch()
        assert b.wait(8)
        w.cancel_all()
    assert w.job_status("two")["state"] == "completed"


def test_crashed_child_is_failed_without_killing_worker(tmp_path):
    w = worker(tmp_path)
    job = w.start_job("crash", params(tmp_path, "crash", crash=True))
    assert job.wait(8)
    assert w.job_status("crash")["state"] == "failed"
    assert "exit code 7" in str(w.job_status("crash"))
    assert w.active_jobs() == 0


def test_legacy_worker_refuses_isolation_requirement(tmp_path):
    w = _Worker(IsolatedPlugin(), "legacy", tmp_path)
    with pytest.raises(ValueError, match="cannot isolate"):
        w.start_job("one", params(tmp_path, "one"))


def _parent_worker(root):
    w = worker(root)
    w.start_job("orphan", params(root, "orphan", stubborn=True, descendant=True))
    time.sleep(30)


def test_worker_death_terminates_its_isolated_children(tmp_path):
    parent = multiprocessing.get_context("spawn").Process(target=_parent_worker, args=(tmp_path,))
    parent.start()
    try:
        wait_for(lambda: (tmp_path / "orphan.started").exists())
        pids = json.loads((tmp_path / "orphan.started").read_text())
        parent.terminate()
        parent.join(5)
        assert not parent.is_alive()

        def stopped():
            states = [
                subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
                for pid in pids.values()
            ]
            return all(not state or state.startswith("Z") for state in states)

        wait_for(stopped)
    finally:
        if parent.is_alive():
            parent.kill()
        parent.join(5)


def test_service_runtime_remains_shared_unless_isolation_is_requested(tmp_path):
    seen = []

    class Service(ComputePlugin):
        def get_ui_fragment(self):
            return ""

        def run_job(self, ctx):
            seen.append(os.getpid())

    w = _Worker(Service(), "service", tmp_path, entry="unused:Service")
    job = w.start_job("service", {})
    assert job.wait(3)
    assert seen == [os.getpid()]


def test_process_setup_failure_reports_outcome_and_clears_busy(tmp_path, monkeypatch):
    from tlc_plugin_sdk import worker as worker_module

    def no_context(_method):
        msg = "test resource exhaustion"
        raise OSError(msg)

    monkeypatch.setattr(worker_module.multiprocessing, "get_context", no_context)
    w = worker(tmp_path)
    job = w.start_job("setup", params(tmp_path, "setup"))
    assert job.wait(3)
    assert w.job_status("setup")["state"] == "failed"
    assert "test resource exhaustion" in str(w.job_status("setup"))
    assert w.active_jobs() == 0


def test_cleanup_race_keeps_the_original_failure_and_releases_queue(tmp_path, monkeypatch):
    from tlc_plugin_sdk import worker as worker_module

    original = worker_module._kill_job_process
    calls = []

    def delayed_cleanup(process):
        calls.append(process.pid)
        if len(calls) == 1:
            msg = "group exit race"
            raise PermissionError(msg)
        original(process)

    monkeypatch.setattr(worker_module, "_kill_job_process", delayed_cleanup)
    w = worker(tmp_path)
    job = w.start_job("failed", params(tmp_path, "failed", fail=True))
    assert job.wait(15)
    assert w.job_status("failed")["state"] == "failed"
    assert "Missing training column" in str(w.job_status("failed"))
    assert w.active_jobs() == 0
    assert len(calls) == 2
