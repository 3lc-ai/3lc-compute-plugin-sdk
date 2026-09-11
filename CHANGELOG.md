# Changelog

All notable changes to `3lc-compute-plugin-sdk` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-09-11

### Added
- **Plugin hierarchy:** `HubPlugin` base class extracted from `ComputePlugin`.
  `ComputePlugin(HubPlugin)` keeps its full interface — no existing import breaks.
  A plugin that only needs a UI fragment, synchronous compute, and/or custom routes
  (e.g. a config-service sidebar tool) can now subclass `HubPlugin` directly.
- **`InfrastructurePlugin(HubPlugin)`:** typed provider contract for remote-node
  plugins (`kind = "infrastructure"`).  Four abstract methods — `capabilities()`,
  `create_node()`, `node_state()`, `delete_node()` — plus an optional `preflight()`.
  The default `get_route_handlers()` auto-mounts Litestar handlers for the five
  `/infra/*` routes, so a provider plugin gets its HTTP surface for free.
- **Typed infra dataclasses** in `tlc_plugin_sdk.infra`: `CreateNodeRequest`,
  `CreateNodeResponse`, `NodeStateResponse`, `CapabilitiesResponse`,
  `PreflightCheck`, `PreflightResponse`, and `NodeProviderState`.  Each carries
  `from_dict` / `to_dict` for wire-format conversion.
- Top-level exports: `HubPlugin` and `InfrastructurePlugin` are now in
  `tlc_plugin_sdk.__all__` alongside `ComputePlugin`.

## [0.4.0] - 2026-09-10

### Added
- `ctx.identity`: a `JobIdentity` (`user_id`, `org_id`, `project_id` — canonical id strings,
  `None` when the host did not know) saying who a job runs for. The host stamps it under the
  host-owned top-level run-body key `_identity`; the worker pops it before `ctx.params` is built,
  so a plugin never reads the key and never persists it with saved params. Every field is `None`
  on a host that stamps nothing (an older host, or a keyless local one). Plugins read it for
  attribution; a later credential API leases to this identity, never to a plugin.
- Remote-worker hardening for TCP workers (opt-in; a local Unix-socket worker is unchanged):
  - `python -m tlc_plugin_sdk.worker --token …` (default `$TLC_WORKER_TOKEN`) requires
    `Authorization: Bearer <token>` on every HTTP route and on every websocket a plugin
    declares (closed with 1008). `build_plugin_app(token=…)` carries the same knob. The worker
    no longer serves generated OpenAPI/Swagger routes.
  - `TLC_WORKER_STREAM_KEEPALIVE_S=<seconds>` makes the `/jobs/{id}/run` NDJSON stream emit
    `{"event": "ping"}` between job events so HTTP proxies do not kill quiet streams during
    long epochs.
- Worker control routes `GET /busy` (`{"active_jobs": n}`) and `POST /jobs/cancel-all`. Both
  are host-owned; plugins implement neither.
- The worker applies the run body's top-level `_alias_overrides` around `run_job` (register
  before, restore after), so a plugin needs no code for aliases the host re-points at staged
  data. A plugin that applies the same overrides itself keeps working.
- `PluginConfigStore.exists(config_id)` and `.directory`.
- Manifest `[runtime] node_routes` documented: the custom routes that must run where the model
  lives when a host with remote-node support runs the plugin on a remote worker; everything
  else stays with the controller's worker. A host without that support ignores the key.
- Run-body conventions for remote-ready plugins documented: inline `project_config`
  (self-contained params, since a remote worker has no controller-local store), the
  `_alias_overrides` shape, and `run_target` / `prepare_job_ids` as host-owned keys.
- `plugin-api.d.ts`: optional `PLUGIN_API` members `getRunTarget`, `onRunTargetChange`,
  `checkDataForRunTarget` and `hostChecksTableInputs` for frontends with remote-node support.
  Absent elsewhere, so fragments feature-detect them.

