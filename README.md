# 3lc-compute-plugin-sdk

The public Python SDK for building [3LC](https://3lc.ai) compute-service plugins — the
**import-light contract** a plugin programs against. Install this (not the full service)
and you have everything you need to write, run, and serve a plugin.

```bash
pip install 3lc-compute-plugin-sdk          # import name: tlc_plugin_sdk
```

> **Distribution `3lc-compute-plugin-sdk` · import `tlc_plugin_sdk`.**

## What it gives you

- **`ComputePlugin`** — the base class you subclass. Implement `compute` / `get_ui_fragment`;
  job and lifecycle hooks ship as no-op defaults. There is no `register()` to call — a plugin's
  metadata lives in its `[tool.tlc-compute]` manifest, and the host discovers it from there.
- **`JobContext`** — the surface a long-running job programs against: `progress` / `metric` /
  `log` / `result` for the generic job panel, `emit` for your plugin's own rich UI, and
  cooperative `cancelled`.
- **The worker** (`python -m tlc_plugin_sdk.worker`) — serves your plugin's Litestar route
  handlers + the generic reserved routes as an ASGI app, identically whether the host runs it
  in-process or out-of-process in its own venv.
- **`tlc_plugin_sdk.shared.*`** — batteries the heavy plugins share: URL-alias registration,
  config storage/UI, the generic-job helpers, image/label/modality utilities, script injection.

## Quickstart

See **[`docs/plugin-guide.md`](docs/plugin-guide.md)** for the full author guide (manifest
format, custom routes, the job model, UI fragment, checklist).

## The contract version

`tlc_plugin_sdk.SDK_CONTRACT_VERSION` is this package's own version — one source of truth.
A plugin pins the SDK (`3lc-compute-plugin-sdk>=X,<Y`) and declares the contract it targets via its
manifest. The host implements a contract range and the SDK version is the lingua franca that
both sides agree on. Versions are SemVer; `< 1.0` means the contract is not yet frozen.

## Boundary (the one rule)

The SDK is the **root** of the plugin dependency graph: it depends only on `3lc` + `uvicorn` +
`litestar`, and **never** on the host (`3lc-compute`) or on any plugin. If your plugin only
needs `tlc_plugin_sdk`, it is portable across host versions and can run in its own isolated venv.

## Status

Pre-1.0 — the contract is still being hardened toward a freeze.
