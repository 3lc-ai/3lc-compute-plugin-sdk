# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Import-light plugin SDK — the contract surface a plugin programs against.

This is the public import path for the plugin contract. Importing it must **not**
pull in the heavy server stack (litestar, python-socketio, uvicorn, the queues,
discovery): a ``venv``-mode plugin installs only this surface, not the full
service. See ``docs/plugin-isolation.md`` → "The SDK boundary: import-light
module". The :mod:`test_plugin_sdk_import_light` test enforces the boundary.

A plugin subclasses :class:`ComputePlugin` and implements at least
``compute``/``get_ui_fragment``; the optional job/lifecycle hooks ship as no-op
defaults. There is no ``register()`` to call — metadata lives in the plugin
manifest, and the host discovers the plugin via its manifest ``entrypoint``.

The contract is **defined here**, in :mod:`tlc_plugin_sdk.contract`, so it lives
next to :class:`JobContext` and the venv worker on the import-light side of the SDK
boundary — nothing in this package pulls in the server stack. This is the standalone
public ``3lc-plugin-sdk`` distribution: the light base a venv plugin installs (the
host ``3lc-compute`` depends on it, never the reverse). See ``CLAUDE.md``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from tlc_plugin_sdk.contract import ComputePlugin
from tlc_plugin_sdk.job_context import JobContext

# Plugin contract (ABI) version = this package's own version — one source of truth
# (the ``[project] version`` in pyproject), read via importlib.metadata rather than a
# separately maintained constant. A plugin declares the contract it targets via its
# manifest's ``sdk_version``.
try:
    SDK_CONTRACT_VERSION = _pkg_version("3lc-plugin-sdk")
except PackageNotFoundError:  # running from a raw checkout that was never installed
    SDK_CONTRACT_VERSION = "0.0.0"

# Capability markers for feature-detection — decoupled from the wheel/SemVer pin.
#
# ``SDK_CONTRACT_VERSION`` above is the package version: the *dependency pin* a plugin
# resolves against (``3lc-plugin-sdk>=X,<Y``). The two constants below are finer-grained
# *capability* markers a plugin (or the host) can feature-detect against at runtime:
#
#   * ``PY_CONTRACT`` — the Python-side contract: ``ComputePlugin`` / ``JobContext`` and
#     the ``tlc_plugin_sdk.shared.*`` helpers (what a plugin's Python programs against).
#   * ``JS_CONTRACT`` — the browser-side contract: the ``PLUGIN_API`` / ``PluginJobs`` /
#     ``TlcData`` surface a plugin's ``ui.html`` programs against (see
#     ``contract/plugin-api.d.ts``; the ``PluginJobs`` client ships from THIS package).
#
# Each MINOR axis increments **independently** as features are added to one side without
# the other. Both are always ``<= `` the package version (a capability can only exist in a
# shipped wheel). Bump the package version when EITHER ``PY_CONTRACT`` or ``JS_CONTRACT``
# moves — the wheel is the thing a plugin actually pins, so it must cover both axes.
PY_CONTRACT = "0.2"
JS_CONTRACT = "0.2"

__all__ = ["JS_CONTRACT", "PY_CONTRACT", "SDK_CONTRACT_VERSION", "ComputePlugin", "JobContext"]
