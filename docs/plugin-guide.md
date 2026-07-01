# 3LC Compute Service — Plugin Development Guide

## Overview

The 3LC Compute Service uses a plugin architecture where each feature (training, import, export, insights, etc.) is a self-contained plugin. Plugins provide:

- **Backend logic** — Python code running in the Compute Service
- **UI fragment** — Self-contained HTML+CSS+JS served to the browser
- **REST endpoints** — Optional custom API routes
- **Job reporting** — Optional progress tracking for long-running tasks

The frontend has **zero knowledge** of any specific plugin. It discovers plugins at runtime via the `/api/plugins/` endpoint and renders their UI generically.

> **Porting an existing plugin** to the current contract? This guide documents the contract in
> full — the main changes to make are adopting `run_job(ctx)` for long-running work, relative
> Litestar route handlers for custom endpoints, and the generic `job_update` channel for UI
> updates (all covered below).

**Important:** Plugins must **not** access the Object Service directly. The Object Service may not be reachable from the plugin's environment. All data access should go through the Compute Service, which uses the `tlc` SDK server-side.

---

## Architecture

```
Browser                         Compute Service (port 5020)
┌─────────────────┐            ┌──────────────────────────────┐
│  plugin-loader.js│───GET────→│  /api/plugins/               │ ← discovery
│                  │           │  /api/plugins/manifest/{id}   │
│                  │───GET────→│  /api/plugins/{id}/ui         │ ← UI fragment
│                  │           │  /api/plugins/{id}/compute    │ ← generic compute
│  PLUGIN_API      │───────── →│  /api/plugins/{id}/*          │ ← custom routes
│  bridge object   │           │                              │
└─────────────────┘            │  ┌──────────────────────────┐│
                               │  │  plugin.toml (manifest)  ││ ← all metadata
                               │  │  + ComputePlugin subclass││
                               │  │  ComputePlugin (ABC)     ││
                               │  │  ├── get_ui_fragment()   ││ ← abstract
                               │  │  ├── compute()           ││ ← abstract
                               │  │  ├── id                  ││ ← host-stamped
                               │  │  ├── run_job(ctx)        ││ ← override (default)
                               │  │  └── get_route_handlers()││ ← override (default)
                               │  └──────────────────────────┘│
                               └──────────────────────────────┘
```

The host owns the job lifecycle: a plugin only *runs* a job (`run_job(ctx)`);
listing, progress fan-out, and cancellation are generic and host-provided. There
is no `get_active_jobs()` / `cancel_job()` on the contract — see
[Long-Running Jobs](#long-running-jobs-run_jobctx).

**Flow:**

1. Frontend calls `GET /api/plugins/` → gets manifests for all plugins
2. Sidebar and action buttons are rendered from manifests (no hardcoded plugin knowledge)
3. When user opens a plugin, frontend calls `GET /api/plugins/{id}/ui` → gets HTML fragment
4. Fragment is injected into the page with a `PLUGIN_API` bridge object
5. Plugin JS uses `PLUGIN_API` to access auth, API clients, Chart.js, SocketIO, etc.

---

## Plugin Types

| `display_mode` | Where it appears | Example |
|---|---|---|
| `sidebar` | Left navigation panel, grouped by `section` | Import, Export, YOLO, SAM3, timm |
| `action` | Action buttons on resource pages (tables, runs) | Merge (2 tables), Run Insights (1+ runs) |
| `hidden` | Not shown in UI; API-only (routes still registered) | Table Statistics (used by project detail inline) |

---

## Step-by-Step: Creating a Plugin

### 1. Create the plugin directory

```
tlc_plugin_my_plugin/          # the default shape: a standalone venv-isolated package
├── plugin.toml    # Manifest — ALL metadata (id, name, ui, runtime)
├── __init__.py    # Plugin object — behavior only, no metadata, no register()
├── ui.html        # UI fragment (HTML + CSS + JS)
├── routes.py      # Custom REST controller (optional — config CRUD, etc.)
├── compute.py     # Pure compute lifted by run_job(ctx) (optional)
└── ...            # All plugin code lives here
```

> **In-tree host exception.** The two private, in-tree plugins shipped inside the
> compute-service host package (`run-insights`, `table-insights`) instead live under
> `compute-service/src/tlc_compute/plugins/my_plugin/` with `isolation = "host"` and an
> entrypoint like `tlc_compute.plugins.my_plugin:MyPlugin`. New open plugins should use the
> `venv` + `tlc_plugin_<name>` shape shown above, not this host form.

### 2. Write the manifest

All metadata lives in a manifest — a standalone `plugin.toml` next to `__init__.py`. The host
reads this **without importing** the plugin, builds a "card" from it, and uses it as the single
source of truth for listing, routing, GPU/CPU classification, SocketIO wiring, and auth-exempt
paths. (`read_manifest()` also accepts a `[tool.tlc-compute]` table in a plugin's `pyproject.toml`
— it checks `plugin.toml` first.)

A `venv` plugin keeps the **same `plugin.toml`** for metadata and adds a separate
`pyproject.toml` alongside it that declares only its isolated venv's dependencies (no
`[tool.tlc-compute]` table there) — see the `timm` / `sam3` / `yolo` plugins for the canonical
layout, and the **Isolation** section below for how the two tiers run.

