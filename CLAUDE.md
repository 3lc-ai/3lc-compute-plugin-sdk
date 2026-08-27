# 3lc-compute-plugin-sdk — agent & contributor orientation

This repo is the **public Python plugin SDK** for the 3LC compute service: the import-light
contract a plugin programs against. The compute-service host that discovers and runs plugins lives
in a separate repository and is **not** a dependency of this SDK.

## What this package is (and is not)

- **Is:** the contract surface — `ComputePlugin`, `JobContext`, the worker entrypoint, and the
  `tlc_plugin_sdk.shared.*` helpers. Distribution `3lc-compute-plugin-sdk`, import `tlc_plugin_sdk`.
- **Is not:** the host. The compute service (`3lc-compute` / import `tlc_compute`) is a separate
  package that *discovers and runs* plugins. It is not a dependency of this SDK and its source is
  not here.

## The invariants — do not break these

1. **No back-edge to the host.** `tlc_plugin_sdk` must never import `tlc_compute` (or anything
   host-side). The SDK is the root of the dependency graph; the host depends on the SDK, never the
   reverse. The whole point — a plugin built against just this wheel runs in its own isolated venv.
2. **Import-light.** Importing `tlc_plugin_sdk` must not eagerly pull the server stack *or the data
   plane*. `litestar`, `uvicorn`, and `tlc` are all base deps but are imported **lazily** — the
   server stack only when a worker actually serves, `tlc` only inside the `shared.*` helpers that
   use it; and the host-only SocketIO server must not be importable from here at all.
   `tests/test_import_light.py` enforces this (guards `litestar`/`socketio`/`uvicorn`/`tlc`) —
   keep it green. Import-light is about worker startup time, not about what is installed.
3. **Dependencies stay minimal.** Base = `uvicorn` + `litestar` + `3lc[pandas]`, nothing else.
   `3lc` is deliberately a base dep, not an extra: every real plugin uses the data plane, and making
   it optional only created a way to build a venv that imports fine and fails on first use. (The
   `[shared]` extra it used to live behind is an empty, deprecated alias — gone at 1.0.) Adding a
   base dep widens what every plugin venv must install — justify it.
4. **The contract is published — every public symbol is forever-ish.** The 0.x line is
   additive-only (see README → Status): additions are the safe move; reshaping
   `JobContext`/`ComputePlugin` waits for a major bump. The version is the contract version
   (see below).

## Versioning

`SDK_CONTRACT_VERSION` is read from this package's own version via `importlib.metadata` — one
source of truth (`[project] version` in `pyproject.toml`). Bump it (SemVer) when the contract
changes; the 0.x line is additive-only (README → Status). Plugins pin a range
(`3lc-compute-plugin-sdk>=X,<Y`); the host implements a range. Don't reintroduce a
separately-maintained version constant.

## Where the rest of the context lives

The author guide and API reference travel with this repo (`docs/plugin-guide.md`, `docs/api.md`)
and are published at <https://3lc-ai.github.io/3lc-compute-plugin-sdk/>. That guide is the canonical,
self-contained source for building a plugin against this contract — including how isolation works
and how to port an existing plugin.

## Dev setup

`3lc` resolves from public PyPI. To develop against an unreleased `3lc`, override locally
(uncommitted) with an editable path source in `[tool.uv.sources]`.

```bash
uv sync                 # installs the SDK + dev tools (ruff, mypy, pytest)
uv run ruff check .
uv run mypy src/
uv run pytest
```

## Conventions

Python 3.10+, uv, Hatchling, Litestar, Ruff (line-length 120), mypy `--strict` clean (no
`# type: ignore`, no `Any` used to silence the checker). Google-style docstrings. Copyright header
on new files.

## Building a new plugin (agent guide)

Reference for building a plugin against this SDK. Read `docs/plugin-guide.md` first — this
section is the condensed working order.

### Before you start

