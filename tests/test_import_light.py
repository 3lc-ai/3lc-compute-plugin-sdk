# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The plugin SDK contract surface must stay cheap to import.

Importing ``tlc_plugin_sdk`` (which every venv plugin's module does) must not *eagerly*
pull in the web/server stack: litestar and uvicorn are deps but are imported lazily —
only when a worker actually serves — by ``tlc_plugin_sdk.asgi_app`` / ``.worker``, never
by the contract module. The host-only SocketIO server must not be importable here at all.
Checked in a fresh subprocess so an already-imported stack can't mask a regression."""

from __future__ import annotations

import subprocess
import sys

# ``tlc`` (the data plane) is an optional extra used only lazily by ``shared.*`` — importing
# the contract surface must not pull it either, so the base can stay featherweight.
_HEAVY = ("litestar", "socketio", "uvicorn", "tlc")


def test_import_is_light() -> None:
    code = (
        "import sys, importlib;"
        "importlib.import_module('tlc_plugin_sdk');"
        f"heavy=[m for m in {_HEAVY!r} if m in sys.modules];"
        "sys.exit('IMPORT-LIGHT VIOLATION: ' + ','.join(heavy) if heavy else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_exposes_contract() -> None:
    import tlc_plugin_sdk
    from tlc_plugin_sdk.contract import ComputePlugin

    assert tlc_plugin_sdk.ComputePlugin is ComputePlugin
    # The contract version is this package's own version — one source of truth.
    from importlib.metadata import version

    assert isinstance(tlc_plugin_sdk.SDK_CONTRACT_VERSION, str)
    assert version("3lc-plugin-sdk") == tlc_plugin_sdk.SDK_CONTRACT_VERSION
    # A plugin subclasses ComputePlugin — there is no register() to export.
    assert not hasattr(tlc_plugin_sdk, "register")