```toml
# plugin.toml — the single source of truth for this plugin's metadata.
# The host loads the plugin via runtime.entrypoint; there is no register()
# call at import and no metadata on the plugin class.
id = "my-plugin"                    # URL-safe slug
name = "My Plugin"                  # Display name
description = "Analyzes table data quality."
version = "1.0.0"
min_service_version = "0.1.0"       # Minimum compute service version required
icon = "🔍"                         # Fallback emoji
# 16x16 SVG, inline in the manifest (no _ICON_SVG import anymore):
icon_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="5"/><path d="M12 12l3 3"/></svg>'

[ui]
display_mode = "sidebar"            # sidebar | action | hidden
section = "Tools"                   # Sidebar section label
compatible_with = ["table"]         # Resource types this acts on
input_types = ["table"]             # What it consumes
output_types = []                   # What it produces (empty = analysis only)
priority = 50                       # Sort order in sidebar (higher = first)
quick_action = false                # Show in dashboard quick actions?
# Optional sidebar grouping:
# group = "My Group"
# group_icon_svg = '<svg ...><rect x="2" y="2" width="12" height="12" rx="2"/></svg>'

[runtime]
isolation = "venv"                  # venv (isolated worker, the default) | host (in-process)
entrypoint = "tlc_plugin_my_plugin:MyPlugin"  # "pkg.module:ClassName"
requires_gpu = false                # drives GPU vs CPU classification
provision_extra = "my-plugin"       # REQUIRED for venv: host runs `uv sync --extra <this>`
# Optional SocketIO namespace for real-time updates. Defaults to "/<plugin-id>";
# declare it only to override (the host registers it at startup so a UI can connect;
# declaring it does NOT mean emitting custom events):
# socketio_namespace = "/my-plugin"
```

`runtime.provision_extra` is **required for every `venv` plugin**: it names the extra the
host installs with `uv sync --extra <that-value>`. For first-party plugins this is a
per-plugin extra in the `3lc-compute-plugins` umbrella `pyproject.toml`. (Host plugins don't
declare it — their deps are a subset of the service.)

**`isolation` — where the plugin runs (the *only* placement knob).** A plugin
declares `isolation` and `requires_gpu`; it never picks a queue or names a lane.

| `isolation` | Loaded by | Deps | Best for |
|---|---|---|---|
| `host` (default) | folder scan → imported **in-process** | must be a subset of the service | lightweight, hot-reloadable plugins |
| `venv` | manifest only → spawn a worker over a UDS | its own uv-managed venv | heavy / conflicting deps (torch, ultralytics, SAM) |

The plugin class is identical either way — `run_job(ctx)` talks only to `ctx`, so
the same code runs in-process or in a worker. `requires_gpu = true` routes the job
through the shared GPU queue (one GPU job at a time, across every plugin);
`requires_gpu = false` jobs run on the CPU queue. Both are host-owned; the plugin
never touches a queue.

> **Legacy:** `socketio_runner_module` / `socketio_runner_fn` are still read but
> are vestigial — they only seeded a runner's initial state under the old job
> machinery. A plugin migrated to `run_job(ctx)` declares only `socketio_namespace`.

### 3. Implement the plugin object

A plugin is a **subclass of `ComputePlugin`** (imported from `tlc_plugin_sdk`) —
there is no `register()` call. You must implement the two abstract methods,
`get_ui_fragment()` and `compute()`; `id` is hydrated onto the instance from the manifest
by the host. Optional behavior (custom routes, jobs, lifecycle hooks) ships as no-op
defaults on the base, so you override only what you need and the host calls every hook
directly.

```python
"""My Plugin — does something useful with tables."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tlc_plugin_sdk import ComputePlugin


class MyPlugin(ComputePlugin):
    """Example plugin that analyzes a table.

    Behavior only — all metadata lives in plugin.toml. The host instantiates this
    via the manifest's runtime.entrypoint and stamps id/name/icon/version onto the
    instance; the class does not declare them.
    """

    _ui_cache: str | None = None

    def get_ui_fragment(self) -> str:
        """Return the self-contained UI HTML."""
        if self._ui_cache is None:
            ui_path = Path(__file__).resolve().parent / "ui.html"
            self._ui_cache = ui_path.read_text(encoding="utf-8")
        return self._ui_cache

    def compute(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle GET /api/plugins/my-plugin/compute requests."""
        url = params.get("url", "")
        if not url:
            return {"error": "No table URL provided."}

        # Do your computation here (using tlc SDK, numpy, etc.)
        import tlc
        table = tlc.Table.from_url(url)
        return {
            "row_count": table.row_count,
            "columns": len(table.columns),
            "message": f"Analyzed table with {table.row_count} rows.",
        }

    def get_route_handlers(self) -> list[Any]:
        """Return custom relative Litestar route handlers (optional)."""
        return []  # Or, typically: `from . import routes; return routes.get_route_handlers()`
```

### 4. Create the UI fragment

The UI fragment is a self-contained `<style>` + `<div>` + `<script>` block. It has access to:

- `PLUGIN_API` — bridge object with context, API clients, and libraries
- `COMPUTE_URL` — shorthand for the compute service base URL
- All CSS variables from `main.css` and `plugin-common.css`
- Vendor libraries: Chart.js, html2canvas, PptxGenJS, Socket.IO, Cytoscape

```html
<style>
.my-plugin-result {
  padding: 16px; font-size: 12px; color: var(--text);
}
.my-plugin-result .count {
  font-size: 24px; font-weight: 700; color: var(--accent);
}
</style>

<div class="plugin-page">
  <div class="card">
    <div style="padding:16px">
      <div style="font-size:14px;font-weight:600;margin-bottom:8px">My Plugin</div>
      <div id="my-plugin-body">
        <span class="spinner"></span> Analyzing...
      </div>
    </div>
  </div>
</div>

<script>
(function () {
  'use strict';

  // ── Context from the plugin host page ───────────────────
  var COMPUTE_URL = PLUGIN_API.getConfig('compute_service_url');
  var resourceUrls = PLUGIN_API.context.resourceUrls || [];
  var body = document.getElementById('my-plugin-body');

  if (resourceUrls.length === 0) {
    body.innerHTML = '<div style="color:var(--text-muted)">No table selected.</div>';
    return;
  }

  // ── Option A: Use the generic compute endpoint ──────────
  var url = resourceUrls[0];
  PLUGIN_API.authFetch(
    COMPUTE_URL + '/api/plugins/my-plugin/compute?url=' + encodeURIComponent(url)
  )
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.error) {
        body.innerHTML = '<div style="color:var(--error)">' + data.error + '</div>';
        return;
      }
      body.innerHTML =
        '<div class="my-plugin-result">' +
        '<div class="count">' + data.row_count + '</div>' +
        '<div>rows across ' + data.columns + ' columns</div>' +
        '</div>';
    })
    .catch(function (err) {
      body.innerHTML = '<div style="color:var(--error)">Failed: ' + err.message + '</div>';
    });

  // ── Option B: Use Chart.js (available via PLUGIN_API.libs) ─
  // var Chart = PLUGIN_API.libs.Chart;
  // new Chart(canvas, { ... });
})();
</script>
```

