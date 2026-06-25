# 3lc-plugin-sdk — agent & contributor orientation

This repo is the **public Python plugin SDK** for the 3LC compute service: the import-light
contract a plugin programs against. It was extracted from the private `3lc-insights` monorepo
(provenance: commit `b3aefd7`), where the host runtime and the full design history live.

## What this package is (and is not)

- **Is:** the contract surface — `ComputePlugin`, `JobContext`, the worker entrypoint, and the
  `tlc_plugin_sdk.shared.*` helpers. Distribution `3lc-plugin-sdk`, import `tlc_plugin_sdk`.
- **Is not:** the host. The compute service (`3lc-compute` / import `tlc_compute`) is a separate,
  **private** package that *discovers and runs* plugins. It is not a dependency of this SDK and
  its source is not here.

## The invariants — do not break these

1. **No back-edge to the host.** `tlc_plugin_sdk` must never import `tlc_compute` (or anything
   host-side). The SDK is the root of the dependency graph; the host depends on the SDK, never the
   reverse. The whole point — a plugin built against just this wheel runs in its own isolated venv.
2. **Import-light.** Importing `tlc_plugin_sdk` must not eagerly pull the server stack *or the data
   plane*. `litestar` and `uvicorn` are base deps but are imported **lazily** (only when a worker
   actually serves); `tlc` is an optional extra imported lazily only by `shared.*`; and the
   host-only SocketIO server must not be importable from here at all. `tests/test_import_light.py`
   enforces this (guards `litestar`/`socketio`/`uvicorn`/`tlc`) — keep it green.
3. **Dependencies stay minimal.** Base = `uvicorn` + `litestar` only. `3lc` is an optional extra
   (`3lc-plugin-sdk[data]`) used solely by the `shared.*` helpers — the contract core needs no data
   plane, so light consumers (e.g. the Hub frontend) install the bare SDK. Adding a base dep widens
   what every plugin venv must install — justify it. (Direction: as `shared.*` graduates into the
   core, `3lc` likely returns to base with `[data]` kept as a no-op alias.)
4. **The contract is published — every public symbol is forever-ish.** Pre-1.0 we can still
   change it, but treat additions as the safe move and reshaping `JobContext`/`ComputePlugin` as
   breaking. The version is the contract version (see below).

## Versioning

`SDK_CONTRACT_VERSION` is read from this package's own version via `importlib.metadata` — one
source of truth (`[project] version` in `pyproject.toml`). Bump it (SemVer) when the contract
changes; `< 1.0` signals it is not yet frozen. Plugins pin a range (`3lc-plugin-sdk>=X,<Y`); the
host implements a range. Don't reintroduce a separately-maintained version constant.

## Where the rest of the context lives

The design rationale (the plugin-architecture arc, isolation model, contract-gap analysis,
distribution/freeze plan) lives in the **private `3lc-insights` monorepo** under `docs/`
(`plugin-arc-tracker.md`, `plugin-isolation.md`, `plugin-contract-gaps.md`, the deployment doc
§14). The author-facing slice travels with this repo as `docs/plugin-guide.md`. If you're an
internal agent with monorepo access, read those for the "why."

## Dev setup (build-out)

During the extraction build-out, `3lc` is sourced from a local monorepo checkout and the host
editable-sources this package — both via clearly-marked **dev-only** `[tool.uv.sources]` (here and
in the host). These revert to index/version pins before any real publish.

```bash
uv sync                 # installs the SDK + dev tools (ruff, mypy, pytest)
uv run ruff check .
uv run mypy src/
uv run pytest
```

## Conventions (inherited from the monorepo)

Python 3.10+, uv, Hatchling, Litestar, Ruff (line-length 120), mypy `--strict` clean (no
`# type: ignore`, no `Any` used to silence the checker — see the monorepo CLAUDE.md for the full
rule). Google-style docstrings. Copyright header on new files.
