// Copyright 2026 3LC Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Runs a plugin page inside the frame the Hub opens on this service's origin. Nothing here holds a
// credential. Protocol 1: the frame says `ready` to the Hub named by ?hub=, the Hub answers `init`
// with a private MessagePort, and every call after that runs on the port:
//   frame -> Hub  {v, type: 'call', id, method, args} | {v, type: 'cancel', id} | {v, type: 'height', value}
//   Hub -> frame  {v, type: 'reply', id, ok, value | error: {code, message, status?}} | {v, type: 'event', name, value}
// Canonical SDK-owned runtime. Compute serves the assembled package asset.
(function () {
  'use strict';

  var PROTOCOL = 1;
  var FRAME_VERSION = '__SDK_FRAME_VERSION__';
  var CALL_TIMEOUT_MS = 30000;
  var DEFAULT_CAPABILITIES = ['compute.request', 'jobs.cancel', 'ui.toast', 'pickers.table', 'credentials.choose',
    'nodes.create', 'run.plan', 'run.checkTable', 'data.snapshot', 'nav.go', 'nav.open', 'sockets.ticket'];

  var hubOrigin = new URLSearchParams(location.search).get('hub') || '';
  var port = null;
  var nextId = 1;
  var pending = {};
  var listeners = { runTarget: [], nav: [] };
  var runTarget = null;
  var warned = {};
  var disposed = false;
  var disposers = [];
  function onDispose(callback) {
    if (disposed) { callback(); return function () {}; }
    disposers.push(callback);
    return function () { disposers = disposers.filter(function (fn) { return fn !== callback; }); };
  }
  function subscribe(name, callback) {
    if (disposed || typeof callback !== 'function') return function () {};
    listeners[name].push(callback);
    return function () { listeners[name] = listeners[name].filter(function (fn) { return fn !== callback; }); };
  }

  function failure(code, message, detail) {
    var error = new Error(message);
    error.code = code;
    if (detail && detail.status) { error.status = detail.status; error.statusText = detail.statusText; }
    return error;
  }

  function call(method, args, timeoutMs, signal) {
    if (signal && signal.aborted) return Promise.reject(failure('aborted', 'The request was aborted.'));
    if (!port) return Promise.reject(failure('closed', 'The Hub is not connected to this page.'));
    return new Promise(function (resolve, reject) {
      var id = nextId++;
      var timer = setTimeout(function () {
        if (!pending[id]) return;
        pending[id].cleanup();
        delete pending[id];
        if (port) port.postMessage({ v: PROTOCOL, type: 'cancel', id: id });
        reject(failure('timeout', 'The Hub did not answer ' + method + ' in time.'));
      }, timeoutMs || CALL_TIMEOUT_MS);
      function cleanup() { if (signal) signal.removeEventListener('abort', abort); }
      function abort() {
        if (!pending[id]) return;
        clearTimeout(timer);
        cleanup();
        delete pending[id];
        if (port) port.postMessage({ v: PROTOCOL, type: 'cancel', id: id });
        reject(failure('aborted', 'The request was aborted.'));
      }
      pending[id] = { resolve: resolve, reject: reject, timer: timer, cleanup: cleanup };
      if (signal) signal.addEventListener('abort', abort, { once: true });
      try {
        port.postMessage({ v: PROTOCOL, type: 'call', id: id, method: method, args: args || [] });
      } catch (error) {
        clearTimeout(timer);
        cleanup();
        delete pending[id];
        reject(error);
      }
    });
  }

  function closePort(reason) {
    if (disposed) return;
    disposed = true;
    disposers.splice(0).forEach(function (fn) { try { fn(); } catch (error) { console.error(error); } });
    listeners = { runTarget: [], nav: [] };
    Object.keys(pending).forEach(function (id) {
      clearTimeout(pending[id].timer);
      pending[id].cleanup();
      pending[id].reject(failure('closed', reason));
    });
    pending = {};
    if (port) port.close();
    port = null;
  }

  function deprecated(name, replacement) {
    if (warned[name]) return;
    warned[name] = true;
    console.warn('[3LC] ' + name + ' in plugin pages is deprecated; use ' + replacement + '.');
  }

  // ── Requests: the Hub fetches on the page's own compute and returns the response ──

  function sendableOptions(options) {
    var out = {};
    ['method', 'body', 'timeout', 'allowErrorStatus', 'cache'].forEach(function (key) {
      if (options && options[key] !== undefined) out[key] = options[key];
    });
    var headers = options && options.headers;
    if (headers) {
      out.headers = {};
      if (headers instanceof Headers) headers.forEach(function (value, name) { out.headers[name] = value; });
      else Object.keys(headers).forEach(function (name) { out.headers[name] = String(headers[name]); });
    }
    var body = out.body;
    if (body instanceof FormData) {
      var entries = [];
      body.forEach(function (value, name) { entries.push([name, value]); });
      out.body = { tlcFormData: entries };
    } else if (body instanceof URLSearchParams) {
      out.headers = Object.assign({ 'Content-Type': 'application/x-www-form-urlencoded' }, out.headers || {});
      out.body = body.toString();
    }
    return out;
  }

  function computeRequest(url, options) {
    var sendable = sendableOptions(options);
    var wait = Math.min((typeof sendable.timeout === 'number' ? sendable.timeout : 10000) + 5000, 125000);
    return call('compute.request', [String(url), sendable], wait, options && options.signal).then(function (reply) {
      var noBody = reply.status === 204 || reply.status === 205 || reply.status === 304;
      return new Response(noBody ? null : reply.body, { status: reply.status, statusText: reply.statusText, headers: reply.headers });
    });
  }

  // ── Helpers that run here, so they stay synchronous ──

  // The Hub's Dashboard link: the given parameters, then the Hub's object_service (in init.dashboardBase).
  function dashboardLink(base, params) {
    var cut = base.indexOf('?');
    var pairs = [];
    function add(key, value) {
      if (value === null || value === undefined || value === '') return;
      pairs.push(encodeURIComponent(key) + '=' + encodeURIComponent(value));
    }
    Object.keys(params || {}).forEach(function (key) {
      var value = params[key];
      if (Array.isArray(value)) value.forEach(function (item) { add(key, item); }); else add(key, value);
    });
    if (cut >= 0 && base.length > cut + 1) pairs.push(base.slice(cut + 1));
    return (cut >= 0 ? base.slice(0, cut) : base) + (pairs.length ? '?' + pairs.join('&') : '');
  }

  // Same output as the Hub's formatLocalDate (main.js).
  function formatLocalDate(utc, mode) {
    if (!utc) return '-';
    var d;
    if (typeof utc === 'number') {
      d = new Date(utc < 1e12 ? utc * 1000 : utc);
    } else {
      var s = String(utc).trim();
      d = new Date(s);
      if (isNaN(d.getTime())) d = new Date(s.replace(' ', 'T'));
    }
    if (isNaN(d.getTime())) return String(utc);
    if (mode === 'relative') {
      var secs = Math.floor((Date.now() - d.getTime()) / 1000);
      if (secs < 60) return 'just now';
      var mins = Math.floor(secs / 60);
      if (mins < 60) return mins + 'm ago';
      var hrs = Math.floor(mins / 60);
      if (hrs < 24) return hrs + 'h ago';
      var days = Math.floor(hrs / 24);
      if (days < 30) return days + 'd ago';
      var months = Math.floor(days / 30);
      if (months < 12) return months + 'mo ago';
      return Math.floor(months / 12) + 'y ago';
    }
    var pad = function (n) { return n < 10 ? '0' + n : '' + n; };
    var day = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    if (mode === 'date') return day;
    var time = pad(d.getHours()) + ':' + pad(d.getMinutes());
    if (mode === 'long') return day + ' ' + time + ':' + pad(d.getSeconds());
    if (mode === 'full') {
      var names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
      return pad(d.getDate()) + ' ' + names[d.getMonth()] + ' ' + d.getFullYear() + ' at ' + time;
    }
    return day + ' ' + time;
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // A read-only copy of the Hub's TlcData with the same synchronous getters; the Hub serves its cache.
  function dataCopy() {
    var snapshot = null;
    var loading = null;
    function rows(list, project) {
      return (list || []).filter(function (row) { return !project || row.project_name === project; });
    }
    var data = {
      load: function () {
        if (snapshot) return Promise.resolve();
        if (!loading) {
          loading = call('data.snapshot', []).then(function (value) { snapshot = value || {}; })
            .finally(function () { loading = null; });
        }
        return loading;
      },
      invalidate: function () { snapshot = null; },
      getProjects: function () { return (snapshot && snapshot.projects) || []; },
      getTables: function (project) { return rows(snapshot && snapshot.tables, project); },
      getTablesByDataset: function (project) {
        var grouped = {};
        data.getTables(project).forEach(function (table) {
          var name = table.dataset_name || '(ungrouped)';
          (grouped[name] = grouped[name] || []).push(table);
        });
        return grouped;
      },
      getRuns: function (project) { return rows(snapshot && snapshot.runs, project); },
      getSummary: function () {
        return (snapshot && snapshot.summary) || { project_count: 0, table_count: 0, run_count: 0 };
      },
      getLocations: function () { return (snapshot && snapshot.locations) || []; },
      resolveLocation: function (url) {
        var found = data.getTables().concat(data.getRuns()).filter(function (row) { return row.url === url; })[0];
        return found ? found.location : null;
      },
    };
    data.load().catch(function () {});
    return data;
  }

  // ── Hub components run here: the result box and the node list ──

  // What the Hub's node list reads from TlcApi, for this frame only. Same output as api-client.js.
  function nodeListApi(init) {
    var labels = {
      ready: 'Ready', provisioning: 'Starting', bootstrapping: 'Starting', degraded: 'Degraded', dead: 'Failed',
      terminating: 'Deleting', gone: 'Deleted', checking: 'Checking', busy: 'Running a job', untracked: 'Untracked',
      offline: 'Not answering', dismissed: 'Removed from list',
    };
    return {
      computeServiceUrl: init.config.compute_service_url,
      authFetch: computeRequest,
      whenVisible: function (fn, ms) {
        var lastRun = Date.now();
        function run() { lastRun = Date.now(); fn(); }
        var timer = setInterval(function () { if (!document.hidden) run(); }, ms);
        function onVisible() { if (!document.hidden && Date.now() - lastRun >= ms) run(); }
        document.addEventListener('visibilitychange', onVisible);
        function stop() { clearInterval(timer); document.removeEventListener('visibilitychange', onVisible); }
        onDispose(stop);
        return { stop: stop };
      },
      stateLabel: function (word) {
        word = String(word || '');
        if (Object.prototype.hasOwnProperty.call(labels, word)) return labels[word];
        word = word.replace(/_/g, ' ');
        return word ? word.charAt(0).toUpperCase() + word.slice(1) : '';
      },
      nodeHourlyCost: function (node) {
        var rate = node.hourly_rate;
        if (typeof rate !== 'number' || !isFinite(rate) || rate < 0) return 'Cost unavailable';
        return (node.pricing === 'spot' ? 'Spot estimate ' : '≈ ') + '$' + rate.toFixed(rate > 0 && rate < 0.1 ? 3 : 2) + '/h';
      },
      failureSummary: function (detail, max) {
        var text = String(detail || '').replace(/\s+/g, ' ').trim();
        max = max || 160;
        if (text.length <= max) return text;
        var parts = [], start = 0, re = /[.!?]["')\]]*\s+(?=\S)/g, m;
        while ((m = re.exec(text))) {
          var end = m.index + m[0].length, piece = text.slice(start, end).trim();
          if (/\b(?:e\.g|i\.e|etc|vs|approx)\.$/i.test(piece.slice(-8))) continue;
          parts.push(piece); start = end;
        }
        parts.push(text.slice(start).trim());
        parts = parts.filter(Boolean);
        if (parts.length < 2) return text.slice(0, max - 1).trim() + '…';
        var head = parts[0].length <= max / 2 ? parts[0] + ' ' : '', tail = [parts[parts.length - 1]];
        for (var i = parts.length - 2; i >= 1; i--) {
          if ((head + '… ' + parts[i] + ' ' + tail.join(' ')).length > max) break;
          tail.unshift(parts[i]);
        }
        return head + '… ' + tail.join(' ');
      },
      infraNodeDescription: function (node) {
        var isHost = node.flavor === 'workspace';
        return {
          type: isHost ? 'Deployment server' : 'GPU node',
          deployment: isHost ? ((node.label && node.label !== 'workspace' ? node.label : node.id || node.node_id) || 'Managed deployment')
            : (init.config.deployment_name || ''),
        };
      },
    };
  }

  function component(source, name, scope) {
    if (!source) return null;
    try {
      return new Function(scope ? 'TlcApi' : '_', source + '\n;return ' + name + ';')(scope);  // eslint-disable-line no-new-func
    } catch (error) {
      console.error('[3LC] Could not start ' + name + ' in the plugin frame:', error);
      return null;
    }
  }

  // ── Sockets: the frame connects to its own compute with a one-use ticket from the Hub ──

  function sessionFilter(socket, session) {
    if (!session) return socket;
    var receive = socket.onevent;
    socket.onevent = function (packet) {
      var args = packet.data || [];
      var payload = args[1];
      if (args[0] !== 'job_update' && payload && payload._tlc_run_target && payload._tlc_run_target !== session.target) return;
      return receive.call(socket, packet);
    };
    return socket;
  }

  function ticketedIo(io, init) {
    if (!io) return null;
    var computeOrigin = new URL(init.config.compute_service_url).origin;
    function connect(url, options) {
      if (disposed) throw failure('closed', 'The plugin page was unloaded.');
      var target = new URL(typeof url === 'string' ? url : '/', location.href);
      if (target.origin !== computeOrigin) throw failure('refused', 'A plugin page may only open sockets to its own compute service.');
      var namespace = target.pathname.replace(/\/$/, '') || '/';
      var opts = Object.assign({}, options || {}, {
        auth: function (done) {
          call('sockets.ticket', [namespace]).then(function (t) { done({ ticket: t.ticket }); }, function () { done({}); });
        },
      });
      var socket = sessionFilter(io(target.href, opts), init.session);
      // Renew in place well before the server lease expires. A transient failure
      // leaves live updates flowing and retries; it never stops or restarts a job.
      var timer = null, snapshotTimer = null, generation = 0, closed = false, snapshotSeen = null;
      var receive = socket.onevent;
      socket.onevent = function (packet) {
        var args = packet.data || [], job = args[1];
        if (snapshotSeen && args[0] === 'job_update' && job && job.id) snapshotSeen[job.id] = true;
        return receive.call(socket, packet);
      };
      function clearSnapshot() {
        if (snapshotTimer !== null) clearTimeout(snapshotTimer);
        snapshotTimer = null;
      }
      function refreshJobs(current) {
        computeRequest('/api/plugins/jobs', { method: 'GET' }).then(function (r) {
          if (!r.ok) throw failure('unavailable', 'Could not refresh job status.');
          return r.json();
        }).then(function (jobs) {
          if (closed || current !== generation || !socket.connected) return;
          jobs = Array.isArray(jobs) ? jobs : ((jobs && jobs.jobs) || []);
          var seen = snapshotSeen || {};
          jobs.forEach(function (job) {
            if (job && job.id && job.plugin_id === init.pluginId && !seen[job.id]) {
              socket.onevent({ data: ['job_update', job] });
            }
          });
          snapshotSeen = null;
        }).catch(function () {
          if (!closed && current === generation && socket.connected) {
            snapshotTimer = setTimeout(function () { refreshJobs(current); }, 15000);
          }
        });
      }
      function clearTimer() { if (timer !== null) clearTimeout(timer); timer = null; }
      function schedule(fn, delay) { clearTimer(); if (!closed) timer = setTimeout(fn, delay); }
      function renew() {
        var current = generation;
        call('sockets.ticket', [namespace, socket.id]).then(function (t) {
          if (closed || current !== generation || !socket.connected) return;
          socket.timeout(10000).emit('tlc:renew', { ticket: t.ticket }, function (err, reply) {
            if (closed || current !== generation) return;
            schedule(renew, !err && reply && reply.ok ? 150000 + Math.random() * 30000 : 15000);
          });
        }, function () {
          if (!closed && current === generation && socket.connected) schedule(renew, 15000);
        });
      }
      socket.on('connect', function () {
        generation++;
        clearSnapshot();
        snapshotSeen = {};
        refreshJobs(generation);
        schedule(renew, 150000 + Math.random() * 30000);
      });
      socket.on('disconnect', function (reason) {
        generation++;
        clearSnapshot();
        clearTimer();
        if (!closed && reason === 'io server disconnect') schedule(function () { socket.connect(); }, 1000);
      });
      socket.on('connect_error', function () {
        if (!closed && !socket.active) schedule(function () { socket.connect(); }, 15000 + Math.random() * 15000);
      });
      var disconnect = socket.disconnect;
      var unregister = function () {};
      socket.disconnect = socket.close = function () {
        closed = true;
        unregister();
        generation++;
        clearSnapshot();
        clearTimer();
        return disconnect.call(socket);
      };
      unregister = onDispose(function () { socket.disconnect(); });
      return socket;
    }
    Object.keys(io).forEach(function (key) { connect[key] = io[key]; });
    connect.connect = connect;
    return connect;
  }

  // ── PLUGIN_API ──

  function buildApi(init) {
    runTarget = init.runTarget || { target: 'local', ready: true, label: '' };
    var credentials = init.credentials || {};
    var capabilities = DEFAULT_CAPABILITIES.concat(init.capabilities || []);
    var alert = component(init.components && init.components.alert, 'TlcAlert');
    var nodeList = component(init.components && init.components.nodeList, 'TlcNodeList', nodeListApi(init));
    var root = document.getElementById('tlc-plugin-root');
    var data = dataCopy();

    function has(name) { return capabilities.indexOf(name) >= 0; }
    function navGo(path) { return call('nav.go', [String(path)]); }
    function toast(message, kind) { call('ui.toast', [String(message), kind || 'info']).catch(function () {}); }

    var api = {
      lifecycle: { onDispose: onDispose },
      protocol: PROTOCOL,
      frameVersion: FRAME_VERSION,
      context: init.context,
      contractVersion: init.contractVersion || '',
      has: has,
      getConfig: function (key) { return (init.config || {})[key] || ''; },
      // Namespaced calls (protocol 1).
      compute: { request: computeRequest },
      jobs: { cancel: function (jobId, options) { return call('jobs.cancel', [String(jobId), { runUrl: (options && options.runUrl) || null }], 600000); } },
      pickers: { table: function () { return call('pickers.table', [], 600000); } },
      credentials: {
        choose: function (service) {
          return call('credentials.choose', [String(service)], 600000).then(function (id) { if (id) credentials[service] = id; return id; });
        },
        remembered: function (service) { return credentials[service] || null; },
      },
      nodes: {
        create: function (providerId, request) { return call('nodes.create', [String(providerId), request || {}], 600000); },
        mount: function (element, options) {
          if (!nodeList) throw failure('unavailable', 'The node list is not available in this Hub.');
          var widget = nodeList.mount(element, { pluginId: init.pluginId, openNodes: !(options && options.openNodes === false),
            openHistory: !!(options && options.openHistory) });
          var unregister = onDispose(function () { widget.destroy(); });
          return { refresh: function () { return widget.refresh(); }, destroy: function () { unregister(); widget.destroy(); } };
        },
      },
      run: {
        plan: function (body) { return call('run.plan', [body]); },
        checkTable: function (url) { return call('run.checkTable', [String(url)]); },
      },
      nav: {
        go: navGo,
        open: function (url) { return call('nav.open', [String(url)]); },
        onChange: function (callback) { return subscribe('nav', callback); },
      },
      format: { date: formatLocalDate },
      ui: { toast: toast },
      data: data,
      location: null,
      guide: null,
      hostChecksTableInputs: false,
      container: root,
      libs: {
        Chart: window.Chart || null,
        html2canvas: window.html2canvas || null,
        PptxGenJS: window.PptxGenJS || null,
        cytoscape: window.cytoscape || null,
        io: ticketedIo(window.io, init),
      },
      getRunTarget: function () { return runTarget; },
      onRunTargetChange: function (callback) { return subscribe('runTarget', callback); },
      getIcon: function (id) { return (!id || id === init.pluginId) ? (init.icon || '') : ''; },
      dashboardUrl: function (params) { return dashboardLink(init.dashboardBase || '', params); },
      nodeHourlyCost: function (node) { return nodeListApi(init).nodeHourlyCost(node || {}); },
    };
    if (has('history')) {
      api.nav.state = function (state, options) {
        return call('nav.state', [{ search: String((state && state.search) || ''), hash: String((state && state.hash) || '') }, { push: !!(options && options.push) }]);
      };
    }
    if (has('identity:name')) api.identity = { name: function () { return call('identity.name', []); } };

    // The names pages already use, kept as aliases of the namespaced calls.
    api.authFetch = computeRequest;
    api.computeFetch = computeRequest;
    api.createNode = api.nodes.create;
    api.mountNodes = api.nodes.mount;
    api.chooseCredential = api.credentials.choose;
    api.credential = api.credentials.remembered;
    api.planRun = api.run.plan;
    api.checkDataForRunTarget = api.run.checkTable;
    api.navigate = navGo;
    api.showToast = toast;
    // The result box renders here, in the page, as TlcAlert.showResult(target, result, options) does inline.
    api.showResult = function (target, result, options) {
      if (!alert) return null;
      if (!(target instanceof Element)) { options = result; result = target; target = root; }
      return alert.showResult(target, result, options);
    };
    return api;
  }

  // Globals pages use without PLUGIN_API, for one release, each warning once.
  function installGlobals(api) {
    window.openTablePicker = function (inputId) {
      deprecated('openTablePicker', 'PLUGIN_API.pickers.table()');
      api.pickers.table().then(function (url) {
        var input = url && document.getElementById(inputId);
        if (!input) return;
        input.value = url;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
      }, function () {});
    };
    window.closeTablePicker = function () {};
    window.showToast = function (message, kind) { deprecated('showToast', 'PLUGIN_API.showToast'); api.showToast(message, kind); };
    window.TlcData = api.data;
    window.CancelJob = {
      show: function (jobId, opts) {
        deprecated('CancelJob', 'PLUGIN_API.jobs.cancel()');
        if (!jobId) return;
        api.jobs.cancel(jobId, opts).then(function (result) {
          if (result && opts && typeof opts.onDone === 'function') opts.onDone(result);
        }, function (error) { api.showToast(error.message, 'error'); });
      },
    };
    window.cssVar = cssVar;
    window.formatLocalDate = formatLocalDate;
    if (api.libs.io) window.io = api.libs.io;
    // <tlc-table-field>'s Browse… opens the Hub's picker through the bridge. The picker cannot
    // see the page's other table fields from here, so it does not tag their tables.
    window.TlcTableBrowser = {
      open: function (options) {
        var target = options && options.target;
        api.pickers.table().then(function (url) {
          if (!url || !target) return;
          target.value = url;
          target.dispatchEvent(new Event('input', { bubbles: true }));
          target.dispatchEvent(new Event('change', { bubbles: true }));
        }, function () {});
      },
    };
  }

  // Runs a Hub component that registers itself (a custom element), in this frame.
  function register(source, name) {
    if (!source) return;
    try {
      new Function(source)();  // eslint-disable-line no-new-func
    } catch (error) {
      console.error('[3LC] Could not start ' + name + ' in the plugin frame:', error);
    }
  }

  // ── Loading the page ──

  function load(tag, attributes) {
    return new Promise(function (resolve) {
      var el = document.createElement(tag);
      Object.keys(attributes).forEach(function (name) { el[name] = attributes[name]; });
      el.onload = el.onerror = function () { resolve(); };
      document.head.appendChild(el);
    });
  }

  function runScripts(root) {
    // In document order, each external script loaded before the next one runs.
    return Array.prototype.reduce.call(root.querySelectorAll('script'), function (previous, old) {
      return previous.then(function () {
        var el = document.createElement('script');
        var loaded = Promise.resolve();
        if (old.src) {
          loaded = new Promise(function (resolve) { el.onload = el.onerror = function () { resolve(); }; });
          el.src = old.src;
        } else {
          el.textContent = old.textContent;
        }
        if (disposed) return;
        old.parentNode.replaceChild(el, old);
        return loaded;
      });
    }, Promise.resolve());
  }

  function placeIcon(root, svg) {
    var heading = svg && root.querySelector('.plugin-hero h2');
    if (!heading) return;
    var text = heading.textContent.replace(/^[^\w\s]\s*/, '');
    heading.innerHTML = '<span class="plugin-hero-icon" style="display:inline-flex;align-items:center;'
      + 'justify-content:center;vertical-align:middle;width:1.1em;height:1.1em;flex:none;margin-right:6px">'
      + svg + '</span>';
    heading.appendChild(document.createTextNode(text));
  }

  function reportHeight() {
    var last = 0;
    var observer = new ResizeObserver(function () {
      var height = Math.ceil(document.documentElement.scrollHeight);
      if (height !== last && port) { last = height; port.postMessage({ v: PROTOCOL, type: 'height', value: height }); }
    });
    observer.observe(document.body);
    onDispose(function () { observer.disconnect(); });
  }

  function onPortMessage(event) {
    var data = event.data;
    if (!data || data.v !== PROTOCOL) return;
    if (data.type === 'reply' && pending[data.id]) {
      var request = pending[data.id];
      delete pending[data.id];
      clearTimeout(request.timer);
      request.cleanup();
      if (data.ok) request.resolve(data.value);
      else request.reject(failure((data.error && data.error.code) || 'error', (data.error && data.error.message) || 'The Hub refused the request.', data.error));
    } else if (data.type === 'event') {
      if (data.name === 'runTarget') {
        runTarget = data.value;
        listeners.runTarget.forEach(function (listener) { try { listener(runTarget); } catch (e) { console.error(e); } });
      } else if (data.name === 'nav') {
        listeners.nav.forEach(function (listener) { try { listener(data.value); } catch (e) { console.error(e); } });
      } else if (data.name === 'theme') {
        document.documentElement.setAttribute('data-theme', String(data.value || ''));
      } else if (data.name === 'closed') {
        closePort('The Hub closed this page.');
      }
    }
  }

  function start(init) {
    var html = document.documentElement;
    if (init.theme) html.setAttribute('data-theme', init.theme);
    if (init.rootStyle) html.style.cssText = init.rootStyle;
    if (init.bodyClass) document.body.className = init.bodyClass;
    // The page reads its launch context from the address, as it did inside the Hub.
    history.replaceState(null, '', location.pathname + '?hub=' + encodeURIComponent(hubOrigin)
      + (init.search ? '&' + init.search.replace(/^\?/, '') : ''));
    var styles = (init.styles || []).map(function (href) { return load('link', { rel: 'stylesheet', href: href }); });
    var libraries = (init.scripts || []).reduce(function (previous, src) {
      return previous.then(function () { return load('script', { src: src }); });
    }, Promise.resolve());
    return Promise.all(styles.concat([libraries])).then(function () {
      if (disposed) return;
      var api = buildApi(init);
      window.PLUGIN_API = api;
      /* SDK_JOB_TRACKER */
      ['start', 'track', 'run', 'on', 'list', 'connect'].forEach(function (name) { api.jobs[name] = window.PluginJobs[name]; });
      installGlobals(api);
      register(init.components && init.components.tableField, '<tlc-table-field>');
      var root = document.getElementById('tlc-plugin-root');
      root.innerHTML = init.html;
      placeIcon(root, init.icon);
      reportHeight();
      return runScripts(root);
    });
  }

  window.addEventListener('message', function (event) {
    var data = event.data;
    if (disposed || port || !hubOrigin || event.source !== window.parent || event.origin !== hubOrigin) return;
    if (!data || data.v !== PROTOCOL || data.type !== 'init' || !event.ports || !event.ports[0]) return;
    port = event.ports[0];
    port.onmessage = onPortMessage;
    start(data);
  });

  window.addEventListener('pagehide', function () { closePort('The plugin page was unloaded.'); });

  if (hubOrigin) window.parent.postMessage({ tlc: 'plugin-frame', v: PROTOCOL, type: 'ready', frameVersion: FRAME_VERSION }, hubOrigin);
})();
