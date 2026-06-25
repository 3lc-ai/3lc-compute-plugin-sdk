# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for discovering and reading class labels from 3LC tables.

Centralizes the label-handling pattern used across plugins. The actual value
map reading is core SDK API — ``table.get_value_map(path)`` and
``table.get_simple_value_map(path)`` (which canonicalize on
``MapElement.internal_name``). What core does not provide, and this module
centralizes, is *discovering the label value path*: the dot-path to the label
value differs by modality and table convention:

- classification: the label column itself (e.g. ``"label"``)
- detection: ``"{column}.instances_additional_data.label"`` (tlc 3.x) or
  ``"{column}.bb_list.label"`` (legacy tables)
- segmentation: ``"{column}.instance_properties.label"``

Porting to tlc core
-------------------
Like ``shared/images.py``, this belongs in the SDK next to
``Table.get_value_map``. Intended mapping: ``find_label_path`` →
``Table.find_label_path(column=None)``; ``get_label_map`` /
``get_label_names`` → thin conveniences over ``Table.get_simple_value_map``.
Plugins import only from this module, so the port touches exactly this file.
"""

from __future__ import annotations

from typing import Any

# Nested sub-paths where instance labels live, in preference order. Verified
# against tlc 3.1: BoundingBoxes2D produces instances_additional_data.label,
# SegmentationPolygons produces instance_properties.label; bb_list.label is
# kept for tables written by older SDKs.
_INSTANCE_LABEL_SUBPATHS = (
    "instances_additional_data.label",
    "bb_list.label",
    "instance_properties.label",
)

# Conventional instance-column names to probe when no column is given.
_INSTANCE_COLUMN_CANDIDATES = (
    "bbs",
    "bbs_predicted",
    "segmentations",
    "segmentations_predicted",
    "predicted_segmentations",
    "segmentation",
    "masks",
)

# Conventional plain (classification) label column, tried last.
_PLAIN_LABEL_COLUMN = "label"


def candidate_label_paths(column: str | None = None) -> list[str]:
    """Enumerate candidate label value paths, most-specific first.

    Args:
        column: An explicit column to search under. When given, only that
            column (direct, then nested) is considered; otherwise the
            conventional instance columns and the plain ``label`` column.

    Returns:
        Dot-paths suitable for ``table.get_value_map``.

    """
    paths: list[str] = []
    if column:
        paths.append(column)
        paths.extend(f"{column}.{sub}" for sub in _INSTANCE_LABEL_SUBPATHS)
        return paths

    for col in _INSTANCE_COLUMN_CANDIDATES:
        paths.extend(f"{col}.{sub}" for sub in _INSTANCE_LABEL_SUBPATHS)
    paths.append(_PLAIN_LABEL_COLUMN)
    return paths


def find_label_path(table: Any, column: str | None = None) -> str | None:
    """Find the dot-path to a table's label value map.

    Args:
        table: A loaded ``tlc.Table``.
        column: Optional column to restrict the search to.

    Returns:
        The first candidate path with a non-empty value map, or ``None``.

    """
    for path in candidate_label_paths(column):
        try:
            if table.get_value_map(path):
                return path
        except Exception:
            continue
    return None


def get_label_map(table: Any, column: str | None = None, *, path: str | None = None) -> dict[int, str]:
    """Read a table's label map as ``{class index: internal name}``.

    Thin wrapper over ``table.get_simple_value_map`` with path discovery.

    Args:
        table: A loaded ``tlc.Table``.
        column: Optional column to restrict path discovery to.
        path: Explicit value path; skips discovery when given.

    Returns:
        The label map, or ``{}`` if the table has none.

    """
    label_path = path or find_label_path(table, column)
    if not label_path:
        return {}
    try:
        simple = table.get_simple_value_map(label_path)
    except Exception:
        return {}
    return dict(simple) if simple else {}


def get_label_names(table: Any, column: str | None = None, *, path: str | None = None) -> list[str]:
    """Read a table's class names, ordered by class index.

    Args:
        table: A loaded ``tlc.Table``.
        column: Optional column to restrict path discovery to.
        path: Explicit value path; skips discovery when given.

    Returns:
        Class names in index order, or ``[]`` if the table has no label map.

    """
    label_map = get_label_map(table, column, path=path)
    return [label_map[k] for k in sorted(label_map)]


def get_class_name_lookup(table: Any, column: str | None = None, *, path: str | None = None) -> dict[str, str]:
    """Read a table's label map as a string-keyed lookup table.

    Keys include both the raw map key (``"1.0"``) and its integer form
    (``"1"``), so callers can index with whichever flavor their data carries.
    This is the shape the run_insights statistics pipeline consumes.

    Args:
        table: A loaded ``tlc.Table``.
        column: Optional column to restrict path discovery to.
        path: Explicit value path; skips discovery when given.

    Returns:
        ``{class key (str): internal name}``, or ``{}`` if no label map.

    """
    label_path = path or find_label_path(table, column)
    if not label_path:
        return {}
    try:
        value_map = table.get_value_map(label_path)
    except Exception:
        return {}
    if not value_map:
        return {}

    names: dict[str, str] = {}
    for key, element in value_map.items():
        names[str(key)] = _element_name(key, element)
        try:
            names.setdefault(str(int(float(key))), names[str(key)])
        except (ValueError, TypeError):
            pass
    return names


def get_display_value_map(table: Any, path: str) -> dict[float, str] | None:
    """Read a value map as ``{float key: display name}`` for UI purposes.

    Unlike the label helpers above (which canonicalize on ``internal_name``,
    matching core's ``get_simple_value_map``), this prefers ``display_name``
    — the shape used when presenting categorical columns to users.

    Args:
        table: A loaded ``tlc.Table``.
        path: Value path (column name or dot-path).

    Returns:
        The display map, or ``None`` if the path has no value map.

    """
    try:
        value_map = table.get_value_map(path)
    except Exception:
        return None
    if not value_map:
        return None
    result: dict[float, str] = {}
    for key, element in value_map.items():
        name = _element_attr(element, "display_name") or _element_name(key, element)
        result[float(key)] = name
    return result or None


def find_label_column(table: Any) -> str | None:
    """Find a top-level categorical label column in a table, if any.

    A column qualifies if it carries a value map directly (categorical), or
    as a name-based fallback, contains ``label`` in its name.

    Args:
        table: A loaded ``tlc.Table``.

    Returns:
        The column name, or ``None``.

    """
    try:
        columns = [str(c) for c in table.columns]
    except Exception:
        return None
    for col in columns:
        try:
            if table.get_value_map(col):
                return col
        except Exception:
            continue
    for col in columns:
        if "label" in col.lower():
            return col
    return None


def _element_name(key: Any, element: Any) -> str:
    """Canonical class name for a value-map element (internal_name first)."""
    return (
        _element_attr(element, "internal_name")
        or _element_attr(element, "display_name")
        or (str(element) if not isinstance(element, dict) else str(key))
    )


def _element_attr(element: Any, attr: str) -> str | None:
    """Read an attribute from a MapElement or a raw dict element."""
    if isinstance(element, dict):
        value = element.get(attr)
    else:
        value = getattr(element, attr, None)
    return str(value) if value else None
