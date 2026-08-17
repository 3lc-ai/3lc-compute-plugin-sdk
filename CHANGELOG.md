# Changelog

All notable changes to `3lc-compute-plugin-sdk` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.2.2] - 2026-08-17

### Changed
- **Distribution moved to PyPI**: `3lc-compute-plugin-sdk` is now published to public
  [PyPI](https://pypi.org/project/3lc-compute-plugin-sdk/) via Trusted Publishing; the private
  CloudRepo index (pypi.3lc.ai) is no longer needed to install the SDK. Manual prerelease
  builds keep publishing to CloudRepo for a grace period (#13).
- The `[shared]` extra's `3lc` dependency resolves from public PyPI (its home since the 3.2
  rust release), so the SDK no longer pins a custom package index (#12).

## [0.2.1] - 2026-08-13

### Added
- Tilde expansion and local-path normalization helpers in `tlc_plugin_sdk.shared` (#10).

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
