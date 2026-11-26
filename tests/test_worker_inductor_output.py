# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The worker keeps PyTorch Inductor's compile-worker output, which Inductor discards by default.

A compiler error inside ``torch.compile`` is otherwise reduced to an exit status. The setting must be in
the environment before the plugin is loaded, since torch reads it when it is first imported, and a value
the operator set stays.
"""

from __future__ import annotations

import os

import pytest

from tlc_plugin_sdk import worker

_NAME = "TORCHINDUCTOR_WORKER_SUPPRESS_LOGGING"


class _Loaded(Exception):
    pass


def _value_when_plugin_loads(monkeypatch: pytest.MonkeyPatch) -> str | None:
    seen: list[str | None] = []

    def load(entry: str) -> None:
        seen.append(os.environ.get(_NAME))
        raise _Loaded

    monkeypatch.setattr(worker, "_load_plugin", load)
    with pytest.raises(_Loaded):
        worker.serve("pkg:Plugin", "plugin", host="127.0.0.1", port=0)
    return seen[0]


def test_compile_worker_output_is_kept_before_the_plugin_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_NAME, raising=False)
    assert _value_when_plugin_loads(monkeypatch) == "0"


def test_a_value_already_set_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_NAME, "1")
    assert _value_when_plugin_loads(monkeypatch) == "1"
