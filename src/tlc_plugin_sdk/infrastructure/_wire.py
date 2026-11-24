# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""How a wire dataclass reads and writes JSON: the coercion helpers and the :class:`Wire` codec.

A peer's shape never raises here, with one exception: a request that refuses unknown values
(:attr:`Wire._refuses_unknown`) raises :class:`~tlc_plugin_sdk.infrastructure.errors.InvalidRequest`
for a ``Literal`` field it cannot read, since acting on a default would do something nobody asked
for. Otherwise scalars are coerced, lists are filtered to their item type, a value of the wrong kind
is the field's default, and a key the type does not know is dropped.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Mapping
from typing import Any, ClassVar, Literal, Self

from tlc_plugin_sdk.infrastructure.errors import InvalidRequest

__all__ = ["Wire"]


# ── Coercion helpers ───────────────────────────────────────────────────────────


def _str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _stripped(value: Any) -> str:
    return _str(value).strip()


def _int(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _float(value: Any, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _opt_bool(value: Any) -> bool | None:
    return None if value is None else _bool(value)


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v) for v in value if v is not None and str(v).strip()]


def _int_list(value: Any) -> list[int]:
    out: list[int] = []
    if isinstance(value, (list, tuple)):
        for v in value:
            parsed = _opt_int(v)
            if parsed is not None:
                out.append(parsed)
    return out


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(v) for v in value if isinstance(v, Mapping)]


def _str_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): str(v) for k, v in value.items() if v is not None}


def _any_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# ── The codec ──────────────────────────────────────────────────────────────────

_MISSING = dataclasses.MISSING


def refuse_unknown(name: str, value: Any, allowed: tuple[Any, ...]) -> None:
    """Raise :class:`InvalidRequest` when a request's ``value`` is set but not one of ``allowed``.

    ``None`` and ``""`` read as unset: the field's default applies.

    Args:
        name: The field, for the message.
        value: What the peer sent.
        allowed: The values the field takes.

    Raises:
        InvalidRequest: ``value`` is set and not allowed.
    """
    text = value.strip() if isinstance(value, str) else value
    if text is None or text == "" or text in allowed:
        return
    choices = ", ".join(repr(a) for a in allowed if a != "")
    raise InvalidRequest(f"Unknown {name} {value!r}", remedy=f"Use one of {choices}.")


def _is_message(tp: Any) -> bool:
    """Whether ``tp`` is a wire dataclass: a :class:`Wire`, or one that writes its own ``from_dict``."""
    return isinstance(tp, type) and dataclasses.is_dataclass(tp) and callable(getattr(tp, "from_dict", None))


def _zero(tp: Any, fallback: Any) -> Any:
    """What a required field reads as when the peer sent nothing usable."""
    origin = typing.get_origin(tp)
    if origin is Literal:
        return fallback if fallback is not None else typing.get_args(tp)[0]
    if origin in (typing.Union, types.UnionType):
        return None
    if origin is list:
        return []
    if origin is dict:
        return {}
    if tp is str:
        return ""
    if tp is bool:
        return False
    if tp is int:
        return 0
    if tp is float:
        return 0.0
    if _is_message(tp):
        return tp.from_dict({})
    return None


def _decode(tp: Any, value: Any, default: Any, fallback: Any) -> Any:
    """``value`` read as ``tp``; ``default`` when it is absent or of the wrong kind."""
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        inner = [arg for arg in typing.get_args(tp) if arg is not type(None)]
        if value is None or len(inner) != 1:
            return None
        return _decode_optional(inner[0], value)
    if value is None:
        return default
    if origin is Literal:
        text = value.strip() if isinstance(value, str) else value
        return text if text in typing.get_args(tp) else default
    if origin is list:
        if not isinstance(value, (list, tuple)):
            return default
        (item,) = typing.get_args(tp)
        return _decode_list(item, value)
    if origin is dict:
        if not isinstance(value, Mapping):
            return default
        return _decode_dict(typing.get_args(tp)[1], value)
    if tp is str:
        return str(value)
    if tp is bool:
        return _bool(value)
    if tp is int:
        return _int(value, default)
    if tp is float:
        return _float(value, default)
    if _is_message(tp):
        return tp.from_dict(value) if isinstance(value, Mapping) else default
    return value


