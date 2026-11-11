# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared non-secret AI policy types and validation.

Production hosts load organization policy from Config Service. ``SettingsStore``
remains an explicit standalone/legacy adapter; it does not choose production scope.
Provider keys live only in separately granted Config Service SECRET Connections.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from .catalog import DEFAULTS, MODELS, PROVIDERS, ROLES, efforts_for
from .state_io import owner_key, save_json

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class Route:
    """Which Connection and model answer a role."""

    service: str
    connection_id: str
    model: str
    effort: str = "medium"
    base_url: str = ""  # openai-compatible only

    @property
    def protocol(self) -> str:
        return PROVIDERS[self.service]["protocol"]

    @property
    def endpoint(self) -> str:
        from .endpoints import endpoint

        return endpoint(self.service, self.base_url)


@dataclass(frozen=True)
class Limits:
    per_turn_usd: float = 1.0
    per_day_usd: float = 10.0
    per_job_usd: float = 100.0
    max_steps: int = 24
    python_seconds: int = 120
    max_output_tokens: int = 8192


@dataclass
class Settings:
    routes: dict[str, Route] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    updated_at: float = 0.0
    enabled: bool = False

    @property
    def ready(self) -> bool:
        return all(role in self.routes for role in ROLES)

    def connection_ids(self) -> dict[str, str]:
        """The Connection a turn names per service: what goes in ``credential_connection_ids``."""
        out: dict[str, str] = {}
        for route in self.routes.values():
            if route.connection_id:
                out.setdefault(route.service, route.connection_id)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "routes": {role: asdict(route) for role, route in self.routes.items()},
            "limits": asdict(self.limits),
            "updated_at": self.updated_at,
            "ready": self.ready,
            "enabled": self.enabled,
            "connection_ids": self.connection_ids(),
        }


def _number(value: object, name: str, low: float, high: float) -> float:
    if isinstance(value, bool):
        msg = f"{name} must be a number, not a boolean"
        raise ValueError(msg)
    try:
        number = float(cast("str | float | int", value))
    except (TypeError, ValueError):
        msg = f"{name} is a number"
        raise ValueError(msg) from None
    if not (low <= number <= high):
        msg = f"{name} is between {low:g} and {high:g}"
        raise ValueError(msg)
    return number


def parse_route(role: str, raw: object) -> Route:
    if not isinstance(raw, dict):
        msg = f"The {role} route is an object"
        raise ValueError(msg)
    service = str(raw.get("service") or "")
    if service not in PROVIDERS:
        msg = f"The {role} route names a service: {', '.join(PROVIDERS)}"
        raise ValueError(msg)
    connection_id = str(raw.get("connection_id") or "")
    if connection_id and not _ID_RE.match(connection_id):
        msg = f"The {role} route's Connection id is malformed"
        raise ValueError(msg)
    if service != "openai-compatible" and not connection_id:
        msg = f"Choose a {PROVIDERS[service]['label']} Connection for {role}"
        raise ValueError(msg)
    model = str(raw.get("model") or DEFAULTS.get(service, {}).get(role) or "")
    if not _MODEL_RE.match(model):
        msg = f"The {role} route names a model"
        raise ValueError(msg)
    if service != "openai-compatible":
        entry = MODELS.get(model)
        if entry is None or entry["service"] != service:
            msg = f"{model} is not a {PROVIDERS[service]['label']} model the assistant prices"
            raise ValueError(msg)
        if role not in entry["roles"]:
            msg = f"{entry['label']} is not offered for {role}"
            raise ValueError(msg)
    effort = str(raw.get("effort") or "medium")
    efforts = efforts_for(model)
    if effort not in efforts:
        msg = f"The {role} route's effort for {model} is one of {', '.join(efforts)}"
        raise ValueError(msg)
    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    if service == "openai-compatible":
        if not re.match(r"^https?://[^\s/]+(/[^\s]*)?$", base_url):
            msg = "Your model server's address is an http(s) URL, e.g. http://127.0.0.1:11434/v1"
            raise ValueError(msg)
    else:
        base_url = ""
    return Route(service=service, connection_id=connection_id, model=model, effort=effort, base_url=base_url)


def parse_limits(raw: object) -> Limits:
    raw = raw if isinstance(raw, dict) else {}
    defaults = Limits()
    return Limits(
        per_turn_usd=_number(raw.get("per_turn_usd", defaults.per_turn_usd), "The limit per message", 0.01, 500),
        per_day_usd=_number(raw.get("per_day_usd", defaults.per_day_usd), "The limit per day", 0.1, 10000),
        per_job_usd=_number(raw.get("per_job_usd", defaults.per_job_usd), "The limit per job", 0.01, 10000),
        max_steps=int(_number(raw.get("max_steps", defaults.max_steps), "Steps per message", 2, 80)),
        python_seconds=int(_number(raw.get("python_seconds", defaults.python_seconds), "Python seconds", 5, 1800)),
        max_output_tokens=int(
            _number(raw.get("max_output_tokens", defaults.max_output_tokens), "Output tokens", 256, 65536)
        ),
    )


def parse_settings(raw: object) -> Settings:
    """Validate a settings body. Raises ``ValueError`` with a message for the person."""
    if not isinstance(raw, dict):
        msg = "Settings are an object"
        raise ValueError(msg)
    routes_raw = raw.get("routes") or {}
    if not isinstance(routes_raw, dict):
        msg = "routes maps roles to routes"
        raise ValueError(msg)
    routes = {role: parse_route(role, routes_raw[role]) for role in ROLES if role in routes_raw}
    by_service: dict[str, tuple[str, str]] = {}
    for route in routes.values():
        binding = (route.connection_id, route.endpoint)
        if route.service in by_service and by_service[route.service] != binding:
            msg = "Roles for one service must use the same Connection and endpoint"
            raise ValueError(msg)
        by_service[route.service] = binding
    unknown = set(routes_raw) - set(ROLES)
    if unknown:
        msg = f"Unknown roles: {', '.join(sorted(unknown))}"
        raise ValueError(msg)
    enabled = raw.get("enabled", False)
    if "features" in raw:
        # Read the old private Hub policy without requiring a Config Service
        # migration. Its Assistant choice becomes the shared AI switch. Never
        # let the former plugin-specific switch enable a disabled Assistant.
        legacy = raw["features"]
        if (
            "enabled" in raw
            or not isinstance(legacy, dict)
            or set(legacy) not in ({"assistant", "refine"}, {"assistant", "automate"})
            or any(type(value) is not bool for value in legacy.values())
        ):
            msg = "Invalid legacy AI enablement"
            raise ValueError(msg)
        enabled = legacy["assistant"]
    if type(enabled) is not bool:
        msg = "AI enablement must be a boolean"
        raise ValueError(msg)
    return Settings(routes=routes, limits=parse_limits(raw.get("limits")), updated_at=time.time(), enabled=enabled)


class SettingsStore:
    """Settings per person under ``<state_dir>/settings/``."""

    def __init__(self, state_dir: Path) -> None:
        self.root = Path(state_dir) / "settings"

    def _path(self, user_id: str) -> Path:
        safe = owner_key(user_id)
        return self.root / f"{safe}.json"

    def load(self, user_id: str) -> Settings:
        path = self._path(user_id)
        if not path.exists():
            return Settings()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            settings = parse_settings(raw)
            settings.updated_at = float(raw.get("updated_at", 0))
        except (OSError, ValueError, TypeError):
            return Settings()
        return settings

    def save(self, user_id: str, raw: object) -> Settings:
        settings = parse_settings(raw)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(user_id)
        save_json(path, settings.as_dict())
        return settings
