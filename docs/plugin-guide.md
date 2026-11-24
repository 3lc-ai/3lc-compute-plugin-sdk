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

`ComputePlugin` is one of three bases in `tlc_plugin_sdk`: `HubPlugin` (the fragment,
optional `compute`, routes, shutdown) for a plugin that never runs a job, `ComputePlugin
(HubPlugin)` for one that does, and `InfrastructurePlugin(HubPlugin)` for a provider that
creates nodes ([Infrastructure plugins](#infrastructure-plugins)).

Two repos go with this guide: the
[**plugin template**](https://github.com/3lc-ai/3lc-compute-plugin-template) is the smallest
viable plugin as a GitHub template repository (with a `/create-plugin` skill for coding
agents), and the [**example plugin**](https://github.com/3lc-ai/3lc-compute-plugin-example),
Mission Control, is a runnable tour of every surface below with an annotated manifest and a
test suite. Both are on the 0.6 line, the contract the Hub and Compute Service 1.2 implement.

**Flow:**

1. Frontend calls `GET /api/plugins/` → gets manifests for all plugins
2. Sidebar and action buttons are rendered from manifests (no hardcoded plugin knowledge)
3. When user opens a plugin, frontend calls `GET /api/plugins/{id}/ui` → gets HTML fragment
4. The SDK runtime mounts the fragment in a separate Compute-origin frame with a `PLUGIN_API` bridge object
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
min_service_version = "1.2.0"       # Minimum compute service version: the one implementing the SDK line you pin (0.6 → 1.2)
# min_frontend_version = "1.2.0"    # Minimum Hub version, only when ui.html needs a Hub 1.2 feature (<tlc-table-field>, TlcPicker, showResult)
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
entrypoint = "tlc_plugin_my_plugin:MyPlugin"  # "pkg.module:ClassName"
requires_gpu = false                # drives GPU vs CPU classification
provision_extra = "my-plugin"       # your plugin's dependency group: host runs `uv sync --extra <this>`
# remote_capable = true             # a run may target a remote node the Hub manages although requires_gpu is false
# Custom routes that must run where the model lives when a host with remote-node support
# runs this plugin on a remote worker (inference, model warm-up/status). Path prefixes
# relative to your route root. Everything NOT listed stays with the controller's worker —
# config stores, model catalogs, table reads — so saved configs have one history. A host
# without remote-node support ignores the key.
# node_routes = ["/preview", "/model-status"]
# Services whose stored token (a SECRET Connection) this plugin may be given, and the custom
# routes that receive it. See "Tokens on runs and routes (SECRET)" below.
# credentials = [{ service = "huggingface", required = false }]
# credential_routes = ["/preview", "/model-warmup"]
# The run-body keys that carry data the run reads, and places it writes (dotted keys into the
# JSON body). The host plans and checks those values for the run target before the run. See
# "Data inputs and run targets" below.
# data_inputs = ["folder", "source.table_url"]
# data_outputs = ["output_path"]
# The plugin's SocketIO namespace is host-derived as "/<plugin-id>" and registered at
# startup — it is NOT declarable in the manifest (a plugin emits via ctx; the host owns
# the transport).
```

**Other keys the host reads** (all optional, read without importing the plugin):

- top-level: `kind = "compute"` (default) or `"infrastructure"`. An **infrastructure
  plugin** provisions nodes instead of computing: it subclasses `InfrastructurePlugin`
  (`tlc_plugin_sdk.infrastructure`) and answers the typed provider contract — see
  [Infrastructure plugins](#infrastructure-plugins). At most one infrastructure plugin is active
  on a host at a time. Also `repository_url` (shown on the plugins page) and
  `max_service_version` / `min_frontend_version` ([Version & Compatibility](#version--compatibility)).
- `[runtime]`: `auth_exempt_paths` (routes served without the auth middleware, for an asset an
  `<img>` fetches: **full paths under the plugin's own prefix**, `/api/plugins/<id>/…`, each
  naming a subpath — `"/api/plugins/example/patch.png"`; anything else is refused and listed on
  the plugin card as `auth_exempt_rejected`), `training` (marks a training plugin), `python` /
  `venv_python` (pin the interpreter the plugin's venv is built with; without one a host builds it
  on its own Python version, at most 3.14, so a plugin that supports 3.15 says `python = "3.15"`), `remote_capable` (a run
  may target a remote node even though `requires_gpu` is false).
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
the host builds and owns (never a `.venv` beside your source). `requires_gpu` is the manifest's **queue knob**: `true` routes the job
through the shared GPU queue (one GPU job at a time, across every plugin); `false` jobs run
on the CPU queue. Both are host-owned; the plugin never picks a queue or names a lane.
`remote_capable = true` lets a plugin that needs no GPU also run on a remote node (see
[Run-body conventions for remote workers](#run-body-conventions-for-remote-workers)).

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

- `PLUGIN_API` — the bridge object with context, API clients, and libraries (the samples
  below alias `PLUGIN_API.getConfig('compute_service_url')` as `COMPUTE_URL`)
- `PluginJobs` — the job-channel client, added to every fragment by the worker
- Shared design tokens and component classes supplied by the Hub (see
  [Styling & UI Conventions](#styling--ui-conventions); `python -m tlc_plugin_sdk.lint src/`
  checks a fragment against them)
- Vendor libraries through `PLUGIN_API.libs`; only `io` (Socket.IO) is stable

```html
<style>
.my-plugin-result { padding: 16px; font-size: 12px; color: var(--text); }
.my-plugin-result .count { font-size: 24px; font-weight: 700; color: var(--accent); }
.my-plugin-note { color: var(--text-muted); }
.my-plugin-error { color: var(--error); }
</style>

<div class="plugin-page my-plugin">
  <div class="card">
    <div class="card-header"><span class="card-title">My Plugin</span></div>
    <div class="card-body" id="my-plugin-body">
      <span class="spinner"></span> Analyzing...
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
    body.innerHTML = '<div class="my-plugin-note">No table selected.</div>';
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
        body.innerHTML = '<div class="my-plugin-error">' + data.error + '</div>';
        return;
      }
      body.innerHTML =
        '<div class="my-plugin-result">' +
        '<div class="count">' + data.row_count + '</div>' +
        '<div>rows across ' + data.columns + ' columns</div>' +
        '</div>';
    })
    .catch(function (err) {
      body.innerHTML = '<div class="my-plugin-error">Failed: ' + err.message + '</div>';
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
> **authorizes** bridge calls; this SDK **implements** the frame runtime and installs
> `PLUGIN_API.jobs` before fragment scripts run. `window.PluginJobs` is a compatibility alias.

### How a fragment reaches the browser

The frontend is a thin Flask + Jinja2 *shell* that renders page skeletons and does **all**
data fetching client-side — it holds zero plugin knowledge and never proxies plugin data.
The mount lifecycle:

```
Browser (3LC Hub frontend, vanilla JS)              Compute service (:5020)
  │  user opens /plugin/{id}  (Flask route → plugin_host.html)
  ├─ TlcPlugins.mountPlugin(id, el, ctx) ───────▶  GET /api/plugins/{id}/ui  → HTML fragment
  │     1. Open a sandboxed frame on the Compute origin
  │     2. Transfer a private MessagePort and initialization data
  │     3. SDK installs PLUGIN_API and evaluates fragment scripts inside the frame
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

### Optional interactive guide

The Hub can supply `PLUGIN_API.guide`, an optional browser-only hook. No Python hook,
compute-service endpoint, plugin allowlist, or guide manifest is needed. The plugin owns its
instructions and registers them after its UI fragment mounts. Feature-detect the member so
the same plugin works on hosts without a guide:

```javascript
var api = window.PLUGIN_API;
var guide = api.guide && api.guide.version === 1 ? api.guide : null;
if (guide) guide.register([
  {
    id: 'inputs',
    target: '[data-guide="inputs"]',
    title: 'Choose input data',
    body: 'Select the table revision this operation should use.',
    task: 'Check the revision before starting.',
    experience: 'revisions'
  },
  {
    id: 'results',
    target: '[data-guide="results"]',
    title: 'Inspect the result',
    body: 'Review the output before using it in another operation.'
  }
]);
```

`guide.register(tips)` replaces this mount's tips in the supplied order. Targets are CSS
selectors scoped to the plugin container, not the whole page. Missing or hidden targets
wait until visible, so asynchronous results can be registered before they arrive.
A nonempty valid registration replaces the Hub's generic tips on this plugin's page,
including the AI/Data category explanation. This holds while targets are still hidden:
the guide waits for your UI instead of showing duplicate generic instructions. An empty
registration or disposal restores the generic fallback. Hub-wide navigation guidance is
unaffected. A runtime widget on a different page adds only its own scoped tips.
`guide.complete(id)` marks a known tip as seen; it neither changes user settings nor
claims that a task succeeded. `guide.dispose()` invalidates the handle. The host also
invalidates handles on fragment replacement or unmount, so late callbacks cannot alter
another mount's guidance. Capture the bridge at initialization rather than looking up the
global `PLUGIN_API` inside a later callback.

The host accepts up to 12 tips per registration and eight live registrations. IDs are
1–48 ASCII letters, digits, underscores, or hyphens and are automatically namespaced by the
plugin ID. Use static IDs, never credentials, URLs, project names, or sample identifiers.
Selectors have a 200-character limit. Titles, body text, and optional task text are bounded
at 90, 700, and 260 characters. Content is plain text. No callbacks, HTML, or navigation URLs
are accepted. Registering tips does not enable a guide the user paused or turned off.

The optional `experience` selects a host-owned explanation, such as `revisions`, `models`,
`dashboard`, `workspace` (the Deployments guide), `storage`, `ecosystem`, `cycle`, `notebooks`, `infrastructure`,
`extend`, `overview`, or `insights`. Unknown names safely use text-only guidance. Plugins do
not inject animation code through this API. This feature does not require a higher compute
service version; the optional member itself is the capability check.

The optional guide hook is unavailable in the isolated frame runtime. Feature-detect it; do not access the Hub DOM as a fallback.

### The bridge object

The SDK installs `window.PLUGIN_API` before running the fragment's scripts.
The complete namespaced surface, examples, errors, capabilities, cleanup and
migration instructions are in [Plugin browser runtime](browser-runtime.md).
The shipped `contract/plugin-api.d.ts` provides the exact signatures.

Prefer `API.http.request`, `API.jobs`, `API.pickers`, `API.credentials`,
`API.nodes`, `API.run`, `API.nav`, `API.ui` and `API.lifecycle`.
`getConfig`, `getRunTarget`, `onRunTargetChange`, `dashboardUrl`, `data`, `libs`,
`getIcon`, `container` and `showResult` remain available. Feature-detect optional
members and libraries. `guide` and `location` are unavailable in the frame.

`http.request` forwards allowed requests to the Hub; it never exposes credentials.
Requests reject on refusal, timeout or transport failure. Handle errors in the UI.
Long work uses the job API, not a long HTTP request. Unmounting the page must never
cancel a job. Local listeners and widgets belong in `lifecycle.onDispose` cleanup.

### Common patterns

```javascript
// Authenticated fetch to your custom endpoint
PLUGIN_API.authFetch(COMPUTE_URL + '/api/plugins/my-plugin/analyze', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ url: tableUrl }),
}).then(function(r) { return r.json(); });

// Draw an outcome the Hub's way (Hub 1.2+; feature-detect)
if (PLUGIN_API.showResult) PLUGIN_API.showResult(el, { success: true, message: 'Done', details: {...} });

// Create a Chart.js chart (best-effort lib — be ready to vendor your own)
var Chart = PLUGIN_API.libs.Chart;
new Chart(document.getElementById('my-canvas'), {
  type: 'bar',
  data: { labels: [...], datasets: [...] },
});

// Listen for a plugin-private ctx.emit event on the plugin's namespace
PluginJobs.connect('/my-plugin');   // on mount, so nothing emitted early is missed
var off = PluginJobs.on('/my-plugin', 'progress_detail', function (data) { ... });
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
real example, and the example plugin's `routes.py` for one of every response shape: a JSON
collection, a path parameter with a real 404, a binary PNG (auth-exempt for an `<img>`), a
raw body, and the Hub's `{success, message, details}` result that `PLUGIN_API.showResult`
draws.

