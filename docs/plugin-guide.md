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
                               │  │  ├── compute()           ││ ← override (default)
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

### 2. Write the manifest

All metadata lives in a manifest — a standalone `plugin.toml` next to `__init__.py`. The host
reads this **without importing** the plugin, builds a "card" from it, and uses it as the single
source of truth for listing, routing, GPU/CPU classification, SocketIO wiring, and auth-exempt
paths. (`read_manifest()` also accepts a `[tool.tlc-compute]` table in a plugin's `pyproject.toml`
— it checks `plugin.toml` first.)

A plugin keeps the **same `plugin.toml`** for metadata and adds a separate
`pyproject.toml` alongside it that declares only its venv's dependencies (no
`[tool.tlc-compute]` table there) — see the `timm` / `sam3` / `yolo` plugins for the canonical
layout.

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
# 16x16 SVG, inline in the manifest:
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
isolation = "venv"                  # "venv" is the only value (and the default when absent)
entrypoint = "tlc_plugin_my_plugin:MyPlugin"  # "pkg.module:ClassName"
requires_gpu = false                # drives GPU vs CPU classification
provision_extra = "my-plugin"       # your plugin's dependency group: host runs `uv sync --extra <this>`
# The plugin's SocketIO namespace is host-derived as "/<plugin-id>" and registered at
# startup — it is NOT declarable in the manifest (a plugin emits via ctx; the host owns
# the transport).
```

**Other keys the host reads** (all optional, read without importing the plugin):

- top-level: `kind = "compute"` (default) or `"infrastructure"`. An **infrastructure
  plugin** provisions GPU nodes instead of computing: it is an ordinary venv plugin
  (sidebar fragment for its configuration, config store for provider credentials) that
  additionally serves the conventional node-CRUD routes the host's infra manager calls
  through the worker proxy — `GET /infra/capabilities`, `POST /infra/nodes`,
  `GET /infra/nodes/{provider_id}`, `DELETE /infra/nodes/{provider_id}`. At most one
  infrastructure plugin is active on a host at a time. `capabilities` should include
  `storage: {"project_root_url": "s3://…"}` when configured — the host's data-prepare
  pipeline (sync + node staging) and the Dashboard union view both key off it.
- `[runtime]`: `auth_exempt_paths` (relative subpaths served without auth, scoped to the
  plugin's own subtree), `training` (marks a training plugin), `python` / `venv_python`
  (pin the interpreter the plugin's venv is built with).
- `[ui]`: `min_input_count` (minimum selected resources an `action` plugin needs — defaults to
  `len(input_types)`; set `0` explicitly to require none), `action_param_names` (query params
  passed through from the action launch), `quick_action_label` / `quick_action_description`
  (dashboard quick-action copy).

`runtime.provision_extra` names the **optional-dependency group** the host installs into your
plugin's venv (`uv sync --extra <that-value>`, or folded into the pip spec for a distribution
install). Keeping a plugin's dependencies behind an extra rather than in the base does two things:
a bare install of the distribution stays light — enough to *discover* the plugin without pulling
its whole stack — and one distribution can carry several plugins, each selecting its own extra.
For first-party plugins each value is a per-plugin extra in the `3lc-compute-plugins` umbrella
`pyproject.toml`. It is optional in the sense that a plugin needing nothing beyond the SDK (which
brings `tlc`) may omit it and still gets its own managed venv with just the base dependencies — but any
plugin sharing an umbrella declares one, since that is how its own dependencies are selected.

Every plugin runs in its own uv-managed venv, behind a worker the host spawns and talks to
over a Unix socket — the host registers the plugin from its manifest alone and never imports
its code. Isolation is venv-only: there is no in-process/host mode, and the venv is always one
the host builds and owns (never a `.venv` beside your source). `requires_gpu` is the manifest's **only placement knob**: `true` routes the job
through the shared GPU queue (one GPU job at a time, across every plugin); `false` jobs run
on the CPU queue. Both are host-owned; the plugin never picks a queue or names a lane.

### 3. Implement the plugin object

A plugin is a **subclass of `ComputePlugin`** (imported from `tlc_plugin_sdk`) —
there is no `register()` call. You must implement the one abstract method,
`get_ui_fragment()`; `id` is hydrated onto the instance from the manifest by the host.
Everything else — `compute()`, custom routes, jobs, lifecycle hooks — ships as a safe
default on the base, so you override only what you need and the host calls every hook
directly. (`compute()`'s default returns an error dict; implement it only if you expose a
synchronous `GET /compute` endpoint.)

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

There is **nothing to register**. On startup, the host scans the plugin directories
for manifests (no imports), builds a card from each, and gates compatibility against the
service version. When a plugin is actually needed, its **worker** imports the module named
in the manifest's `runtime.entrypoint` and instantiates the class inside the plugin's own
venv — the host never imports plugin code.

Because metadata is read without any import, a plugin whose environment is broken (or whose
manifest is invalid) still **lists** (greyed-out with a reason) instead of vanishing.

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
> /// <reference types="3lc-compute-plugin-sdk/contract/plugin-api" />
> var API = window.PLUGIN_API;   // now typed
> ```
>
> That file declares the browser-side contract — versioned by the single
> `SDK_CONTRACT_VERSION` (see "Version & Compatibility" below). The 3LC Hub frontend
> **implements** `PLUGIN_API` when it mounts a fragment; `window.PluginJobs` **ships from this
> package** (auto-injected by the host, layered on top of the bridge, not part of it).