### 5. Discovery

There is **nothing to register**. On startup, `discover.py` scans the plugin directories
for manifests (no imports), builds a card from each, and gates compatibility against the
service version. When a host plugin is actually needed, the host imports the module named in
the manifest's `runtime.entrypoint` and instantiates the class, stamping the display
identity (`id`, `name`, `icon`, `version`) from the manifest onto the instance.

Because metadata is read before any import, a plugin whose dependency is missing still
**lists** (greyed-out with a reason) instead of vanishing — its behavior code is never
imported until it's used.

That's it. Drop the directory in place with a `plugin.toml` and it will be discovered on
startup.

---

## The PLUGIN_API Bridge

> **Typed declaration.** The full browser surface below is declared in
> `tlc_plugin_sdk/contract/plugin-api.d.ts` (ships in this wheel; lands at
> `<site-packages>/tlc_plugin_sdk/contract/plugin-api.d.ts`). A plain-JS `ui.html` can opt
> into editor type-checking without a build step:
>
> ```javascript
> /// <reference types="3lc-plugin-sdk/contract/plugin-api" />
> var API = window.PLUGIN_API;   // now typed
> ```
>
> That file is the source of truth for **`JS_CONTRACT` (0.1)** — the browser-side contract.
> The Hub frontend (`3lc-hub-frontend/frontend/static/js/plugin-loader.js`, `mountPlugin`)
> **implements** `PLUGIN_API`; `window.PluginJobs` **ships from this package** (it is layered
> on top of the bridge, not part of it). See "Two version axes" below.

### How a fragment reaches the browser

The frontend is a thin Flask + Jinja2 *shell* that renders page skeletons and does **all**
data fetching client-side — it holds zero plugin knowledge and never proxies plugin data.
The mount lifecycle:

```
Browser (3lc-hub-frontend, vanilla JS)            Compute service (:5020)
  │  user opens /plugin/{id}  (Flask route → plugin_host.html)
  ├─ TlcPlugins.mountPlugin(id, el, ctx) ───────▶  GET /api/plugins/{id}/ui  → HTML fragment
  │     1. innerHTML = fragment
  │     2. window.PLUGIN_API = {…}   (the bridge, built in mountPlugin)
  │     3. re-exec the fragment's <script> tags
  │
  │  fragment JS now runs, talking back through PLUGIN_API:
  ├─ PLUGIN_API.authFetch(.../compute?…) ───────▶  GET  /api/plugins/{id}/compute   → compute()
  ├─ window.PluginJobs.run(id, params, cbs) ────▶  POST /api/plugins/{id}/run       → run_job()
  │     └─ subscribes to SocketIO namespace "/{id}", event "job_update" (generic schema)
  └─ PLUGIN_API.authFetch(.../{subpath}) ───────▶  ANY  /api/plugins/{id}/{subpath} → route handler
```

A plugin fragment is plain HTML+JS+CSS and is the **same bytes** whether the plugin runs
in-process (`host` mode) or in an isolated venv worker (reverse-proxied) — the frontend
can't tell. `PLUGIN_API` is the **single** host→fragment JS contract; a fragment should
reach for nothing else (the `API` shorthand some plugins use is just
`var API = window.PLUGIN_API`).

### The bridge object

When a plugin UI fragment is mounted, the frontend creates a global `PLUGIN_API` object:

```javascript
PLUGIN_API = {
  context: {
    resourceType: "run" | "table" | null,  // What resource type was passed
    resourceUrls: ["url1", "url2", ...],   // Resource URLs from query params
    projectName: "MyProject",              // Current project (from query or localStorage)
  },

  // Config values
  getConfig: function(key) { ... },
  // Keys: "dashboard_url", "compute_service_url", "object_service_url"

  // API clients (authenticated)
  compute: TlcApi.computeService,     // Compute service methods
  objects: TlcApi.objectService,      // Object service methods
  authFetch: TlcApi.authFetch,        // fetch() with auth headers
  data: TlcData,                      // Cached data (projects, tables, runs)

  computeFetch: TlcApi.computeFetch,  // authFetch joined to the compute-service base URL

  // Vendor libraries (each null if the host didn't load it). Stability tiers (frozen):
  libs: {
    io: io,                           // Socket.IO client — STABLE (the job channel rides it)
    Chart: Chart,                     // Chart.js          — best-effort (may change w/o bump)
    html2canvas: html2canvas,         // Screenshot export — best-effort
    PptxGenJS: PptxGenJS,             // PowerPoint export — best-effort
    cytoscape: cytoscape,             // Graph viz         — best-effort
  },

  // Utilities
  container: HTMLElement,             // The DOM element the plugin is mounted in
  navigate: function(path) { ... },   // Navigate to a route
  showToast: function(msg, type) { }, // Show a toast notification
  getIcon: function(id) { ... },      // Get SVG icon for this plugin (or another by ID)
}
```

**Notes on the bridge surface** (full signatures in `plugin-api.d.ts`):

- **`getConfig(key)`** recognizes exactly three keys — `compute_service_url`, `dashboard_url`,
  `object_service_url`. Any other key returns `''`. `compute_service_url` is the GPU/CPU-routed
  service for *this* plugin.
- **`authFetch(url, opts)`** is the most-used member: it waits for auth to resolve, injects the
  `Authorization` header and a JSON `Accept`, and aborts after `opts.timeout` ms (default 10000,
  a custom non-standard option deleted before the real `fetch`) unless you pass your own `signal`.
  It rejects non-ok responses with the parsed error detail.
- **`libs` stability tiers (frozen contract):** `io` (socket.io) is **stable** — the job-tracker
  channel rides it and it is the only `libs` member a plugin may depend on. `Chart`, `cytoscape`,
  `html2canvas`, `PptxGenJS` are **best-effort** — exposed for convenience but may be swapped or
  removed without a contract bump; a plugin that needs one should be prepared to vendor its own.