1. Read the full plugin guide (`docs/plugin-guide.md`).
2. Read an existing plugin closest to what you're building — the open first-party plugins live
   in [`3lc-compute-plugins`](https://github.com/3lc-ai/3lc-compute-plugins) under
   `src/tlc_plugin_<name>/`, and the GPU training/labeling plugins in their own repos
   (`3lc-compute-plugin-timm` / `-sam3` / `-yolo`):
   - **Form → execute → result**: `tlc_plugin_exporter` or `tlc_plugin_importer`
   - **Config + GPU job + SocketIO**: sam3, yolo, or timm
   - **Inline data display (no standalone page)**: `tlc_plugin_table_statistics`
   - **Action on selected resources**: `tlc_plugin_merger`
3. Read `src/tlc_plugin_sdk/contract.py` — `ComputePlugin` is an `abc.ABC` with one abstract
   method (`get_ui_fragment()`) and an `id` attribute the host stamps from the manifest.
   `compute()` and the other hooks (`run_job`, `initialise_runtime`, `shutdown_runtime`,
   `get_route_handlers`) ship as defaults (`compute()` reports that the plugin doesn't
   implement it), so you override only what you need. There is
   no `register()` and **no** `get_active_jobs`/`cancel_job` — the host owns job listing and
   cancellation.
4. Read `tlc_plugin_exporter`'s `plugin.toml` + `__init__.py` — the canonical manifest +
   subclass exemplar.

### Step-by-step

1. **Create the package**: the standalone `tlc_plugin_<name>/` shape from the guide's
   "Create the plugin directory" section.
2. **Create `plugin.toml`** (the manifest) with all metadata: top-level identity/version,
   `[ui]`, and `[runtime]` (including `entrypoint = "pkg.module:ClassName"`). Copy the shape
   from `tlc_plugin_exporter`.
3. **Create `__init__.py`** with a `ComputePlugin` subclass:
   - Subclass `ComputePlugin` (from `tlc_plugin_sdk`); no metadata attributes, no `register()`
   - `get_ui_fragment()` reading from `ui.html`
   - `get_route_handlers()` returning your route handlers (optional)
   - Any optional hooks you need (`run_job` for long-running tasks, `initialise_runtime`,
     `shutdown_runtime`)
4. **Create `ui.html`** following the guide's UI Consistency section.
5. **Create `routes.py`** if you need custom endpoints (e.g. config CRUD).
6. **If you have a long-running task**: implement `run_job(ctx)` and set `requires_gpu` in
   `[runtime]`. Reference: `tlc_plugin_image_metrics` (simplest), the training plugins.

### Code patterns

**Config store** (if the plugin has saved configurations):
- Use the shared `PluginConfigStore` from `tlc_plugin_sdk.shared.config_store`
  (generic over your config dataclass; JSON persistence under the plugin's config dir).
- Pair it with `config_ui_script()` from `tlc_plugin_sdk.shared.config_ui` for the config bar
  (inject via `inject_scripts()`). See the sam3 / timm plugins.

**Long-running jobs** (training, inference, multi-step import):
- Implement `run_job(ctx)` — the host owns the queue, GPU/CPU slot, progress fan-out,
  listing, and cancellation.
- Drive the generic Queue & Progress panel via `ctx.progress`/`ctx.metric`/`ctx.result`;
  poll `ctx.cancelled` at checkpoints. The same `run_job` runs in `host` or `venv` mode.
- Do **not** hand-roll a queue, job store, or job-schema translation — all host-provided.

**Routes** (custom REST):
- A module-level `get_route_handlers()` returning bare `@get`/`@post` handlers with
  **relative** paths (no `Controller`, no `/api/plugins` prefix, `sync_to_thread=True` for
  blocking work). The plugin class delegates to it. They resolve under
  `/api/plugins/<plugin-id>/...` via the host's per-plugin app + catch-all.
- CRUD for configs: relative `GET /configs`, `POST /configs`, `GET /configs/{id}`,
  `POST /configs/{id}/delete`.
- Do **not** add `GET /queue` / `POST /cancel/{id}` / a job-start route — long-running work
  goes through the generic `POST /api/plugins/{id}/run`, `GET /api/plugins/jobs`, and
  `POST /api/plugins/jobs/{job_id}/cancel`. Custom routes carry only genuinely
  plugin-specific surface (config CRUD, metadata lookups, column detection, …).

**Error response convention:**
- Pre-validation errors (bad input): return `{"error": "description"}`
- Execution results (job completed): return `{"success": true/false, "message": "..."}`
- Not found: raise `HTTPException(status_code=404)`

**URL aliases** (if the plugin creates tables from image folders):
- `from tlc_plugin_sdk.shared.aliases import register_alias`; call
  `register_alias(project_name, image_folder, alias_token)` after table creation.
- Shared UI component: `from tlc_plugin_sdk.shared.alias_ui import alias_ui_script`; inject
  into the fragment with `inject_scripts()` (never `str.replace` — see
  `tlc_plugin_sdk/shared/ui_inject.py` for why).
- In the UI: `_tlcAliasSettingsHtml(prefix, project, folder)` renders the form;
  `_tlcBindAliasToggle(prefix)` + `_tlcBindAliasAutoUpdate(prefix, projectInputId,
  folderInputId)` bind it; `_tlcGetAliasValues(prefix)` at submit time; after programmatic
  form fills call `_tlcSyncAliasFromForm(prefix, projectId, folderId)`.

**SocketIO** (if real-time updates are needed):
- The namespace is host-derived as `/<plugin-id>` and auto-registered at startup; the manifest
  does not (and never did) declare it.
- Prefer the `window.PluginJobs` client over a hand-rolled socket. It connects a namespace
  lazily; call `PluginJobs.connect('/<plugin-id>')` on mount if the fragment needs custom
  events from the first second (`run()` connects for you).
- Use `ctx.emit(name, payload)` only for telemetry the generic schema can't express
  (e.g. a loss curve); never re-emit the generic lifecycle by hand.

### Common mistakes to avoid

- Don't add a `register()` call or metadata class attributes — the manifest is the only
  metadata source
- Don't add plugin-specific logic to the frontend (templates, JS modules)
- Don't use bare `fetch()` in the UI — always use `PLUGIN_API.authFetch()`
- Don't hardcode colors — use CSS variables
- Don't create new shared utilities for one-off operations
- Don't duplicate the alias UI HTML/JS — use `tlc_plugin_sdk/shared/alias_ui.py`, injected
  at serve time via `inject_scripts()`
- Don't grab a GPU queue or push a closure — implement `run_job(ctx)`
- Don't implement `get_active_jobs`/`cancel_job` or add `/queue`/`/cancel` routes — host-owned
- Don't `async`-define `run_job` — it runs synchronously on a host/worker thread; poll
  `ctx.cancelled`

### Testing

- Co-locate tests with the plugin (its own `tests/`).
- Test both success and error paths for all endpoints.
- For `run_job(ctx)`, drive it with a fake `JobContext` (a recording sink + a
  `threading.Event` for cancel) and assert the emitted `progress`/`metric`/`result`
  events — no GPU or queue needed.