### How a fragment reaches the browser

The frontend is a thin Flask + Jinja2 *shell* that renders page skeletons and does **all**
data fetching client-side — it holds zero plugin knowledge and never proxies plugin data.
The mount lifecycle:

```
Browser (3LC Hub frontend, vanilla JS)              Compute service (:5020)
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

A plugin fragment is plain HTML+JS+CSS, served by the plugin's worker and reverse-proxied
by the host — the frontend can't tell where it came from. `PLUGIN_API` is the **single** host→fragment JS contract; a fragment should
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
  location: TlcLocation,              // Location renderers (chips/labels for project roots) — SDK 0.2+

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
- **`location`** (SDK 0.2+) exposes the host's shared location renderers (`TlcLocationApi`):
  chips and labels for the project roots / scan URLs that tables, runs, and projects from
  `PLUGIN_API.data` resolve to (their `location` / `locations` fields, also 0.2). Every renderer
  returns `''` on single-root installs, so output can be concatenated unconditionally. Feature-detect
  (`PLUGIN_API.location && ...`) — hosts predating 0.2 set neither the member nor the data fields.

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
prefix. The host serves them through the plugin's own app in its worker, behind the generic
`/api/plugins/{id}/{subpath}` catch-all.

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
`tlc_plugin_image_metrics/routes.py` in the `3lc-compute-plugins` repo for the simplest
real example.

---

## Long-Running Jobs (`run_job(ctx)`)

A plugin with a long-running task (training, inference, import) **declares** the job
in its manifest and **implements** it as `run_job(ctx)`. It does **not** grab a queue,
push a closure, or poll a shared `cancel_flag` — the host owns the queue, the GPU/CPU
slot lease, progress fan-out, listing, and cancellation. `run_job` runs in the plugin's
worker and only ever touches `ctx`.

**Declare the job in the manifest.** `requires_gpu` is the only knob:

```toml
[runtime]
isolation = "venv"
entrypoint = "tlc_plugin_my_gpu_plugin:MyGpuPlugin"
requires_gpu = true                 # → routed through the shared GPU queue (1 at a time)
provision_extra = "my-gpu-plugin"   # venv deps installed via `uv sync --extra <this>`
# SocketIO namespace is host-derived as "/my-gpu-plugin" — not declarable here
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

        ctx.result(created_table_url)             # the one "open result" link (run or table URL)
        # Raise to fail the job (or ctx.fail("message") for a clean, user-facing message) —
        # the host records the error and ends the stream.
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
| `ctx.result(url)` | The canonical result link the Open button opens — a run *or* a table URL (last write wins). |
| `ctx.fail(message)` | Fail the job with a clean, user-facing message (raises `JobFailed`; reported verbatim, no type prefix). |
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

### Run-body conventions for remote workers

A `requires_gpu` job may execute on a **remote GPU node**: the host derives a spec
pointing at a TCP worker there and dispatches over the same stream contract. Three
conventions make a plugin remote-ready — all optional locally, all host/plugin-additive:

- **Self-contained params (`project_config`).** A remote worker has none of the
  controller's local state — in particular, nothing saved by a worker-local config or
  project store exists on the node. If your `run_job` resolves an id against such a
  store, also accept the frozen configuration inline (recommended key:
  `project_config`) and prefer it when present; have your fragment always include it in
  the run body (harmless locally, required remotely).
