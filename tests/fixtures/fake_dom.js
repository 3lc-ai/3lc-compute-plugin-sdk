// Copyright 2026 3LC Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Just enough DOM for the shared widgets to run in node: elements by id, style, dataset, classList,
// listeners and a text-escaping innerHTML for `_esc`. A test registers the elements its widget renders
// (`addEl(id)`), stubs `window.PLUGIN_API`, runs the widget and inspects what it wrote. HTML set through
// innerHTML is kept as a string and never parsed, so querySelector finds nothing.

function FakeEl(id) {
  this.id = id;
  this.style = {};
  this.dataset = {};
  this.value = '';
  this.checked = false;
  this.innerHTML = '';
  this._text = '';
  this._listeners = {};
  this._attrs = {};
  this.options = [];
  var classes = {};
  this.classList = {
    add: function (c) { classes[c] = true; },
    remove: function (c) { delete classes[c]; },
    contains: function (c) { return !!classes[c]; },
    toggle: function (c, on) {
      var want = on === undefined ? !classes[c] : !!on;
      if (want) classes[c] = true; else delete classes[c];
      return want;
    },
  };
}
Object.defineProperty(FakeEl.prototype, 'textContent', {
  get: function () { return this._text; },
  set: function (v) {
    this._text = String(v);
    this.innerHTML = this._text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  },
});
FakeEl.prototype.addEventListener = function (type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); };
FakeEl.prototype.dispatchEvent = function (ev) {
  (this._listeners[ev.type] || []).forEach(function (fn) { fn(ev); });
  return true;
};
FakeEl.prototype.click = function () { this.dispatchEvent(new Event('click')); };
FakeEl.prototype.querySelector = function () { return null; };
FakeEl.prototype.querySelectorAll = function () { return []; };
FakeEl.prototype.closest = function () { return null; };
FakeEl.prototype.contains = function () { return true; };
FakeEl.prototype.appendChild = function (el) { this.options.push(el); return el; };
FakeEl.prototype.setAttribute = function (k, v) { this._attrs[k] = String(v); };
FakeEl.prototype.getAttribute = function (k) { return k in this._attrs ? this._attrs[k] : null; };

function Event(type) { this.type = type; }

var _els = {};
function addEl(id) { _els[id] = new FakeEl(id); return _els[id]; }
var document = {
  head: { appendChild: function () {} },
  body: new FakeEl('body'),
  getElementById: function (id) { return _els[id] || null; },
  createElement: function () { return new FakeEl(''); },
  addEventListener: function () {},
};
var window = { localStorage: { getItem: function () { return null; }, setItem: function () {} } };

// A PLUGIN_API whose authFetch answers from `routes` ({url substring: JSON body}) and records each URL.
// The first matching substring wins; an unmatched URL answers 404.
function fakeApi(routes, extra) {
  var api = { fetched: [], listeners: [] };
  api.authFetch = function (url) {
    api.fetched.push(url);
    var keys = Object.keys(routes || {});
    for (var i = 0; i < keys.length; i++) {
      if (url.indexOf(keys[i]) !== -1) {
        var body = routes[keys[i]];
        return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(body); } });
      }
    }
    return Promise.reject(new Error('HTTP 404'));
  };
  api.getConfig = function (k) { return (extra && extra.config && extra.config[k]) || ''; };
  Object.keys(extra || {}).forEach(function (k) { if (k !== 'config') api[k] = extra[k]; });
  return api;
}

function tick() { return new Promise(function (resolve) { setTimeout(resolve, 0); }); }