Test them without a compute service: `tlc_plugin_sdk.harness.PluginHarness.from_manifest(dir)`
builds the same app the worker serves and calls it in-process (`h.get("/status")`,
`h.post("/analyze", json_body={...})`), including `/compute` and `/ui` — see
[Testing a plugin](#testing-a-plugin) and the example plugin's `tests/`.

---

## Infrastructure plugins

An infrastructure plugin (`kind = "infrastructure"`) is a provider: it lets 3LC into a cloud
account and starts things there. The contract serves three goals a person has on any cloud
(connect to cloud storage, start a GPU node, start a managed deployment) and a fourth they stand
on, connecting an account. Everything above the plugin (the Compute Service, the Hub, the Config
Service) handles a provider's values as data it stores and shows and never interprets: your
plugin declares what an account is and what a person is asked for, and your plugin is the only
code that reads the answers.

The host owns the node registry, the lifecycle state machine, heartbeats, idle teardown, job
dispatch, grants, deployment names and DNS records. You own the provider's API. The host calls
your plugin through the worker proxy on the `/infra/*` routes the SDK mounts for you.

The contract is specified in full, with every type and route, in
[Infrastructure provider contract](provider-contract.md). This chapter is how to build against it.

### Which methods to write

Subclass `InfrastructurePlugin`, write `capabilities()`, and add one mixin per group of
operations you offer. A mixin's abstract methods are what you must write; every other method is
optional, and **overriding one is how you offer it**: the SDK mounts a route only for an
operation your class provides and lists exactly those in `capabilities.operations`. A host calls
an operation only when it is listed, so you never serve a route you did not write.

**(a) Nodes only.** A provider that starts machines and needs no account (a list of machines
reached over ssh):

```python
from tlc_plugin_sdk.infrastructure import (
    CapabilitiesResponse, CreateNodeRequest, CreateNodeResponse, InfrastructurePlugin,
    NodeCapabilities, NodeProvider, NodeStateResponse,
)


class MyProvider(InfrastructurePlugin, NodeProvider):
    def get_ui_fragment(self) -> str:
        return "<div>…</div>"

    def capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse(
            provider="myprovider",          # equals the plugin id
            display_name="My provider",
            nodes=NodeCapabilities(kinds=["gpu"], types=["box-1"], storage_access=["ambient"]),
            **self.readiness(),             # ready + missing_fields from the settings layer
        )

    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse: ...   # idempotent on node_id
    def node_state(self, provider_id: str) -> NodeStateResponse: ...
    def delete_node(self, provider_id: str) -> NodeStateResponse: ...              # idempotent
```

Optional in this group: `check_node(request)` (funds, stock, quota before a start) and
`node_diagnostics(provider_id)` (startup diagnostics from a console or boot log).

**(b) Nodes and connect.** A provider whose nodes live in a customer's account also declares the
account and how 3LC gets in, and writes the connect methods it can answer:

| Declare in `capabilities()` | Write | Offers |
|---|---|---|
| `account`, `access_methods`, `permissions`, `requires_connection=True` | `verify_connection(request)` | `connection.verify`: who the Connection is, as the cloud says |
| an access method with `grants=True` | `describe_grant(request)` | `connection.grant`: what the customer runs in their own account |
| | `prepare_grant_update(request)` | `connection.grant_update`: only for a cloud with a reviewed change |
| | `connection_node_access(request, apply=)` | `connection.node_access`: a scoped node identity set up from an own sign-in |
| `permissions.storage` | `storage_candidates()` | `storage.candidates`: locations a rule editor offers |
| a `KEYLESS` method | `connections.register_resolver("myprovider", resolver)` at import | the exchange into the account |

Add `StorageFacet` for storage the Data page fills and browses, and `CatalogFacet` for live
offers (`catalog(request)`, and optionally `locations(node_type=)`).

**(c) Deployments.** A provider that hosts managed deployments adds `DeploymentProvider` and
declares `deployments=DeploymentCapabilities(...)`:

```python
class MyProvider(InfrastructurePlugin, NodeProvider, DeploymentProvider, CatalogFacet):
    def create_deployment(self, request: DeploymentRequest) -> DeploymentResponse: ...    # idempotent on deployment_id
    def deployment_state(self, provider_id: str, *, diagnostics: str = "") -> NodeStateResponse: ...
    def delete_deployment(self, provider_id: str) -> NodeStateResponse: ...               # idempotent
```

Optional: `deployment_options(selected)` (placement fields, narrowed by what is selected),
`check_deployment(request)` (provider-side refusals and the shared-ingress key),
`prepare_deployment(request)` (what deployments share, one step per call),
`retain_deployment(provider_id)`, `list_deployments(owner=)` and `sweep_deployments(request)`.
Sizes are `catalog(CatalogRequest(purpose="deployment"))`. A provider never derives a hostname
and never writes a DNS record: the launcher allocates the names and writes its zone through
whichever plugin implements `DnsFacet` (`list_records`, `change_records`), which need not be the
deployment's provider.

**The test for all three.** `FakeProvider` (`tlc_plugin_sdk.infrastructure.testing`) is the
reference implementation of every operation, in memory; read it next to this chapter. The
conformance kit drives your plugin the way a host does:

```python
from tlc_plugin_sdk.infrastructure.testing import FakeProvider, assert_conformant


def test_the_reference_provider(tmp_path):
    assert_conformant(FakeProvider(), plugin_id="fake", config_root=tmp_path, create_nodes=True)


def test_my_provider(tmp_path):
    with patch_my_cloud_client():  # your repo's fake for the cloud's client
        assert_conformant(MyProvider(), plugin_id="myprovider", config_root=tmp_path, create_nodes=True)
```

Start from the first test (it passes as written), then make the second pass.

### Capabilities: what the provider declares

`GET /infra/capabilities` answers a `CapabilitiesResponse`. It is never sent a Connection, so
anything it says about this host is read from the machine's own identity.

- **`provider`, `display_name`, `contract`.** The Connection provider slug (your plugin id), the
  name a person reads, and the contract generation (`2`, the default).
- **`ready`, `not_ready_reason`, `missing_fields`.** Whether your own settings are complete, and
  the prompts for what is missing. `self.readiness()` fills `ready` and `missing_fields` from
  the settings layer.
- **`account`.** An `AccountSchema`: what an account is called (`noun`), its identity `fields`,
  and `defaults` that are not identity (a default location). Every account field is compared,
  key for key, with what the cloud says when a Connection is verified. `None` means the provider
  takes no Connection.
- **`access_methods`.** One `AccessMethod` per Connection kind you support: `AMBIENT` (the
  machine's own sign-in), `KEYLESS` (access the customer grants; no secret is stored) and
  `SECRET` (a stored value). Each has the words a person reads, the `purposes` it can carry
  (`deployments`, `nodes`, `storage`), whether this host can use it (`available`,
  `unavailable_reason`), the `fields` a person enters, and `grants` when `describe_grant` applies.
  At most one is `preferred`: the method a new Connection gets.
- **`host`.** A `HostIdentity`: who this host is to the provider. A Connection that carries
  every key of `matches` with the same value in its `access` fields acts for this host; `{}`
  scopes nothing. On a deployment, `deployment_of` names the Connection it was launched through.
- **`permissions`.** A `PermissionSchema`: your own switches (`options`) and, when storage rules
  are a thing for you, how a location is written (`storage`: scheme, example, pattern, the access
  modes your cloud can express).
- **`nodes`**, set exactly when the class is a `NodeProvider`: kinds, configured types, pricing,
  how a node reaches storage, and `type_connections` for node types that each have a credential
  of their own.
- **`deployments`**, set exactly when the class is a `DeploymentProvider`: who obtains the
  certificate (`machine` or `provider`), `shared_ingress`, and `sslip` (the provider can name a
  deployment by its own address).
- **`storage`** and **`operations`** are filled by the SDK; a value you set is overwritten.

Every value a person sees comes with its display text from you (`label`, `help`, `example`):
the Hub renders a `FormField` and never parses a provider identifier. There is no `extra` and no
free-form map: a value the host or the Hub needs is a typed field, and a value only your own
page needs is served by your own route.

The SDK checks the declaration before answering and answers 502 with the rules it breaks
(`capabilities_problems`): a provider slug or field key of the wrong shape, a key listed twice,
a field whose `secret` and `kind` disagree, two methods of one kind, two `preferred`, and
`nodes` or `deployments` that disagree with the class.

### The wire, typed

Every message is a dataclass in `tlc_plugin_sdk.infrastructure.types` (the Connection binding
and its permissions are in `tlc_plugin_sdk.connections`), with the same rules:

- `from_dict(data)` reads only known keys, drops unknown ones, coerces scalars and never raises
  on a peer's shape. A missing key is the field default.
- `to_dict()` writes every field, and omits a key whose presence carries meaning when it is
  unset (a quote you do not have, a part of capabilities you do not offer).
- A new optional field or operation may appear in a later minor; a provider that does not use
  it changes nothing.

One JSON example per type, as `FakeProvider` emits it, is in the SDK repository under
`tests/fixtures/contract/`.

### Connect an account

- **`describe_grant(request)`** answers a `Grant`: what the customer runs in their own account
  and what it creates. It is called with no Connection, changes nothing and needs nothing of the
  account; it may read this host's own identity to name what the grant trusts. Answer a link
  that opens your cloud's console with everything filled in, or `files` and a `command` for doing
  it by hand, the `steps`, and what is created per purpose. `permissions` is the request's
  permissions in canonical form (normalise each rule's location): what you return is what is
  stored and later compared with an observation. `access` is what the Hub stores on the
  Connection before the person follows the link. With `action="update"`, answer a link or files
  that change the existing grant in place, never a create link. 3LC never applies a grant.
- **`prepare_grant_update(request)`** is for a cloud with a reviewed change (a change set). It
  runs under the Connection and prepares, never executes; it answers one of five states
  (`pending`, `ready`, `applied`, `no_changes`, `failed`) with your own sentence in `detail` and
  the `review_url` where the customer applies it. Without a reviewed change, do not implement
  it: your update is `describe_grant(action="update")`.
- **`verify_connection(request)`** runs under the Connection, after the SDK resolved it. Answer
  who the credential is (`identity`), the `account` your cloud's API says it is in (as your
  account fields, so the Config Service can compare it without knowing your grammar), the steps
  that succeeded (`checked`), and the installed grant as read back (`observation`). A step that
  fails raises; a sample that could not be confirmed is a `warn` check; a refused operation is an
  `error` check. `facts` are labelled rows the Hub shows under the check.
- **`connection_node_access(request, apply=False)`** is how a machine on its own sign-in sets up
  a scoped node identity directly, when it has the rights to. `GET` inspects and must change
  nothing; `POST` applies the displayed setup. `ready` confirms configuration, not a live data
  test. A provider that lets an administrator widen node access answers the active rules as
  `storage_access` with a `revision`, and on `POST` receives the reviewed rules and the
  `expected_revision`: refuse a stale revision with `Conflict`, validate before any cloud write,
  and make the rules active only once the cloud setup succeeded.
- **`storage_candidates()`** runs under the Connection with a discovery-only grant and returns
  names, never contents: at most 2000, with `truncated` set when the account holds more.

### Nodes

- **`create_node(request)`** starts a machine with the node agent on it and answers the
  `provider_id` the host uses afterwards and the `agent_url`, the one address the host needs:
  it reaches the node's loopback-only workers through the agent's proxy.
  - *Idempotent on `node_id`.* A repeat, after a timeout or a host restart, returns the resource
    the first call created. Find it by tag: every resource you create for a node carries `3lc` =
    `node`, `3lc-id` = `node_id`, `3lc-owner` = `owner` and `3lc-host` = `host_id` (or the
    nearest thing your cloud has).
  - *`kind`* is `gpu` or `cpu`. *`location`* is one string from your `locations()`; `""` is the
    Connection's default (`binding.defaults`), else your own setting. Anything finer (a subnet,
    an image) is your settings.
  - *Identity.* The request carries no identity. Attach the node identity the Connection's grant
    created, which you read from the binding's `access`, and say which in `identity`. Never copy
    a credential onto the node; drop credential variables from `env`.
  - *`storage_roots`* say what the node's identity should at least reach. They are a hint,
    never a boundary to restrict by. Answer `storage_access`: `identity`, `ambient` or `none`.
  - *`compute_spec`* is the pip requirement for the node agent, to install as one requirement.
  - *`idle_ttl_s`* of `0` or less means never.
- **`node_state(provider_id)`** answers `pending`, `running`, `exited`, `terminated`, `gone` or
  `unknown`. An id you do not know is `gone`, not an error.
- **`delete_node(provider_id)`** is idempotent: a repeat answers `terminated` or `gone`.
- **`check_node(request)`** answers a `PreflightResponse`: checks with a level (`info`, `warn`,
  `error`) and `ok`, which is false when an `error` check failed.
- **`node_diagnostics(provider_id)`** fills the `bootstrap_*` fields of the state:
  the milestones seen, the current one in a sentence, and whether startup failed (`None` when
  you cannot tell).

A machine with more than one identity attached tells its workers which to use through the
machine's environment, under a name the node agent passes to workers. Which machine image, GPU
driver and boot script a node gets is yours and not part of the contract.

### Catalog

`catalog(request)` answers typed `Offer`s for one `purpose` (`gpu_node`, `cpu_node`,
`deployment`) and location: an id to send back, what it is (CPUs, memory, GPUs), `hourly` and
`hourly_spot` in USD, `available` (`None` when you cannot tell) with an `unavailable_reason`,
and your own words for stock in `capacity`. At most 500 offers. A failed lookup is an empty list
with `error` set, never an exception. `locations(node_type=)` answers the places a `location`
may name, with stock where known and the `default`.

Your own catalogue page reads these through your plugin's routes like any fragment; see *Listing
what a provider offers* below for `TlcCatalog`.

### Storage

`StorageFacet` is the buckets, containers or volumes the Data page fills and browses. Abstract:
`storage_capabilities()` (what your storage can do) and `list_storage(fallback_url=)`. Optional,
each answering 501 until you override it and set its flag: `create_storage`, `delete_storage`,
`presign` (the browser's upload and download URLs; say `cors=False` when the storage would
refuse the browser's upload), `list_objects`, `delete_objects`, and `transfer_registry(url)` /
`bundle_registry(url)`. The transfer and bundle routes are the SDK's: hand back a
`TransferRegistry` or `BundleRegistry` for a URL, cached per storage, and the SDK serves start,
status and cancel over it.

Every storage route acts through a Connection. The upper bound on what a Connection may reach is
its `permissions.storage`, which you check a project root and data locations against before you
create anything.

### Deployments

A deployment is one machine in the customer's account running an Object Service and a Compute
Service, reachable under a public hostname with a valid certificate. It is created only by a
launcher. The launch, as the launcher runs it:

1. **Options and size.** `deployment_options(selected)` answers the placement fields in the order
   they are chosen, each with the choices valid for what is selected so far, plus costs,
   warnings and blockers. Sizes are your catalog for `purpose="deployment"`.
2. **Check.** The launcher derives a DNS label from the name and refuses a malformed or reserved
   one itself. `check_deployment(request)` answers what only you can know (a label too long for
   your ingress, a placement that cannot host one, quota, a project root outside the
   Connection's storage rules) and, when deployments share an ingress, the opaque `ingress_key`:
   the same key means the same shared ingress.
3. **Allocate.** The launcher allocates the hostnames in its zone.
4. **Prepare**, when you list it. `prepare_deployment(request)` is called repeatedly until
   `ready` or `failed`. It is idempotent, may create what deployments share (a load balancer, a
   certificate) and answers the DNS records that must exist for it to finish, every one under
   the request's `namespace`. The launcher writes them and calls again after `retry_after_s`.
   The request carries no token.
5. **Create.** `create_deployment(request)` returns when the machine exists. It is idempotent on
   `deployment_id`; every resource carries `3lc` = `deployment` and `3lc-id` = `deployment_id`.
   Answer the `addresses` the launcher points its names at and the two https `endpoints`. With
   `hostnames` unset, name the deployment by its own address and report those names (you must
   declare `deployments.sslip`). With `certificate == "machine"` the machine requests its
   certificates once its names resolve to it, so its startup waits for that. Set `failure` when
   resources exist and the launch did not complete: the launcher records it, then deletes.
6. **Readiness** is the launcher's: both endpoints answer, while it follows
   `deployment_state(provider_id, diagnostics="baseline")` for the reason a boot fails.

Afterwards: `delete_deployment` is idempotent and is called after the launcher removed its
records; `retain_deployment` stops routing traffic to a failed machine and keeps the machine;
`deployment_state(diagnostics="detailed")` is the separately granted collector;
`list_deployments` answers every deployment the account runs, tracked or not; and
`sweep_deployments(request)` releases what no `keep` item owns once it is older than `grace_s`,
and reports the addresses live deployments hold. A sweep never touches DNS.

The bootstrap token in `request.bootstrap.token` is the one secret of the launch: put it in the
machine's startup data and keep nothing. The SDK scrubs it from error answers.

### The launcher's DNS zone

A plugin with `DnsFacet` writes a launcher's zone, with the launcher's own identity and never a
Connection. `list_records(zone, under=, cursor=)` pages at 300 records. `change_records(change)`
is one atomic change of at most 100 records: a create of a name and type that exists with the
same values succeeds; with other values it raises `Conflict` and nothing in the change is
applied, so two launches racing for a name cannot both win. A delete removes a record only when
it matches exactly as listed, and a missing one is success. Only `A`, `AAAA`, `CNAME` and `TXT`
are ever written.

### The transitional deployment surface

A host that does not yet drive the deployment group creates a deployment as a node:
`POST /infra/nodes` with `flavor == "workspace"` and a `workspace` object, answered with
`services`. A provider serving that path adds `WorkspaceFacet`
(`tlc_plugin_sdk.infrastructure.transitional`): `list_workspaces`, and optionally
`workspace_placement`, `workspace_diagnostics` and `workspace_retain_failed`. The SDK then lists
the operation `workspace.create`; a provider that mounts its own name-check and sweep routes
names them in `private_operations = ("workspace.name_check", "workspace.sweep")`. New providers
implement `DeploymentProvider`.

### Identity: the credential seam

A **Connection** is an account, the access method into it, and what 3LC may do there. The host
sends its non-secret *binding* in the host-owned `x-3lc-connection` header; the worker resolves
it before your method runs and exposes the result for that one request:

```python
from tlc_plugin_sdk import connections

binding = connections.current_connection()     # ConnectionBinding | None
credential = connections.current_credential()  # Ambient | SecretToken | your own class | None
session = connections.credential_as(MySession) # your resolver's class, or CredentialUnavailable
```

The binding names no vendor concept: `account`, `access` and `defaults` are maps of the fields
you declared, `permissions` is what is installed in the account (`None` before a first
verification), and `grant_ref` is your reference to the installed grant. The Config Service
generates `access["external_id"]` for every `KEYLESS` Connection; a provider that needs no nonce
ignores it.

- **`AMBIENT`** is resolved by the SDK to `Ambient(provider, account, defaults)`: use the
  machine's own identity, your cloud library's default chain. Do **not** fall back to anything
  saved in your settings: the Connection said which identity to use. A host's built-in
  own-identity Connection carries an empty `account`; take the account from your settings then,
  and ask for them in `missing_fields`.
- **`KEYLESS`** calls the resolver you register at import, in a worker thread, with a
  `ResolveRequest` (the binding and the use). Return your own credential class: any object with
  a read-only `expires_at` (ISO-8601, or `""`) and `secret_values()`, which the SDK scrubs from
  every error answer. Raise `CredentialUnavailable` with a sentence a person can act on.

  ```python
  @dataclass(frozen=True)
  class MySession:
      token: str
      expires_at: str = ""

      def secret_values(self) -> list[str]:
          return [self.token]


  def resolve(request: connections.ResolveRequest) -> MySession:
      return exchange_for(request.binding.account, request.binding.access)


  connections.register_resolver("myprovider", resolve)
  ```

  A deployment acting on the Connection it was launched through acts as itself: its resolver
  returns `Ambient` and exchanges nothing. A binding that carries your `host.matches` keys with
  other values is refused before the cloud is asked.
- **`SECRET`** is not resolved: the value arrives beside the binding in `x-tlc-bound-credential`
  and `current_credential()` is a `SecretToken`. A value of several fields (an API key and a
  storage key pair) is a JSON object whose keys are the method's `secret` fields:
  `token.fields()` returns it and raises `CredentialUnavailable` for any other shape.
- **What the Connection is used for.** A host may add `x-3lc-connection-use`
  (`{resource_id, source_identity}`: the node the call is about and the person it acts for).
  `connections.current_use()` returns it, and a resolver receives it in its request, so a
  customer's audit trail can say which node and whose.

What the SDK answers before your method runs: a malformed or repeated header, and a binding
over 8 KiB, **400**; a binding it cannot resolve **424** with the resolver's reason; a `SECRET`
binding without its value, or with a value for another Connection, **424**. A route that reads
the host's own identity (capabilities, the grant, the DNS routes) answers **400** when any
Connection header is present, so those handlers can never observe one. A route that acts on an
account (verification, the storage routes, every deployment call but listing and sweeping)
answers **400** without a Connection.

The rules for what you keep:

1. Host identity is ambient-only. Whatever a grant will trust is read from the machine's own
   credential chain, never from a request, and a cache of it is keyed by that credential.
2. A resolved credential belongs to one request. Never write one to `os.environ`, a file or
   your settings.
3. You may keep an exchanged credential in memory until 60 seconds before its `expires_at`,
   keyed by a digest of the whole binding plus the use's `source_identity`. Drop it on any
   authentication failure.
4. Never cache a secret value.
5. Cache clients per credential, never per location alone, and anything per account under the
   account the credential belongs to.
6. Background work a request starts (a transfer, a bundle) keeps that request's credential and
   cannot outlive it. When the credential expires, stop, record `credential_expired` with what
   was done, and never continue on the host's own identity.

The headers are host-owned: a host strips any copy a caller sends, so a fragment can never
choose the identity.

**A job's tokens (`SECRET`).** A run may name a Connection for each declared service. The
host obtains all values for that job and sends a host-owned `_credentials` service map, or
`_credential` for one token. The worker removes these before `ctx.params` exists. During
`run_job`, use `ctx.get_credential(service)` or `connections.current_credential(service)` to
read a `SecretToken` (`provider`, `connection_id`, `secret`; its repr masks the value).
`ctx.credential` and no-argument `current_credential()` expose a singleton only:
on a job with several tokens they are `None` and warn, so name the service.
Never log, persist or emit secret values. The SDK does **not** set environment variables (see
*Concurrency* below): hand each token to its library explicitly.

#### Tokens on runs and routes (SECRET)

A `SECRET` Connection's `provider` names the **service** its value is for (`huggingface`,
`kaggle`, `wandb`, …), never a plugin: one person's Hugging Face token serves every plugin they
allow to use it. Declare what your plugin asks for in `[runtime]`, so a Hub can offer the person a
choice of Connection without knowing anything about your plugin:

```toml
[runtime]
credentials = [{ service = "huggingface", required = true }]  # services; required = the plugin cannot work without one
credential_routes = ["/preview", "/model-warmup"]               # custom routes that receive the token
```

`credential_routes` are path prefixes relative to your route root, with the same rules as
`node_routes` (`"preview"`, `"/preview/"` and `"/preview"` all mean `/preview`, which covers
`/preview/x` but not `/previewer`; `"/"` alone covers every custom route). Each `service` is a
lower-case slug (`^[a-z0-9][a-z0-9._-]*$`).

Each declaration may include `value_hint`, a short plain-text sentence shown by the Hub when
someone adds a service token. Explain the expected value and where to obtain it, for example
`value_hint = "A token from huggingface.co/settings/tokens. Read access for imports; write access for sharing."`
or `value_hint = "The contents of kaggle.json (username and key)."`. This is help, not validation
or a default value: never put credentials in it. Older hosts ignore it; without a hint the Hub
uses generic token wording. The harness validates that it is a string.


- **On a run** the Hub sends `credential_connection_ids: {service: connection_id}` when several
  services are selected. `credential_connection_id` is accepted for one selection;
  do not send both fields. The host checks every grant and its service before queueing anything.
  Plugins read `ctx.credentials` (a read-only map), `ctx.get_credential("huggingface")`, or
  `connections.current_credential("huggingface")`. Each lookup returns `None` if that service was
  not supplied. `ctx.credential` and no-argument `current_credential()` remain available for a
  singleton; both return `None` for a multi-service job, so there is no arbitrary default token,
  and warn (a `UserWarning`) that the service must be named. A `_credential` from an older host
  that names no service is that singleton only: it is never in `ctx.credentials`.
  Secrets are removed from `ctx.params` before the plugin runs and must never be saved in configs.
  Single-token jobs retain the legacy worker wire. Before sending a multi-token job, the host checks
  the worker's `job_credentials_by_service` health capability; older workers need an SDK update.
  The host advertises `credential_connections_by_service: true` on plugin cards. When absent,
  the Hub retains the older single-Connection behavior; explicit service maps require a host update.
- **On a listed route** the Hub sends the chosen Connection id to the host, which obtains the value
  for the person calling (cached briefly per person, Connection and plugin) and forwards it to
  your worker in the host-owned `x-tlc-bound-credential` header
  (`connections.BOUND_CREDENTIAL_HEADER`; the same `{connection_id, provider, secret}` object as a
  run body's `_credential`). The SDK's worker middleware binds it around your handler exactly as
  around `run_job`: `connections.current_credential("huggingface")` is the request's
  `SecretToken` (a route carries one token, so no-argument `current_credential()` finds it too),
  restored when the handler returns. The host strips any copy a caller sent, and sends it on no other
  route.
- **On an infrastructure plugin's calls about a node** the same header carries the value of the
  node's Connection, when that Connection is a `SECRET` one: the account is reached with a stored
  value (an API key, an ssh private key) instead of an assumed role. The host accepts such a
  Connection for a node only when its `provider` is a service your manifest declares under
  `[runtime] credentials`, and sends the value with `POST /infra/nodes`, `GET` and `DELETE
  /infra/nodes/{id}` and `POST /infra/nodes/check`. It asks the Config Service for a grant each time, so
  the status polls and the teardown that run with nobody signed in receive it too, also after a
  restart. The Connection's binding and its use travel beside it as for any other kind, so in your
  contract method `connections.current_connection()` is the binding (its `access` holds what
  the Connection records beside the value, such as an `endpoint_url`),
  `connections.current_use()` the node and the person, and
  `connections.current_credential("<service>")` the value. There is no resolver to register. Treat
  a `None` credential as a call that may not act with that Connection. The worker answers 424
  itself when a `SECRET` binding arrives without its value, or with a value granted for another
  Connection or service.

  A Connection is normally chosen when the node is created. A provider whose node types are things
  that each have a credential of their own (a machine and its ssh key) can name them instead:
  `NodeCapabilities(type_connections={node_type: connection_id})`. The host
  creates a node of that type on the named Connection when the request names none, under a grant
  for the person creating it. The plugin chooses the Connection here, not the person creating the
  node: they must be allowed to use it and the grant is recorded in their name, but they are not
  asked which one. What lets your plugin have the value at all is the Connection's owner listing
  the plugin on it, so naming a Connection your plugin is not listed on gets a refusal. Let the person pick the
  Connection on your page with `PLUGIN_API.chooseCredential("<service>")`, store the id it resolves,
  and never the value.

  Keep the value in memory and out of anything shared: not a command line, not a file, not
  `os.environ`, not a log line or an error message. The host masks it in the error body of a call
  that carried it, and in nothing else your plugin writes.

Environment variables per service, for a tool that reads only its variables:
`connections.credential_environment(token)` returns them — `huggingface` → `HF_TOKEN`, `wandb` →
`WANDB_API_KEY` (`connections.ENV_VAR_BY_PROVIDER`); `kaggle`'s value is the JSON object
`{"username": "...", "key": "..."}` → `KAGGLE_USERNAME` + `KAGGLE_KEY`
(`connections.ENV_VARS_FROM_JSON_BY_PROVIDER`; a value of another shape fails the job, and answers
a route 424, before your code runs). A service not listed returns `{}`: read the token's
`secret` yourself. Get the token by its service — `ctx.get_credential("huggingface")` in
`run_job`, `connections.current_credential("huggingface")` anywhere in a job or a listed route —
and check it for `None` (that service was not supplied). Pass the variables to a subprocess that
works for this one job or request
(`subprocess.run(cmd, env={**os.environ, **credential_environment(token)})`), never into
`os.environ`.

**Concurrency.** `current_credential(service)` is scoped to the job or request (a context
variable), and a `ctx` belongs to its one job, so concurrent requests and jobs each see their own
tokens, in `async` and `def` handlers alike, and work with different Connections runs side by side in one
worker. Nothing process-wide is set: `os.environ` is shared by every job and request in the
worker — including those of other people on a shared host, and ones that name no Connection — so
a token placed there would be used by any of them whose library reads its default environment.
Pass the token's `secret` to your library explicitly:

```python
token = connections.current_credential("huggingface")  # or ctx.get_credential("huggingface") in run_job
if token is not None:
    hf_hub_download(..., token=token.secret)
```

Always name the service: on a job with tokens for two services, no-argument
`current_credential()` is `None`, so `current_credential().secret` raises `AttributeError`. A
request whose route is not listed, or that
names no Connection, has no credential bound: fall back to whatever the plugin did before
Connections, or answer that a token is needed.

Test a route with a token through the harness by sending the header yourself:

```python
from tlc_plugin_sdk.connections import BOUND_CREDENTIAL_HEADER, SecretToken, encode_credential

token = SecretToken(provider="huggingface", secret="hf_test", connection_id="c-1")
response = harness.post("/preview", json={...}, headers={BOUND_CREDENTIAL_HEADER: encode_credential(token)})
```

**Precedence inside a provider.** The Connection decides (`current_credential()`; `Ambient`
never falls back to saved keys). The plugin's own settings supply a credential only when the
request names no Connection.

### Settings from a dataclass

`PluginSettings(MySettings, "myprovider")` on the class turns one dataclass into the whole
settings surface: `load()`, `save(update)`, `redacted(settings)`, `secret_values(settings)`,
`missing_fields(settings)` and `field_view()`, plus `GET /settings` and `POST /settings` mounted
by the SDK. Neither route is ever sent a Connection. The dataclass carries the store's envelope (`id`, `created`,
`last_run`) and, per field, optional metadata:

- `secret(label="", help=, href=, placeholder=, required=True)` — a secret string, default `""`:
  the fragment sees `<name>_set: bool`, never the value; `""` on save keeps it, `"-"` clears it.
  Without a label (a key a request or a Connection usually supplies) it is still masked and
  scrubbed, but never prompted for — `required` does not matter then.
- `option(default, label=, help=, href=, placeholder=, required=, validate=, clearable=, coerce=)`
  — a described non-secret setting. `coerce` runs on the incoming value first (a fallback, a
  parse); `validate` is then called with it and its return is stored — raise `ValueError` with a
  sentence and the route answers 400; an empty value keeps the current one unless
  `clearable=True`. A field with a `label` is a prompt: `missing_fields()` lists it while its
  value is empty, and the plugin's `self.readiness()` turns that into the `ready` and
  `missing_fields` keyword arguments of `CapabilitiesResponse`, each prompt as a `FormField`. Prompts come in the dataclass's field order:
  declare the fields in the order a person should be asked for them.
- A plain `field()` or default: merged by annotation, never prompted. `int` (a failed conversion
  keeps the current value), `bool` (`1/true/yes/on`), `float`, `list[str]` (stripped, empties
  dropped), `dict[str, str]`, `list[<dataclass>]` (each row from its known fields; unknown keys
  dropped, missing keys defaulted — a row saved before a field existed still loads), `str`.

Keys that are not fields, and `id`/`created`/`last_run`, are ignored on save. After the merge a
`normalise(self)` method on the dataclass runs when it has one (cross-field rules). The
redacted view the fragment reads is `id`, every non-secret field by name, every secret as
`<name>_set`, and a `dict[str, str]` named `env` or `*_env` as `<name>_keys` (sorted) — nested
rows follow the same rules. Override `settings_view(settings)` to add computed keys (an
effective placement, a derived URL, per-row problems). A settings file that exists but does not
parse is `SettingsUnreadable` → 409 with *"fix or delete it"*, before any merge; it is never read
as defaults.

`secret()` and `option()` return dataclass fields, like `dataclasses.field`; a repo that lints
with ruff lists them by qualified name under `[tool.ruff.lint.flake8-bugbear]
extend-immutable-calls` (`tlc_plugin_sdk.infrastructure.secret`, `.option`, and the
`tlc_plugin_sdk.shared.settings.` pair) so `RUF009` accepts them as defaults.

A provider that keeps hand-rolled `/settings` routes leaves `settings = None`: the SDK mounts
nothing, and the conformance kit flags a plugin that has both.

### Errors people can read

Raise with the provider's own sentence and let the SDK answer; never let a 500 through — the
host writes the sentence on the node record, and *"Internal Server Error"* tells nobody
anything. `tlc_plugin_sdk.infrastructure.errors`:

| Raised in a method | HTTP | `code` | When, and what the `remedy` says |
|---|---|---|---|
| `InvalidRequest` | 400 | `invalid_request` | a bad id, url, mode or name; what to change in the request |
| `PermissionDenied` | 403 | `permission_denied` | the account refused the call; which permission is missing and where it is granted |
| `NotFound` | 404 | `not_found` | no such node, deployment, storage, transfer or bundle |
| `NotConfigured` | 409 | `not_configured` | no key yet, no size configured; which setting or setup step is missing |
| `Conflict` | 409 | `conflict` | the request clashes with what is there; what is in the way |
| `connections.CredentialUnavailable` | 424 | `credential_unavailable` | the Connection could not be used from this host; why |
| `CredentialExpired` | 424 | `credential_expired` | a credential that was valid when the call started stopped being so during it |
| `RateLimited` | 429 | `rate_limited` | the provider is throttling; raise it with `retry_after_s` |
| `NotSupported` | 501 | `not_supported` | an operation the provider does not implement |
| `ProviderError` | 502 | `provider_error` | the provider answered something a person must read |
| `SettingsUnreadable` | 409 | `not_configured` | the settings file does not parse |
| `ValueError` (a validator's, the transfer engine's `TransferError`), `TypeError` | 400 | `invalid_request` | a value that cannot be used |
| `NotImplementedError` | 501 | `not_supported` | |
| a litestar `HTTPException` | as raised | the code of that status | |
| anything else | 502 | `provider_error` | `describe_error(exc)`, never an opaque 500 |

Every body is `{"detail", "code", "remedy", "retry_after_s"}`. `detail` is the sentence a person
reads; `code` is what the host branches on, never the words of a sentence; `remedy` says what a
person can do about it (`raise PermissionDenied("…", remedy="…")`). Detail and remedy are scrubbed
of `secret_values()`, of the request's credential (`secret_values()` of what your resolver
returned, a `SecretToken`'s value and fields) and of the request's own secrets (a node token, a
bootstrap token). What the host does with a code: it asks for a fresh grant and repeats the call
once on `credential_expired`, which is safe because every create is idempotent; it repeats after
`retry_after_s`, at most three times, on `rate_limited`; it reports everything else.

An exception you did not word — a cloud SDK's, say — is answered with
`InfrastructurePlugin.describe_error(exc)`: by default its own text scrubbed of
`secret_values()`. Override it to strip what such messages carry (ARNs, request ids,
endpoints) instead of wrapping every call; it words every route the SDK mounts, the transfer
and bundle registries' plan, start and status included, and the errors their background jobs
record. The SDK scrubs the exception's raw text and your answer (coerced to text) of whole
secret values, then truncates to 500 characters — so return the whole sentence, never truncated
or re-encoded (quoted, escaped, base64): a cut or re-encoded secret is no longer recognised. An
empty answer or a hook that raises falls back to the scrubbed raw text:

```python
def describe_error(self, exc: Exception) -> str:
    return user_facing_cloud_error(exc)  # log the raw text yourself when you want it
```

### Testing

`PluginHarness` runs your plugin's app in-process for one call at a time
(`python -m tlc_plugin_sdk.harness src/my_provider GET /infra/capabilities`). The conformance
kit drives the whole contract and reports every check by group:

```python
from tlc_plugin_sdk.infrastructure.testing import assert_conformant


def test_conformance(tmp_path):
    with patch_my_cloud_client():  # your repo's fake for the cloud's client
        assert_conformant(MyProvider(), plugin_id="myprovider", config_root=tmp_path)
```

By default the kit runs the checks that create nothing: capabilities (the declaration follows
the contract's rules, `operations` lists exactly what the class provides, a Connection header is
refused), the connect group, node checks, catalogs, deployment options and checks, storage flags,
errors (every non-2xx body is the contract's error body; no secret appears in any answer),
settings and routes. `create_nodes=True` adds the node lifecycle (create twice for idempotency,
state, diagnostics, delete twice); `create_deployments=True` the deployment lifecycle (prepare,
create twice, state, list, delete twice, sweep), naming the deployment by its address;
`dns_zone=DnsZone(id=…, name=…)` the DNS checks, which write and remove records under
`_3lc-conformance.<zone>`; `live_storage=True` the object listing and a transfer dry-run against
the first listed storage. `skip=("routes",)` leaves a group out.

Every call that acts on an account carries a Connection. Without `headers` the kit sends an
`AMBIENT` binding for your provider, and none at all when your capabilities declare no account.
To run under another one, build the headers a host would send:

```python
from tlc_plugin_sdk.connections import ConnectionBinding, SecretToken
from tlc_plugin_sdk.infrastructure.testing import connection_headers

binding = ConnectionBinding("conn-1", "myprovider", "SECRET")
headers = connection_headers(binding, credential=SecretToken("myprovider", "test-key", "conn-1"))
assert_conformant(MyProvider(), plugin_id="myprovider", headers=headers, create_nodes=True)
```

To call a provider method directly in a unit test, without the routes, set the same context with
`acting_as(binding, credential=None, use=None)`. Inside the block `current_connection()`,
`current_credential()` and `current_use()` answer what you pass; on exit they answer what they
did before, also when the block raises. Nothing is resolved, so pass the credential the method
should see:

```python
from tlc_plugin_sdk.connections import Ambient, ConnectionBinding, ConnectionUse
from tlc_plugin_sdk.infrastructure.testing import acting_as

binding = ConnectionBinding("conn-1", "myprovider", "AMBIENT")
with acting_as(binding, Ambient("myprovider"), use=ConnectionUse("node-1", "ada")):
    created = MyProvider().create_node(request)
```

Settings are read and written under `config_root`, a temporary directory when none is given, so
a run never touches `~/.3lc-plugin-configs`. The CLI is `python -m
tlc_plugin_sdk.infrastructure.testing src/my_provider [--config-root DIR] [--create-nodes]
[--create-deployments] [--dns-zone ID=NAME] [--live-storage] [--node-type T] [--skip GROUP]
[--header NAME=VALUE]`.

`FakeProvider` in the same module implements every operation in memory: nodes and deployments
that are idempotent on the host's id, a DNS zone with the atomic-change rules, buckets, catalogs,
all three access methods and its own `KEYLESS` resolver. A host is tested against it in process,
and it emits the JSON fixtures of the wire types.

### Your own routes and fragment

Anything the contract does not cover stays provider-private, reached only from your own
fragment through `PLUGIN_API`: an auth flow (`/auth/*`), a machine check (`/machines/check`), a
region picker. Append them to the SDK's handlers:

```python
def get_route_handlers(self) -> list[Any]:
    return [*super().get_route_handlers(), *my_private_routes()]
```

A private route that runs its work through `routes.answer(plugin, fn)` and is mounted with
`exception_handlers=routes.error_handlers()` answers the same error body as the SDK's routes.

The reserved worker paths (`/health`, `/ui`, `/compute`, `/busy`, `/reclaim`, `/jobs/*`) are never
mounted for a plugin, and a second handler on a path the SDK mounts (a hand-rolled `/settings`
next to `settings = PluginSettings(...)`) stops the app from building — the kit names it.
Catalog rows are drawn with `TlcCatalog` (see *Listing what a provider offers* below).

### Checklist for a new provider

1. Manifest: `kind = "infrastructure"`, `[runtime] entrypoint = "my_provider:MyProvider"`.
2. Class bases: `InfrastructurePlugin` plus the mixin of each group you offer.
3. `settings = PluginSettings(MySettings, "<plugin id>")` with `secret()`/`option()` metadata.
4. `capabilities()` declares the account, the access methods, the permissions and each group's
   part, with `**self.readiness()`; `provider` equals the plugin id.
5. Creates are idempotent on the host's id, and every resource carries the `3lc` tags.
6. Credentials from `connections.current_credential()` or `credential_as(...)`, never from a
   request field or your settings when the request names a Connection.
7. Errors raised with the provider's sentence and a remedy (`PermissionDenied`, `RateLimited`,
   `ProviderError` and friends).
8. `assert_conformant(...)` green in CI, with `create_nodes=True` (and `create_deployments=True`)
   against your patched seam.

## Long-Running Jobs (`run_job(ctx)`)

A plugin with a long-running task (training, inference, import) **declares** the job
in its manifest and **implements** it as `run_job(ctx)`. It does **not** grab a queue,
push a closure, or poll a shared `cancel_flag` — the host owns the queue, the GPU/CPU
slot lease, progress fan-out, listing, and cancellation. `run_job` runs in the plugin's
worker and only ever touches `ctx`.

**Declare the job in the manifest.** `requires_gpu` is the only knob:

```toml
[runtime]
entrypoint = "tlc_plugin_my_gpu_plugin:MyGpuPlugin"
requires_gpu = true                 # → routed through the shared GPU queue (1 at a time)
provision_extra = "my-gpu-plugin"   # venv deps installed via `uv sync --extra <this>`
# SocketIO namespace is host-derived as "/my-gpu-plugin" — not declarable here
```

GPU jobs are serialized — only one runs at a time across every GPU plugin (YOLO, SAM3,
timm, image-metrics). `requires_gpu = false` jobs use a bounded CPU pool, independent
of the GPU slot. CPU capacity respects the node's detected CPU allocation, falls back
to four when unknown, and can be lowered with `TLC_CPU_JOB_CONCURRENCY`. A slot is a
concurrent job, not a pinned core: multithreaded or memory-heavy jobs may need a lower cap.
The plugin never names or touches a queue. Data-copy dependencies are resolved before
allocating training's GPU slot; failed or cancelled copies prevent that training from starting.

Workers advertising `isolated_job_processes` can run finite jobs in separate spawned
processes on Linux/macOS. Each process uses the installed plugin environment and calls
`initialise_runtime()` then `run_job(ctx)`. This isolates 3LC's process-global aliases,
credentials bound to a job, and native runtime state. It is process isolation, not a sandbox
for untrusted plugin code. Older workers and platforms without process-group cleanup keep
same-plugin jobs serialized. Long-running services keep their shared serving runtime.

Usually no plugin change is needed. A plugin that reads transient state created by one
of its routes (for example uploaded file bytes) may implement `prepare_job(ctx)`. This
fast hook runs on the serving worker before spawning: add picklable inputs to `ctx.params`.
The SDK sends them over private IPC on that same node; nothing is written to a queue
journal or sent through another node. Do not do lengthy work or change global aliases in
this hook. Durable configuration should continue to use the existing stores.

The generic job record optionally includes `queue_kind`, `execution_slot`, and
`waiting_reason`. Interfaces display these on job rows within the existing CPU/GPU
sections; they must not create a separate visual queue per CPU.

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
| `ctx.params` | Job parameters (parsed request body / query), without host-granted secrets. |
| `ctx.credentials` / `ctx.get_credential(service)` | Read-only service-token map / optional lookup for this job. |
| `ctx.credential` | Legacy singleton token, or `None` when there are zero or multiple tokens (multiple also warns: use `ctx.get_credential(service)`). |
| `ctx.cancelled` | `True` once cancel is requested — poll at checkpoints. |
| `ctx.state_dir` | Writable per-plugin scratch dir (never write inside the package). |
| `ctx.identity` | Who the job runs for: a `JobIdentity` with `user_id`, `org_id`, `project_id` (canonical id strings, or `None` when the host did not know). Read it for attribution; never set it. |
| `ctx.project_root_url` | The project root this job writes to, without a trailing slash: the person's choice, else the host's configured root, resolved and stamped by the host at submit. A plugin that creates tables or runs passes it as `root_url` to the core library; a plugin that derives an object from its input places it beside the input and ignores it. Never empty on a current host; a body from an older host falls back to this worker's own `tlc` root. |
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

**A cancel always ends the job — poll `ctx.cancelled` so it ends cleanly.** Cancel is
cooperative first: the worker sets `ctx.cancelled` and your loop returns at its next
checkpoint (the host marks the job "cancelled"). A loop that never checks is not allowed
to keep a GPU busy: after `TLC_WORKER_CANCEL_GRACE_S` seconds (default 60; `0` disables)
the isolated job process group is terminated, including its data-loader children, without
interrupting sibling jobs. Its slot is held until process exit is confirmed. A worker that
exits also terminates its isolated children. The grace period includes cleanup: flush
checkpoints before returning. Legacy shared-runtime jobs retain the earlier behavior:
if no siblings are live, their worker exits with status 3; otherwise the unresponsive
thread is logged and remains live until the worker exits.
The worker keeps a job registered for its full execution lifetime, even if its event
stream disconnects: the host can reconnect to its status or cancel it afterwards
(`POST /jobs/{id}/cancel`, or `POST /jobs/cancel-all` for everything still live), so a lost
stream never leaves a training running for nobody. Job ids are chosen by the host and must
match `[A-Za-z0-9_-]{1,64}`; a run for an id that is still live on the worker answers 409.

**Start a job from the UI** with the generic run route — `POST /api/plugins/{id}/run`
with the params as the JSON body; it returns `{job_id, status, namespace}`. The easiest
way to consume it is `window.PluginJobs` (next section).

### Run-body conventions for remote workers

A host with remote-node support can run a `requires_gpu` job on a **remote worker** — a
TCP worker on another machine — over the same stream contract. A plugin that needs no GPU
opts in with `remote_capable = true` in `[runtime]`; without either flag the host refuses a
run aimed at a node. These conventions make a
plugin remote-ready; all are optional locally and additive:

- **Self-contained params (`project_config`).** A remote worker has none of the
  controller's local state — in particular, nothing saved by a worker-local config or
  project store exists on the node. If your `run_job` resolves an id against such a
  store, also accept the frozen configuration inline (recommended key:
  `project_config`) and prefer it when present; have your fragment always include it in
  the run body (harmless locally, required remotely).
- **`_alias_overrides` is applied by the worker.** When a table's data has been staged
  somewhere else for this worker (a copy on a remote node), the host puts
  `{"enabled": true, "overrides": [{"token": "MY_DATA", "path": "/staged/…"}]}` at the top
  level of the run body (tokens are bare, no angle brackets). The worker registers those
  aliases before `run_job` and restores them after, so the table resolves to the staged copy
  with no code in the plugin. A plugin that persists its own alias overrides with its saved
  config keeps applying those itself; a plugin that used to read the top-level key can drop
  that code (applying the same override twice is a no-op).
- **`run_target` is host-owned.** The run body may carry `run_target` (where to run); the
  host consumes it before params reach the worker — never read or set it in a plugin. The
  same goes for `prepare_job_ids` (the data-copy jobs a remote run waits for): the host pops
  it, orders the run behind the copies, and delivers their result as `_alias_overrides`,
  which the worker applies for you.
- **`project_root_url` is stamped by the host and becomes `ctx.project_root_url`.** Every run body
  carries the root the job writes to: the value the fragment sent (the shared "Create project in"
  select; validated, and refused in words for a node run when a node cannot write it), else the
  host's configured root. It is written at the top level and inside an inline
  `project_config.params`. Read it through `ctx.project_root_url`; a fragment may send it, a plugin
  never invents another root. A plugin that persists its params may keep it.
  Treat this value as the accepted job's destination: later effective-configuration changes supply
  defaults for new submissions, not a replacement destination for queued/running jobs or retries.
  Preserve the stamped root when retrying a job. Do not reread `tlc.config.project_root_url` mid-job
  to choose another output location. Node configuration remains useful for non-job operations and
  legacy requests; it is not a competing authority for a current host's stamped job.
- **`_identity` is host-owned and becomes `ctx.identity`.** The host stamps who the job runs
  for (`{"user_id", "org_id", "project_id"}`, canonical id strings) under the top-level
  `_identity` key; the worker pops it before `ctx.params` is built and exposes it as
  `ctx.identity` (a `JobIdentity`; every field `None` when the host did not know it, as on a
  keyless local host). A fragment never sets it — the host overwrites whatever the browser
  sent — and a plugin never persists it with saved params. Read it for attribution; the
  forthcoming credential API leases credentials to *this* identity, never to a plugin.
- **Custom routes stay with the controller unless declared.** A host with remote-node
  support forwards a custom route to the remote worker only if your manifest lists it under
  `[runtime] node_routes` (e.g. sam3's `/preview`). Config/project stores, model catalogs
  and table reads must NOT be listed: they are controller state, and the remote copy of your
  worker starts with an empty store. Nothing to code; keep the list honest.
- **Selection-time data notes.** A frontend with remote-node support exposes optional
  `PLUGIN_API` members — `getRunTarget`, `onRunTargetChange`, `checkDataForRunTarget` (see
  `plugin-api.d.ts`) — and may annotate every table input in a GPU fragment with the verdict
  itself, announced by `PLUGIN_API.hostChecksTableInputs`; opt a field in or out with
  `data-run-target-check="table"|"off"`. Feature-detect the members (frontends without
  remote-node support have none of them) and call `checkDataForRunTarget` yourself only for
  bespoke placement, skipping it when `hostChecksTableInputs` is true.

**Listing what a provider offers** (infrastructure plugins): give a container the class
`tlc-catalog` and the worker injects `window.TlcCatalog` ahead of your script —
`TlcCatalog.mount(container, {columns, rows, rowId, sort, search, selectedId, onSelect, action,
details, unavailable})` renders a sortable, searchable table with one action button per row and
a row that opens to the details you return; `handle.update(rows)` re-renders with fresh data
and keeps the sort, search and open rows. Column types: `text` (with an optional `sub` line),
`num` (right-aligned, optional `unit`), `price` (`$/h`, missing prices sort last), `status`
(a coloured dot from `column.status(row)` → `{level: ok|warn|bad|muted, text}`). See
`tlc_plugin_sdk/shared/catalog_table.py` for the full option list.

**Folder downloads** (storage plugins): `tlc_plugin_sdk.shared.storage_bundle.BundleRegistry`
builds one zip from every object under a prefix on a background thread and reports progress;
you give it `list_objects(url)`, `open_object(key)` and `store_bundle(path, name) -> url`, and
expose `start`/`status`/`cancel` as `POST /infra/storage/bundle`, `GET /infra/storage/bundle/{id}`,
`DELETE /infra/storage/bundle/{id}`. Single files need no bundle: answer
`POST /infra/storage/presign` with `mode: "download"` with presigned GET links. Copies, moves
and renames inside one provider run the same way through
`tlc_plugin_sdk.shared.storage_transfer.TransferRegistry` (`POST /infra/storage/transfer`,
`GET`/`DELETE /infra/storage/transfer/{id}`); a failed copy never deletes its source. When a
transfer finishes — done, failed or cancelled — the registry tells 3LC object discovery about
every table, run or `.3lc.yaml` object it wrote or removed, so the Object Service lists the
moved objects without a rescan; a notification that fails is reported in the status
payload's `warnings`, never as a failed transfer. A plugin that deletes raw storage itself
calls `notify_storage_deletes(urls, removed_folders=...)` from the same module for the same
effect.

**Paths or URLs, one vocabulary** (import and export plugins): people point at a folder on the
machine the run reads from (`/data/coco`) or at a bucket (`s3://bucket/data/coco`), and the shared
data-source picker offers both for the current run target. `tlc_plugin_sdk.shared.url_utils` (`is_url`, `join_path_or_url`, `parent_of`,
`iter_files`, `read_bytes`, …) lets a plugin treat the two alike — URLs go through `tlc.Url`
and its adapters, the same transport and credentials that read and write tables, so no extra
cloud SDKs.

**When a table has landed** (importers, converters): end the same way every time. Inject
`tlc_plugin_sdk.shared.table_landed.table_landed_script()` and render
`_tlcTableLandedHtml(project, dataset, tableUrl)` — **Open in Project** (the Datasets tab with
the new table selected) and **Open in Dashboard** (built by the host via
`PLUGIN_API.dashboardUrl`, so it carries this deployment's object service; a plain
`dashboard_url + '?table='` concatenation works on a laptop and points at the wrong endpoint
anywhere else). The shared alias widget takes the root a new table goes to from the host — its configured
default (`GET /api/deployment/storage`) and the deployment's other locations through
`PLUGIN_API.data.getLocations()` — and never asks the worker; the value it sends travels as
`project_root_url` and comes back to your `run_job` as `ctx.project_root_url`.

Remote TCP workers run token-guarded (`--token` / `TLC_WORKER_TOKEN`: every request must
carry `Authorization: Bearer <token>`; on a provisioned node the agent mints a token per
worker, binds the worker to loopback and proxies the host's traffic to it) and may emit
`{"event": "ping"}` keepalives on the
job stream (`TLC_WORKER_STREAM_KEEPALIVE_S`) so provider proxies don't kill quiet
streams; a host that enables keepalives filters the pings before events reach any
consumer. Neither affects a local Unix-socket worker. Every worker also answers `GET /busy`
(`{"active_jobs": n}`, read before a remote worker's machine is torn down) and
`POST /jobs/cancel-all` (what a host calls after a restart it could not re-attach to);
plugins implement neither.

---

## Data inputs and run targets

A run reads data and writes data, and the run target decides what it can reach: a folder on the
compute host is not on a node, and a node may not have credentials for every bucket. The host
plans that before each run, so the person is asked once, in the Hub, instead of the job failing
on its first image.

### Declare the keys that carry data

```toml
[runtime]
data_inputs  = ["folder", "source_table_url"]  # data the run READS
data_outputs = ["output_path"]                  # places the run WRITES (not the table root)
```

Each entry is a dotted key into the JSON body your fragment posts to `/run` (or to a route listed
in `node_routes`): `"folder"`, `"source.table_url"`. The value there is a string, or a list of
strings; an empty value is skipped. A value that is a 3LC table URL (it contains `/tables/`) is
planned as a table — the aliases its rows use — and anything else as a folder, file, prefix or URL.
`harness.read_manifest()` validates both lists (`Manifest.data_inputs`, `Manifest.data_outputs`).
A plugin that declares neither gets the older behaviour: the host plans only table URLs it finds in
body keys whose name contains "table".

Leave out the project root: the host stamps that itself (`ctx.project_root_url`).

### A declared input is "where to read it for this run"

Before a run the host plans every declared value and asks the person about anything the target
cannot reach where it is: read a bucket in place or copy it to the node first, data that is already
on the node, a folder to use instead. It may then **rewrite the value in the run body** — to a
staged copy on the node, or the path the person gave. So a declared input means where to read the
data *for this run*, not where it lives:

- **Persist durable locations from your own field.** A plugin that registers an alias keeps the
  lasting location in its own form field (the shared alias card's `alias_folder`), never the
  rewritten input. Read the data from the input; persist the alias from `alias_folder`.
- **Register the alias before writing rows.** For a table written from a folder the person
  picked, call `register_run_alias(ctx.params, <the folder you read>, project_name=…,
  root_url=ctx.project_root_url)` before the table writer runs: rows written first keep absolute
  paths, and an absolute path from a node is useless everywhere else. It reads the alias card's
  values (including `alias_source_folder`, the folder as picked, which the host never rewrites)
  and applies one rule (`register_source_alias`): the saved alias points at `alias_folder`; when
  that is a folder above the picked one, the rows keep the subfolders between them
  (`<TOKEN>/sub/a.jpg`), also when the run reads a copy that kept the layout; a copy that did not
  is refused before any row. A plugin that writes rows into a folder it chose itself (no picked
  source) can keep calling `register_alias` directly.
- **A folder on the compute host cannot run on a node.** The host refuses such a run with a sentence
  saying to put the data in a bucket (or name where it is on the node); it does not copy a host
  folder to a node. Tables whose aliases point at host folders keep their own
  path: the host syncs that data through the project's bucket.

The Hub's run dialog replaces the per-plugin "read this data from somewhere else" card
(`shared.alias_override_ui`, deprecated): drop the card from your fragment. On a Hub that asks
(its `PLUGIN_API` has `planRun`) the card draws nothing.

### The shared widgets follow the run target

- **The data-source picker** (`shared.data_source_ui`) reads `PLUGIN_API.getRunTarget()` and
  re-reads it on `onRunTargetChange`. Each offered folder is a location labelled with its machine
  and folder. Node folders are browsed through the host, followed by buckets annotated with the
  node's storage check. Node runs offer no Upload (an upload lands on the compute host).
  Host operators configure `TLC_DATA_SOURCE_ROOTS` as an `os.pathsep`-separated list of absolute
  paths or tilde paths. The SDK `/browse` endpoint resolves and enforces these roots; unset, empty,
  relative-only or nonexistent roots offer no folders. Symlinks cannot escape them. An explicit
  empty `browse_roots` list offers buckets only, even on localhost. Older bridges without root
  metadata retain their previous picker behavior, but the updated browse endpoint still enforces
  explicit configuration. Update plugin workers to this SDK when adopting the policy.
  Offering a folder changes browsing only; it grants no extra file deletion, copy destination or
  runtime file-access permission. A value the target cannot reach gets a note under the field.
- **The alias card** (`shared.alias_ui`) configures an alias name and source root, with a live
  mapping. It follows both typed and picked source changes, while preserving manual edits.
  An automatically detected parent root stays until the source changes. Existing media stays at
  its source: deliberate relocation belongs in Storage, while execution staging belongs in the
  common run planner. Legacy `copyOffer` arguments and `_tlcAliasReviewCopy` no longer offer
  transfers; `_tlcGetAliasValues` returns false/empty legacy copy fields for older fragments.
  Plugins must not silently honor or ignore an obsolete explicit copy request: explain that the
  user should copy the data first and select the resulting source.

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
var jobId = null, cancelPressed = false;
PluginJobs.run('my-plugin', { table_url: url }, {
  onUpdate: function (job) {
    // generic schema: job.status, job.progress.percent/label, job.metrics[]
    setProgress(job.progress.percent, job.progress.label);
  },
  onDone: function (job) {   // completed AND cancelled: read job.status
    if (job.status === 'cancelled') { showCancelled(job.run_url); return; }
    showResult(job.run_url);
  },
  onError: function (job) { showError(job.error); },  // failure message on job.error
}).then(function (resp) {
  // The start answer carries the job's id: keep it from here for Cancel.
  if (!resp || !resp.job_id) { return; }   // refused: onError has already said why
  jobId = resp.job_id;
  if (cancelPressed) { PluginJobs.cancel(jobId); }
});

cancelButton.onclick = function () {
  cancelPressed = true;
  if (jobId) { PluginJobs.cancel(jobId); }   // else when the start answer brings the id
};
```

**`onDone` also ends a cancelled job.** It fires for `completed` and for `cancelled`; only
`failed` reaches `onError`. Read `job.status` before saying the job finished, whether the
Cancel was your page's or the Hub's (its Queue page can stop any job). A job stopped while it
waits for a slot or for its worker has done nothing, and ends `cancelled` with no `run_url`. A
cancel does not undo work already done: when `run_job` returns after a cancel was requested,
the job ends `cancelled` even if it wrote its output, so a cancelled job can carry the
`run_url` of what it wrote. Say that the output was written before the stop reached it. To let
a stop prevent the output, check `ctx.cancelled` before the step that writes it
([Long-Running Jobs](#long-running-jobs-run_jobctx)).

`run()` pre-subscribes and buffers, so a job that finishes between the `/run` response and
the client subscribing still delivers its terminal event. (`PluginJobs.start/track/cancel`
are the lower-level pieces.) The separate frontend's generic panel polls the same schema
independently.

**Keep the job's id from the start answer.** `run()` resolves with the `/run` answer,
`{job_id, status, namespace}`, and `resp.job_id` is the handle `PluginJobs.cancel()` takes. A
refused start resolves with `{error}` and also reaches `onError`; `run()` never rejects. Do not
learn the id from the first `onUpdate` instead: the `queued` update is emitted when the job is
submitted, often before the page's socket has connected, and is never replayed. The first
update a page then sees is the worker's first progress, after a queue slot and the worker's
start, which can be minutes; a Cancel pressed before it would have nothing to cancel. A Cancel
pressed before the answer itself arrives is remembered and sent when it does, as above.

One timing rule to know: `track()` and `on()` open the namespace socket **lazily**, on first
use, and SocketIO does not replay server→client events to a client that was not yet
connected. `run()` opens the socket before it posts but does not wait for it to connect, so a
job's first updates can be missed. A fragment that shows a job's `queued` state, listens for
custom events from the first second, or re-attaches to a job it did not start (seed-on-mount)
should warm the socket on mount: `PluginJobs.connect('/my-plugin')`.

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
// frontend — listen for it on the same namespace through the PluginJobs client, subscribed
// before run() so the first event is not missed; the returned function unsubscribes:
var off = PluginJobs.on('/my-plugin', 'epoch_metrics', function (d) { lossChart.push(d.epoch, d.loss); });
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

The SDK runtime mounts the fragment in a separate Compute-origin frame. The Hub
supplies an ordered shared-style manifest, also used for its own template links:

1. Shared fonts (Plus Jakarta Sans and JetBrains Mono)
2. `main.css` — theme tokens, typography, forms, buttons, cards and spinners
3. `3lc-neo.css` — Neo theme tokens and component treatments
4. `plugin-common.css` — hero, workflow, metric cards and viewer scaffolding
5. `controls.css` — shared controls
6. `connection-status.css` — connection indicators
7. `node-list.css` — node components

These are host-provided assets, not imports a plugin should hardcode. The runtime
receives them in cascade order, as URLs or inline CSS according to its asset
capability. The Hub synchronizes the selected theme and subsequent theme changes.
See [the browser runtime contract](browser-runtime.md#shared-styling).

Write `class="btn btn-primary"` or `class="form-control"` to get the themed component
without custom CSS. Prefix bespoke classes with your plugin id (`.myplugin-…`) and
never restyle a shared class globally: your widgets share the frame's CSS namespace.

The frame document has a transparent canvas; cards and controls retain their themed
surfaces. Hub shell styles (outer background, sidebar, navigation and viewport
layout), Hub body classes and root inline styles are not part of the styling
contract. Neither are page-template-only classes such as `.pbadge`. Do not depend
on them or copy Hub layout resets into your fragment. Use the documented `flow`
or `fill` layout for your page.

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

**Dropdowns.** On a Hub 1.2 or later, a `<select>` opens the Hub's own list, in the theme's colours, instead of the operating system's menu. There is nothing to do for it: the `<select>` stays the control your code reads and sets, and a pick fires `input` and `change` as before. So don't build a dropdown of your own just to pick a value. Multi-select lists, `size > 1` and touch devices keep the native control. To keep the system menu for one select, put `data-native-select` on it or on an element around it.

In the list, the ✓ marks the selected row on the left. A select's options can show a muted hint at the row's end with `data-hint`, and be removable with `data-removable` once you say how to remove one:

```js
select.innerHTML = '<option value="p1" data-hint="2 days ago" data-removable>Small objects 1280</option>';
if (typeof TlcPicker !== 'undefined' && TlcPicker.onRemove) {
  TlcPicker.onRemove(select, option => deletePreset(option.value));   // returns a promise
}
```

The trash sits in a column under the chevron and removes the row at once, with no question. The list stays open, so several can go in a row. Your function deletes the thing and rebuilds the options; the list follows them. A rejected promise shows its message in the row. On a Hub without `onRemove`, `data-hint` and `data-removable` are ignored, so keep another way to delete.

For a menu that is not a plain select, show the same list with `TlcPicker.open(anchor, {items, value, onPick})`. A Hub before 1.2 has no `TlcPicker`, so feature-detect it and fall back to a `<select>`:

```js
if (typeof TlcPicker !== 'undefined') {
  TlcPicker.open(button, {
    value: currentPresetId,
    items: [
      { value: 'defaults', label: 'Defaults' },
      { separator: true },
      { value: 'p1', label: 'Small objects 1280', hint: '2 days ago',
        remove: { run: item => deletePreset(item.value) } },
    ],
    onPick: item => applyPreset(item.value),
  });
}
```

Items can also be `{group: 'Saved'}` headers. `remove.run` returns a promise: the row dims while it runs, goes when it resolves, and shows the error when it rejects. Give `remove.confirm` a question to ask in the row first. See `TlcPickerApi` in `plugin-api.d.ts` for the rest.

**Table fields.** For a field that holds a table URL, write the Hub's `<tlc-table-field>` instead of your own `<input>` and Browse button:

```html
<div class="form-group">
  <label class="form-label required" for="source-table">Source table</label>
  <tlc-table-field id="source-table" placeholder="Paste URL or click Browse…" required></tlc-table-field>
</div>
```

It is a text field with a Browse… button. Browse…, or a double-click in the empty field, opens the Hub's Browse Tables dialog. The element is the field, so your code stays as it was: `document.getElementById('source-table').value` reads and writes the URL, and `input` and `change` from it reach `oninput=` and your listeners, for a typed URL and a picked one alike. Browse Tables opens at the table the field holds, and tags the tables your other table fields hold with their labels ("Source table"), so a second pick starts where the first one is. Label the field with a `<label for>`, or give it a `label` attribute.

For a list of tables, put one field below the list to add to it: take its value on `change`, add it to the list, and empty the field. Set the field's `tagged` to the list's tables (`field.tagged = urls.map(url => ({url, label: 'Added'}))`) and Browse Tables marks them as this field's selection, so it shows what is already in the list.

A Hub before 1.2 doesn't define the element, so a plugin that uses it sets `min_frontend_version = "1.2.0"` in `plugin.toml`. The bare `openTablePicker(inputId)` still works for a field you build yourself, without the double-click or the tags. See `TlcTableFieldElement` in `plugin-api.d.ts`.

**Path fields.** For a file or folder on the machine the run reads from, or in a bucket, render the SDK's data-source field: `_tlcDataSourceHtml(id, {placeholder, accept})` for the markup and `_tlcBindDataSource(id, computeUrl, pluginId, {mode, purpose, accept, suggestedName})` once it is in the DOM (`shared.data_source_ui`; its docstring has the full usage). Browse…, or a double-click in the empty field, opens a file chooser under the field: a Location line naming the place being browsed (a folder of the run target's machine, or a bucket; a dropdown when there are several), a path bar that follows the browsing, and the rows. A click opens a folder or picks a file (an output picker puts the file's name in its box instead); the arrow keys highlight a row, Enter opens a highlighted folder or picks a highlighted file, Backspace goes up and Escape closes. A file picker has no buttons: the click is the pick. A folder picker (`mode: 'folder'`) dims files and has a footer with Cancel and "Select this folder", which picks the folder shown. For an input that takes a file or a folder, such as COCO annotations (one JSON file, or a folder of them), pass `mode: 'any'`: a click on a file picks it, as in a file picker, and the footer's "Select this folder" picks the folder shown. An SDK before `'any'` reads it as `'file'`. The picked path lands in the field and fires `input` and `change`.

For a field that names where a job will *write*, pass `purpose: 'output'`: the footer gains a name box, prefilled from `suggestedName`, and the pick is `<folder shown>/<name>`. A file output's Save adds the first extension in `accept` when the name has none, and a click on a file puts its name in the box ("Replaces <name>"). A folder output's box names a new folder under the one shown, or an existing one ("Adds to <name>"); empty, it picks the folder shown. The job creates the folder. `mode`, `accept` and `suggestedName` may be functions, read every time the chooser opens, for a form whose choice changes them, such as an export format; when that choice changes while the chooser may be open, call `refresh()` on the handle the bind returns:

```js
const chooser = _tlcBindDataSource('export-output', computeUrl, 'exporter', {
  purpose: 'output',
  mode: () => format() === 'yolo' ? 'folder' : 'file',
  accept: () => '*' + extensionOf(format()),
  suggestedName: () => suggestedNameFor(table(), format()),
});
onFormatChange(() => chooser.refresh());
```

### Presets

A plugin whose form is worth keeping (a training setup, a metric selection) gets the Hub's preset bar from the SDK rather than a bar of its own. It is one line at the top of the form:

```
Preset  [ Small objects 1280 · modified   ⌄ ]  Save changes  Save as…
```

A preset contains the complete setup: tables, project, destination, run name, model and processing parameters. Explicitly choosing a preset restores that setup. Only explicit saves update a preset; editing the form or starting a run never does.

The select shows Default settings, Default settings · modified for an unsaved setup, or the selected preset with "· modified". Save changes updates the selected preset; Save as… opens a compact naming dialog while the preset bar stays in place. The suggested name is selected for editing. Enter saves, Escape or Cancel dismisses without changing the setup, and a taken name asks before replacement. Failed saves keep the dialog open with the error and name intact. Saved presets retain their summary, last-use hint and delete action.

Opening from external tables/folders always starts a fresh setup: defaults plus those inputs and their project, marked modified. It never restores an older draft, even for the same inputs. Opening without external inputs can resume an unfinished draft scoped to deployment, plugin, storage and project. Old unscoped drafts and config selections are ignored. Saved presets live in the plugin's server-side config store; drafts live in browser storage.

Table URLs and revision-selection flags are saved as entered. A setup using fixed revision URLs reopens those revisions. A setup using "latest revision" deliberately follows new data; it is not a frozen record of a completed run. Keep the actual resolved inputs and parameters in the run's own record.

Inject `preset_ui_script()` from `tlc_plugin_sdk.shared.preset_ui` with `inject_scripts()`, put an empty `<div id="my-presets"></div>` where the bar goes, and wire it:

```js
var presets = _tlcPluginPresets({
  pluginId: 'my-plugin',
  apiPath: '/api/plugins/my-plugin/configs',   // the config CRUD routes, see below
  mount: 'my-presets',
  formRoot: 'my-form',                          // input/change events under it mark the form as changed
  collect: function () { return { table_url: field('table').value, epochs: +field('epochs').value }; },
  apply: function (v) { field('table').value = v.table_url || ''; field('epochs').value = v.epochs; },
  defaults: function () { return { table_url: '', epochs: 10 }; },
  inputs: ['table_url'],
  contextInputs: function () {
    var urls = PLUGIN_API.context.resourceUrls || [];
    return urls.length ? { table_url: urls[0] } : null;   // null when opened bare
  },
  autoName: function (v) { return 'resnet50 · ' + new Date().toISOString().slice(0, 10); },
  summary: function (v) { return [v.model, v.epochs && v.epochs + ' epochs']; },   // empty parts are left out
});
presets.init();
```

- **Routes.** The config CRUD a `PluginConfigStore` plugin already serves: `GET /configs`, `POST /configs` (an empty `id` creates, an `id` replaces), `GET /configs/{id}`, `POST /configs/{id}/delete`. Saved configs appear as presets as they are.
- **`collect` and `apply`** move the whole form, without `id` and `name`. Return a promise from `apply` when filling the form takes a round trip (a model's parameters fetched after the model is set): the bar compares the form only once it resolves.
- **Context.** `inputs` names tables and folders (dotted paths supported). A nonempty launch input means a new setup. `contextInputs()` supplies its initial tables and project; later explicit preset picks restore the saved form in full. All collected values participate in saving and modification checks.
- **Primary table changes.** Set `tableContext: {input: 'train_table_url', project: 'project_name', preserve: ['mode']}` for table-driven forms. After successfully opening the table, return `table_project_context(table)` from `tlc_plugin_sdk.shared.table_context` alongside task metadata. It uses the core's resolved URL and project layout, including the storage root, without reading rows. Before updating task/model controls, await `presets.tableResolved(url, metadata)`. If it returns `true`, the controller already applied the new setup; stop that outer reader. During an explicit preset/draft/Undo application it records identity only, preserving saved output overrides. Reject stale metadata replies in the plugin using both a request generation and the current URL.
- **Reset policy.** A manually changed primary table always derives the output project. Within the same source project, settings stay. Across source projects, defaults replace settings, validation inputs and run names; only the primary table and explicitly listed `preserve` fields (such as the current operation) survive. The bar becomes Default settings · modified and offers a temporary Undo until the next edit. Validation-only changes do not reset. No new named preset is created. Drafts follow the source project/storage; partial or failed primary-table reads cannot overwrite the preceding complete draft.
- **Reusing metadata.** `apply(values, resolvedTable)` receives an optional `{url, info}` for resets and Undo. Reuse it when the URL matches, and still call `tableResolved`; this establishes identity without a second metadata request. Cancel pending input debounces when applying values. Reset every form field, and return a promise covering all model/task normalization before the controller compares or saves the form.
- **JS state.** Call `presets.changed()` after a change no DOM event reports, such as a card toggled in a JS object.
- **The run body.** Send `project_config: Object.assign(collect(), { id: presets.presetId(), name: presets.presetName() })`. Both are set while the form is unchanged from a preset, so the job card can name it, and empty otherwise. A run never saves a preset.
- **Accepted submission.** After receiving a valid job id, clear the form's run name and call `presets.submitted()` to remove that context's draft. Failed or cancelled submissions must leave it intact. The accepted job retains the full submitted configuration.
- **Last use.** When a run sent with a preset's id finishes, stamp it: `config_store().update_last_run(config_id)`, best-effort, as the list shows it. A worker on another machine has its own store, so a run there leaves no stamp.
- **Where saved configs live.** A `PluginConfigStore` keeps a plugin's configs in `<root>/<plugin-id>/`, where the root is `config_store.config_root()`: the `TLC_PLUGIN_CONFIG_DIR` environment variable when the host sets it, so several instances on one machine keep separate configs, else `~/.3lc-plugin-configs`. The host passes the variable to its workers; build stores through `PluginConfigStore` and never hard-code the path.

`_tlcPluginConfig` (`shared.config_ui`), the earlier config bar whose Train saved into the selected config, is superseded by this; it stays for the plugins still on it. See `TlcPresetOptions` and `TlcPresetControl` in `plugin-api.d.ts`.

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
9. **Results** — `PLUGIN_API.showResult(el, result)` for the outcome of a run, so every plugin's result looks and closes the same.
10. **Dark mode** — must work; only `var(--*)` colors, and confirm the raised/flat card distinction actually reads.

### What NOT to do

- Don't hardcode colors — always a `var(--…)` token.
- Don't inline surface/elevation styles (`background`, `box-shadow`) on shared classes — let the class do it.
- Don't define custom button, input, or card styles — the shared classes exist.
- Don't restyle a shared class globally, and prefix your own classes with the plugin id.
- Don't reach for host-template-only classes like `.pbadge` — they aren't inherited.
- Don't create custom modal/dialog implementations — use `.card` with show/hide.
- Don't build your own result banner — use `PLUGIN_API.showResult`.
- Don't use `position: fixed` — it breaks the plugin container.
- Don't add font sizes below 10px or custom scrollbars.
- Don't skip a dark-mode check.

### Check a fragment

`tlc_plugin_sdk.lint` checks a fragment against the rules above that a machine can check: no hardcoded colours
(`hardcoded-color`), no `background`, `box-shadow` or `color` inline (`surface-inline-style`), no `!important`, no
`position: fixed`, every rule in the fragment's `<style>` scoped by a class or id that starts with the plugin id
(`unscoped-selector`), and no rule that styles a shared `.plugin-*` class (`shared-class-restyled`). It reads the
markup, the `<style>` block and the strings a script builds HTML from, and it leaves a `var(--token, #fallback)`, a
link and a comment alone.

```bash
python -m tlc_plugin_sdk.lint src/                            # every plugin under src/
python -m tlc_plugin_sdk.lint src/tlc_plugin_my_plugin/ui.html
```

Each finding names the file, line, column and rule. To keep a clean fragment clean, test it:

```python
from tlc_plugin_sdk.lint import lint_plugin

def test_the_ui_follows_the_hub_styling_conventions() -> None:
    findings = lint_plugin("src/tlc_plugin_my_plugin")
    assert not findings, "\n".join(map(str, findings))
```

---

## Existing Plugins Reference

The plugins 3LC maintains, as of Hub 1.2. Read the one closest to what you are building.

| Plugin ID | Kind | GPU | Repo | What it does |
|---|---|---|---|---|
| `example` | compute | — | [3lc-compute-plugin-example](https://github.com/3lc-ai/3lc-compute-plugin-example) | Mission Control: a runnable tour of the whole contract, with tests |
| `importer` | compute | — | [3lc-compute-plugins](https://github.com/3lc-ai/3lc-compute-plugins) | Import data (YOLO, COCO, folder, CSV, unlabeled); the data-source picker and alias card |
| `exporter` | compute | — | 3lc-compute-plugins | Export tables to CSV, XLSX, YOLO, COCO; an output path field |
| `splitter` | compute | — | 3lc-compute-plugins | Split tables into train/val/test sets |
| `merger` | compute | — | 3lc-compute-plugins | Merge tables (an `action` on selected tables) |
| `image-metrics` | compute | — | 3lc-compute-plugins | Image quality metric columns |
| `table-statistics` | compute | — | 3lc-compute-plugins | Per-column stats and thumbnails, `hidden` (API-only, drawn inline by the Hub) |
| `yolo` | compute | GPU | [3lc-compute-plugin-yolo](https://github.com/3lc-ai/3lc-compute-plugin-yolo) | Ultralytics YOLO fine-tuning with presets, metrics collection |
| `sam3` | compute | GPU | [3lc-compute-plugin-sam3](https://github.com/3lc-ai/3lc-compute-plugin-sam3) | Auto-labeling with SAM3; `node_routes` for a preview on the node |
| `timm` | compute | GPU | [3lc-compute-plugin-timm](https://github.com/3lc-ai/3lc-compute-plugin-timm) | Image classification fine-tuning with timm models |
| `rf-detr` | compute | GPU | 3lc-compute-plugin-rf-detr (private) | RF-DETR detection fine-tuning |
| `huggingface` | compute | — | 3lc-compute-plugin-huggingface (private) | Browse, preview and import Hugging Face datasets; share tables back; a SECRET token on routes and jobs |
| `kaggle-exdark`, `kaggle-classification` | compute | GPU | [3lc-compute-plugin-kaggle](https://github.com/3lc-ai/3lc-compute-plugin-kaggle), [-kaggle-classification](https://github.com/3lc-ai/3lc-compute-plugin-kaggle-classification) | Competition starters, installed from GitHub through the catalog |
| `run-insights`, `table-insights` | compute | — | 3lc-compute-plugin-insights (private) | Run and table analysis, as `action`s on selected runs and tables |
| `jupyter` | compute | — | 3lc-compute-plugin-jupyter (private) | A notebook server beside the data |
| `aws`, `azure`, `runpod`, `machines` | infrastructure | — | 3lc-compute-plugin-aws / -azure / -runpod / -machines (private) | Node providers: GPU nodes, workspaces, storage, through Connections |

The open first-party plugins are all `remote_capable` and declare their `data_inputs` /
`data_outputs`; none of them needs a GPU. The GPU trainers and the infrastructure providers
each live in a repo of their own.

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
# or everything under the folder at once (re-provisions when deps changed):
curl -X POST http://localhost:5020/api/admin/plugins/dirs/reload \
  -H 'Content-Type: application/json' -d '{"directory": "/path/to/repo/src"}'
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

The folder Source serves one host's own workers. To try the plugin on a remote node, or on
another deployment, publish it the way the people installing it will: a
[catalog](#publishing-via-a-catalog) naming a git ref or an index release.

## Testing a plugin

Most of a plugin is testable with no compute service, no Hub and no browser. The template and
the example plugin ship a `tests/` that does all three of these:

- **Routes, `/compute` and `/ui`** through `tlc_plugin_sdk.harness.PluginHarness`: it builds the
  same Litestar app the worker serves and calls it in-process, loaded from the manifest the way
  a host would (`PluginHarness.from_manifest("src/tlc_plugin_my_plugin")`; `h.get(path,
  params=…)`, `h.post(path, json_body=… | content=…, headers=…)`). The harness adds nothing to a
  request: a token or a Connection a host would stamp, the test sends as a header.
- **`run_job`** with a `JobContext` whose `sink` is a list and whose `cancel_event` is a
  `threading.Event`: the recorded `progress` / `metric` / `log` / `result` / `custom` dicts are
  exactly what the host streams, so a test can assert the generic panel's view, a `ctx.fail()`
  (`pytest.raises(JobFailed)`) and a cancel that unwinds cleanly. No GPU, no queue.
- **The fragment** with `tlc_plugin_sdk.lint.lint_plugin(dir)` — see [Check a fragment](#check-a-fragment).

`uv sync --extra <your extra>` gives the venv the host would provision (plus pytest from the
`dev` group), and `uv run pytest -q` runs it all; the template's CI does the same. Infrastructure
plugins have a conformance kit on top ([Infrastructure plugins → Testing](#testing)).

---

## Version & Compatibility

All versions use **SemVer** (`MAJOR.MINOR.PATCH`).

### Two kinds of version — never conflate them

There are **two independent** kinds of "version" in play. Keep them separate:

**(a) CONTRACT — what a plugin *programs against*.** Pinned at build/install time via the
`3lc-compute-plugin-sdk` dependency. There is **one contract axis**, `tlc_plugin_sdk.SDK_CONTRACT_VERSION`:

| Constant | Covers | Value |
|----------|--------|-------|
| `SDK_CONTRACT_VERSION` | the whole contract — the Python surface (`HubPlugin` / `ComputePlugin` / `InfrastructurePlugin` / `JobContext` / `shared.*`) *and* the browser surface (`PLUGIN_API` / `PluginJobs` / `TlcData`, see `plugin-api.d.ts`); also the dependency *pin* (`3lc-compute-plugin-sdk>=X,<Y`) | = package version (e.g. `0.6.1`) |

It is this package's own version — one source of truth. A plugin that needs a newer capability
raises its `3lc-compute-plugin-sdk` floor; the host and frontend implement a range and compare
compatibility on `MAJOR.MINOR`. (Earlier 0.x lines split this into separate `PY_CONTRACT` /
`JS_CONTRACT` markers; those were removed in 0.3 — there is one axis now.)

Each Hub release implements one line: the Compute Service and the Hub **1.2** implement the
**0.6** line, so a plugin pinning `>=0.6.1,<0.7.0` sets `min_service_version = "1.2.0"` — a
1.1 service would refuse its worker on contract skew anyway, and the manifest floor turns that
into a greyed-out card with a reason. The Hub reports the line it implements as
`PLUGIN_API.contractVersion` (`"0.6"`), and a plugin's worker reports its SDK version on
`/health`; the plugin card shows the skew when the two lines differ.

**Per-feature SDK floors.** The typed request models drop keys they do not know, so a plugin
whose SDK predates a field the host sends does not fail: it answers as if the field was never
sent. `MAJOR.MINOR` cannot catch that within a line, so the host also keeps a table of features
and the first SDK release that carries each one's fields. Before it sends such a feature it
compares the floor against the SDK version the plugin's worker reports on `/health` and, when the
worker is older, refuses the request with a sentence naming the plugin, its SDK version and the
floor; the plugin card carries the same sentence. A pre-release of the floor counts as the floor,
and an unknown version refuses nothing.

At Compute 1.2 that table is empty: every field the host sends exists on the whole 0.6 line,
so a plugin on the line is never refused a feature. The mechanism stays for a later release
that adds a field inside a line.

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
min_service_version = "1.2.0"   # Minimum compute service version: the one implementing your SDK line
max_service_version = ""        # Maximum service version (empty = no upper bound)
min_frontend_version = ""       # Minimum Hub version for this plugin's UI (empty = none; "1.2.0" for <tlc-table-field>, TlcPicker, showResult)
```

### When to Bump Versions

| Change | What to bump |
|--------|-------------|
| Bug fix in plugin logic | Plugin `version` PATCH (1.0.0 → 1.0.1) |
| New feature in plugin | Plugin `version` MINOR (1.0.0 → 1.1.0) |
| Plugin moves to a newer SDK line | Plugin `version` MINOR + the SDK pin + `min_service_version` |
| Plugin UI uses a newer Hub feature | Plugin `version` MINOR + `min_frontend_version` |
| Breaking change to plugin UI/API | Plugin `version` MAJOR (1.0.0 → 2.0.0) |

### Compatibility Behavior

- **Compatible** plugins load normally and appear in the sidebar.
- **Incompatible** plugins are still loaded but **disabled** — visible in the sidebar with an "update" badge, grayed out, not clickable. Users can see what's available but can't use it until the service is updated.
- The Plugins page shows an "Incompatible" badge with the reason.

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
  "generated_at": "2026-10-31T00:00:00Z",
  "plugins": [
    {
      "id": "my-plugin",
      "latest": "1.0.0",
      "versions": [
        {
          "version": "1.0.0",
          "source": "github:your-org/your-repo@v1.0.0",
          "manifest": {
            "id": "my-plugin",
            "name": "My Plugin",
            "version": "1.0.0",
            "min_service_version": "1.2.0",
            "ui": { "display_mode": "sidebar", "section": "Tools" },
            "runtime": { "entrypoint": "tlc_plugin_my_plugin:MyPlugin", "provision_extra": "my-plugin" }
          }
        }
      ]
    }
  ]
}
```

`latest` names the release a host offers. Without it the newest-sorting compatible version is
offered, and only upward; with it a prerelease can be listed without being offered, and a
release withdrawn by moving the tag back — hosts that follow their catalogs move with it. Keep
every release you still want installable in `versions`.

### The `source` field

`source` is the install spec the host hands off unchanged — it accepts the same forms
whether or not your plugin has a published wheel:

| Your plugin is... | `source` looks like |
|---|---|
| a plain git/GitHub repo, no published wheel (the common case for a one-off or a plugin you're sharing to test) | `"github:your-org/your-repo@v1.0.0"` or `"git+https://github.com/your-org/your-repo.git@v1.0.0"` |
| published to a package index | `"my-plugin==1.0.0"` or `"my-dist[extra]==1.0.0"` |

A host's default install policy, `catalog-only`, installs nothing a **catalog** did not
list — a bare git URL needs an operator to widen the policy first (`catalog+index`, `any`).
Wrapping your repo in a catalog like the one above is the normal path for a git-hosted
plugin, not a workaround: a catalog-listed source is trusted whatever its kind.

### Trying it out

Host the JSON somewhere reachable — a raw GitHub file URL is the easiest. Which catalogs a
compute service reads is **configuration**, because a catalog decides what code gets
installed: the person running the service names yours in the environment it starts with,

```bash
TLC_COMPUTE_PLUGIN_CATALOG_URLS=https://raw.githubusercontent.com/your-org/your-repo/main/catalog.json
```

or as `compute-service.plugins.catalog-urls` in the service's configuration file — or, for a
deployment, in a Config Service scope, so every host of an organisation reads it. A change
applies at the host's next catalog read, with no restart. The Hub's Plugins page lists the
catalogs in force under **Plugin sources**, and your plugin shows up as a card: **Add**
registers it on the deployment without building anything, and the first visit (on the host
or a node the person chose) materializes its venv from your `source`. (A local `file://` path
works too for testing on your own machine; plaintext `http://` is only accepted for a loopback
host.)

If your card doesn't show up, check `GET /api/admin/plugins/catalogs` on the compute service:
it lists each catalog it reads, the tier that contributed it and the error if it could not be
fetched.

## Checklist

- [ ] `plugin.toml` manifest present with all metadata (id, name, `[ui]`, `[runtime]`)
- [ ] `runtime.entrypoint` points at the behavior class (`"pkg.module:ClassName"`)
- [ ] Plugin class subclasses `ComputePlugin` (or `HubPlugin`) — behavior-only, no metadata attrs, no `register()`
- [ ] `version` set to meaningful SemVer (not just "1.0.0")
- [ ] `min_service_version` set to the Compute Service that implements the SDK line you pin (`1.2.0` for the 0.6 line); `min_frontend_version` only when the UI needs a Hub 1.2 feature
- [ ] `[runtime] data_inputs` / `data_outputs` name the run-body keys that carry data; `remote_capable` says whether a run may leave the host
- [ ] Tests run the routes through `tlc_plugin_sdk.harness`, `run_job` through a recording `JobContext`, and the fragment through `tlc_plugin_sdk.lint`
- [ ] `icon_svg` set to an inline 16x16 SVG literal in the manifest
- [ ] `get_ui_fragment()` returns self-contained HTML+CSS+JS
- [ ] UI uses `PLUGIN_API` bridge (never raw `fetch` without auth)
- [ ] No plugin-specific logic in frontend code (plugin boundary)
- [ ] Custom CSS uses `var(--*)` variables, not hardcoded colors
- [ ] Job progress follows the generic schema (no plugin-specific fields in frontend)
- [ ] If GPU-bound: `requires_gpu = true` in `[runtime]`; long work is `run_job(ctx)` — never grab a queue
- [ ] If creating tables from images: registers URL aliases via `tlc_plugin_sdk/shared/aliases.py` + `tlc_plugin_sdk/shared/alias_ui.py` (inject with `inject_scripts()`). The alias is not optional — the shared widget no longer offers to turn it off (a table written without one is full of absolute paths that resolve on one machine only); the token and folder stay editable in the alias card, and data inside the project is aliased relative to the table so the token survives a move. Register the alias before writing rows, from the durable folder (`alias_folder`), not a declared input the host may rewrite for the run (see [Data inputs and run targets](#data-inputs-and-run-targets))
- [ ] UI follows the page structure and card conventions from "Styling & UI Conventions" above
- [ ] Hero section with icon, title, description, and 3 feature badges
- [ ] Preset bar (`preset_ui_script()`, guide → Presets) if the plugin has saved setups
- [ ] Dark mode works correctly (no hardcoded colors)

---

## For AI coding agents

Agent-facing guidance for building a plugin end-to-end (reading order, step-by-step,
code patterns, common mistakes, testing) lives in this repo's
[`CLAUDE.md`](https://github.com/3lc-ai/3lc-compute-plugin-sdk/blob/main/CLAUDE.md). The
[plugin template](https://github.com/3lc-ai/3lc-compute-plugin-template) ships a
`/create-plugin` skill that turns a one-line description into a working plugin in a repo
made from it, with a condensed copy of this contract beside it.

## Which 3LC project a job belongs to

The Hub's project page shows a job in its **Queue & Progress** panel only when the host knows the job's
3LC project. The host resolves it once, when the run starts, in this order — nothing comes from the browser
address:

1. `project_name` in the run body. The SDK's job starter sets it from the launch context; a plugin that knows
   the project can set it explicitly, and an explicit value always wins.
2. Any table or run URL in the body. The Object Service lays projects out as `…/projects/<project>/…`, so an
   inline config with `train_table_url` names its project by itself.
3. The plugin's saved project config. A body that carries only `project_id` refers to a record the plugin
   serves at `GET /projects/{project_id}`; the host reads its `project_name` (the **3LC Project** field every
   training form has). Keep that route and field if your plugin saves its configs.

While the job runs, any event whose payload carries `project_name` (or a `run_url` under a project) also sets
the project if it was still unknown. Emit `ctx.result(run_url)` as soon as the run exists and the panel
catches up even when the start carried nothing.


### Alias location validation

The shared alias card suggests a name from the source root; an explicitly edited name stays fixed,
and clearing it restores the suggestion. Bind it with `_tlcBindAliasAutoUpdate` and check
`_tlcAliasLocationError(prefix)` before submitting a table-creation request. It explains a local
source/cloud project mismatch beside the mapping and follows changes to the selected project root.

`register_alias` enforces the same rule before either persistent or session registration: a cloud
project cannot default to a machine-local source. It raises `JobFailed` for that combination; call
it before constructing a table writer. Omitted roots use `tlc.config.project_root_url`, matching the
writer. Validate the durable source, never a temporary stage: `image_folder` may be local when
`remote_path` names the lasting cloud source. Import does not relocate data: for local data and a
cloud project, choose a local project location, or upload the folder to the bucket first (for
example with `aws s3 sync`) and select the cloud source.

## Table data that must be local

A plugin whose image reader requires filesystem paths declares
`requires_local_table_data = true` in `[runtime]`. This is independent of
`requires_gpu`: it applies on the deployment host and on an added worker.
Leave it unset for readers that support cloud URLs through `tlc`.

The shared preflight shows a copy hint without moving data. Starting a run opens
the data confirmation; only confirming starts preparation. The host offers copy
or an existing local path, never streaming for such a plugin. The data-copy job
runs ahead of the dependent run on the same execution queue. Its resolved alias
overrides apply to that run only; the original table and project aliases remain
unchanged. A failed or cancelled copy prevents execution of the dependent run.

## Isolated browser runtime

For SDK 0.7.0 framed hosts, follow the [browser runtime guide](browser-runtime.md) and [agent instructions](agent-guide.md).
The framed runtime supersedes legacy inline/global browser examples in this document.
