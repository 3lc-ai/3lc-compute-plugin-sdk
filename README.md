# 3lc-compute-plugin-sdk

[![Docs](https://img.shields.io/badge/docs-3lc--ai.github.io-blue)](https://3lc-ai.github.io/3lc-compute-plugin-sdk/)
[![Try it](https://img.shields.io/badge/try%20it-use%20the%20plugin%20template-brightgreen)](https://github.com/3lc-ai/3lc-compute-plugin-template/generate)

The public Python SDK for building plugins for the [3LC](https://3lc.ai) [compute service](https://docs.3lc.ai/3lc/latest/hub/index.html) — the
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
both sides agree on. Versions are SemVer; see **Status** below for the 0.x stability stance.

## Boundary (the one rule)

The SDK is the **root** of the plugin dependency graph: it depends only on `3lc` + `uvicorn` +
`litestar`, and **never** on the host (`3lc-compute`) or on any plugin. If your plugin only
needs `tlc_plugin_sdk`, it is portable across host versions and can run in its own isolated venv.

## Status

**0.1 is the released contract line.** Within 0.x the contract evolves **additively only**:
symbols and schemas that exist keep working, and anything breaking waits for a major bump. In
the browser bridge, `PLUGIN_API.libs.io` is a stable part of the contract; the other bundled
libs are best-effort (see the guide).