- **`compute` / `objects` / `data` / `computeFetch` / `navigate` / `getIcon` / `container`** are
  part of the declared surface but rarely used directly by `ui.html` (plugins reach data through
  `authFetch`); they are documented in the `.d.ts` for completeness.

### Common patterns

```javascript
// Authenticated fetch to your custom endpoint
PLUGIN_API.authFetch(COMPUTE_URL + '/api/my-plugin/analyze', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ url: tableUrl }),
}).then(function(r) { return r.json(); });

// Create a Chart.js chart
var Chart = PLUGIN_API.libs.Chart;
new Chart(document.getElementById('my-canvas'), {
  type: 'bar',
  data: { labels: [...], datasets: [...] },
});

// Connect to a SocketIO namespace
var io = PLUGIN_API.libs.io;
var socket = io(COMPUTE_URL + '/my-plugin');
socket.on('progress', function(data) { ... });
```

---

## Custom REST Endpoints

For plugins that need more than the generic `compute()` method (e.g., POST bodies, multiple
endpoints, streaming), return **relative Litestar route handlers** from `get_route_handlers()` —
bare `@get`/`@post` handlers with **relative** paths, no `Controller` and no `/api/plugins`
prefix. The host serves them through the plugin's own per-plugin app, behind the generic
`/api/plugins/{id}/{subpath}` catch-all — in-process for `host` plugins, reverse-proxied to the
worker for `venv` plugins. (Pre-transport-unification plugins mounted a `Controller` at the
absolute path `/api/plugins/{id}`, which shadowed the generic routes and forced an explicit
`@get("/ui")`. That is gone — there is **no** `handle()` and no shadowing caveat.)

```python
from typing import Any

from litestar import get, post
from litestar.handlers import BaseRouteHandler


def get_route_handlers() -> list[BaseRouteHandler]:
    # `sync_to_thread=True` for blocking work so it doesn't stall the event loop.
    @get("/status", sync_to_thread=False)
    async def get_status() -> dict[str, Any]:
        return {"ready": True}

    @post("/analyze", sync_to_thread=True)
    def analyze(data: dict[str, Any]) -> dict[str, Any]:
        url = data.get("url", "")
        # ... do work ...
        return {"result": "done"}

    return [get_status, analyze]
```

These resolve at `GET /api/plugins/my-plugin/status` and `POST /api/plugins/my-plugin/analyze`.
The plugin class's `get_route_handlers()` delegates to this module-level function (often a
`routes.py`); a lazy import inside it avoids import cycles with the package `__init__`. See
`plugins/image_metrics/routes.py` (simplest) and `plugins/sam3/routes.py`.

---

## Long-Running Jobs (`run_job(ctx)`)

A plugin with a long-running task (training, inference, import) **declares** the job
in its manifest and **implements** it as `run_job(ctx)`. It does **not** grab a queue,
push a closure, or poll a shared `cancel_flag` — the host owns the queue, the GPU/CPU
slot lease, progress fan-out, listing, and cancellation. The same `run_job` code runs
in-process (`host`) or in a worker (`venv`); it only ever touches `ctx`.

**Declare the job in the manifest.** `requires_gpu` is the only knob:

```toml
[runtime]
isolation = "venv"
entrypoint = "tlc_plugin_my_gpu_plugin:MyGpuPlugin"
requires_gpu = true                 # → routed through the shared GPU queue (1 at a time)
provision_extra = "my-gpu-plugin"   # venv deps installed via `uv sync --extra <this>`
# socketio_namespace defaults to "/my-gpu-plugin"; declare only to override
```

GPU jobs are serialized — only one runs at a time across every GPU plugin (YOLO, SAM3,
timm, image-metrics). `requires_gpu = false` jobs run on the CPU queue. Either way the
plugin never names or touches a queue.

**Implement `run_job(ctx)`.** `ctx` is a `JobContext` (`tlc_plugin_sdk`); the
host provides it and the surface is identical in both modes:

```python
from tlc_plugin_sdk import ComputePlugin, JobContext


class MyGpuPlugin(ComputePlugin):
    def run_job(self, ctx: JobContext) -> None:
        table_url = ctx.params["table_url"]      # parsed request body / query
        # ctx.state_dir → writable per-plugin scratch that survives a reload/reinstall

        for i, batch in enumerate(load(table_url)):
            if ctx.cancelled:                     # cooperative cancel checkpoint
                return                            # host marks the job "cancelled"
            ctx.progress(percent=100 * i / n, label=f"batch {i}/{n}")
            ctx.metric("loss", 0.042)             # key/value card on the generic panel

        ctx.result(run_url=created_table_url)     # the one "open result" link
        # Raise to fail the job — the host records the error and ends the stream.
```

`JobContext` surface:

| Member | Purpose |
|---|---|
| `ctx.job_id` | Unique id for this job. |
| `ctx.params` | Job parameters (parsed request body / query). |
| `ctx.cancelled` | `True` once cancel is requested — poll at checkpoints. |
| `ctx.state_dir` | Writable per-plugin scratch dir (never write inside the package). |
| `ctx.progress(*, percent, label="", timing=None)` | Generic progress bar. `percent=-1` = indeterminate. `timing` = `{elapsed_s, eta_s, avg_step_s, step_label}`. |
| `ctx.metric(label, value)` | Scalar metric card on the generic panel. |
| `ctx.log(message)` | A log line for the job. |
| `ctx.result(*, run_url)` | The canonical result link (last write wins). |
| `ctx.emit(name, payload)` | A **custom** event for the plugin's OWN rich UI (see below). |

**The host owns listing and cancellation — there is nothing to implement.** Because the
host started every job (via `run_job`), it serves the generic Queue & Progress panel
(`GET /api/plugins/jobs`) and cancels (`POST /api/plugins/jobs/{job_id}/cancel`) from its
own `JobManager`. There is **no** `get_active_jobs()` / `cancel_job()` on the contract.
The `progress` / `metric` / `result` calls above are what populate that generic panel —
translated to the frontend's plugin-agnostic schema by the host, so no plugin-specific
field ever reaches the frontend.