### Changed
- A job stays registered on the worker for as long as its **thread** runs, not its event
  stream, so a host that restarts mid-run can still cancel it. A cancel the job ignores for
  `TLC_WORKER_CANCEL_GRACE_S` seconds (default 60; `0` disables) makes the worker exit with
  status 3 when that job is the last live one; the supervisor spawns a fresh worker for the
  next job.
- Job ids must match `[A-Za-z0-9_-]{1,64}` (400 otherwise); a `/jobs/{id}/run` for an id
  still live on the worker answers 409 rather than replacing the running job.
- When the host's job stream disconnects the worker stops buffering that job's events (the
  thread keeps running, cancellable), so a lost stream no longer grows memory for the rest of
  a training run.
- A plugin route handler on a reserved path (`/health`, `/ui`, `/compute`, `/busy`,
  `/reclaim`, `/jobs/*`) is not mounted; the collision is logged as an error.
- `PluginConfigStore` writes its directory `0700` and config files `0600` (they hold keys).

## [0.3.3] - 2026-09-07

### Changed
- Requires `3lc>=3.3.0`. The SDK carries the `3lc[pandas]` data plane for every plugin venv, so
  raising the floor here moves the whole plugin fleet onto the current 3lc release.

## [0.3.2] - 2026-08-31

