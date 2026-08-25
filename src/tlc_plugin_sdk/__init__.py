# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Import-light plugin SDK — the contract surface a plugin programs against.

This is the public import path for the plugin contract. Importing it must **not**
pull in the heavy server stack (litestar, python-socketio, uvicorn, the queues,
discovery): a ``venv``-mode plugin installs only this surface, not the full
service. The ``tests/test_import_light.py`` test enforces the boundary.

A plugin subclasses :class:`ComputePlugin` and implements at least
``compute``/``get_ui_fragment``; the optional job/lifecycle hooks ship as no-op
defaults. There is no ``register()`` to call — metadata lives in the plugin
manifest, and the host discovers the plugin via its manifest ``entrypoint``.

The contract is **defined here**, in :mod:`tlc_plugin_sdk.contract`, so it lives
next to :class:`JobContext` and the venv worker on the import-light side of the SDK
boundary — nothing in this package pulls in the server stack. This is the standalone
public ``3lc-compute-plugin-sdk`` distribution: the light base a venv plugin installs (the
host ``3lc-compute`` depends on it, never the reverse). See ``CLAUDE.md``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from tlc_plugin_sdk.contract import ComputePlugin
from tlc_plugin_sdk.job_context import JobContext, JobFailed

# The plugin contract version — one axis, one source of truth: this package's own
# version (the ``[project] version`` in pyproject), read via importlib.metadata rather
# than a separately maintained constant. It IS the contract a plugin programs against —
# both the Python surface (``ComputePlugin`` / ``JobContext`` / ``shared.*``) and the
# browser surface (``PLUGIN_API`` / ``PluginJobs`` / ``TlcData``, declared in
# ``contract/plugin-api.d.ts``) — and the thing a plugin pins (``>=X,<Y``). The host and
# frontend implement a range and compare compatibility on ``MAJOR.MINOR``.
try:
    SDK_CONTRACT_VERSION = _pkg_version("3lc-compute-plugin-sdk")
except PackageNotFoundError:  # running from a raw checkout that was never installed
    SDK_CONTRACT_VERSION = "0.0.0"

__all__ = ["SDK_CONTRACT_VERSION", "ComputePlugin", "JobContext", "JobFailed"]
