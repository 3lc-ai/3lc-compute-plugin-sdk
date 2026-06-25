# 3lc-plugin-sdk

The public Python SDK for building [3LC](https://3lc.ai) compute-service plugins —
the **import-light contract** a plugin programs against. Install this (not the full
service) and you have everything you need to write, run, and serve a plugin.

```bash
pip install 3lc-plugin-sdk          # import name: tlc_plugin_sdk
```

## What it gives you

- **{class}`~tlc_plugin_sdk.ComputePlugin`** — the base class you subclass.
  Implement `compute` / `get_ui_fragment`; job and lifecycle hooks ship as no-op
  defaults.
- **{class}`~tlc_plugin_sdk.JobContext`** — the surface a long-running job programs
  against: `progress` / `metric` / `log` / `result`, `emit` for rich UI, and
  cooperative `cancelled`.
- **The worker** (`python -m tlc_plugin_sdk.worker`) — serves your plugin's Litestar
  route handlers as an ASGI app, in-process or out-of-process in its own venv.

```{toctree}
:maxdepth: 2
:caption: Contents

plugin-guide
api
```
