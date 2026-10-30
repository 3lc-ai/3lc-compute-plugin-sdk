# 3lc-compute-plugin-sdk

[![PyPI](https://img.shields.io/pypi/v/3lc-compute-plugin-sdk)](https://pypi.org/project/3lc-compute-plugin-sdk/)
[![Docs](https://img.shields.io/badge/docs-3lc--ai.github.io-blue)](https://3lc-ai.github.io/3lc-compute-plugin-sdk/)
[![Try it](https://img.shields.io/badge/try%20it-use%20the%20plugin%20template-brightgreen)](https://github.com/3lc-ai/3lc-compute-plugin-template/generate)

Plugins are how you extend the [3LC Hub](https://docs.3lc.ai/3lc/latest/hub/index.html) —
your own importers, exporters, training jobs, and data tools, appearing in the Hub right
next to the built-ins. This SDK is everything a plugin needs: one small Python package to
program against, while the Hub takes care of discovery, isolation, serving, and job
orchestration.

```bash
pip install 3lc-compute-plugin-sdk          # import name: tlc_plugin_sdk
```

> **Distribution `3lc-compute-plugin-sdk` · import `tlc_plugin_sdk`.**

**PyPI carries the 0.4 contract line.** The 0.5 line this README and the guide describe (the
plugin hierarchy, infrastructure plugins, Connections) is published only to the 3LC staging
index, so a plain `pip install` gets a 0.4 SDK without them; see
[Snapshot builds](#snapshot-builds) for installing a 0.5 snapshot.

## What it gives you

- **The plugin hierarchy** — `HubPlugin` is the root base class (UI fragment, optional
  `compute`, custom routes, shutdown). You subclass one of its two children:
  - **`ComputePlugin`** for importers, exporters, training and data tools: implement
    `get_ui_fragment` (and `compute` if you expose a synchronous endpoint) and `run_job` for
    long-running work; lifecycle hooks ship as safe defaults.
  - **`InfrastructurePlugin`** (`tlc_plugin_sdk.infrastructure`, manifest `kind =
    "infrastructure"`) for providers that create remote nodes and workspaces: a typed
    create / state / delete contract plus opt-in facets for storage, catalogs and workspaces.

  There is no `register()` to call — a plugin's metadata lives in its `plugin.toml` manifest (or
  a `[tool.tlc-compute]` table in `pyproject.toml`), and the Hub discovers it from there.
- **`JobContext`** — the surface a long-running job programs against: `progress` / `metric` /
  `log` / `result` for the generic job panel, `emit` for your plugin's own rich UI, and
  cooperative `cancelled`.
- **The worker** (`python -m tlc_plugin_sdk.worker`) — serves your plugin's Litestar route
  handlers + the generic reserved routes as an ASGI app, out-of-process in the plugin's
  own venv.
- **`tlc_plugin_sdk.shared.*`** — batteries the heavy plugins share: URL-alias registration,
  config storage/UI, the generic-job helpers, image/label/modality utilities, script injection.

## Quickstart

See **[`docs/plugin-guide.md`](docs/plugin-guide.md)** for the full author guide (manifest
format, custom routes, the job model, UI fragment, checklist).

## The contract version

`tlc_plugin_sdk.SDK_CONTRACT_VERSION` is this package's own version — one source of truth, one
contract axis (both the Python and the browser surface). A plugin pins the SDK
(`3lc-compute-plugin-sdk>=X,<Y`); the compute service and frontend implement a range and compare
compatibility on `MAJOR.MINOR`. Versions are SemVer; see **Status** below for the 0.x stability
stance.

`MAJOR.MINOR` is not the whole check. Request models drop keys they do not know, so a plugin
whose SDK predates a field the host sends would answer as if it was never sent. The host
therefore keeps a per-feature SDK floor, compares it against the SDK version the plugin's worker
reports, and refuses a request that needs a newer SDK with a sentence naming the plugin, its SDK
version and the floor (the plugin card shows the same sentence). The first floor is the
deployment DNS zone on `WorkspaceRequest` (`dns_zone_id` / `dns_zone_name`), which needs SDK
0.5.3 or newer.

## How it fits

This SDK sits at the root of the plugin dependency graph. Its dependencies are `uvicorn`,
`litestar`, and the `3lc` data plane (`3lc[pandas]`) — so installing the SDK is all a plugin needs
to use `tlc` and the `shared.*` helpers built on it. All three are imported lazily; `import
tlc_plugin_sdk` stays cheap. It **never** depends on the compute service itself or on any plugin.
A plugin built against `tlc_plugin_sdk` alone is therefore portable across service versions and
runs in its own isolated environment.

## Status

**0.5 is the current development contract line.** Within 0.x the contract still evolves — mostly additively,
but a coordinated breaking change may land on a MINOR bump while the fleet re-pins in lockstep (0.3
did: `JobContext.result` took a positional `url`, and the `PY_CONTRACT`/`JS_CONTRACT` axes
collapsed into `SDK_CONTRACT_VERSION`). Anything reshaping the core still waits for a major bump. In
the browser bridge, `PLUGIN_API.libs.io` is a stable part of the contract; the other bundled
libs are best-effort (see the guide).

## Snapshot builds

The manual `Release` workflow tests and builds an SDK snapshot. Its default is build-only;
download the wheel and source archive from the workflow artifacts. Select `publish` to upload
them to the private staging index (CloudRepo `prereleases`). The `release` environment needs
the `CLOUDREPO_USERNAME` / `CLOUDREPO_PASSWORD` secrets for publishing.

Snapshots use `BASE.UTCSTAMP.RUN.ATTEMPT` versions, for example `0.5.0.20260916080000.42.1`.
They satisfy ordinary `>=0.5.0,<0.6.0` requirements and sort after older timestamped snapshots.
The run and attempt distinguish concurrent builds and reruns.
Consumers need authenticated access to that index; a public PyPI release is not required to
test them. A consumer declares it as an explicit uv index named `staging`
(`https://pypi.3lc.ai/repositories/prereleases/`), routes the SDK to it with
`3lc-compute-plugin-sdk = { index = "staging" }` in `[tool.uv.sources]`, and sets only
`UV_INDEX_STAGING_USERNAME` / `UV_INDEX_STAGING_PASSWORD`. Record the exact tested snapshot when validating a group of consumers. A Git source
override in a development checkout does not provide that SDK to users installing a built wheel.

Public releases still use a `vX.Y.Z` tag whose version matches `pyproject.toml`. Snapshots
are never published to PyPI.

## Isolated browser runtime

For SDK 0.6.2 framed hosts, follow the [browser runtime guide](docs/browser-runtime.md) and [agent instructions](docs/agent-guide.md).
The framed runtime supersedes legacy inline/global browser examples in this document.