def _decode_optional(tp: Any, value: Any) -> Any:
    if tp is float:
        return _opt_float(value)
    if tp is int:
        return _opt_int(value)
    if tp is bool:
        return _opt_bool(value)
    if tp is str:
        return str(value)
    if typing.get_origin(tp) is dict:
        return dict(value) if isinstance(value, Mapping) else None
    if _is_message(tp):
        return tp.from_dict(value) if isinstance(value, Mapping) else None
    return value


def _decode_list(item: Any, value: Any) -> list[Any]:
    origin = typing.get_origin(item)
    if origin is Literal:
        allowed = typing.get_args(item)
        return [v for v in value if v in allowed]
    if item is str:
        return _str_list(value)
    if item is int:
        return _int_list(value)
    if origin is dict:
        return _dict_list(value)
    if _is_message(item):
        return [item.from_dict(v) for v in value if isinstance(v, Mapping)]
    return list(value)


def _decode_dict(item: Any, value: Mapping[Any, Any]) -> dict[str, Any]:
    if item is str:
        return _str_dict(value)
    if item is bool:
        return {str(k): _bool(v) for k, v in value.items() if v is not None}
    if typing.get_origin(item) is list:
        return {str(k): _str_list(v) for k, v in value.items()}
    return {str(k): v for k, v in value.items()}


def _encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return value.to_dict() if hasattr(value, "to_dict") else dataclasses.asdict(value)
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _encode(v) for k, v in value.items()}
    return value


@dataclasses.dataclass
class Wire:
    """A dataclass that crosses the wire: ``from_dict`` reads a peer's JSON, ``to_dict`` writes it.

    ``from_dict`` reads the keys named after the dataclass's fields and nothing else. A missing
    key, ``None`` or a value of the wrong kind is the field's default; a required field reads as
    the empty value of its type. ``to_dict`` writes every field but one that is ``None``: an
    absent key is how "the provider has nothing to say here" travels.
    """

    _wire_hints: ClassVar[dict[str, Any]]
    #: Field name → what an unreadable value of a required ``Literal`` field reads as (the safe
    #: reading: ``"failed"`` for a status). Without an entry it reads as the type's first value.
    _unreadable: ClassVar[dict[str, str]] = {}
    #: Whether an unknown value of a ``Literal`` field raises :class:`InvalidRequest` instead of
    #: reading as the default: set on what the host asks a provider to act on, never on answers.
    _refuses_unknown: ClassVar[bool] = False

    @classmethod
    def _hints(cls) -> dict[str, Any]:
        cached = cls.__dict__.get("_wire_hints")
        if cached is None:
            cached = typing.get_type_hints(cls)
            cls._wire_hints = cached
        return cached

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        """Read a peer's JSON object; anything else reads as an empty one.

        Args:
            data: The object, as parsed from JSON.

        Returns:
            The message.

        Raises:
            InvalidRequest: The class refuses unknown values and a ``Literal`` field holds one.
        """
        source = _mapping(data)
        hints = cls._hints()
        values: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            tp = hints[f.name]
            fallback = cls._unreadable.get(f.name)
            if f.default is not _MISSING:
                default = f.default
            elif f.default_factory is not _MISSING:
                default = f.default_factory()
            else:
                default = _zero(tp, fallback)
            if cls._refuses_unknown and typing.get_origin(tp) is Literal:
                refuse_unknown(f.name, source.get(f.name), typing.get_args(tp))
            values[f.name] = _decode(tp, source.get(f.name), default, fallback)
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        """Every field that is not ``None`` as JSON, nested messages included.

        Returns:
            The object to send.
        """
        values = ((f.name, getattr(self, f.name)) for f in dataclasses.fields(self))
        return {name: _encode(value) for name, value in values if value is not None}
