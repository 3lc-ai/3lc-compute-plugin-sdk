# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import concurrent.futures
import threading

import pytest
from litestar.testing import TestClient
from test_worker_control_routes import _wait, _worker_app

from tlc_plugin_sdk.contract import ComputePlugin
from tlc_plugin_sdk.host_requests import MAX_BYTES, HostRequests


def test_host_requests_are_bounded_and_cancel_without_a_reply():
    events = []
    cancelled = threading.Event()
    channel = HostRequests(events.append, cancelled)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(channel.call, "model", {"message": "hello"})
        assert _wait(lambda: len(events) == 1)
        pending = channel.pending()
        assert len(pending) == 1
        assert channel.answer(pending[0]["request_id"], {"result": "ok"})
        assert not channel.answer(pending[0]["request_id"], {"result": "different"})
        assert future.result() == "ok"
        assert channel.pending() == []
        future = pool.submit(channel.call, "state", {})
        assert _wait(lambda: len(events) == 2)
        cancelled.set()
        with pytest.raises(RuntimeError, match="cancelled"):
            future.result()
    with pytest.raises(ValueError):
        channel.call("model", {"text": "x" * (MAX_BYTES + 1)})


def test_host_request_round_trip_uses_authenticated_worker_control(tmp_path):
    class Plugin(ComputePlugin):
        def get_ui_fragment(self):
            return "<div/>"

        def run_job(self, ctx):
            assert not ctx.credentials
            ctx.output("answer", ctx.request_host("model", {"question": "hello"}, 5))

    app, worker = _worker_app(tmp_path, Plugin(), token="test-control-token")
    with TestClient(app) as client:
        job = worker.start_job("agent", {"_mcp": {"transport": "host_requests"}})
        assert _wait(lambda: bool(job.host_requests.pending()))
        request = job.host_requests.pending()[0]
        path = f"/jobs/agent/host-response/{request['request_id']}"
        assert client.post(path, json={"result": "forged"}).status_code == 401
        response = client.post(path, json={"result": "hello"}, headers={"Authorization": "Bearer test-control-token"})
        assert response.status_code == 200, response.text
        assert _wait(lambda: not job.is_alive())
        status = client.get("/jobs/agent/status", headers={"Authorization": "Bearer test-control-token"}).json()
        assert any(e.get("name") == "answer" and e.get("value") == "hello" for e in status["events"])


def test_parallel_requests_have_independent_answers_and_bounded_admission():
    from tlc_plugin_sdk.host_requests import MAX_PENDING

    events = []
    channel = HostRequests(events.append, threading.Event())
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PENDING) as pool:
        work = [pool.submit(channel.call, "read", {"index": i}, 5) for i in range(MAX_PENDING)]
        assert _wait(lambda: len(channel.pending()) == MAX_PENDING)
        with pytest.raises(TimeoutError, match="busy"):
            channel.call("read", {}, 0.01)
        for event in reversed(events):
            assert channel.answer(event["request_id"], {"result": event["payload"]["index"]})
        assert [future.result() for future in work] == list(range(MAX_PENDING))
        assert not channel.pending()
