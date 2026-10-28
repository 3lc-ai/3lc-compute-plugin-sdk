# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""A config saved again under its id keeps when it was made and when it was last run.

The config pages did not send ``created`` back, so every save stamped a new one and old configs looked
new; the same save dropped ``last_run``. The store keeps both when a save leaves them out.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tlc_plugin_sdk.shared import config_store
from tlc_plugin_sdk.shared.config_store import PluginConfigStore


@dataclass
class _Cfg:
    id: str = ""
    name: str = ""
    epochs: int = 0
    created: str = ""
    last_run: str | None = None


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PluginConfigStore[_Cfg]:
    monkeypatch.setattr(config_store, "CONFIG_ROOT", tmp_path)
    return PluginConfigStore(_Cfg, "resave")


def test_a_resave_without_the_times_keeps_the_stored_ones(store: PluginConfigStore[_Cfg]) -> None:
    first = store.save_config(_Cfg(name="Baseline", epochs=10))
    store.update_last_run(first.id)
    stored = store.get_config(first.id)
    assert stored is not None and stored.last_run

    store.save_config(_Cfg(id=first.id, name="Baseline", epochs=20))

    again = store.get_config(first.id)
    assert again is not None
    assert again.epochs == 20
    assert again.created == first.created
    assert again.last_run == stored.last_run


def test_times_a_save_gives_win(store: PluginConfigStore[_Cfg]) -> None:
    first = store.save_config(_Cfg(name="Baseline"))
    store.save_config(_Cfg(id=first.id, name="Baseline", created="2026-01-01T00:00:00+00:00", last_run="2026-02-01"))
    again = store.get_config(first.id)
    assert again is not None
    assert again.created == "2026-01-01T00:00:00+00:00" and again.last_run == "2026-02-01"


def test_a_new_id_gets_a_new_created(store: PluginConfigStore[_Cfg]) -> None:
    saved = store.save_config(_Cfg(id="fresh", name="New"))
    assert saved.created and saved.last_run is None
