# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""``PluginJobs.run``: a start the compute refuses reaches the card's ``onError`` instead of escaping."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk.shared.job_tracker import job_tracker_script

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(tmp_path: Path, *, start: str) -> Any:
    """Mount the tracker on a bare window whose authFetch answers ``start`` (a JS expression) and run one job."""
    harness = tmp_path / "tracker.js"
    harness.write_text(
        "var window = globalThis; var unhandled = [];\n"
        "process.on('unhandledRejection', function (e) { unhandled.push(String(e && e.message || e)); });\n"
        "window.PLUGIN_API = { getConfig: function () { return 'http://compute.test'; }, libs: {},\n"
        "  authFetch: function () { return "
        + start
        + "; } };\n"
        + job_tracker_script()
        + "\nvar seen = { errors: [], done: [], updates: [] };\n"
        "var result = PluginJobs.run('jupyter', { idle_minutes: 60 }, {\n"
        "  onUpdate: function (j) { seen.updates.push(j); }, onDone: function (j) { seen.done.push(j); },\n"
        "  onError: function (j) { seen.errors.push(j); } });\n"
        "result.then(function (r) { seen.result = r; })\n"
        "  .then(function () { return new Promise(function (res) { setTimeout(res, 20); }); })\n"
        "  .then(function () { seen.unhandled = unhandled; process.stdout.write(JSON.stringify(seen)); });\n",
        encoding="utf-8",
    )
    done = subprocess.run(["node", str(harness)], check=True, capture_output=True, text=True)
    return json.loads(done.stdout)


@needs_node
def test_a_refused_start_reaches_on_error_and_escapes_nowhere(tmp_path: Path) -> None:
    refusal = "This run would write its project to this machine's disk, which the node cannot reach."
    out = _run(tmp_path, start=f"Promise.reject(new Error({json.dumps(refusal)}))")
    assert out["errors"] == [{"status": "failed", "error": refusal, "subtitle": refusal}]
    assert out["result"] == {"error": refusal}
    assert out["done"] == [] and out["unhandled"] == []


@needs_node
def test_a_start_answered_with_an_error_body_reaches_on_error(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        start="Promise.resolve({ json: function () { return Promise.resolve({ error: 'no such plugin' }); } })",
    )
    assert [e["error"] for e in out["errors"]] == ["no such plugin"]
    assert out["unhandled"] == []
