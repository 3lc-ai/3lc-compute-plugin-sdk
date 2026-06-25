# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared client helper: subscribe a plugin's own UI to the generic job channel.

Every host/venv job already broadcasts a generic ``job_update`` SocketIO event on
its namespace, carrying the schema the frontend renders without plugin knowledge
(``status``, ``progress.{percent,label,timing}``, ``run_url``, ``metrics[]``). A
plugin's *own* ``ui.html`` can be a **second consumer** of that same channel to
drive a richer, plugin-tailored view — no bespoke events needed for the generic
lifecycle (a plugin reaches for ``ctx.emit`` only for telemetry the generic
schema can't express, e.g. a loss curve).

This module returns that subscription client as an injectable ``<script>`` block,
mirroring :func:`tlc_plugin_sdk.shared.alias_ui.alias_ui_script`. After injecting it
a plugin UI gets a ``window.PluginJobs`` object::

    PluginJobs.run(pluginId, params, {onUpdate, onDone, onError})  // start + track
    PluginJobs.start(pluginId, params)                              // -> Promise<{job_id,status,namespace}>
    PluginJobs.track(namespace, jobId, {onUpdate, onDone, onError}) // -> unsubscribe()
    PluginJobs.cancel(jobId)                                        // -> Promise<{cancelled}>
    PluginJobs.on(namespace, event, handler)                        // -> unsubscribe(); custom ctx.emit() events

``run`` pre-subscribes and buffers so a job that finishes between the ``/run``
response and the client subscribing still delivers its terminal event.
"""

from __future__ import annotations

# fmt: off
JOB_TRACKER_JS = r"""
// ── Shared plugin job tracker (generic job_update channel) ──────────────
(function(){
  if (window.PluginJobs) { return; }
  var API = window.PLUGIN_API;
  function _computeUrl(){ return (API && API.getConfig && API.getConfig('compute_service_url')) || ''; }

  // One socket per namespace, reused across jobs.
  var _sockets = {};
  function _socket(namespace){
    if (_sockets[namespace]) { return _sockets[namespace]; }
    var io = API && API.libs && API.libs.io;
    if (!io) { return null; }
    var s = io(_computeUrl() + namespace, {
      path: '/socket.io/', transports: ['websocket', 'polling'], reconnection: true
    });
    _sockets[namespace] = s;
    return s;
  }

  function start(pluginId, params){
    params = params || {};
    // Attribute the job to the launch project so it shows in that project's generic
    // Queue & Progress pane — the panel polls /api/plugins/jobs?project=<name> and the
    // host filters by the record's project_name. Default it from the plugin's launch
    // context (empty when launched bare from the sidebar — such jobs only appear on the
    // global Queue page). An explicit params.project_name always wins.
    var ctxProject = (API && API.context && API.context.projectName) || '';
    if (ctxProject && !params.project_name){
      var merged = {}; for (var k in params){ if (params.hasOwnProperty(k)) { merged[k] = params[k]; } }
      merged.project_name = ctxProject;
      params = merged;
    }
    return API.authFetch(_computeUrl() + '/api/plugins/' + pluginId + '/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params)
    }).then(function(r){ return r.json(); });
  }

  function cancel(jobId){
    return API.authFetch(_computeUrl() + '/api/plugins/jobs/' + jobId + '/cancel', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
    }).then(function(r){ return r.json(); });
  }

  // Subscribe to a plugin's own custom ctx.emit() event on its namespace.
  // Returns an unsubscribe function. Use for rich per-job detail the flat generic
  // job_update schema can't carry (a result payload, a loss curve, per-item rows).
  // Subscribe BEFORE calling run() so the socket is connected when the event fires.
  function on(namespace, event, handler){
    var s = _socket(namespace);
    if (!s) { return function(){}; }
    s.on(event, handler);
    return function(){ s.off(event, handler); };
  }

  // Subscribe to job_update for one job on a known namespace. Returns unsubscribe().
  function track(namespace, jobId, handlers){
    handlers = handlers || {};
    var s = _socket(namespace);
    if (!s) { return function(){}; }
    var done = false;
    function off(){ s.off('job_update', onUpd); }
    function onUpd(job){
      if (!job || job.id !== jobId) { return; }
      if (handlers.onUpdate) { handlers.onUpdate(job); }
      if (done) { return; }
      if (job.status === 'completed' || job.status === 'cancelled'){
        done = true; off(); if (handlers.onDone) { handlers.onDone(job); }
      } else if (job.status === 'failed'){
        done = true; off(); if (handlers.onError) { handlers.onError(job); }
      }
    }
    s.on('job_update', onUpd);
    return off;
  }

  // Start a job and track it. Pre-subscribes on the default namespace and buffers
  // events so a job that completes before we learn its id still delivers onDone.
  function run(pluginId, params, handlers){
    handlers = handlers || {};
    var ns = '/' + pluginId;          // default channel; corrected from resp.namespace
    var s = _socket(ns);
    var jobId = null;
    var done = false;
    var buffer = [];

    function cleanup(){ if (s) { s.off('job_update', onUpd); } }
    function dispatch(job){
      if (handlers.onUpdate) { handlers.onUpdate(job); }
      if (done) { return; }
      if (job.status === 'completed' || job.status === 'cancelled'){
        done = true; cleanup(); if (handlers.onDone) { handlers.onDone(job); }
      } else if (job.status === 'failed'){
        done = true; cleanup(); if (handlers.onError) { handlers.onError(job); }
      }
    }
    function onUpd(job){
      if (!job) { return; }
      if (jobId === null) { buffer.push(job); return; }   // id unknown yet -> buffer
      if (job.id !== jobId) { return; }
      dispatch(job);
    }
    if (s) { s.on('job_update', onUpd); }

    return start(pluginId, params).then(function(resp){
      if (!resp || resp.error || !resp.job_id){
        cleanup();
        var emsg = (resp && resp.error) || 'Failed to start job';
        if (handlers.onError) { handlers.onError({ status: 'failed', subtitle: emsg }); }
        return resp;
      }
      jobId = resp.job_id;
      // If the job actually runs on a custom namespace, move the listener there.
      if (resp.namespace && resp.namespace !== ns){
        cleanup();
        s = _socket(resp.namespace);
        if (s) { s.on('job_update', onUpd); }
      }
      // Replay anything buffered for this job (may include the terminal event).
      var pending = buffer.filter(function(j){ return j.id === jobId; });
      buffer = [];
      pending.forEach(dispatch);
      return resp;
    });
  }

  window.PluginJobs = { start: start, track: track, run: run, cancel: cancel, on: on };
})();
"""
# fmt: on


def job_tracker_script() -> str:
    """Return the shared plugin-job tracker JavaScript block.

    Inject once into a plugin's ``ui.html`` (inside the opening ``<script>``),
    then drive the generic lifecycle via ``window.PluginJobs`` — see the module
    docstring for the API.

    Returns:
        The ``PluginJobs`` client as a self-installing JS string.

    """
    return JOB_TRACKER_JS
