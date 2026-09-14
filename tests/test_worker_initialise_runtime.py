# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The worker runs ``initialise_runtime`` for plugins that have it, and only for those.

``ComputePlugin`` declares the hook; ``HubPlugin`` and ``InfrastructurePlugin`` do not. A worker
serving one of the latter must start without a traceback in its log.
"""

from __future__ import annotations

import logging

import pytest

from tlc_plugin_sdk.contract import ComputePlugin, HubPlugin
from tlc_plugin_sdk.worker import _initialise_runtime


class _Compute(ComputePlugin):
    def __init__(self) -> None:
        self.initialised = 0

    def get_ui_fragment(self) -> str:
        return ""

    def initialise_runtime(self) -> None:
        self.initialised += 1


class _Bare(HubPlugin):
    def get_ui_fragment(self) -> str:
        return ""


class _Broken(ComputePlugin):
    def get_ui_fragment(self) -> str:
        return ""

    def initialise_runtime(self) -> None:
        msg = "no GPU"
        raise RuntimeError(msg)


def test_a_compute_plugin_is_initialised_once() -> None:
    plugin = _Compute()
    _initialise_runtime(plugin, "c")
    assert plugin.initialised == 1


def test_a_hub_plugin_without_the_hook_starts_clean(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _initialise_runtime(_Bare(), "bare")
    assert caplog.records == [], "no hook means nothing to run and nothing to log"


def test_a_failing_hook_is_logged_not_fatal(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        _initialise_runtime(_Broken(), "broken")
    assert any("initialise_runtime failed for plugin broken" in r.getMessage() for r in caplog.records)