### Fixed
- Data-source browse fields with a comma-separated `accept` (e.g. `*.yaml,*.yml`, `*.pt,*.pth`)
  now list matching files; previously they showed none. `/browse` was matching the whole
  `accept` string as one literal glob pattern instead of any pattern in the list.
  ([#28](https://github.com/3lc-ai/3lc-compute-plugin-sdk/pull/28))

## [0.3.1] - 2026-08-27

A patch on the 0.3 line, additive and pin-compatible (`>=0.3.0,<0.4.0` resolves it). The SDK now
brings the `3lc` data plane as a base dependency, and the shared data-source browse widget gains an
input/output picker split.

### Added
- The shared data-source browse widget shows a small caption ("Showing files matching:
  `<glob>`") when a field declares an `accept` glob, so users understand why some files in a
  listed folder don't appear as selectable — folders themselves are never glob-filtered.

### Changed
- **`3lc[pandas]` is now a base dependency.** Installing `3lc-compute-plugin-sdk` brings the 3LC
  data plane, so a plugin pins the SDK alone and gets `tlc` and every `shared.*` helper with it.
  The former `[shared]` extra is kept as an empty, deprecated alias — existing
  `3lc-compute-plugin-sdk[shared]` pins keep resolving — and will be removed at 1.0. Drop the
  suffix when you raise your floor to a release carrying this change. Nothing changes at import
  time: `tlc` is still loaded lazily, only by the helpers that use it.
- **Docs:** clarified `runtime.provision_extra` in the plugin guide — it names the
  optional-dependency group the host installs into your plugin's venv (keeping the base light for
  discovery and letting one distribution carry several plugins), not an "umbrella-only, required"
  key. Restated that isolation is venv-only: every plugin runs from a host-built managed venv,
  with no in-process/host mode and no `.venv` beside the source. No contract or code change.
- **Data-source browse widget:** the widget now takes a `purpose` config axis (`'input'`, the
  default, or `'output'`), mirrored by a `purpose` query param on `/browse`. Input pickers now
  **hide** folders the compute process cannot open (rather than showing them disabled), so the
  listing only offers places you can actually descend into. Output pickers show **all** folders
  and instead flag the ones that are not writable — dimmed with a lock glyph and a tooltip, but
  still navigable so you can descend to a writable subdirectory — and disable "Select This
  Folder" when the current directory is not writable. In output mode `/browse` returns a
  `writable` flag on each directory entry and for the listed directory itself (computed only in
  output mode; an older host that omits the flag is treated as writable).

### Fixed
- **Data-source browse widget:** folders the compute process cannot actually open (e.g. macOS
  TCC-protected packages like `Photos Library.photoslibrary`) are now marked inaccessible and
  rendered disabled — dimmed, with a lock glyph and a tooltip — instead of listed as navigable
  and then failing with a red "Permission denied" panel when clicked. The `/browse` route probes
  each directory's openability with `os.scandir` (not `os.access`, which TCC packages spuriously
  pass) and tags directory entries with an `accessible` flag; an older host that omits the flag
  keeps working (entries are treated as accessible).

## [0.3.0] - 2026-08-25

A coordinated contract wave: breaking **for plugin authors** but **not on the wire** — the
worker→host NDJSON events are unchanged, so a host keeps running a plugin venv still on 0.2.x
(skew badge only). The whole first-party fleet re-pins `>=0.3.0,<0.4.0` in the same release.

### Added
- `JobContext.fail(message)` raises `JobFailed` (exported from `tlc_plugin_sdk`); the worker
  reports it as the terminal `error` event with the **bare** message, while any other exception
  keeps its `TypeName: …` prefix.
- `PluginJobs.list(pluginId?)` — `GET {compute}/api/plugins/jobs`, filtered client-side by
  `plugin_id`. Use it to **seed a fragment on mount** from the durable host job list (the
  fragment is torn down on navigation and `job_update` is live-only). Declared in the `.d.ts`.
- `PluginJobs.connect(namespace)` — warm the namespace socket on mount. `track()`/`on()`
  connect lazily and SocketIO does not replay events to a not-yet-connected client, so a
  fragment listening for custom events from the first second (or re-attaching to a job it did
  not start) calls this first; `run()` already connects for you.
- The host's `/ui` handler now **auto-injects** the `PluginJobs` client into every fragment; the
  client is idempotent, so a plugin that still injects it by hand keeps working.
- `ComputePlugin.compute()` ships a default (returns an error dict), so only `get_ui_fragment()`
  stays abstract.
- `.d.ts` truth pass: `PluginJobUpdate.error`, `PluginFetchOptions.allowErrorStatus`,
  `PluginJobsApi.list`, `TlcLocationApi.rowsSpanLocations`/`shortLabel`, and a new
  **Legacy globals** ambient block (`openTablePicker`/`closeTablePicker`/`CancelJob`/`cssVar`
  and the injected `_tlc*` helpers).

### Changed
- **One contract axis.** `PY_CONTRACT` and `JS_CONTRACT` are removed; `SDK_CONTRACT_VERSION`
  (the package version) is the whole contract, compared on `MAJOR.MINOR`. `/health` reports
  `sdk_version` only (no `py_contract`/`js_contract`).
- `JobContext.result` takes a **positional** `url` (`ctx.result(url)`); it opens the Open
  button — a run *or* a table URL. The `run_url=` keyword is gone; the emitted `result` event
  still carries `run_url` (wire unchanged).
- `JobContext.progress` documents `percent=-1` as indeterminate.

### Removed
- `shared.modality.detect_modality_from_url` (zero users). `detect_modality_from_schema`,
  `classify_metrics_columns`, `labels.candidate_label_paths`, and `labels.find_label_path`
  are now internal (underscore-prefixed). Public alternatives: `detect_modality_from_table`
  for modality; `get_label_map` / `get_label_names` / `find_label_column` for labels.
  `classify_metrics_columns` was a plugin-specific column-name heuristic with no public
  replacement — inline it (its one known user did).
- The orphan `_ICON_SVG` constant; the dead `params.setdefault("url", "")` in the `/compute`
  handler.

### Migration
- `ctx.result(run_url=x)` → `ctx.result(x)`.
- Validation failures: raise, or `ctx.fail("message")` for a clean user-facing card.
- Reading `tlc_plugin_sdk.PY_CONTRACT` / `JS_CONTRACT`, or a worker's `/health`
  `py_contract`/`js_contract`: use `SDK_CONTRACT_VERSION` / `sdk_version` and compare on
  `MAJOR.MINOR`.
- `shared.modality.detect_modality_from_url(url)` → load the table yourself and call
  `detect_modality_from_table(table)`. `classify_metrics_columns(columns)` → copy the
  heuristic into your plugin.
- A fragment that kept its own always-connected socket for custom events: switch to
  `PluginJobs.on(namespace, event, handler)` and call `PluginJobs.connect(namespace)` on mount
  so nothing emitted before the first `track()` is missed.
- Fragments may delete their manual `inject_scripts(raw, job_tracker_script())` — the host
  injects `PluginJobs` now (harmless to keep).
- The manifest never declared the SocketIO namespace or `sdk_version`; both were dead and are
  documented as such. The namespace is host-derived as `/<plugin-id>`.

## [0.2.3] - 2026-08-21

### Changed
- Docs: the plugin guide and API docstrings describe the single runtime — a plugin runs in
  its own venv behind a worker; the manifest's `isolation` key accepts only `"venv"` (the
  default when absent). The guide's isolation table, the in-tree host exception callout, and
  the `unload` endpoint reference are gone; the dev-workflow section now documents the
  folder-Source + editable-venv + `worker/stop` loop. No code or contract change.