- **`_alias_overrides`.** `{"enabled": true, "overrides": [{"token": "<MY_DATA>",
  "path": "s3://…"}]}` — per-job URL-alias overrides your `run_job` applies before
  touching data and restores after (see `tlc_plugin_sdk.shared.aliases.
  apply_alias_overrides`). The host's data pre-flight injects these so the same table
  resolves to node-readable storage on the node.
- **`run_target` is host-owned.** The run body may carry `run_target` (which node to run
  on); the host consumes it before params reach the worker — never read or set it in a
  plugin.

Remote TCP workers run token-guarded (`--token` / `TLC_WORKER_TOKEN`: every request must
carry `Authorization: Bearer <token>`) and may emit `{"event": "ping"}` keepalives on the
job stream (`TLC_WORKER_STREAM_KEEPALIVE_S`) so provider proxies don't kill quiet
streams; the host filters pings before events reach any consumer. Neither affects a
local Unix-socket worker.

---

## Real-Time Updates: `window.PluginJobs` + custom events

Every job already broadcasts a generic `job_update` SocketIO event on the plugin's
namespace, carrying the frontend's plugin-agnostic schema (`status`,
`progress.{percent,label,timing}`, `run_url`, `metrics[]`). A plugin's **own** `ui.html`
can be a second consumer of that same channel for a richer, tailored view — it needs **no
bespoke events** for the generic lifecycle (queued → running → done, %, result link,
metrics).

**The `window.PluginJobs` client** is a global the **host auto-injects** into every fragment
(its `/ui` handler prepends `job_tracker_script()`), so you can call it directly — no manual
injection needed. It is idempotent (`if (window.PluginJobs) return;`), so an older plugin that
still injects it by hand via `inject_scripts(raw, job_tracker_script())` keeps working. It
starts a job and tracks it over the generic channel:

```javascript
// The host registers the namespace automatically as "/<plugin-id>".
PluginJobs.run('my-plugin', { table_url: url }, {
  onUpdate: function (job) {
    // generic schema: job.status, job.progress.percent/label, job.metrics[]
    setProgress(job.progress.percent, job.progress.label);
  },
  onDone: function (job) { showResult(job.run_url); },
  onError: function (job) { showError(job.error); },  // failure message on job.error
});
```

`run()` pre-subscribes and buffers, so a job that finishes between the `/run` response and
the client subscribing still delivers its terminal event. (`PluginJobs.start/track/cancel`
are the lower-level pieces.) The separate frontend's generic panel polls the same schema
independently.

One timing rule to know: `track()` and `on()` open the namespace socket **lazily**, on first
use, and SocketIO does not replay server→client events to a client that was not yet
connected. `run()` connects before it posts, so the common path is safe — but a fragment that
listens for custom events from the first second, or re-attaches to a job it did not start
(seed-on-mount), should warm the socket on mount: `PluginJobs.connect('/my-plugin')`.

**Custom events** — `ctx.emit(name, payload)` is reserved for telemetry the generic schema
**can't** express (e.g. a training plugin's per-epoch loss curve). The host relays it
verbatim on the plugin's namespace; the generic panel ignores it. The name `job_update` is
reserved and rejected. A plugin should **not** open its own SocketIO connection — the host
owns the transport; a plugin only ever emits through `ctx`.

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

## The job page is a launcher — the Queue is the durable view

A plugin fragment is **torn down on navigation**: the host remounts it via `innerHTML`, so
its JS state and any live SocketIO subscription are gone the moment the user leaves and comes
back. The generic **Queue & Progress** panel is the opposite — it is host-owned, polls
`GET /api/plugins/jobs`, and survives navigation. So treat your fragment as the place a job is
*launched and configured*, and the generic Queue card as the place a job is *watched*.

Two obligations follow:

**1. Seed on mount from the durable job list.** `job_update` is live-only — a long job already
running when the fragment remounts has no event to catch until it next progresses, so the
fragment would show an empty form over a job that is very much alive. On mount, ask the host
what's running and render a compact running-state instead:

```js
PluginJobs.list('<id>').then(function (jobs) {
  var live = jobs.filter(function (j) { return j.status === 'queued' || j.status === 'running'; });
  if (live.length) renderRunningState(live[0]);   // compact "job running — watch it in the Queue"
});
```

