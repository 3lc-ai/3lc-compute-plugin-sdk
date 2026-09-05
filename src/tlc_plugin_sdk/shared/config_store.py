# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Generic on-disk store for a plugin's saved job configs.

A "config" here is a **reusable job parameterization** a user names and re-runs
from the plugin UI (the "New config" / config-bar feature driven by
:func:`tlc_plugin_sdk.shared.config_ui.config_ui_script`) — NOT the
service/host settings (those live in ``persistent_settings`` / ``settings.json``).

Each plugin keeps its own ``@dataclass`` config schema and hands the *type* to
:class:`PluginConfigStore`, which owns the JSON-on-disk CRUD. The config
dataclass must carry the common envelope fields the store manages:

- ``id: str``         — assigned on first save
- ``created: str``    — ISO timestamp, assigned on first save; list order key
- ``last_run: str | None`` — bumped by :meth:`update_last_run`

(``name: str`` is conventional for the UI but not required by the store.)

Configs live under ``~/.3lc-plugin-configs/<plugin-id>/``. Pass ``legacy_dir``
to lazily migrate a pre-standardization location on first construction.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generic, TypeVar

logger = logging.getLogger(__name__)

# Standardized root for all plugins' saved job configs.
CONFIG_ROOT = Path.home() / ".3lc-plugin-configs"

T = TypeVar("T")


class PluginConfigStore(Generic[T]):
    """Persist a plugin's saved job configs as JSON files, one per config.

    Args:
        config_cls: The plugin's config ``@dataclass`` (must have ``id`` /
            ``created`` / ``last_run`` fields). Instances are (de)serialized via
            :func:`dataclasses.asdict` and ``config_cls(**known_fields)``.
        plugin_id: The plugin's manifest id; configs live under
            ``~/.3lc-plugin-configs/<plugin_id>/``.
        legacy_dir: Optional back-compat directory. If the standardized
            directory has no configs yet and ``legacy_dir`` holds some, they are
            moved on construction (one-time, idempotent).

    """

    def __init__(self, config_cls: type[T], plugin_id: str, *, legacy_dir: Path | str | None = None) -> None:
        if not is_dataclass(config_cls):
            msg = f"PluginConfigStore requires a dataclass config type, got {config_cls!r}"
            raise TypeError(msg)
        self._cls = config_cls
        self._dir = CONFIG_ROOT / plugin_id
        # Configs hold provider keys and tokens: owner-only directory and files.
        self._dir.mkdir(parents=True, exist_ok=True)
        _restrict(self._dir, 0o700)
        if legacy_dir is not None:
            self._migrate_legacy(Path(legacy_dir))

    # ── CRUD ─────────────────────────────────────────────────────────────
    def list_configs(self) -> list[T]:
        """Return all saved configs, newest first (by ``created``)."""
        configs: list[T] = []
        for f in sorted(self._dir.glob("*.json")):
            cfg = self._read(f)
            if cfg is not None:
                configs.append(cfg)
        configs.sort(key=lambda c: getattr(c, "created", "") or "", reverse=True)
        return configs

    def get_config(self, config_id: str) -> T | None:
        """Load a config by id, or None if missing/unreadable."""
        return self._read(self._path(config_id))

    def save_config(self, config: T) -> T:
        """Save a config, assigning ``id`` and ``created`` on first save."""
        if not getattr(config, "id", ""):
            config.id = str(uuid.uuid4())  # type: ignore[attr-defined]
        if not getattr(config, "created", ""):
            config.created = datetime.now(timezone.utc).isoformat()  # type: ignore[attr-defined]
        path = self._path(config.id)  # type: ignore[attr-defined]
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(config), f, indent=2)  # type: ignore[call-overload]
        _restrict(path, 0o600)  # an existing world-readable file is tightened too
        return config

    def exists(self, config_id: str) -> bool:
        """True when a config with ``config_id`` is saved (no read, no deserialisation)."""
        return self._path(config_id).exists()

    def delete_config(self, config_id: str) -> bool:
        """Delete a config. Returns True if it existed."""
        path = self._path(config_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def update_last_run(self, config_id: str) -> None:
        """Stamp ``last_run`` with the current time, if the config exists."""
        config = self.get_config(config_id)
        if config is not None:
            config.last_run = datetime.now(timezone.utc).isoformat()  # type: ignore[attr-defined]
            self.save_config(config)

    # ── internals ────────────────────────────────────────────────────────
    @property
    def directory(self) -> Path:
        """Where this plugin's configs live (read-only view for callers that need the path)."""
        return self._dir

    def _path(self, config_id: str) -> Path:
        return self._dir / f"{config_id}.json"

    def _read(self, path: Path) -> T | None:
        """Load + deserialize one config file, tolerating drift/corruption.

        Unknown keys (older/newer schema) are dropped and missing fields fall
        back to dataclass defaults, so a config schema can evolve without
        invalidating saved files.
        """
        if not path.exists():
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            known = {k: v for k, v in data.items() if k in self._cls.__dataclass_fields__}  # type: ignore[attr-defined]
            return self._cls(**known)
        except (json.JSONDecodeError, OSError, TypeError):
            return None

    def _migrate_legacy(self, legacy_dir: Path) -> None:
        """Move configs from a pre-standardization dir into the new location once."""
        if not legacy_dir.is_dir() or any(self._dir.glob("*.json")):
            return
        moved = 0
        for f in legacy_dir.glob("*.json"):
            dest = self._dir / f.name
            if not dest.exists():
                shutil.move(str(f), str(dest))
                moved += 1
        if moved:
            logger.info("Migrated %d config(s) from %s → %s", moved, legacy_dir, self._dir)


def _restrict(path: Path, mode: int) -> None:
    """Best-effort chmod: the store must keep working on filesystems without POSIX modes."""
    try:
        os.chmod(path, mode)
    except OSError:
        logger.debug("Could not chmod %s", path, exc_info=True)