**Start a job from the UI** with the generic run route — `POST /api/plugins/{id}/run`
with the params as the JSON body; it returns `{job_id, status, namespace}`. The easiest
way to consume it is `window.PluginJobs` (next section).

---

## Real-Time Updates: `window.PluginJobs` + custom events

Every job already broadcasts a generic `job_update` SocketIO event on the plugin's
namespace, carrying the frontend's plugin-agnostic schema (`status`,
`progress.{percent,label,timing}`, `run_url`, `metrics[]`). A plugin's **own** `ui.html`
can be a second consumer of that same channel for a richer, tailored view — it needs **no
bespoke events** for the generic lifecycle (queued → running → done, %, result link,
metrics).

**The `window.PluginJobs` client** is a global the plugin injects into its fragment in
`get_ui_fragment()`, exactly like `alias_ui_script()` — pass `job_tracker_script()` (from
`tlc_plugin_sdk.shared.job_tracker`) to `inject_scripts(raw, job_tracker_script())`
(see `importer/__init__.py`). It starts a job and tracks it over the generic channel:

```javascript
// The host registers the namespace automatically (default "/<plugin-id>");
// declare socketio_namespace in plugin.toml only to override it.
PluginJobs.run('my-plugin', { table_url: url }, {
  onUpdate: function (job) {
    // generic schema: job.status, job.progress.percent/label, job.metrics[]
    setProgress(job.progress.percent, job.progress.label);
  },
  onDone: function (job) { showResult(job.run_url); },
  onError: function (job) { showError(job.subtitle); },  // failure message rides subtitle
});
```

`run()` pre-subscribes and buffers, so a job that finishes between the `/run` response and
the client subscribing still delivers its terminal event. (`PluginJobs.start/track/cancel`
are the lower-level pieces.) The separate frontend's generic panel polls the same schema
independently.

**Custom events** — `ctx.emit(name, payload)` is reserved for telemetry the generic schema
**can't** express (e.g. a training plugin's per-epoch loss curve). The host relays it
verbatim on the plugin's namespace; the generic panel ignores it. The name `job_update` is
reserved and rejected. A plugin should **not** open its own SocketIO connection — emitting
through `ctx` keeps `run_job` host/venv portable.

```python
# backend — inside run_job, for a plugin-specific chart the generic panel can't show:
ctx.emit("epoch_metrics", {"epoch": 3, "loss": 0.042, "map50": 0.85})
```

```javascript
// frontend — listen for it on the same namespace via PluginJobs.track, or directly:
var socket = PLUGIN_API.libs.io(COMPUTE_URL + '/my-plugin');
socket.on('epoch_metrics', function (d) { lossChart.push(d.epoch, d.loss); });
```

> Don't leak plugin internals into the generic surface. Training fields (`epoch`,
> `loss`, `model_name`, `mode`, …) belong in a `ctx.emit` payload for your own UI — never
> in `ctx.progress`/`ctx.metric`, which feed the plugin-agnostic frontend panel.

---

## CSS Conventions

Plugin UI fragments inherit all CSS from `main.css` (via `base.html`) and `plugin-common.css` (via `plugin_host.html`). Use these shared patterns:

| Class | Purpose |
|---|---|
| `.plugin-page` | Max-width 1200px container |
| `.plugin-page-narrow` | Max-width 700px container |
| `.card` | Standard card with shadow |
| `.btn`, `.btn-sm`, `.btn-primary` | Buttons |
| `.plugin-hero` | Hero section with accent background |
| `.plugin-form-grid` | 2-column responsive form layout |
| `.plugin-form-grid-3` | 3-column responsive form layout |
| `.plugin-param-group` | Titled section within a card |
| `.plugin-action-bar` | Submit/cancel row with spinner |
| `.plugin-metric-card` | Metric display card |
| `.plugin-progress-wrap` + `.plugin-progress-bar` | Progress bar |
| `.plugin-log-area` | Monospace log output |
| `.plugin-two-col` | Sidebar + main layout |
| `.plugin-three-col` | Sidebar + main + preview layout |
| `.plugin-config-item` | Selectable list item |

Use CSS variables for colors — never hardcode:

```css
color: var(--text);             /* Primary text */
color: var(--text-muted);       /* Secondary text */
background: var(--bg);          /* Background */
background: var(--bg-card);     /* Card background */
border-color: var(--border);    /* Border */
color: var(--accent);           /* Accent color (#2a4a61) */
```

---

## Existing Plugins Reference

| Plugin ID | display_mode | Section | isolation | Description |
|---|---|---|---|---|
| `importer` | sidebar | Data Ops | **venv** | Import data (YOLO, COCO, Folder, CSV, Unlabeled) |
| `exporter` | sidebar | Data Ops | **venv** | Export tables to CSV, XLSX, YOLO, COCO |
| `splitter` | sidebar | Data Ops | **venv** | Split tables into train/val/test sets |
| `merger` | sidebar | Data Ops | **venv** | Merge 2 tables by column join |
| `image-metrics` | sidebar | Data Ops | **venv** (GPU) | Image quality metrics (brightness, sharpness, noise, etc.) |
| `yolo` | sidebar | AI Tools | **venv** (GPU) | Ultralytics YOLO training + metrics collection |
| `sam3` | sidebar | AI Tools | **venv** (GPU) | Auto-labeling with SAM3/GroundingDINO |
| `timm` | sidebar | AI Tools | **venv** (GPU) | Image classification with timm models |
| `table-statistics` | hidden | Analysis | **venv** | Per-column stats & image thumbnails (API-only) |
| `run-insights` | action | Analysis | host (in-tree) | Run statistics, health scores, per-class metrics |
| `table-insights` | action | Analysis | host (in-tree) | GT-only data quality analysis (bbox sizes, balance, etc.) |

---

## UI Consistency Guide

All plugin UIs should look like they belong to the same product. Follow these patterns
from the existing plugins (SAM3, YOLO, timm are the reference implementations).

### Page Structure

Every sidebar plugin UI should follow this structure:

```html
<style>/* Plugin-specific styles only — use shared classes for everything else */</style>

<div class="plugin-page-narrow">       <!-- Centered 700px container -->
  <div class="plugin-hero">            <!-- Accent-tinted header -->
    <h2>🎯 Plugin Name</h2>
    <p>Short description of what this plugin does.</p>

    <!-- Feature badges — 3 items in a grid -->
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px">
      <div class="plugin-hero-badge">Feature 1</div>
      <div class="plugin-hero-badge">Feature 2</div>
      <div class="plugin-hero-badge">Feature 3</div>
    </div>

    <!-- Workflow steps (optional) -->
    <div class="plugin-workflow">
      <span class="plugin-workflow-step active">1. Configure</span>
      <span class="plugin-workflow-arrow">→</span>
      <span class="plugin-workflow-step">2. Run</span>
      <span class="plugin-workflow-arrow">→</span>
      <span class="plugin-workflow-step">3. Results</span>
    </div>
  </div>

  <!-- Config bar (for plugins with saved configs) -->
  <div class="card" style="margin-bottom:12px;padding:10px 14px">
    <div style="display:flex;align-items:center;gap:8px">
      <select id="config-select">...</select>
      <button class="btn btn-sm">Load</button>
      <button class="btn btn-sm btn-primary">Save</button>
    </div>
  </div>

  <!-- Main form -->
  <div class="card" style="padding:16px">
    <div class="plugin-param-group">
      <div class="plugin-param-group-title">Section Title</div>
      <div class="plugin-form-grid">
        <!-- Form fields -->
      </div>
    </div>

    <div class="plugin-action-bar">
      <button class="btn btn-primary">Run</button>
      <span class="spinner" style="display:none"></span>
    </div>
  </div>

  <!-- Results area (appears after execution) -->
  <div id="results" class="card" style="display:none;padding:16px;margin-top:12px">
  </div>
</div>
```

### Visual Consistency Rules

