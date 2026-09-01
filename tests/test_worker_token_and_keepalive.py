# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Bearer-token guard and job-stream keepalive — the two TCP-worker hardenings.

A worker on a Unix socket is guarded by file permissions; a worker bound to TCP on a
GPU node is reachable through the provider's proxy, so it (a) requires a bearer token
on every route when one is configured, and (b) can emit ``ping`` keepalive events on
the job stream so proxy idle windows (~100 s) don't kill long training epochs. Both
are strictly opt-in: no token → no middleware; no env → no pings.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from litestar.testing import TestClient

from tlc_plugin_sdk.asgi_app import build_plugin_app
from tlc_plugin_sdk.contract import ComputePlugin
from tlc_plugin_sdk.worker import _control_handlers, _stream_keepalive_seconds, _Worker

if TYPE_CHECKING:
    from tlc_plugin_sdk.job_context import JobContext


class _TinyPlugin(ComputePlugin):
    """Smallest viable plugin: a fragment, a custom route, a slow job."""

    def get_ui_fragment(self) -> str:
        return "<div>tiny</div>"

    def run_job(self, ctx: JobContext) -> None:
        time.sleep(float(ctx.params.get("sleep", 0)))


def _app(tmp_path: Path, *, token: str | None) -> Any:
    plugin = _TinyPlugin()
    plugin.id = "tiny"
    worker = _Worker(plugin, "tiny", tmp_path / "state")
    return build_plugin_app(plugin, extra_handlers=_control_handlers(worker), token=token)


# ── bearer guard ─────────────────────────────────────────────────────────────


def test_no_token_installs_no_guard(tmp_path: Path) -> None:
    with TestClient(app=_app(tmp_path, token=None)) as client:
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "s3cret"},  # missing scheme
    ],
)
def test_tokened_worker_rejects_bad_auth(tmp_path: Path, headers: dict[str, str]) -> None:
    with TestClient(app=_app(tmp_path, token="s3cret")) as client:
        for path in ("/health", "/ui", "/compute"):
            assert client.get(path, headers=headers).status_code == 401, path
        assert client.post("/jobs/j1/cancel", headers=headers).status_code == 401


def test_tokened_worker_accepts_the_bearer_everywhere(tmp_path: Path) -> None:
    auth = {"Authorization": "Bearer s3cret"}
    with TestClient(app=_app(tmp_path, token="s3cret")) as client:
        health = client.get("/health", headers=auth)
        assert health.status_code == 200
        assert health.json()["ok"] is True
        assert client.get("/ui", headers=auth).status_code == 200
        # cancel for an unknown job answers 404 THROUGH the guard — auth passed.
        assert client.post("/jobs/nope/cancel", headers=auth).status_code == 404


# ── keepalive ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("30", 30.0), ("0.5", 0.5), ("0", None), ("-5", None), ("abc", None)],
)
def test_keepalive_env_parsing(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float | None) -> None:
    if raw is None:
        monkeypatch.delenv("TLC_WORKER_STREAM_KEEPALIVE_S", raising=False)
    else:
        monkeypatch.setenv("TLC_WORKER_STREAM_KEEPALIVE_S", raw)
    assert _stream_keepalive_seconds() == expected


def _run_stream_lines(client: TestClient[Any], params: dict[str, Any]) -> list[dict[str, Any]]:
    with client.stream("POST", "/jobs/j1/run", json=params) as resp:
        assert resp.status_code < 300  # Litestar answers POST streams with 201
        return [json.loads(line) for line in resp.iter_lines() if line.strip()]


def test_quiet_stream_carries_pings_between_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLC_WORKER_STREAM_KEEPALIVE_S", "0.05")
    with TestClient(app=_app(tmp_path, token=None)) as client:
        events = _run_stream_lines(client, {"sleep": 0.4})
    kinds = [e.get("event") for e in events]
    assert "ping" in kinds, kinds
    assert events[-1] == {"event": "done", "status": "completed", "job_id": "j1"}
    # Pings carry the job id so a multiplexing client could route them, nothing more.
    assert all(e == {"event": "ping", "job_id": "j1"} for e in events if e.get("event") == "ping")


def test_no_keepalive_means_no_pings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TLC_WORKER_STREAM_KEEPALIVE_S", raising=False)
    with TestClient(app=_app(tmp_path, token=None)) as client:
        events = _run_stream_lines(client, {"sleep": 0.2})
    assert all(e.get("event") != "ping" for e in events)
    assert events[-1]["event"] == "done"


def test_busy_reports_in_flight_jobs(tmp_path: Path) -> None:
    """/busy is the node-agent's self-destruct guard: >0 while a job is in flight.

    Driven through the worker object directly — the ASGI test transport buffers a
    streamed response to completion before returning, so an over-HTTP job is already
    finished by the time the stream context yields.
    """
    plugin = _TinyPlugin()
    plugin.id = "tiny"
    worker = _Worker(plugin, "tiny", tmp_path / "state")
    app = build_plugin_app(plugin, extra_handlers=_control_handlers(worker), token=None)
    with TestClient(app=app) as client:
        assert client.get("/busy").json() == {"active_jobs": 0}
        worker.start_job("j1", {"sleep": 0.2})
        assert client.get("/busy").json()["active_jobs"] == 1
        worker.finish_job("j1")  # what the run stream's finally does
        assert client.get("/busy").json() == {"active_jobs": 0}
