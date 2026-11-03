// Copyright 2026 3LC Inc.
// SPDX-License-Identifier: Apache-2.0
// The Hub's own stylesheets and libraries arrive as text, so the frame fetches nothing from the Hub's address.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
async function main() {
  const appended = [], events = {}, ready = [];
  const root = {querySelector: () => null, querySelectorAll: () => [], innerHTML: ''};
  const parent = {postMessage(message, origin) { ready.push([message, origin]); }};
  let context;
  const doc = {documentElement: {setAttribute() {}, style: {}, scrollHeight: 100},
    body: {}, getElementById: () => root,
    head: {appendChild(el) {
      appended.push(el);
      // An inline script runs as it is inserted; a linked file loads.
      if (el.tag === 'script' && el.text) vm.runInContext(el.text, context);
      else if (el.onload) el.onload();
    }},
    createElement: tag => ({tag}), addEventListener() {}, removeEventListener() {}};
  context = {console, URL, URLSearchParams, Headers, Response, FormData, AbortController,
    document: doc, location: {search: '?hub=https://hub.test', href: 'https://compute.test/api/plugin-frame/', pathname: '/api/plugin-frame/'},
    history: {replaceState() {}}, parent, addEventListener: (name, fn) => events[name] = fn,
    ResizeObserver: class {observe() {} disconnect() {}}, setTimeout: () => 0, clearTimeout() {}};
  context.window = context; vm.createContext(context); vm.runInContext(source, context);

  assert.equal(ready.length, 1);
  assert.equal(ready[0][0].type, 'ready');
  assert.equal(ready[0][0].inlineAssets, 1, 'the frame says it takes the Hub files as text');
  assert.equal(ready[0][1], 'https://hub.test');

  const port = {close() {}, postMessage() {}};
  events.message({data: {v: 1, type: 'init', pluginId: 'one', html: 'hello',
    config: {compute_service_url: 'https://compute.test'},
    styles: [{css: '.hub{color:red}'}, 'https://fonts.test/css2?family=Sans'],
    scripts: [{js: 'window.order = ["first"];'}, {js: 'window.order.push("second");'}]},
    source: parent, origin: 'https://hub.test', ports: [port]});
  await new Promise(setImmediate);

  const style = appended.find(el => el.tag === 'style');
  assert.equal(style.textContent, '.hub{color:red}', 'a Hub stylesheet is a <style> with its text');
  const link = appended.find(el => el.tag === 'link');
  assert.equal(link.href, 'https://fonts.test/css2?family=Sans', 'another origin stays a link');
  assert.equal(appended.filter(el => el.tag === 'script' && el.src).length, 0, 'no Hub library is fetched');
  assert.deepEqual([...context.order], ['first', 'second'], 'libraries run in order');
  assert.ok(context.PLUGIN_API, 'the page starts');
  console.log('inline Hub assets passed');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
