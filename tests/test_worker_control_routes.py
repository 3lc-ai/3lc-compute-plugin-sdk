# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The worker's control channel over HTTP — every route the host and node-agent drive.

Pinned over HTTP (not by calling ``_Worker`` methods) because ``/busy`` and ``/jobs/cancel-all``
each shipped once without being registered: the unit tests called the methods and passed.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from litestar import get, websocket_listener
from litestar.testing import TestClient

from tlc_plugin_sdk.asgi_app import RESERVED_WORKER_PATHS, build_plugin_app
from tlc_plugin_sdk.contract import ComputePlugin
from tlc_plugin_sdk.worker import _control_handlers, _Worker

if TYPE_CHECKING:
    from tlc_plugin_sdk.job_context import JobContext


class _Plugin(ComputePlugin):
    def __init__(self) -> None:
        self.started = threading.Event()

    def get_ui_fragment(self) -> str:
        return "<div/>"

    def run_job(self, ctx: JobContext) -> None:
        self.started.set()
        while not ctx.cancelled:
            time.sleep(0.01)


def _worker_app(
    tmp_path: Path, plugin: ComputePlugin | None = None, *, token: str | None = None
) -> tuple[Any, _Worker]:
    plugin = plugin or _Plugin()
    plugin.id = "p"
    worker = _Worker(plugin, "p", tmp_path / "state")
    return build_plugin_app(plugin, extra_handlers=_control_handlers(worker), token=token), worker


def _wait(pred: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_cancel_all_is_a_registered_route_and_stops_live_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLC_WORKER_CANCEL_GRACE_S", "0")  # no escalation thread in tests
    plugin = _Plugin()
    app, worker = _worker_app(tmp_path, plugin)
    with TestClient(app=app) as client:
        # Start a job without consuming its stream (the host that started it has "restarted").
        job = worker.start_job("j1", {})
        assert plugin.started.wait(2)
        assert client.get("/busy").json() == {"active_jobs": 1}
        resp = client.post("/jobs/cancel-all")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"cancelling": ["j1"]}
        assert _wait(lambda: not job.is_alive())
        assert client.get("/busy").json() == {"active_jobs": 0}


@pytest.mark.parametrize("bad", ["..", "a/b", "x" * 65, "sp ace", "%2e%2e"])
def test_run_rejects_job_ids_that_are_not_safe_directory_names(tmp_path: Path, bad: str) -> None:
    app, worker = _worker_app(tmp_path)
    with pytest.raises(ValueError):
        worker.start_job(bad, {})
    with TestClient(app=app) as client:
        resp = client.post(f"/jobs/{bad}/run", json={})
        assert resp.status_code in (400, 404), (bad, resp.status_code)  # 404 when the router rejects the path
    assert not (tmp_path / "state" / bad).exists()


def test_run_for_a_live_job_id_answers_409_and_keeps_the_first_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TLC_WORKER_CANCEL_GRACE_S", "0")
    plugin = _Plugin()
    app, worker = _worker_app(tmp_path, plugin)
    with TestClient(app=app) as client:
        first = worker.start_job("dup", {})
        assert plugin.started.wait(2)
        resp = client.post("/jobs/dup/run", json={})
        assert resp.status_code == 409, resp.text
        assert worker.active_jobs() == 1
        assert client.post("/jobs/dup/cancel").status_code == 200  # the FIRST job is still the one addressed
        assert _wait(lambda: not first.is_alive())
        # Once ended, the id may be reused.
        second = worker.start_job("dup", {})
        assert second is not first
        worker.cancel_all()
        assert _wait(lambda: not second.is_alive())


def test_abandoned_stream_stops_buffering_events(tmp_path: Path) -> None:
    class Chatty(ComputePlugin):
        def get_ui_fragment(self) -> str:
            return ""

        def run_job(self, ctx: JobContext) -> None:
            while not ctx.cancelled:
                ctx.progress(percent=1.0)  # a per-step training emitter
                time.sleep(0.001)

    plugin = Chatty()
    plugin.id = "c"
    worker = _Worker(plugin, "c", tmp_path / "s")
    job = worker.start_job("j", {})
    assert _wait(lambda: job.events.qsize() > 5)
    job.abandon()
    time.sleep(0.05)
    size = job.events.qsize()
    time.sleep(0.05)
    assert job.events.qsize() == size == 0  # nothing accumulates once the host is gone
    assert job.is_alive() and worker.active_jobs() == 1  # still registered and cancellable
    job.ctx.request_cancel()
    assert _wait(lambda: not job.is_alive())


def test_websocket_routes_are_guarded_by_the_token(tmp_path: Path) -> None:
    class WsPlugin(_Plugin):
        def get_route_handlers(self) -> list[Any]:
            @websocket_listener("/ws")
            async def echo(data: str) -> str:
                return data

            return [echo]

    app, _ = _worker_app(tmp_path, WsPlugin(), token="s3cret")
    with TestClient(app=app) as client:
        with pytest.raises(Exception), client.websocket_connect("/ws") as ws:  # closed with 1008 before accept
            ws.send_text("x")
            ws.receive_text()
        with client.websocket_connect("/ws", headers={"Authorization": "Bearer s3cret"}) as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "hello"


def test_openapi_routes_are_not_mounted(tmp_path: Path) -> None:
    app, _ = _worker_app(tmp_path)
    with TestClient(app=app) as client:
        assert client.get("/schema").status_code == 404
        assert client.get("/schema/openapi.json").status_code == 404


def test_plugin_handlers_on_reserved_paths_are_not_mounted(tmp_path: Path) -> None:
    class Shadow(_Plugin):
        def get_route_handlers(self) -> list[Any]:
            @get("/busy", sync_to_thread=False)
            def fake_busy() -> dict[str, Any]:
                return {"active_jobs": 0, "shadowed": True}

            @get("/jobs/cancel-all", sync_to_thread=False)
            def fake_cancel() -> dict[str, Any]:
                return {}

            @get("/mine", sync_to_thread=False)
            def mine() -> dict[str, Any]:
                return {"ok": True}

            return [fake_busy, fake_cancel, mine]

    assert "/busy" in RESERVED_WORKER_PATHS
    app, _worker = _worker_app(tmp_path, Shadow())
    with TestClient(app=app) as client:
        assert client.get("/busy").json() == {"active_jobs": 0}  # the worker's, not the plugin's
        assert client.get("/mine").json() == {"ok": True}
        assert client.get("/jobs/cancel-all").status_code == 405  # only the worker's POST exists


def test_config_store_writes_owner_only_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tlc_plugin_sdk.shared import config_store as cs

    monkeypatch.setattr(cs, "CONFIG_ROOT", tmp_path / "cfg")

    @dataclass
    class Cfg:
        id: str = ""
        created: str = ""
        secret: str = field(default="k")

    store = cs.PluginConfigStore(Cfg, "p")
    saved = store.save_config(Cfg(secret="topsecret"))
    path = store.directory / f"{saved.id}.json"
    assert store.exists(saved.id)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.directory).st_mode) == 0o700
