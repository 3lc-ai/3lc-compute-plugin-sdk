# 3lc-compute-plugin-sdk

The public Python SDK for building [3LC](https://3lc.ai) compute-service plugins —
the **import-light contract** a plugin programs against. Install this (not the full
service) and you have everything you need to write, run, and serve a plugin.

```bash
pip install 3lc-compute-plugin-sdk          # import name: tlc_plugin_sdk
```

## What it gives you

- **{class}`~tlc_plugin_sdk.ComputePlugin`** — the base class you subclass for a plugin that
  runs jobs. Implement `get_ui_fragment` (and `compute` for a synchronous endpoint); job and
  lifecycle hooks ship as safe defaults. Its parent {class}`~tlc_plugin_sdk.HubPlugin` is
  enough for a plugin with no jobs; {class}`~tlc_plugin_sdk.InfrastructurePlugin` is the
  typed contract for a provider that creates nodes.
- **{class}`~tlc_plugin_sdk.JobContext`** — the surface a long-running job programs
  against: `progress` / `metric` / `log` / `result` / `fail`, `emit` for rich UI,
  cooperative `cancelled`, and what the host stamped (`project_root_url`, `identity`,
  `get_credential`).
- **The worker** (`python -m tlc_plugin_sdk.worker`) — serves your plugin's Litestar
  route handlers as an ASGI app, out-of-process in the plugin's own venv.
- **The harness and the lint** (`tlc_plugin_sdk.harness`, `tlc_plugin_sdk.lint`) — the
  plugin called in-process without a compute service, and its fragment checked against the
  Hub's styling conventions: what a plugin's tests run.

The [plugin template](https://github.com/3lc-ai/3lc-compute-plugin-template) is the place to
start; the [example plugin](https://github.com/3lc-ai/3lc-compute-plugin-example) is a runnable
tour of the whole contract. The 0.6 line is the contract the Hub and Compute Service 1.2
implement.

```{toctree}
:maxdepth: 2
:caption: Contents

plugin-guide
api
```
