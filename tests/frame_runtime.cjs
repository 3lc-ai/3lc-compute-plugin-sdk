// Copyright 2026 3LC Inc.
// SPDX-License-Identifier: Apache-2.0
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
async function main() {
  let nextTimer = 0, disconnected = 0, observerClosed = 0, apiCalls = [], ready = [], addresses = [];
  const timers = new Map(), events = {}, sockets = [], root = {
    querySelector: () => null, querySelectorAll: () => [], innerHTML: '', style: {}
  };
  const parent = {postMessage: (...args) => ready.push(args)};
  const doc = {documentElement: {setAttribute() {}, style: {}, scrollHeight: 100},
    body: {}, getElementById: () => root, head: {appendChild: el => el.onload(), insertBefore() {}},
    createElement: () => ({}), addEventListener() {}, removeEventListener() {}};
  const context = {console, URL, URLSearchParams, Headers, Response, FormData, AbortController,
    document: doc, location: {search: '?hub=https://hub.test', href: 'https://compute.test/api/plugin-frame/', pathname: '/api/plugin-frame/'},
    history: {replaceState(state, title, url) {addresses.push(url);}}, parent, addEventListener: (name, fn) => events[name] = fn,
    ResizeObserver: class {observe() {} disconnect() {observerClosed++;}},
    setTimeout: (fn, ms) => {timers.set(++nextTimer, {fn,ms}); return nextTimer;}, clearTimeout: id => timers.delete(id),
    io: () => {const listeners = {}; const socket = {
      connected: true, active: true, id: 'viewer',
      on(name, fn) {(listeners[name] ||= []).push(fn);},
      off(name, fn) {listeners[name] = (listeners[name] || []).filter(x => x !== fn);},
      onevent(packet) {(listeners[packet.data[0]] || []).slice().forEach(fn => fn(packet.data[1]));},
      disconnect() {disconnected++; this.connected = false; return this;},
      timeout() {return this;}, emit() {}, connect() {}, listeners
    }; sockets.push(socket); return socket;}
  };
  context.window = context; vm.createContext(context); vm.runInContext(source, context);
  assert.equal(ready.length, 1); assert.equal(ready[0][1], 'https://hub.test');
  const port = {closed: false, close() {this.closed = true;}, postMessage(message) {
    apiCalls.push(message);
    if (message.type !== 'call') return;
    let value;
    if (message.method === 'data.snapshot') value = {};
    else if (message.method === 'jobs.cancel') value = {cancelled: true};
    else if (message.method === 'compute.request' && message.args[0].endsWith('/run')) {
      sockets[0].onevent({data: ['job_update', {id:'early', status:'completed'}]});
      value = {status:200, body:JSON.stringify({job_id:'early', namespace:'/one'}), headers:{}};
    } else return;
    queueMicrotask(() => this.onmessage({data:{v:1,type:'reply',id:message.id,ok:true,value}}));
  }};
  const init = {data: {v:1,type:'init',pluginId:'one',config:{compute_service_url:'https://compute.test'},html:'hello'},
    source:parent,origin:'https://hub.test',ports:[port]};
  events.message({...init, origin:'https://evil.test'}); assert(!context.PLUGIN_API);
  events.message({...init, source:{}}); assert(!context.PLUGIN_API);
  events.message({...init, data:{...init.data,v:2}}); assert(!context.PLUGIN_API);
  events.message(init); await new Promise(setImmediate);
  const API = context.PLUGIN_API;
  assert.equal(API.protocol, 1); assert(!API.frameVersion.includes('__'));
  assert.equal(typeof API.jobs.run, 'function');
  assert.throws(() => API.libs.io('https://evil.test/one'));
  let updates = 0, done = 0;
  await API.jobs.run('one', {}, {onUpdate: () => updates++, onDone: () => done++});
  assert.equal(updates, 1); assert.equal(done, 1);
  // Legacy helper cancellation cancels at once, exactly once (the dialog is jobs.confirmCancel).
  await context.PluginJobs.cancel('early');
  assert.equal(apiCalls.filter(x => x.method === 'jobs.cancel').length, 1);
  let nav = 0; const off = API.nav.onChange(() => nav++);
  port.onmessage({data:{v:1,type:'event',name:'nav',value:{}}}); off();
  port.onmessage({data:{v:1,type:'event',name:'nav',value:{}}}); assert.equal(nav, 1);
  // Back/Forward in the Hub keeps the frame's own address in step, so the page reads the current query.
  port.onmessage({data:{v:1,type:'event',name:'nav',value:{search:'view=reviews',hash:'top'}}});
  assert.equal(addresses.at(-1), '/api/plugin-frame/?hub=https%3A%2F%2Fhub.test&view=reviews#top');
  // A shareable link is the Hub's page of this plugin, not the frame's address.
  assert.equal(API.nav.href({search:'?review=r1'}), 'https://hub.test/plugin/one?review=r1');
  assert.equal(API.nav.href(), 'https://hub.test/plugin/one');
  assert.equal(API.layout.mode, 'flow');
  const abort = new AbortController();
  const request = API.http.request('/api/plugins/one/slow', {signal:abort.signal});
  abort.abort(); await assert.rejects(request, error => error.code === 'aborted');
  const before = apiCalls.length;
  await assert.rejects(API.http.request('/never', {signal:abort.signal}), error => error.code === 'aborted');
  assert.equal(apiCalls.length, before);
  const pending = API.http.request('/api/plugins/one/still-running');
  const rejected = assert.rejects(pending, error => error.code === 'closed');
  let disposed = 0; API.lifecycle.onDispose(() => disposed++);
  if (process.argv[3] === 'host-close') port.onmessage({data:{v:1,type:'event',name:'closed'}});
  else events.pagehide();
  await rejected;
  assert.equal(disposed, 1); assert.equal(disconnected, 1); assert.equal(observerClosed, 1);
  assert.equal(port.closed, true); assert.equal(timers.size, 0);
  assert.equal(apiCalls.filter(x => x.method === 'jobs.cancel').length, 1, 'teardown never cancels jobs');
  events.pagehide(); events.message(init); await new Promise(setImmediate);
  assert.equal(disposed, 1); assert.equal(disconnected, 1);
  assert.throws(() => API.libs.io('https://compute.test/one'), error => error.code === 'closed');
  await assert.rejects(API.http.request('/closed'), error => error.code === 'closed');
  console.log('handshake, early completion, trusted cancellation, abort, unsubscribe, disposal and job preservation passed');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