1. **Container**: Use `.plugin-page-narrow` (700px) for form-based plugins, `.plugin-page` (1200px) for data-heavy plugins like run-insights.
2. **Hero section**: Always include `.plugin-hero` with icon + title + description + 3 feature badges.
3. **Color**: Use `var(--accent)` (#2a4a61) as the accent. Never use teal (#5a9aad).
4. **Cards**: Wrap all content sections in `.card`. Use `margin-bottom:12px` between cards.
5. **Forms**: Use `.plugin-form-grid` (2-col) or `.plugin-form-grid-3` (3-col) for form layouts.
6. **Buttons**: Primary actions get `.btn-primary`. Secondary actions get plain `.btn`.
7. **Spinners**: Use `<span class="spinner"></span>` (defined in main.css).
8. **Toasts**: Use `PLUGIN_API.showToast(msg, 'success'|'error'|'info')` for feedback.
9. **Config bar**: Plugins with saved configurations (YOLO, SAM3, timm) should have a config select/save/delete bar below the hero.
10. **Dark mode**: All CSS must work in dark mode. Use `var(--text)`, `var(--bg)`, `var(--border)`, etc. Never hardcode colors.

### What NOT to Do

- Don't define custom button styles — use `.btn`, `.btn-sm`, `.btn-primary`.
- Don't use inline colors — always use CSS variables.
- Don't create custom modal/dialog implementations — use `.card` with show/hide.
- Don't add custom scrollbar styles or font sizes below 10px.
- Don't use `position: fixed` — it breaks the plugin container layout.

---

## Hot-Reload (Development)

During development, you can hot-reload a plugin without restarting the Compute Service:

```bash
# 1. Edit your plugin files on the server
# 2. Call the admin reload endpoint:
curl -X POST http://localhost:5020/api/admin/plugins/my-plugin/reload
```

Or from the browser console:
```javascript
TlcApi.authFetch(TlcApi.computeServiceUrl + '/api/admin/plugins/my-plugin/reload', {method:'POST'})
  .then(r => r.json()).then(console.log)
```

This purges the plugin's Python modules from `sys.modules`, re-imports the package
(picking up file changes), and re-initialises the runtime. Running jobs in other
plugins are unaffected. The UI cache is automatically cleared.

To unload a plugin entirely:
```bash
curl -X POST http://localhost:5020/api/admin/plugins/my-plugin/unload
```

---

## Version & Compatibility

All versions use **SemVer** (`MAJOR.MINOR.PATCH`).

### Two version axes — never conflate them

There are **two independent** kinds of "version" in play. Keep them separate:

**(a) CONTRACT capability — what a plugin *programs against*.** Pinned at build/install time
via the `3lc-compute-plugin-sdk` dependency. The SDK exposes three constants in `tlc_plugin_sdk`:

| Constant | Covers | Value |
|----------|--------|-------|
| `SDK_CONTRACT_VERSION` | the wheel/SemVer — the actual dependency *pin* (`3lc-compute-plugin-sdk>=X,<Y`) | = package version (`0.1.0`) |
| `PY_CONTRACT` | the Python surface: `ComputePlugin` / `JobContext` / `shared.*` | `0.1` |
| `JS_CONTRACT` | the browser surface: `PLUGIN_API` / `PluginJobs` / `TlcData` (see `plugin-api.d.ts`) | `0.1` |

`PY_CONTRACT` and `JS_CONTRACT` are **feature-detection markers** that increment *independently*
as features are added to one side without the other. Both are always `<=` the package version (a
capability can only exist in a shipped wheel). **Bump the package version when *either* moves.**
A plugin that needs a newer capability raises its `3lc-compute-plugin-sdk` floor; it can also
feature-detect at runtime by reading `tlc_plugin_sdk.PY_CONTRACT` / `JS_CONTRACT`.

**(b) SERVICE compatibility — what a plugin *runs against*.** Negotiated at *runtime*, not pinned.
The compute-service and frontend version independently (separate repos); a plugin declares floors
in its manifest and the host gates them (over `/health`, which reports the service `mode`/version):

| Manifest field | Meaning |
|----------------|---------|
| `min_service_version` | minimum compute-service version this plugin needs |
| `max_service_version` | maximum service version (empty = no upper bound) |
| `min_frontend_version` | minimum frontend version this plugin's UI needs |

An incompatible plugin is **loaded but disabled** (shown with an "update" badge), never silently
dropped. So: **contract capability** is a compile/install-time pin against this SDK; **service
compatibility** is a runtime negotiation against the host services. The SDK wheel does not pin a
service version, and the manifest floors do not pin a contract version — they are orthogonal.

### Plugin Version Fields

All version fields live at the top level of the manifest (`plugin.toml`, or
`[tool.tlc-compute]` in `pyproject.toml`):

```toml
version = "1.0.0"               # Plugin's own version
min_service_version = "0.2.0"   # Minimum compute service version required
max_service_version = ""        # Maximum service version (empty = no upper bound)
min_frontend_version = "0.2.0"  # Minimum frontend version for this plugin's UI
```

### When to Bump Versions

| Change | What to bump |
|--------|-------------|
| Bug fix in plugin logic | Plugin `version` PATCH (1.0.0 → 1.0.1) |
| New feature in plugin | Plugin `version` MINOR (1.0.0 → 1.1.0) |
| Plugin uses new service API | Plugin `version` MINOR + bump `min_service_version` |
| Breaking change to plugin UI/API | Plugin `version` MAJOR (1.0.0 → 2.0.0) |

### Compatibility Behavior

- **Compatible** plugins load normally and appear in the sidebar.
- **Incompatible** plugins are still loaded but **disabled** — visible in the sidebar with an "update" badge, grayed out, not clickable. Users can see what's available but can't use it until the service is updated.
- The Settings → Plugins page shows an "Incompatible" badge with the reason.

### Future: Plugin Repository

Plugins have placeholder fields for a future remote update system:
- `update_available`: Latest version from repository (empty until repo is built)
- `changelog_url`: Link to changelog
- `upgrade_required`: If True, plugin must be upgraded to continue
- `repository_url`: Where to fetch updates

These are empty today but included in the manifest schema for forward compatibility.

## Checklist

- [ ] `plugin.toml` manifest present with all metadata (id, name, `[ui]`, `[runtime]`)
- [ ] `runtime.entrypoint` points at the behavior class (`"pkg.module:ClassName"`)
- [ ] Plugin class subclasses `ComputePlugin` — behavior-only, no metadata attrs, no `register()`
- [ ] `version` set to meaningful SemVer (not just "1.0.0")
- [ ] `min_service_version` set to the actual minimum service version needed
- [ ] `icon_svg` set to an inline 16x16 SVG literal in the manifest
- [ ] `get_ui_fragment()` returns self-contained HTML+CSS+JS
- [ ] UI uses `PLUGIN_API` bridge (never raw `fetch` without auth)
- [ ] No plugin-specific logic in frontend code (plugin boundary)
- [ ] Custom CSS uses `var(--*)` variables, not hardcoded colors
- [ ] Job progress follows the generic schema (no plugin-specific fields in frontend)
- [ ] If GPU-bound: `requires_gpu = true` in `[runtime]`; long work is `run_job(ctx)` — never grab a queue
- [ ] If creating tables from images: registers URL aliases via `tlc_plugin_sdk/shared/aliases.py` + `tlc_plugin_sdk/shared/alias_ui.py` (inject with `inject_scripts()`)
- [ ] UI follows the page structure from "UI Consistency Guide" above
- [ ] Hero section with icon, title, description, and 3 feature badges
- [ ] Config bar if plugin has saved configurations
- [ ] Dark mode works correctly (no hardcoded colors)

---

## Guide for Claude: Building New Plugins

This section is a reference for Claude Code when asked to create a new plugin.

### Before You Start

1. Read this entire guide first.
2. Read an existing plugin that's closest to what you're building:
   - **Form → execute → result**: Look at `exporter/` or `importer/`
   - **Config + GPU job + SocketIO**: Look at `sam3/`, `yolo/`, or `timm/`
   - **Inline data display (no standalone page)**: Look at `table_statistics/`
   - **Action on selected resources**: Look at `merger/` or `run_insights/`
3. Read `tlc_plugin_sdk/contract.py` — `ComputePlugin` is an `abc.ABC` with two abstract
   methods (`get_ui_fragment()`, `compute()`) and an `id` attribute the host stamps from
   the manifest. The optional hooks (`run_job`, `initialise_runtime`, `shutdown_runtime`,
   `get_route_handlers`) ship as no-op defaults, so you override only what you
   need and the host calls each directly. There is no `register()`, no `handle()`, and **no**
   `get_active_jobs`/`cancel_job` — the host owns job listing and cancellation.
4. Read `plugins/exporter/plugin.toml` and `plugins/exporter/__init__.py` — the canonical
   manifest + `ComputePlugin` subclass exemplar.
5. Read `plugins/discover.py` to see how manifests are scanned and cards are built.

### Step-by-Step

1. **Create directory**: `plugins/<plugin_name>/` (snake_case)
2. **Create `plugin.toml`** (the manifest) with all metadata: top-level identity/version,
   `[ui]`, and `[runtime]` (including `entrypoint = "pkg.module:ClassName"`). Copy the shape
   from `exporter/plugin.toml`.
3. **Create `__init__.py`** with a `ComputePlugin` subclass:
   - Subclass `ComputePlugin` (from `tlc_plugin_sdk`); no metadata attributes, no `register()`
   - `get_ui_fragment()` reading from `ui.html`
   - `get_route_handlers()` returning your controller class (optional)
   - Any optional hooks you need (`run_job` for long-running tasks,
     `initialise_runtime`, `shutdown_runtime`) — override the base's no-op defaults
4. **Create `ui.html`** following the UI structure above
5. **Create `routes.py`** if you need custom endpoints (e.g. config CRUD)
6. **If you have a long-running task**: implement `run_job(ctx)` (see
   [Long-Running Jobs](#long-running-jobs-run_jobctx)) and set `requires_gpu` in
   `[runtime]`. Reference: `image_metrics/`, `yolo/`, `sam3/`, `timm/`, `importer/`.

### Code Patterns to Follow

**Config Store** (if plugin has saved configurations):
- Use the shared `PluginConfigStore` from `tlc_plugin_sdk.shared.config_store`
  (generic over your config dataclass; JSON file persistence under the plugin's state dir)
- Pair it with `config_ui_script()` from `tlc_plugin_sdk.shared.config_ui` for the config bar
  (inject via `inject_scripts()`). See `plugins/sam3/` and `plugins/timm/`.

**Long-running jobs** (training, inference, multi-step import):
- Implement `run_job(ctx)` — the host owns the queue, GPU/CPU slot, progress fan-out,
  listing, and cancellation (see [Long-Running Jobs](#long-running-jobs-run_jobctx)).
- Drive the generic Queue & Progress panel via `ctx.progress`/`ctx.metric`/`ctx.result`;
  poll `ctx.cancelled` at checkpoints. The same `run_job` runs in `host` or `venv` mode.
- Reference implementations: `image_metrics/` (simplest), `merger/` / `splitter/` (CPU),
  `yolo/`, `sam3/`, `timm/`, `importer/`. Every long-running plugin uses `run_job(ctx)` now.
  Do **not** hand-roll a queue, `GpuJob`, `get_active_jobs`, or job-schema translation — all of
  that is host-provided. (The old `AsyncJobStore` CPU pattern is gone; `merger`/`splitter` were
  migrated to `run_job`.)

**Routes** (custom REST):
- Follow `sam3/routes.py` or `timm/routes.py`: a module-level `get_route_handlers()` returning
  bare `@get`/`@post` handlers with **relative** paths (no `Controller`, no `/api/plugins` prefix,
  `sync_to_thread=True` for blocking work). The plugin class delegates to it.
- They resolve under `/api/plugins/<plugin-name>/...` via the host's per-plugin app + catch-all.
  Do **not** mount a `Controller` at an absolute path and do **not** add a `@get("/ui")` — there
  is no shadowing to work around anymore.
- CRUD for configs: relative `GET /configs`, `POST /configs`, `GET /configs/{id}`, `POST /configs/{id}/delete`
- Do **not** add `GET /queue` / `POST /cancel/{id}` / a job-start route — long-running work
  goes through the generic `POST /api/plugins/{id}/run`, `GET /api/plugins/jobs`, and
  `POST /api/plugins/jobs/{job_id}/cancel`. Custom controllers carry only genuinely
  plugin-specific routes (config CRUD, metadata lookups, column detection, …).

**Error response convention:**
- Pre-validation errors (bad input): return `{"error": "description"}`
- Execution results (job completed): return `{"success": true/false, "message": "..."}`
- Not found: raise `HTTPException(status_code=404)`

**URL Aliases** (if plugin creates tables from image folders):
- Use shared utilities: `from tlc_plugin_sdk.shared.aliases import register_alias`
- Call `register_alias(project_name, image_folder, alias_token)` after table creation
- Shared UI component: `from tlc_plugin_sdk.shared.alias_ui import alias_ui_script`
- Inject into the fragment with `inject_scripts()`, never `str.replace`:
  `from tlc_plugin_sdk.shared.ui_inject import inject_scripts` then
  `self._ui_cache = inject_scripts(raw, alias_ui_script())`. `inject_scripts(html, *scripts)`
  appends into the fragment's first **real** inline `<script>` (skipping comments / `src=`)
  and raises if there is none — the old `raw.replace("<script>", …)` matched a `<script>`
  inside a comment and silently injected into the wrong place. See `plugins/yolo/__init__.py`.
- In the UI, call `_tlcAliasSettingsHtml(prefix, project, folder)` to render the alias form
- Call `_tlcBindAliasToggle(prefix)` and `_tlcBindAliasAutoUpdate(prefix, projectInputId, folderInputId)`
- At submit time, call `_tlcGetAliasValues(prefix)` and include in the POST body
- After programmatic form fills (e.g. config load), call `_tlcSyncAliasFromForm(prefix, projectId, folderId)`
- See `importer/__init__.py` and `sam3/runner.py` for working examples

**SocketIO** (if real-time updates needed):
- `socketio_namespace` is optional — it defaults to `/<plugin-id>`, which the host
  auto-registers at startup (no shared file changes); declare it in the manifest's
  `[runtime]` only to override that default. Either way the UI can subscribe to the generic
  `job_update` channel; you do **not** need to emit anything custom.
- Prefer the `window.PluginJobs` client over a hand-rolled socket — see
  [Real-Time Updates](#real-time-updates-windowpluginjobs--custom-events).
- Use `ctx.emit(name, payload)` only for telemetry the generic schema can't express
  (e.g. a loss curve); never re-emit the generic lifecycle by hand.
- `socketio_runner_module`/`socketio_runner_fn` are legacy and unnecessary for `run_job`
  plugins.

### Common Mistakes to Avoid

- Don't add a `register()` call or metadata class attributes — the manifest is the only metadata source
- Don't add plugin-specific logic to the frontend (templates, JS modules)
- Don't use bare `fetch()` in the UI — always use `PLUGIN_API.authFetch()`
- Don't hardcode colors — use CSS variables
- Don't forget to handle errors in both backend and frontend
- Don't create new shared utilities for one-off operations
- Don't duplicate the alias UI HTML/JS — use `tlc_plugin_sdk/shared/alias_ui.py`, injected at serve time via `inject_scripts()`
- Don't grab `get_gpu_queue()`, build a `GpuJob`, or push a closure — implement `run_job(ctx)`
- Don't implement `get_active_jobs`/`cancel_job` or add `/queue`,`/cancel` routes — host-owned
- Don't `async`-define `run_job` — it runs synchronously on a host/worker thread; poll `ctx.cancelled`

### Testing

- Add tests in `compute-service/tests/test_<plugin_name>.py`
- Use the TestClient factory from `conftest.py`
- Test both success and error paths for all endpoints
- For `run_job(ctx)`, drive it with a fake `JobContext` (a recording sink + a
  `threading.Event` for cancel) and assert the emitted `progress`/`metric`/`result`
  events — no GPU/queue needed. See `tests/test_venv_jobs.py` for the manager-level path.
