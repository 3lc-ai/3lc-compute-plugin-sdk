// Copyright 2026 3LC Inc.
// SPDX-License-Identifier: Apache-2.0
// A page's actions: offered to a Hub that has a registry, run from the Hub, and gone with the page.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
function page(capabilities) {
  const events = {};
  const root = {querySelector: () => null, querySelectorAll: () => [], innerHTML: ''};
  const parent = {postMessage() {}};
  const doc = {documentElement: {setAttribute() {}, style: {}, scrollHeight: 100}, body: {}, getElementById: () => root,
    head: {appendChild: el => el.onload()}, createElement: () => ({}), addEventListener() {}, removeEventListener() {}};
  const context = {console: {...console, error() {}}, URL, URLSearchParams, Headers, Response, FormData, AbortController,
    document: doc, location: {search: '?hub=https://hub.test', href: 'https://compute.test/api/plugin-frame/', pathname: '/api/plugin-frame/'},
    history: {replaceState() {}}, parent, addEventListener: (name, fn) => events[name] = fn,
    ResizeObserver: class {observe() {} disconnect() {}}, setTimeout: () => 0, clearTimeout() {}};
  context.window = context; vm.createContext(context); vm.runInContext(source, context);
  const posted = [];
  const port = {close() {}, postMessage(message) {
    posted.push(message);
    if (message.type === 'call') queueMicrotask(() => port.onmessage({data: {v: 1, type: 'reply', id: message.id, ok: true, value: null}}));
  }};
  events.message({data: {v: 1, type: 'init', pluginId: 'one', html: 'hello', capabilities,
    config: {compute_service_url: 'https://compute.test'}}, source: parent, origin: 'https://hub.test', ports: [port]});
  return {context, port, posted, events};
}
async function main() {
  const directoryPage = page(['organization:directory']);
  await new Promise(setImmediate);
  await directoryPage.context.PLUGIN_API.organization.directory();
  assert.equal(directoryPage.posted.at(-1).method, 'organization.directory');
  assert.deepEqual(Array.from(directoryPage.posted.at(-1).args), []);
  const older = page([]);
  await new Promise(setImmediate);
  assert.equal(older.context.PLUGIN_API.actions, undefined, 'no registry on an older Hub: pages feature-detect');

  const {context, port, posted, events} = page(['actions']);
  await new Promise(setImmediate);
  const API = context.PLUGIN_API;
  assert.equal(API.has('actions'), true);
  const seen = [];
  API.actions.register('set_form', {title: 'Set the form', description: 'Fill the fields',
    params: {type: 'object', properties: {epochs: {type: 'integer'}}}, run(params) { seen.push(params); return {set: Object.keys(params)}; }});
  API.actions.register('fail', {title: 'Fail', run() { throw new Error('no such field'); }});
  API.actions.register('quiet', {title: 'Quiet', run() {}});
  assert.throws(() => API.actions.register('bad', {title: 'No run'}), error => error.code === 'invalid');
  const registered = posted.filter(m => m.type === 'call' && m.method === 'actions.register');
  assert.equal(JSON.stringify(registered[0].args), JSON.stringify(['set_form', {title: 'Set the form', description: 'Fill the fields', readOnly: false,
    params: {type: 'object', properties: {epochs: {type: 'integer'}}}}]));
  assert.equal(JSON.stringify(registered[1].args[1]), JSON.stringify({title: 'Fail', description: '', readOnly: false, params: {type: 'object'}}),
    'an action without a schema takes no params');
  assert.equal(registered.length, 3);
  const done = () => posted.filter(m => m.type === 'done');
  port.onmessage({data: {v: 1, type: 'run', id: 7, action: 'set_form', params: {epochs: 3}}});
  port.onmessage({data: {v: 1, type: 'run', id: 8, action: 'fail', params: {}}});
  port.onmessage({data: {v: 1, type: 'run', id: 9, action: 'quiet'}});
  port.onmessage({data: {v: 1, type: 'run', id: 10, action: 'missing', params: {}}});
  await new Promise(setImmediate);
  assert.equal(JSON.stringify(seen), JSON.stringify([{epochs: 3}]));
  const byId = Object.fromEntries(done().map(m => [m.id, m]));
  assert.equal(JSON.stringify(byId[7]), JSON.stringify({v: 1, type: 'done', id: 7, ok: true, value: {set: ['epochs']}}));
  assert.equal(byId[8].ok, false); assert.match(byId[8].error.message, /no such field/);
  assert.equal(JSON.stringify(byId[9].value), JSON.stringify({ok: true}), 'an action that returns nothing says ok');
  assert.equal(byId[10].error.code, 'unknown');
  API.actions.unregister('fail');
  API.actions.unregister('fail');
  assert.equal(posted.filter(m => m.method === 'actions.unregister').length, 1, 'unregistering twice tells the Hub once');
  port.onmessage({data: {v: 1, type: 'run', id: 11, action: 'fail', params: {}}});
  await new Promise(setImmediate);
  assert.equal(done().find(m => m.id === 11).error.code, 'unknown', 'an unregistered action is unknown');
  // Into the back/forward cache and back: the page keeps its channel, so Back does not leave a dead page.
  events.pagehide({persisted: true});
  port.onmessage({data: {v: 1, type: 'run', id: 12, action: 'set_form', params: {}}});
  await new Promise(setImmediate);
  assert.equal(seen.length, 2, 'a page in the back/forward cache keeps its channel');
  assert.equal(done().find(m => m.id === 12).ok, true);
  events.pagehide({persisted: false});
  port.onmessage({data: {v: 1, type: 'run', id: 13, action: 'set_form', params: {}}});
  await new Promise(setImmediate);
  assert.equal(seen.length, 2, 'an unloaded page runs nothing');
  console.log('actions: feature detection, registration, runs, failures, unregister and close passed');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
