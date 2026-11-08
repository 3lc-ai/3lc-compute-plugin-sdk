// Copyright 2026 3LC Inc.
// SPDX-License-Identifier: Apache-2.0
// A link written as a Hub path goes to the Hub (nav.go, or nav.open for a new tab), not to the compute
// service the frame is served from; links meant for the compute service, or elsewhere, are left alone.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(process.argv[2], 'utf8');

async function main() {
  const events = {}, docListeners = {};
  const root = {querySelector: () => null, querySelectorAll: () => [], innerHTML: ''};
  const parent = {postMessage() {}};
  const doc = {documentElement: {setAttribute() {}, style: {}, scrollHeight: 100},
    body: {}, getElementById: () => root, head: {appendChild: el => el.onload(), insertBefore() {}},
    createElement: () => ({}),
    addEventListener(name, fn) { (docListeners[name] ||= []).push(fn); },
    removeEventListener(name, fn) { docListeners[name] = (docListeners[name] || []).filter(f => f !== fn); }};
  const errors = [];
  const context = {console: {...console, error: (...a) => errors.push(a.join(' '))}, URL, URLSearchParams, Headers,
    Response, FormData, AbortController, document: doc,
    location: {search: '?hub=https://hub.test', href: 'https://compute.test/api/plugin-frame/', pathname: '/api/plugin-frame/'},
    history: {replaceState() {}}, parent, addEventListener: (name, fn) => events[name] = fn,
    ResizeObserver: class {observe() {} disconnect() {}}, setTimeout: () => 0, clearTimeout() {}};
  context.window = context; vm.createContext(context); vm.runInContext(source, context);

  const calls = [];
  const port = {close() {}, postMessage(message) {
    if (message.type !== 'call') return;
    if (String(message.method).startsWith('nav.')) calls.push([message.method, message.args]);
    queueMicrotask(() => port.onmessage({data: {v: 1, type: 'reply', id: message.id, ok: true, value: null}}));
  }};
  events.message({data: {v: 1, type: 'init', pluginId: 'importer', layout: 'fill', html: '',
    config: {compute_service_url: 'https://compute.test'}}, source: parent, origin: 'https://hub.test', ports: [port]});
  await new Promise(setImmediate);
  assert.ok(context.PLUGIN_API, 'the page started');

  function link(href, attrs = {}) {
    const all = {href, ...attrs};
    const a = {getAttribute: n => (n in all ? all[n] : null), hasAttribute: n => n in all};
    a.closest = sel => (sel === 'a[href]' ? a : null);
    return a;
  }
  function fire(type, target, extra = {}) {
    const event = {type, target, button: type === 'auxclick' ? 1 : 0, defaultPrevented: false, ...extra,
      preventDefault() { this.defaultPrevented = true; }};
    (docListeners[type] || []).forEach(fn => fn(event));
    return event;
  }
  async function settle() { await new Promise(setImmediate); }

  // The importer's "Open in Project": the Hub navigates, and the frame does not follow its own href.
  const project = '/projects/WEEDS?dataset=all-data&table=s3%3A%2F%2Fb%2Ft#datasets';
  let e = fire('click', link(project));
  await settle();
  assert.equal(e.defaultPrevented, true, 'the frame does not navigate itself to the compute service');
  assert.equal(JSON.stringify(calls.pop()), JSON.stringify(['nav.go', [project]]));

  // A click on content inside the link (an icon, a span) counts as a click on the link.
  const inner = link('/projects/p');
  e = fire('click', {closest: sel => inner.closest(sel)});
  await settle();
  assert.equal(JSON.stringify(calls.pop()), JSON.stringify(['nav.go', ['/projects/p']]));

  // A new tab — target=_blank, Ctrl/Cmd/Shift-click, a middle click — opens the Hub page in a new tab.
  for (const [type, target, extra] of [
    ['click', link('/projects/a', {target: '_blank'}), {}],
    ['click', link('/projects/a'), {ctrlKey: true}],
    ['click', link('/projects/a'), {metaKey: true}],
    ['click', link('/projects/a'), {shiftKey: true}],
    ['auxclick', link('/projects/a'), {}],
  ]) {
    e = fire(type, target, extra);
    await settle();
    assert.equal(e.defaultPrevented, true);
    assert.equal(JSON.stringify(calls.pop()), JSON.stringify(['nav.open', ['https://hub.test/projects/a']]), type + ' ' + JSON.stringify(extra));
  }

  // Left alone: links meant for the compute service or elsewhere, fragments, and clicks the page handled.
  for (const [what, target, extra] of [
    ['an absolute link', link('https://docs.3lc.ai/x'), {}],
    ['a protocol-relative link', link('//evil.test/x'), {}],
    ['a backslash path', link('/\\evil.test/x'), {}],
    ['a compute API path', link('/api/plugins/importer/file?x=1'), {}],
    ['the bare compute API path', link('/api'), {}],
    ['a download link', link('/projects/a/export.zip', {download: ''}), {}],
    ['a fragment', link('#section'), {}],
    ['a relative path', link('details'), {}],
    ['a click the page already cancelled', link('/projects/a'), {defaultPrevented: true}],
    ['a right click', link('/projects/a'), {button: 2}],
    ['a click outside any link', {closest: () => null}, {}],
    ['a click with no element', null, {}],
  ]) {
    e = fire('click', target, extra);
    await settle();
    assert.equal(calls.length, 0, what + " is not routed: " + JSON.stringify(calls));
    if (!extra.defaultPrevented) assert.equal(e.defaultPrevented, false, what + ' keeps its own behaviour');
  }

  // A refusal from the Hub is reported, not thrown into the page.
  port.postMessage = message => {
    if (message.type === 'call') queueMicrotask(() => port.onmessage({data: {v: 1, type: 'reply', id: message.id, ok: false,
      error: {code: 'refused', message: 'A plugin page may only navigate within the Hub.'}}}));
  };
  fire('click', link('/projects/a'));
  await settle(); await settle();
  assert.equal(errors.length, 1);
  assert.match(errors[0], /did not open \/projects\/a/);

  // Closing the page removes the listeners.
  events.pagehide();
  assert.equal((docListeners.click || []).length, 0);
  assert.equal((docListeners.auxclick || []).length, 0);
  console.log('hub links passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