## [0.2.2] - 2026-08-18

### Added
- Shared data-source picker (`shared.data_source_ui`) and its browse/upload route handlers
  (`shared.data_source_routes`): plugins mount one consistent widget for choosing a local
  path or uploading a file, instead of each fragment rolling its own (#11).

### Changed
- **Distribution moved to PyPI**: `3lc-compute-plugin-sdk` is now published to public
  [PyPI](https://pypi.org/project/3lc-compute-plugin-sdk/) via Trusted Publishing; the private
  CloudRepo index (pypi.3lc.ai) is no longer needed to install the SDK. Manual prerelease
  builds keep publishing to CloudRepo for a grace period (#13).
- The `[shared]` extra's `3lc` dependency resolves from public PyPI (its home since the 3.2
  rust release), so the SDK no longer pins a custom package index (#12).

### Security
- The data-source `/browse` route is confined to operator-configured roots
  (`TLC_DATA_SOURCE_ROOTS`, `os.pathsep`-separated; default: the service user's home).
  Every requested path is realpath-resolved before the containment check, so `..`
  segments and symlinks pointing outside a root are denied rather than followed, and
  the widget's breadcrumb stops at the root instead of offering the whole filesystem (#11).
- The data-source `/upload-temp` route strips directory components from the
  client-supplied filename (closing a path traversal where a `../…` name chose the
  write location), lands each upload in its own private temp directory so same-named
  uploads never clobber each other, and rejects bodies larger than
  `TLC_DATA_SOURCE_MAX_UPLOAD_MB` (default 512) (#11).

## [0.2.1] - 2026-08-13

### Added
- Tilde expansion and local-path normalization helpers in `shared.url_utils`
  (`normalize_local_path`, `normalize_url`): user-supplied paths expand `~` and resolve
  to absolute form at every ingress, so exported files and stored URLs never carry a
  user-relative path (#10).

## [0.2.0] - 2026-08-05

### Added
- JS_CONTRACT 0.2: project locations (`TlcLocation`) are available to plugin UI fragments
  through `PLUGIN_API`, so plugins can resolve and present where a project's data lives (#8).
- The plugin worker reports the SDK contract version from its `/health` endpoint, letting the
  host enforce compatibility floors precisely instead of guessing (#6).

### Changed
- The plugin worker releases cached GPU memory after every job, so a finished GPU job no longer
  pins CUDA memory that other plugins need (#7).

## [0.1.1] - 2026-07-03

### Changed
- **Distribution renamed**: `3lc-plugin-sdk` is now published as `3lc-compute-plugin-sdk`
  (the import name `tlc_plugin_sdk` is unchanged). Update your dependency declarations.

### Fixed
- Regenerated `uv.lock` for the renamed distribution; release builds append the run number as a
  build counter.

## [0.1.0] - 2026-06-30

First public release of the plugin SDK for the 3LC compute service.

### Added
- The plugin contract: `ComputePlugin`, `JobContext`, and the `PLUGIN_API` browser bridge
  (JS_CONTRACT) that plugin UI fragments use to talk to the host.
- Slim base package with a `[shared]` extra for common plugin dependencies.
- Apache-2.0 license, public CI, and a documentation site including the plugin author guide and
  a rendered browser-contract reference.
