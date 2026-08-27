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

## What it gives you

- **`ComputePlugin`** — the base class you subclass. Implement `get_ui_fragment` (and `compute`
  if you expose a synchronous endpoint); job and lifecycle hooks ship as safe defaults. There is
  no `register()` to call — a plugin's metadata lives in its `plugin.toml` manifest (or a
  `[tool.tlc-compute]` table in `pyproject.toml`), and the Hub discovers it from there.
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

## How it fits

This SDK sits at the root of the plugin dependency graph. Its dependencies are `uvicorn`,
`litestar`, and the `3lc` data plane (`3lc[pandas]`) — so installing the SDK is all a plugin needs
to use `tlc` and the `shared.*` helpers built on it. All three are imported lazily; `import
tlc_plugin_sdk` stays cheap. It **never** depends on the compute service itself or on any plugin.
A plugin built against `tlc_plugin_sdk` alone is therefore portable across service versions and
runs in its own isolated environment.

## Status

**0.3 is the current contract line.** Within 0.x the contract still evolves — mostly additively,
but a coordinated breaking change may land on a MINOR bump while the fleet re-pins in lockstep (0.3
did: `JobContext.result` took a positional `url`, and the `PY_CONTRACT`/`JS_CONTRACT` axes
collapsed into `SDK_CONTRACT_VERSION`). Anything reshaping the core still waits for a major bump. In
the browser bridge, `PLUGIN_API.libs.io` is a stable part of the contract; the other bundled
libs are best-effort (see the guide).
