// Copyright 2026 3LC Inc.
// SPDX-License-Identifier: Apache-2.0
// A `fill` page: the Hub sizes the frame, so the page reports no height; links use the Hub's page and keys.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
async function main() {
  const observed = [], attributes = {}, events = {};
  const root = {querySelector: () => null, querySelectorAll: () => [], innerHTML: ''};
  const parent = {postMessage() {}};
  const doc = {documentElement: {setAttribute(name, value) {attributes[name] = value;}, style: {}, scrollHeight: 100},
    body: {}, getElementById: () => root, head: {appendChild: el => el.onload()},
    createElement: () => ({}), addEventListener() {}, removeEventListener() {}};
  const context = {console, URL, URLSearchParams, Headers, Response, FormData, AbortController,
    document: doc, location: {search: '?hub=https://hub.test', href: 'https://compute.test/api/plugin-frame/', pathname: '/api/plugin-frame/'},
    history: {replaceState() {}}, parent, addEventListener: (name, fn) => events[name] = fn,
    ResizeObserver: class {observe(el) {observed.push(el);} disconnect() {}},
    setTimeout: () => 0, clearTimeout() {}};
  context.window = context; vm.createContext(context); vm.runInContext(source, context);
  const posted = [];
  const port = {close() {}, postMessage(message) {
    posted.push(message);
    if (message.type === 'call' && message.method === 'storage.choose') {
      queueMicrotask(() => port.onmessage({data: {v: 1, type: 'reply', id: message.id, ok: true, value: 'c-2'}}));
    }
  }};
  events.message({data: {v: 1, type: 'init', pluginId: 'one', layout: 'fill', html: 'hello',
    page: 'https://hub.test/plugin/one', hubQuery: {compute_service: 'https://c.test'},
    config: {compute_service_url: 'https://compute.test'}, storage: {aws: 'c-1'}},
    source: parent, origin: 'https://hub.test', ports: [port]});
  await new Promise(setImmediate);
  const API = context.PLUGIN_API;
  assert.equal(API.layout.mode, 'fill');
  assert.equal(attributes['data-tlc-layout'], 'fill', 'the page can style itself for fill');
  assert.equal(observed.length, 0, 'a fill page reports no height');
  assert.equal(API.nav.href({search: 'view=reviews'}),
    'https://hub.test/plugin/one?view=reviews&compute_service=https%3A%2F%2Fc.test', "the Hub's keys stay");
  assert.equal(API.storage.remembered('aws'), 'c-1', 'the remembered storage Connection comes with init');
  assert.equal(await API.storage.choose('aws', {ask: 'always'}), 'c-2');
  assert.equal(JSON.stringify(posted.filter(m => m.method === 'storage.choose').map(m => m.args)), JSON.stringify([['aws', {ask: 'always'}]]));
  assert.equal(API.storage.remembered('aws'), 'c-2');
  assert.equal(API.has('http.request'), true, 'a page asks by the names it calls');

  // A Hub on another contract is told, and the page does not start.
  const root2 = {querySelector: () => null, querySelectorAll: () => [], innerHTML: '', textContent: ''};
  const ctx2 = {...context, document: {...doc, getElementById: () => root2}, console: {...console, error() {}}};
  delete ctx2.PLUGIN_API; delete ctx2.PluginJobs;
  const events2 = {};
  ctx2.addEventListener = (name, fn) => events2[name] = fn; ctx2.window = ctx2;
  vm.createContext(ctx2); vm.runInContext(source, ctx2);
  events2.message({data: {v: 1, type: 'init', pluginId: 'one', html: 'hello', contractVersion: '0.6',
    config: {compute_service_url: 'https://compute.test'}}, source: parent, origin: 'https://hub.test', ports: [{close() {}, postMessage() {}}]});
  await new Promise(setImmediate);
  assert.equal(ctx2.PLUGIN_API, undefined, 'no API on a mismatched contract');
  assert.match(root2.textContent, /contract 0\.6 .*Update the Hub/);
  console.log('fill layout, nav.href, storage and contract check passed');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
