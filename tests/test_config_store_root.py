# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The config root is resolved when a store is built, never at import.

A filtered worker environment may carry no home directory. On Windows ``Path.home()`` then
raises. Importing the SDK must still succeed, and
only building a store without an override fails, with an error that says what to set."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tlc_plugin_sdk.harness import PluginHarness
from tlc_plugin_sdk.shared import config_store

_HOME_VARS = ("USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME")


@dataclass
class _Cfg:
    id: str = ""
    created: str = ""
    last_run: str | None = None


def _no_home(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise() -> Path:
        msg = "Could not determine home directory."
        raise RuntimeError(msg)

    monkeypatch.setattr(config_store.Path, "home", staticmethod(_raise))


def test_every_sdk_module_imports_without_a_home_directory() -> None:
    code = (
        "import importlib, pkgutil, tlc_plugin_sdk;"
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(tlc_plugin_sdk.__path__, 'tlc_plugin_sdk.')]"
    )
    env = {k: v for k, v in os.environ.items() if k.upper() not in _HOME_VARS}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


def test_config_root_follows_the_home_directory_at_call_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_store, "CONFIG_ROOT", None)
    monkeypatch.delenv("TLC_PLUGIN_CONFIG_DIR", raising=False)
    monkeypatch.setattr(config_store.Path, "home", staticmethod(lambda: tmp_path))
    assert config_store.config_root() == tmp_path / ".3lc-plugin-configs"
    assert config_store.PluginConfigStore(_Cfg, "p").directory == tmp_path / ".3lc-plugin-configs" / "p"


def test_a_store_without_a_home_directory_names_what_to_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_store, "CONFIG_ROOT", None)
    monkeypatch.delenv("TLC_PLUGIN_CONFIG_DIR", raising=False)
    _no_home(monkeypatch)
    with pytest.raises(config_store.ConfigRootUnavailable, match=r"USERPROFILE.*HOME.*TLC_PLUGIN_CONFIG_DIR") as info:
        config_store.PluginConfigStore(_Cfg, "p")
    assert isinstance(info.value, RuntimeError)


def test_the_override_needs_no_home_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLC_PLUGIN_CONFIG_DIR", str(tmp_path / "from-environment"))
    monkeypatch.setattr(config_store, "CONFIG_ROOT", tmp_path)
    _no_home(monkeypatch)
    assert config_store.PluginConfigStore(_Cfg, "p").directory == tmp_path / "p"


def test_environment_roots_keep_configs_separate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_store, "CONFIG_ROOT", None)
    _no_home(monkeypatch)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "old.json").write_text("{}", encoding="utf-8")

    first_root = tmp_path / "one"
    monkeypatch.setenv("TLC_PLUGIN_CONFIG_DIR", str(first_root))
    first = config_store.PluginConfigStore(_Cfg, "p", legacy_dir=legacy)
    saved = first.save_config(_Cfg())
    assert first.directory == first_root / "p"

    monkeypatch.setenv("TLC_PLUGIN_CONFIG_DIR", str(tmp_path / "two"))
    second = config_store.PluginConfigStore(_Cfg, "p", legacy_dir=legacy)
    assert second.list_configs() == []
    assert first.get_config(saved.id) == saved
    assert (legacy / "old.json").is_file()


def test_the_harness_redirects_stores_and_restores_no_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tlc_plugin_sdk.contract import HubPlugin

    class _Plugin(HubPlugin):
        def get_ui_fragment(self) -> str:
            return "<div>probe</div>"

    monkeypatch.setattr(config_store, "CONFIG_ROOT", None)
    _no_home(monkeypatch)
    with PluginHarness(_Plugin(), plugin_id="probe", config_root=tmp_path, initialise=False):
        assert config_store.PluginConfigStore(_Cfg, "probe").directory == tmp_path / "probe"
    assert config_store.CONFIG_ROOT is None
