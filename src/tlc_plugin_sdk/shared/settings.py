# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""A plugin's settings from one dataclass: load, merge-and-save, redact, and what is still missing.

Built on :class:`~tlc_plugin_sdk.shared.config_store.PluginConfigStore` (the fixed-id
``"default"`` record). The dataclass carries the store's envelope (``id``, ``created``,
``last_run``) and, per field, optional prompt and merge metadata from :func:`secret` and
:func:`option`; a plain ``field()`` or default works too (no metadata = no prompt, no check).

.. code-block:: python

    @dataclass
    class MySettings:
        id: str = "default"
        created: str = ""
        last_run: str | None = None
        api_key: str = secret(label="My API key", href="https://…", help="Where to find it")
        region: str = option("us-east-1", validate=check_region)
        node_types: list[str] = field(default_factory=lambda: ["gpu-small"])


    settings = PluginSettings(MySettings, "myprovider")
    current = settings.load()
    settings.save({"api_key": "…", "region": "eu-north-1"})
    settings.redacted(current)  # what a fragment shows: secrets as <name>_set markers
    settings.readiness(current)  # {"missing_fields": [...], "missing": [...], "ready": bool}

**Merge rules** (:meth:`PluginSettings.save`): keys that are not fields, and ``id``/``created``/
``last_run``, are ignored. A secret field: ``""`` keeps the current value, ``"-"`` clears it,
anything else sets it. Other fields follow their annotation — ``int`` (a failed conversion keeps
the current value), ``bool`` (``1/true/yes/on``), ``float``, ``list[str]`` (stripped, empties
dropped), ``dict[str, str]``, ``list[<dataclass>]`` (each row from its known fields, unknown
keys dropped), ``str``. ``coerce=`` runs on the incoming value first; ``validate=`` is then
called with it and its return is stored (a ``ValueError`` is the caller's 400), and an empty
value keeps the current one unless ``clearable=True``. After the merge, a ``normalise(self)``
method on the dataclass runs, when it has one.

An infrastructure plugin that sets ``settings = PluginSettings(...)`` on its class gets
``GET /settings`` and ``POST /settings`` mounted by the SDK.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar, Union

from tlc_plugin_sdk.shared.config_store import PluginConfigStore

if TYPE_CHECKING:
    from tlc_plugin_sdk.infrastructure.types import SettingsField

__all__ = ["PluginSettings", "SettingsUnreadable", "option", "secret"]

S = TypeVar("S")

_META = "tlc_settings"
_ENVELOPE = ("id", "created", "last_run")


class SettingsUnreadable(ValueError):
    """The settings file exists but does not parse; it must be fixed or deleted, never read as defaults."""


@dataclass(frozen=True)
class _Spec:
    """The prompt and merge metadata of one field."""

    secret: bool = False
    label: str = ""
    help: str = ""
    href: str = ""
    placeholder: str = ""
    required: bool = False
    validate: Callable[[Any], Any] | None = None
    clearable: bool = False
    coerce: Callable[[Any], Any] | None = None

    @property
    def prompts(self) -> bool:
        """Whether the field is described to a person (has a label)."""
        return bool(self.label)

    def describe(self, key: str) -> SettingsField:
        """The field as a prompt."""
        # Local: ``infrastructure`` imports this module, so the type cannot be imported at the top.
        from tlc_plugin_sdk.infrastructure.types import SettingsField

        return SettingsField(
            key=key,
            label=self.label,
            required=self.required,
            secret=self.secret,
            help=self.help,
            href=self.href,
            placeholder=self.placeholder,
        )


def secret(*, label: str, help: str = "", href: str = "", placeholder: str = "", required: bool = True) -> Any:  # noqa: A002
    """A secret string setting (default ``""``): masked in the fragment, never echoed, ``"-"`` clears it.

    Args:
        label: What the prompt says.
        help: Where the value is found, in a sentence.
        href: A link to where the value is found.
        placeholder: An example value.
        required: Whether a node cannot be created without it.

    Returns:
        A dataclass field.
    """
    spec = _Spec(secret=True, label=label, help=help, href=href, placeholder=placeholder, required=required)
    return field(default="", metadata={_META: spec})


def option(
    default: Any = MISSING,
    *,
    default_factory: Callable[[], Any] | None = None,
    label: str = "",
    help: str = "",  # noqa: A002
    href: str = "",
    placeholder: str = "",
    required: bool = False,
    validate: Callable[[Any], Any] | None = None,
    clearable: bool = False,
    coerce: Callable[[Any], Any] | None = None,
) -> Any:
    """A non-secret setting with prompt and merge metadata.

    Args:
        default: The default value (or give ``default_factory``).
        default_factory: A factory for a mutable default.
        label: What the prompt says when the value is missing (no label: never prompted).
        help: Where the value is found, in a sentence.
        href: A link to where the value is found.
        placeholder: An example value.
        required: Whether a node cannot be created without it.
        validate: Called with the incoming value on save; its return is stored; raise
            ``ValueError`` with a sentence to refuse it.
        clearable: Whether an empty incoming value clears a validated field (else it is kept).
        coerce: Called with the incoming value before validation (a fallback, a parse).

    Returns:
        A dataclass field.
    """
    spec = _Spec(
        label=label,
        help=help,
        href=href,
        placeholder=placeholder,
        required=required,
        validate=validate,
        clearable=clearable,
        coerce=coerce,
    )
    if default_factory is not None:
        return field(default_factory=default_factory, metadata={_META: spec})
    if default is MISSING:
        return field(metadata={_META: spec})
    return field(default=default, metadata={_META: spec})


# ── Annotation-driven coercion ─────────────────────────────────────────────────


def _hints(cls: type[Any]) -> dict[str, Any]:
    try:
        return typing.get_type_hints(cls)
    except Exception:  # a forward reference the module cannot resolve: fall back to the raw annotations
        return {f.name: f.type for f in fields(cls)}


def _strip_optional(hint: Any) -> tuple[Any, bool]:
    origin = typing.get_origin(hint)
    if origin is Union or (hasattr(types, "UnionType") and origin is types.UnionType):
        args = [a for a in typing.get_args(hint) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return hint, False


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _row_from(cls: type[Any], raw: Mapping[str, Any]) -> Any:
    """One nested dataclass row from its known keys; unknown keys dropped, missing keys defaulted."""
    hints = _hints(cls)
    known = {f.name for f in fields(cls)}
    blank = cls()
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key in known:
            kwargs[key] = _coerce(hints.get(key, str), value, getattr(blank, key))
    return cls(**kwargs)


def _coerce(hint: Any, value: Any, current: Any) -> Any:
    """``value`` as ``hint`` says, or ``current`` when it cannot be read that way."""
    inner, optional = _strip_optional(hint)
    if value is None:
        return None if optional else current
    origin = typing.get_origin(inner)
    args = typing.get_args(inner)
    if inner is str or (isinstance(inner, str) and inner == "str"):
        return str(value)
    if inner is bool:
        return _as_bool(value)
    if inner is int:
        if isinstance(value, bool):
            return current
        try:
            return int(value)
        except (TypeError, ValueError):
            return current
    if inner is float:
        if isinstance(value, bool):
            return current
        try:
            return float(value)
        except (TypeError, ValueError):
            return current
    if origin is list:
        if not isinstance(value, (list, tuple)):
            return current
        item = args[0] if args else str
        if isinstance(item, type) and is_dataclass(item):
            rows: list[Any] = []
            for v in value:
                if isinstance(v, item):
                    rows.append(v)
                elif isinstance(v, Mapping):
                    rows.append(_row_from(item, v))
            return rows
        if item is int:
            out: list[int] = []
            for v in value:
                try:
                    out.append(int(v))
                except (TypeError, ValueError):
                    continue
            return out
        return [str(v).strip() for v in value if v is not None and str(v).strip()]
    if origin is dict:
        if not isinstance(value, Mapping):
            return current
        return {str(k): str(v) for k, v in value.items() if v is not None}
    if isinstance(inner, type) and is_dataclass(inner):
        if isinstance(value, inner):
            return value
        return _row_from(inner, value) if isinstance(value, Mapping) else current
    return value


def _is_env_dict(name: str, hint: Any) -> bool:
    inner, _ = _strip_optional(hint)
    return typing.get_origin(inner) is dict and (name == "env" or name.endswith("_env"))


def _row_class(hint: Any) -> type[Any] | None:
    inner, _ = _strip_optional(hint)
    if typing.get_origin(inner) is list:
        args = typing.get_args(inner)
        if args and isinstance(args[0], type) and is_dataclass(args[0]):
            return args[0]
    return None


# ── The settings object ────────────────────────────────────────────────────────


class PluginSettings(Generic[S]):
    """A plugin's settings record, typed by its dataclass.

    Args:
        settings_cls: The settings dataclass (with ``id``, ``created``, ``last_run``).
        plugin_id: The plugin's manifest id (the store's directory).
        record_id: The record's id (``"default"``: one record per plugin).

    Raises:
        TypeError: When ``settings_cls`` is not a dataclass.
    """

    def __init__(self, settings_cls: type[S], plugin_id: str, *, record_id: str = "default") -> None:
        if not is_dataclass(settings_cls):
            msg = f"PluginSettings needs a dataclass, got {settings_cls!r}"
            raise TypeError(msg)
        self.settings_cls = settings_cls
        self._cls: type[Any] = settings_cls
        self.plugin_id = plugin_id
        self.record_id = record_id
        self._specs: dict[str, _Spec] = {}
        for f in fields(self._cls):
            meta = f.metadata.get(_META) if f.metadata else None
            if isinstance(meta, _Spec):
                self._specs[f.name] = meta
        self._hint_cache: dict[str, Any] | None = None

    # ── reading ──

    def _store(self) -> PluginConfigStore[S]:
        # Built per call: the store's directory follows ``config_store.CONFIG_ROOT`` as it is now
        # (a harness redirects it for its lifetime).
        return PluginConfigStore(self.settings_cls, self.plugin_id)

    def _hints_of(self) -> dict[str, Any]:
        if self._hint_cache is None:
            self._hint_cache = _hints(self._cls)
        return self._hint_cache

    def load(self) -> S:
        """The record, with nested dataclass rows re-hydrated; defaults when never saved.

        Returns:
            The settings.

        Raises:
            SettingsUnreadable: When the file exists but does not parse.
        """
        store = self._store()
        settings = store.get_config(self.record_id)
        if settings is None:
            if store.exists(self.record_id):
                msg = (
                    f"The {self.plugin_id} plugin's {self.record_id}.json exists but is not valid JSON — "
                    "fix or delete it"
                )
                raise SettingsUnreadable(msg)
            return self.settings_cls()
        hints = self._hints_of()
        for f in fields(self._cls):
            hint = hints.get(f.name)
            if hint is None:
                continue
            value = getattr(settings, f.name)
            if _row_class(hint) is not None or _is_env_dict(f.name, hint):
                setattr(settings, f.name, _coerce(hint, value, value))
        return settings

    # ── writing ──

    def save(self, update: Mapping[str, Any]) -> S:
        """Merge ``update`` onto the stored record and save it.

        Args:
            update: The incoming keys (the fragment's form).

        Returns:
            The settings after the merge.

        Raises:
            SettingsUnreadable: When the file exists but does not parse (nothing is merged).
            ValueError: From a field's ``validate`` (or ``coerce``): the value cannot be used.
        """
        current = self.load()
        hints = self._hints_of()
        names = {f.name for f in fields(self._cls)}
        for key, value in update.items():
            if key not in names or key in _ENVELOPE:
                continue
            spec = self._specs.get(key, _Spec())
            hint = hints.get(key, str)
            if spec.secret:
                text = "" if value is None else str(value)
                if not text:
                    continue
                setattr(current, key, "" if text == "-" else text)
                continue
            if spec.coerce is not None:
                value = spec.coerce(value)
            if spec.validate is not None:
                if value is None or value == "" or value == [] or value == {}:
                    if spec.clearable:
                        setattr(current, key, _coerce(hint, value, getattr(current, key)) if value is not None else "")
                    continue
                setattr(current, key, spec.validate(value))
                continue
            setattr(current, key, _coerce(hint, value, getattr(current, key)))
        normalise = getattr(current, "normalise", None)
        if callable(normalise):
            normalise()
        self._store().save_config(current)
        return current

    # ── views ──

    def redacted(self, settings: S) -> dict[str, Any]:
        """The fragment's view: ``id``, every non-secret field, secrets as ``<name>_set``, env dicts as ``<name>_keys``.

        Args:
            settings: The record to view.

        Returns:
            A JSON-serializable dict with no secret value in it.
        """
        return self._redact(settings, self._cls, self._specs, skip=("created", "last_run"))

    def _redact(self, obj: Any, cls: type[Any], specs: Mapping[str, _Spec], *, skip: tuple[str, ...]) -> dict[str, Any]:
        hints = _hints(cls)
        out: dict[str, Any] = {}
        for f in fields(cls):
            if f.name in skip:
                continue
            value = getattr(obj, f.name)
            spec = specs.get(f.name)
            hint = hints.get(f.name, str)
            if spec is not None and spec.secret:
                out[f"{f.name}_set"] = bool(value)
            elif _is_env_dict(f.name, hint) and isinstance(value, Mapping):
                out[f"{f.name}_keys"] = sorted(str(k) for k in value)
            elif (row_cls := _row_class(hint)) is not None and isinstance(value, list):
                row_specs = {
                    rf.name: rf.metadata[_META]
                    for rf in fields(row_cls)
                    if rf.metadata and isinstance(rf.metadata.get(_META), _Spec)
                }
                out[f.name] = [
                    self._redact(row, row_cls, row_specs, skip=()) if isinstance(row, row_cls) else row for row in value
                ]
            elif isinstance(value, list):
                out[f.name] = list(value)
            elif isinstance(value, Mapping):
                out[f.name] = dict(value)
            else:
                out[f.name] = value
        return out

    def secret_values(self, settings: S) -> list[str]:
        """Every value an error message must never echo: secret fields and env-dict values, nested rows included.

        Args:
            settings: The record.

        Returns:
            The non-empty values.
        """
        return self._secrets(settings, self._cls, self._specs)

    def _secrets(self, obj: Any, cls: type[Any], specs: Mapping[str, _Spec]) -> list[str]:
        hints = _hints(cls)
        found: list[str] = []
        for f in fields(cls):
            value = getattr(obj, f.name)
            spec = specs.get(f.name)
            hint = hints.get(f.name, str)
            if spec is not None and spec.secret:
                if value:
                    found.append(str(value))
            elif _is_env_dict(f.name, hint) and isinstance(value, Mapping):
                found.extend(str(v) for v in value.values() if v)
            elif (row_cls := _row_class(hint)) is not None and isinstance(value, list):
                row_specs = {
                    rf.name: rf.metadata[_META]
                    for rf in fields(row_cls)
                    if rf.metadata and isinstance(rf.metadata.get(_META), _Spec)
                }
                for row in value:
                    if isinstance(row, row_cls):
                        found.extend(self._secrets(row, row_cls, row_specs))
        return found

    def missing_fields(self, settings: S) -> list[SettingsField]:
        """The described (labelled) fields whose value is empty, each as a prompt.

        Args:
            settings: The record.

        Returns:
            The prompts, in field order.
        """
        out: list[SettingsField] = []
        for f in fields(self._cls):
            spec = self._specs.get(f.name)
            if spec is None or not spec.prompts:
                continue
            value = getattr(settings, f.name)
            if value is None or value == "" or value == [] or value == {}:
                out.append(spec.describe(f.name))
        return out

    def readiness(self, settings: S) -> dict[str, Any]:
        """``{"missing_fields": [...], "missing": [required keys], "ready": bool}`` for a capabilities answer.

        Args:
            settings: The record.

        Returns:
            Keyword arguments for :class:`~tlc_plugin_sdk.infrastructure.CapabilitiesResponse`.
        """
        missing_fields = self.missing_fields(settings)
        missing = [f.key for f in missing_fields if f.required]
        return {"missing_fields": missing_fields, "missing": missing, "ready": not missing}

    def secret_fields(self) -> list[str]:
        """The names of the secret fields (what ``redacted`` reports as ``<name>_set``)."""
        return [name for name, spec in self._specs.items() if spec.secret]

    def field_view(self) -> list[dict[str, Any]]:
        """The schema of every described field, for a fragment that renders its own form.

        Returns:
            One :class:`~tlc_plugin_sdk.infrastructure.SettingsField` dict per labelled field.
        """
        return [spec.describe(name).to_dict() for name, spec in self._specs.items() if spec.prompts]
