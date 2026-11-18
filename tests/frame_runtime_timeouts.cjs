// Copyright 2026 3LC Inc.
// SPDX-License-Identifier: Apache-2.0
// How long the frame waits on the Hub for a compute request: the Hub's own defaults when the page names no
// timeout (120 s for a plugin or infrastructure route, 10 s otherwise), plus the bridge's 5 s margin.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(process.argv[2], 'utf8');

async function main() {
  const events = {}, timers = [];
  const root = {querySelector: () => null, querySelectorAll: () => [], innerHTML: ''};
  const parent = {postMessage() {}};
  const doc = {documentElement: {setAttribute() {}, style: {}, scrollHeight: 100},
    body: {}, getElementById: () => root, head: {appendChild: el => el.onload(), insertBefore() {}},
    createElement: () => ({}), addEventListener() {}, removeEventListener() {}};
  const context = {console, URL, URLSearchParams, Headers, Response, FormData, AbortController, document: doc,
    location: {search: '?hub=https://hub.test', href: 'https://compute.test/api/plugin-frame/', pathname: '/api/plugin-frame/'},
    history: {replaceState() {}}, parent, addEventListener: (name, fn) => events[name] = fn,
    ResizeObserver: class {observe() {} disconnect() {}},
    setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; }, clearTimeout() {}};
  context.window = context; vm.createContext(context); vm.runInContext(source, context);

  // The Hub never answers a compute request here, so each one leaves its timer armed.
  const sent = [];
  const port = {close() {}, postMessage(message) {
    if (message.type !== 'call') return;
    if (message.method === 'compute.request') { sent.push(message); return; }
    queueMicrotask(() => port.onmessage({data: {v: 1, type: 'reply', id: message.id, ok: true, value: {}}}));
  }};
  events.message({data: {v: 1, type: 'init', pluginId: 'yolo', html: '', config: {compute_service_url: 'https://compute.test'}},
    source: parent, origin: 'https://hub.test', ports: [port]});
  await new Promise(setImmediate);
  const API = context.PLUGIN_API;
  assert.ok(API, 'the page started');

  function waitFor(start) {
    const before = timers.length;
    start().catch(() => {});
    const armed = timers.slice(before);
    assert.equal(armed.length, 1, 'one bridge timer per request');
    return armed[0].ms;
  }

  // A job start: PluginJobs.start names no timeout, and the compute may plan the run before it answers.
  assert.equal(waitFor(() => context.PluginJobs.start('yolo', {})), 125000);
  assert.match(sent.at(-1).args[0], /\/api\/plugins\/yolo\/run$/);
  assert.equal(waitFor(() => API.http.request('/api/plugins/yolo/tasks')), 125000);
  assert.equal(waitFor(() => API.authFetch('/api/infra/nodes', {method: 'POST', body: '{}'})), 125000);
  // Any other route keeps the short wait.
  assert.equal(waitFor(() => API.http.request('/health')), 15000);
  // A timeout the page names wins, on any route, and the bridge never waits past the Hub's longest.
  assert.equal(waitFor(() => API.http.request('/api/plugins/yolo/tasks', {timeout: 3000})), 8000);
  assert.equal(waitFor(() => API.http.request('/health', {timeout: 60000})), 65000);
  assert.equal(waitFor(() => API.http.request('/api/plugins/yolo/tasks', {timeout: 600000})), 125000);

  // When the wait runs out, the page is told the Hub did not answer.
  const late = API.http.request('/api/plugins/yolo/run', {method: 'POST', body: '{}'});
  timers.at(-1).fn();
  await assert.rejects(late, error => error.code === 'timeout' && /did not answer compute\.request/.test(error.message));
  console.log('request timeouts passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
