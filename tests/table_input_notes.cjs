// Copyright 2026 3LC Inc.
// SPDX-License-Identifier: Apache-2.0
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/tlc_plugin_sdk/contract/frame-runtime.js'), 'utf8');
const code = source.slice(source.indexOf('  function watchTableInputs('), source.indexOf('  // ── PLUGIN_API'));
const flush = () => new Promise(setImmediate);
const warning = {ok: true, notes: ['Copy image data before training.']};
const ready = {ok: true, notes: []};
function page() {
  const pending = [], timers = new Map(), events = {};
  let base = 'https://compute.test', target = {target: 'node', node_id: 'one'}, tick = 0, dispose, onTarget;
  const note = {parentNode: {}, style: {display: 'none'}, textContent: ''};
  const field = {value: 's3://bucket/projects/Fire/datasets/train/tables/initial', mode: '',
    isConnected: true, _tlcRunTargetNote: note, getAttribute() {return this.mode;}, matches() {return true;}};
  const api = {container: {querySelectorAll: () => [field], contains: el => el === field},
    getConfig: () => base, getRunTarget: () => target,
    onRunTargetChange(fn) {onTarget = fn; return () => {onTarget = null;};},
    lifecycle: {onDispose(fn) {dispose = fn;}},
    checkDataForRunTarget: url => new Promise((resolve, reject) => pending.push({url, resolve, reject}))};
  const context = vm.createContext({document: {
    addEventListener: (type, fn) => events[type] = fn,
    removeEventListener: type => delete events[type],
  }, setTimeout: fn => {timers.set(++tick, fn); return tick;}, clearTimeout: id => timers.delete(id)});
  vm.runInContext(code, context); context.watchTableInputs(api);
  return {field, note, pending, events, timers, dispose: () => dispose(),
    poll() {const [id, fn] = timers.entries().next().value; timers.delete(id); fn();},
    target(value) {target = value; onTarget();}, base(value) {base = value;}};
}
test('a new preset clears the previous warning and rejects a late reply before the next poll', async () => {
  const p = page(); p.pending[0].resolve(warning); await flush();
  assert.equal(p.note.style.display, '');
  p.field.value = '/local/projects/Fire/datasets/train/tables/initial'; p.poll();
  assert.equal(p.note.style.display, 'none');
  p.field.value = '/local/projects/Fire/datasets/train/tables/edited';
  p.pending[1].resolve(warning); await flush();
  assert.equal(p.note.style.display, 'none');
  p.poll(); p.pending[2].resolve(ready); await flush();
  assert.equal(p.note.style.display, 'none'); p.dispose();
});
test('unchanged fields are deduplicated and clearing or opting out hides their warning', async () => {
  const p = page(); p.poll(); assert.equal(p.pending.length, 1);
  p.pending[0].resolve(warning); await flush();
  p.field.value = ''; p.events.input({type: 'input', target: p.field});
  assert.equal(p.note.style.display, 'none'); p.poll(); assert.equal(p.pending.length, 1);
  p.field.value = '/local/projects/Fire/datasets/train/tables/initial'; p.field.mode = 'off'; p.poll();
  assert.equal(p.pending.length, 1); p.dispose();
});
test('target, deployment and detached-field replies cannot restore old warnings', async () => {
  for (const change of ['target', 'deployment', 'detached']) {
    const p = page();
    if (change === 'target') p.target({target: 'local'});
    if (change === 'deployment') p.base('https://another.test');
    if (change === 'detached') p.field.isConnected = false;
    p.pending[0].resolve(warning); await flush();
    assert.equal(p.note.style.display, 'none'); p.dispose();
  }
});
test('a failed check explains uncertainty and disposal removes polling and ignores pending replies', async () => {
  const p = page(); p.pending[0].reject(new Error('Check timed out')); await flush();
  assert.match(p.note.textContent, /Data access has not been verified.*Check timed out/);
  p.field.value = '/local/projects/Fire/datasets/train/tables/initial'; p.poll();
  p.dispose(); p.pending[1].resolve(warning); await flush();
  assert.equal(p.note.style.display, 'none'); assert.equal(p.timers.size, 0);
  assert.deepEqual(Object.keys(p.events), []);
});