**2. Keep the generic card meaningful.** If you emit custom progress for your own rich UI
(`ctx.emit`), keep `ctx.progress(percent, label, timing)` current *too* — the generic card is
the durable view, and a job that only drives a custom channel looks stalled there. Report the
one result the Open button opens with **`ctx.result(url)`** (a run or a table URL), and report
failures by **raising** (or `ctx.fail("message")` for a clean, user-facing message — it lands
on the card's `error`, without a `TypeError:`-style prefix). That is all the generic Queue
needs to render a good, navigation-proof view.

---

## Styling & UI Conventions

A plugin's `ui.html` gets the host's entire design system for free — and the host counts on it using that system rather than reinventing it. This section is the class catalog and the rules that keep every plugin looking like one product, in every theme.

### Why the shared classes are available (no import, no build step)

The frontend mounts a plugin by injecting its fragment straight into the host page — `containerEl.innerHTML = fragment` (see [How a fragment reaches the browser](#how-a-fragment-reaches-the-browser)). There is **no iframe and no shadow DOM**, so the fragment lands inside the host document and inherits its two stylesheets:

- `main.css` — the base design system (forms, buttons, cards, spinners, theme tokens)
- `plugin-common.css` — the plugin page scaffolding (hero, workflow, metric cards, viewer)

Write `class="btn btn-primary"` or `class="form-control"` and you get the fully themed, dark-mode-correct component with zero CSS of your own. The flip side: your fragment shares the global namespace. Prefix any bespoke class with your plugin id (`.myplugin-…`) and never restyle a shared class globally — you'd repaint every other plugin.

> **Only `main.css` and `plugin-common.css` are inherited.** Styles defined inside a host *template* — for example the `.pbadge` badges in `settings/plugins.html` — live in that page's own `<style>` block and are **not** available to a mounted fragment. Don't reach for them.

### Theme tokens — never hardcode a color

Every color, in both light and dark mode, comes from a CSS variable. Hardcoding a hex value produces a control that looks wrong the moment the user switches theme.

```css
color: var(--text);            /* primary text                    */
color: var(--text-secondary);  /* secondary text                  */
color: var(--text-muted);      /* de-emphasized / helper text     */
color: var(--accent);          /* accent (light mode #2a4a61)     */
background: var(--bg);         /* recessed page ground            */
background: var(--bg-card);    /* ELEVATED surface (raised cards) */
border-color: var(--border);
border-color: var(--border-light);
```

`--bg` and `--bg-card` are the pair that matters most: `--bg-card` is an *elevated* surface, `--bg` the *recessed* page ground. In dark mode they are visibly different depths, and swapping them inverts every affordance on the page (see **Cards**, below). Also available: `--accent-light` (tinted wash), `--error` / `--danger`, and the `--badge-<color>-bg` / `--badge-<color>-text` pairs. Never use the legacy teal `#5a9aad` — `var(--accent)` is the accent.

### Forms

Wrap each field in `.form-group`; label it with `.form-label`; use `.form-control` for text inputs and `<textarea>`, `.form-select` for `<select>`; add `.form-help` for a line of helper text. For multi-field layouts, `.plugin-form-grid` (2-col) and `.plugin-form-grid-3` (3-col) are responsive grids.

```html
<div class="form-group">
  <label class="form-label required">Dataset name</label>
  <input type="text" class="form-control" required placeholder="my-dataset">
  <div class="form-help">Lowercase, no spaces.</div>
</div>
```

- `.form-label.required` appends a red `*`.
- Placeholders render dimmed + italic automatically.
- **Required-field cue — worth adopting:** put both `required` **and** a `placeholder` on a required text field. While the field is empty (its placeholder still showing) the host paints it with a muted red wash (`:required:placeholder-shown`), which clears the instant the user types. A free "you still need to fill this in" signal — no JS.

### Buttons

Use `.btn` plus one variant. Never define your own button styles.

| Class | Use |
|---|---|
| `.btn` | base — required on every button |
| `.btn-primary` | the one main action (Run, Import) |
| `.btn-secondary` | secondary action |
| `.btn-ghost` | low-emphasis / borderless |
| `.btn-danger` | destructive |
| `.btn-plugin` | accent-styled plugin action |
| `.btn-sm` / `.btn-lg` | size modifiers, combine with the above |

### Page scaffolding (`plugin-common.css`)

| Class | Purpose |
|---|---|
| `.plugin-page` | max-width page container — wrap the whole fragment in it |
| `.plugin-hero` | intro banner (accent-tinted); put `<h2>` + `<p>` inside |
| `.plugin-hero-badge` | informational "what this does" tile — flat, recessed, **non-interactive** |
| `.plugin-workflow` / `.plugin-workflow-step` | numbered step strip (`.num` child is the circle) |
| `.plugin-param-group` / `.plugin-param-group-label` | titled section inside a card |
| `.plugin-form-grid` / `.plugin-form-grid-3` | 2- and 3-column responsive form layouts |
| `.plugin-action-bar` | submit/cancel row |
| `.plugin-config-item` | selectable list item |
| `.plugin-metric-card` (+ `-label` / `-value`) | scalar metric tile |
| `.plugin-progress-wrap` + `.plugin-progress-bar` | progress bar |
| `.plugin-log-area` | monospace log output |
| `.plugin-two-col` / `.plugin-three-col` | sidebar + main (+ preview) layouts |
| `.plugin-viewer-card` (+ `-header` / `-toolbar` / `-viewport` / `-footer`) | image viewer shell |
| `.spinner` / `.spinner-lg` | loading spinner |

### Cards: interactive vs. informational — the one distinction to get right

Two kinds of tile look superficially similar but must read differently:

- **Interactive / selectable** — something the user clicks or picks (a config item, a selectable tile). These sit on the **raised** surface: `--bg-card` + a drop shadow, with a `.selected` / `.active` state. The generic `.card` / `.card-header` / `.card-title` / `.card-body` panel is the standard raised container for config forms.
- **Informational** — pure display, not clickable (the hero "what this does" badges). These stay **flat and recessed**: use `.plugin-hero-badge`, which the host styles on `--bg` with **no shadow**. That flatness is exactly what marks it as "not a button".

Get these backwards and a static badge invites a click it won't answer. The mistake is nearly invisible in light mode and obvious in dark mode, where `--bg-card` is a distinctly elevated surface — so **verify your plugin in dark mode.**

> **Don't inline surface styles.** `.plugin-hero-badge` carries its whole look, so write `class="plugin-hero-badge"` and nothing else. Several plugins once inlined `style="background:var(--bg-card);box-shadow:…"` on it, and because inline styles outrank the host stylesheet, those badges kept the raised, clickable look the class was written to remove — the exact dark-mode bug above. Keep background, elevation, and color out of your `style=` attributes and let the class do the work. (`.plugin-hero-badge-card` is a deprecated alias kept only for backward compatibility — prefer `.plugin-hero-badge` alone.)

### Page structure

A sidebar plugin UI follows this shape:

```html
<style>
  /* Plugin-specific rules only — layout for your own elements. Everything
     visual (surface, color, elevation) comes from the shared classes. */
  .myplugin-badges { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 12px; }
</style>

<div class="plugin-page">
  <div class="plugin-hero">
    <h2>🎯 Plugin Name</h2>
    <p>Short description of what this plugin does.</p>

    <!-- Informational badges — flat, non-interactive -->
    <div class="myplugin-badges">
      <div class="plugin-hero-badge">Fast</div>
      <div class="plugin-hero-badge">No config</div>
      <div class="plugin-hero-badge">Exports CSV</div>
    </div>

    <!-- Numbered workflow (optional) -->
    <div class="plugin-workflow">
      <div class="plugin-workflow-step"><span class="num">1</span> Configure</div>
      <div class="plugin-workflow-step"><span class="num">2</span> Run</div>
      <div class="plugin-workflow-step"><span class="num">3</span> Results</div>
    </div>
  </div>

  <!-- Main form -->
  <div class="card">
    <div class="card-header"><span class="card-title">Configuration</span></div>
    <div class="card-body">
      <div class="plugin-param-group">
        <div class="plugin-param-group-label">Source</div>
        <div class="form-group">
          <label class="form-label required">Table name</label>
          <input id="myplugin-name" type="text" class="form-control" required placeholder="my-table">
          <div class="form-help">The table to analyze.</div>
        </div>
      </div>
      <div class="plugin-action-bar">
        <button id="myplugin-run" class="btn btn-primary">Run</button>
        <span class="spinner" style="display:none"></span>
      </div>
    </div>
  </div>

  <!-- Results area (appears after execution) -->
  <div id="myplugin-results" class="card" style="display:none"></div>
</div>
```

### Visual rules

1. **Container** — wrap the fragment in `.plugin-page`.
2. **Hero** — `.plugin-hero` with icon + title + description, optionally three `.plugin-hero-badge` tiles.
3. **Accent** — `var(--accent)`; never the legacy teal `#5a9aad`.
4. **Cards** — group content in `.card`; raised for interactive, flat `.plugin-hero-badge` for informational.
5. **Forms** — `.form-group` + `.form-control` / `.form-select`, laid out with `.plugin-form-grid` where useful.
6. **Buttons** — `.btn` + one variant; the primary action gets `.btn-primary`.
7. **Spinners** — `<span class="spinner"></span>` (defined in `main.css`).
8. **Toasts** — `PLUGIN_API.showToast(msg, 'success'|'error'|'info')` for feedback.
9. **Dark mode** — must work; only `var(--*)` colors, and confirm the raised/flat card distinction actually reads.

### What NOT to do

- Don't hardcode colors — always a `var(--…)` token.
- Don't inline surface/elevation styles (`background`, `box-shadow`) on shared classes — let the class do it.
- Don't define custom button, input, or card styles — the shared classes exist.
- Don't restyle a shared class globally, and prefix your own classes with the plugin id.
- Don't reach for host-template-only classes like `.pbadge` — they aren't inherited.
- Don't create custom modal/dialog implementations — use `.card` with show/hide.
- Don't use `position: fixed` — it breaks the plugin container.
- Don't add font sizes below 10px or custom scrollbars.
- Don't skip a dark-mode check.

---

## Existing Plugins Reference

| Plugin ID | display_mode | Section | GPU | Description |
|---|---|---|---|---|
| `importer` | sidebar | Data Ops | — | Import data (YOLO, COCO, Folder, CSV, Unlabeled) |
| `exporter` | sidebar | Data Ops | — | Export tables to CSV, XLSX, YOLO, COCO |
| `splitter` | sidebar | Data Ops | — | Split tables into train/val/test sets |
| `merger` | sidebar | Data Ops | — | Merge 2 tables by column join |
| `image-metrics` | sidebar | Data Ops | GPU | Image quality metrics (brightness, sharpness, noise, etc.) |
| `yolo` | sidebar | AI Tools | GPU | Ultralytics YOLO training + metrics collection |
| `sam3` | sidebar | AI Tools | GPU | Auto-labeling with SAM3/GroundingDINO |
| `timm` | sidebar | AI Tools | GPU | Image classification with timm models |
| `table-statistics` | hidden | Analysis | — | Per-column stats & image thumbnails (API-only) |
| `run-insights` | action | Analysis | — | Run statistics, health scores, per-class metrics |
| `table-insights` | action | Analysis | — | GT-only data quality analysis (bbox sizes, balance, etc.) |

---

## The Dev Loop (Development)

The fast edit-run cycle is a **folder Source + an editable venv + a worker restart** — no
service restart, no rebuild:

1. Register your checkout as a folder Source (point `--plugin-dir` / the Settings page's
   plugin-directories UI at the directory holding your plugin folder). The host provisions
   the plugin's venv with `uv sync`, which installs your project **editable** — so the venv
   always runs the code on disk.
2. Edit your plugin files.
3. Retire the worker; the next request cold-starts on the current code (sub-second):

```bash
curl -X POST http://localhost:5020/api/admin/plugins/my-plugin/worker/stop
```

Or from the browser console:
```javascript
TlcApi.authFetch(TlcApi.computeServiceUrl + '/api/admin/plugins/my-plugin/worker/stop', {method:'POST'})
  .then(r => r.json()).then(console.log)
```

For **dependency** changes (a new package, a version bump in your `pyproject.toml`), rebuild
the venv instead — `POST /api/admin/plugins/my-plugin/reload` runs `uv sync --reinstall` in
the background and retires the worker when the rebuilt venv swaps in. Running jobs in other
plugins are unaffected either way.

---

## Version & Compatibility

All versions use **SemVer** (`MAJOR.MINOR.PATCH`).

### Two kinds of version — never conflate them

There are **two independent** kinds of "version" in play. Keep them separate:

**(a) CONTRACT — what a plugin *programs against*.** Pinned at build/install time via the
`3lc-compute-plugin-sdk` dependency. There is **one contract axis**, `tlc_plugin_sdk.SDK_CONTRACT_VERSION`:

| Constant | Covers | Value |
|----------|--------|-------|
| `SDK_CONTRACT_VERSION` | the whole contract — the Python surface (`ComputePlugin` / `JobContext` / `shared.*`) *and* the browser surface (`PLUGIN_API` / `PluginJobs` / `TlcData`, see `plugin-api.d.ts`); also the dependency *pin* (`3lc-compute-plugin-sdk>=X,<Y`) | = package version (e.g. `0.3.0`) |

It is this package's own version — one source of truth. A plugin that needs a newer capability
raises its `3lc-compute-plugin-sdk` floor; the host and frontend implement a range and compare
compatibility on `MAJOR.MINOR`. (Earlier 0.x lines split this into separate `PY_CONTRACT` /
`JS_CONTRACT` markers; those were removed in 0.3 — there is one axis now.)

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
min_service_version = "0.1.0"   # Minimum compute service version required
max_service_version = ""        # Maximum service version (empty = no upper bound)
min_frontend_version = "0.1.0"  # Minimum frontend version for this plugin's UI
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

### Plugin catalog fields

The manifest reserves fields the host's plugin catalog uses for update signaling:
- `update_available`: latest version listed in the host's configured catalog
- `changelog_url`: link to a changelog
- `upgrade_required`: if true, the plugin must be upgraded to continue
- `repository_url`: where the plugin's source lives

A host without a configured catalog leaves them empty.

## Publishing via a Catalog

Dropping a `plugin.toml`-bearing folder into a host's plugin directory works great for your
own dev loop, but it doesn't scale to "let a few other people try this." For that, publish a
**catalog** — a single static JSON document a host can point at to discover your plugin
without anyone touching that host's filesystem or building a wheel.

### Minimal catalog.json

A catalog lists one or more plugins, each with one or more installable versions. The
`manifest` field is just your `plugin.toml` (`[tool.tlc-compute]` table), reproduced as JSON,
so the host can list it and check compatibility **without** importing or downloading
anything:

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-31T00:00:00Z",
  "plugins": [
    {
      "id": "my-plugin",
      "versions": [
        {
          "version": "1.0.0",
          "source": "github:your-org/your-repo@v1.0.0",
          "manifest": {
            "id": "my-plugin",
            "name": "My Plugin",
            "version": "1.0.0",
            "min_service_version": "0.1.0",
            "ui": { "display_mode": "sidebar", "section": "Tools" },
            "runtime": { "isolation": "venv", "entrypoint": "tlc_plugin_my_plugin:MyPlugin" }
          }
        }
      ]
    }
  ]
}
```

### The `source` field

`source` is the install spec the host hands off unchanged — it accepts the same forms
whether or not your plugin has a published wheel:

| Your plugin is... | `source` looks like |
|---|---|
| a plain git/GitHub repo, no published wheel (the common case for a one-off or a plugin you're sharing to test) | `"github:your-org/your-repo@v1.0.0"` or `"git+https://github.com/your-org/your-repo.git@v1.0.0"` |
| published to a package index | `"my-plugin==1.0.0"` or `"my-dist[extra]==1.0.0"` |

Most hosts default to a conservative install policy that only trusts sources a **catalog**
names — a bare git URL typically needs an operator to explicitly loosen that policy first.
Wrapping your repo in a catalog like the one above is the normal path for a git-hosted
plugin, not a workaround.

### Trying it out

Host the JSON somewhere reachable — a raw GitHub file URL is the easiest — and point a
tester's Hub at it: **Settings → Plugins → Catalogs → add URL**. Your plugin then shows up
as an installable card; Install resolves whatever `source` you declared. (A local `file://`
path works too for testing on your own machine; plaintext `http://` is only accepted for a
loopback host.)

If your entry doesn't show up after that, check that the tester's compute-service version
(on `GET /health`) is recent enough to support catalogs.

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
- [ ] UI follows the page structure and card conventions from "Styling & UI Conventions" above
- [ ] Hero section with icon, title, description, and 3 feature badges
- [ ] Config bar if plugin has saved configurations
- [ ] Dark mode works correctly (no hardcoded colors)

---

## For AI coding agents

Agent-facing guidance for building a plugin end-to-end (reading order, step-by-step,
code patterns, common mistakes, testing) lives in this repo's
[`CLAUDE.md`](https://github.com/3lc-ai/3lc-compute-plugin-sdk/blob/main/CLAUDE.md).
