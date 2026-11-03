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


@needs_node
@pytest.mark.parametrize(
    "scenario",
    [
        "already_done",
        "missed_event",
        "reconnect",
        "no_socket",
        "cancelled",
        "failed",
        "unsubscribe",
        "coalesced",
        "start_race",
        "retry",
    ],
)
def test_terminal_reconciliation(tmp_path: Path, scenario: str) -> None:
    """A missed live event is recovered once, without leaks or parallel polling."""
    harness = tmp_path / "reconcile.js"
    harness.write_text(
        r"""
const assert = require('node:assert/strict');
var window = globalThis;
var timers = new Map(), timerId = 0, requests = 0, fail = false;
var jobs = [], sockets = {}, updates = [], done = [], errors = [];
var setTimeout = (fn) => { timers.set(++timerId, fn); return timerId; };
var clearTimeout = (id) => timers.delete(id);
function socket(namespace) {
  if (!sockets[namespace]) {
    var listeners = {};
    sockets[namespace] = {
      on: (event, fn) => { (listeners[event] ||= []).push(fn); },
      off: (event, fn) => { var a = listeners[event] || []; var i=a.indexOf(fn); if(i>=0) a.splice(i,1); },
      emit: (event, payload) => (listeners[event] || []).slice().forEach(fn => fn(payload))
    };
  }
  return sockets[namespace];
}
window.PLUGIN_API = {getConfig: () => 'http://compute.test', libs:{io:socket}, authFetch: (url) => {
  if (url.endsWith('/run')) return Promise.resolve({json:async () => ({job_id:'a'})});
  requests++;
  return fail ? Promise.reject(new Error('offline')) : Promise.resolve({json:async () => jobs.slice()});
}};
var handlers = {onUpdate:j=>updates.push(j), onDone:j=>done.push(j), onError:j=>errors.push(j)};
async function flush() { for(var i=0;i<15;i++) await Promise.resolve(); }
async function tick() { var copy=[...timers.values()]; timers.clear(); copy.forEach(fn=>fn()); await flush(); }
"""
        + job_tracker_script()
        + "\nvar scenario = "
        + json.dumps(scenario)
        + ";\n"
        + r"""
(async () => {
  if (scenario === 'no_socket') window.PLUGIN_API.libs.io = null;
  if (['already_done','no_socket','start_race','unsubscribe'].includes(scenario)) {
    jobs=[{id:'a',status:'completed'}];
  }
  if (scenario === 'retry') fail=true;
  var off;
  if (scenario === 'start_race') await PluginJobs.run('trainer',{},handlers);
  else off=PluginJobs.track('/trainer','a',handlers);
  if (scenario === 'coalesced') PluginJobs.track('/trainer','b',handlers);
  if (scenario === 'unsubscribe') off();
  await flush();
  if (scenario === 'coalesced') assert.equal(requests,1, 'tracks share an in-flight read');
  if (scenario === 'already_done' || scenario === 'no_socket' || scenario === 'start_race') {
    assert.equal(done.length,1); assert.equal(timers.size,0);
  } else if (scenario === 'unsubscribe') {
    jobs=[{id:'a',status:'completed'}]; await tick();
    socket('http://compute.test/trainer').emit('job_update',jobs[0]);
    assert.equal(done.length,0); assert.equal(timers.size,0);
  } else {
    assert.equal(done.length,0); assert.equal(timers.size,1);
    fail=false;
    var status=scenario === 'cancelled' ? 'cancelled' : scenario === 'failed' ? 'failed' : 'completed';
    jobs=[{id:'a',status}];
    if (scenario === 'coalesced') jobs.push({id:'b',status:'completed'});
    if (scenario === 'reconnect') { socket('http://compute.test/trainer').emit('connect'); await flush(); }
    else await tick();
    assert.equal(done.length,scenario === 'failed' ? 0 : scenario === 'coalesced' ? 2 : 1);
    assert.equal(errors.length,scenario === 'failed' ? 1 : 0);
    assert.equal(timers.size,0);
  }
  var count=updates.length;
  socket('http://compute.test/trainer').emit('job_update',{id:'a',status:'running'});
  socket('http://compute.test/trainer').emit('job_update',{id:'a',status:'completed'});
  assert.equal(updates.length,count,'late events cannot reopen a terminal/unsubscribed tracker');
})().catch(e=>{ console.error(e); process.exitCode=1; });
""",
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(harness)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
