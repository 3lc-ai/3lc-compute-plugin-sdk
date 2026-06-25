# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared modality detection — single source of truth for all table/schema inspection.

Detects whether a table represents a detection, classification, segmentation,
pose, or OBB task by walking the schema tree. Used by insights, YOLO, SAM3,
and timm plugins.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical sample_type → modality mapping
# ---------------------------------------------------------------------------

_SAMPLE_TYPE_MAP: dict[str, str] = {
    "bounding_boxes": "detection",
    "bounding_box": "detection",
    "instance_segmentation_masks": "segmentation",
    "instance_segmentation": "segmentation",
    "semantic_segmentation": "segmentation",
    "segmentation_masks": "segmentation",
    "segmentation": "segmentation",
    "categorical_label": "classification",
    "classification": "classification",
    "pose": "pose",
    "keypoints": "pose",
    "obb": "obb",
    "oriented_bounding_boxes": "obb",
}


@dataclass
class ModalityInfo:
    """Result of modality detection."""

    modality: str = "unknown"
    """Detected modality: detection, segmentation, classification, pose, obb, or unknown."""

    gt_col: str | None = None
    """Ground-truth column name (e.g. ``bbs``, ``segmentations``, ``label``)."""

    pred_col: str | None = None
    """Prediction column name (e.g. ``bbs_predicted``, ``predicted``)."""

    class_names: dict[str, str] = field(default_factory=dict)
    """Class index → display name mapping."""

    image_column: str | None = None
    """Image column name (if detected — first one found)."""

    image_columns: list[str] = field(default_factory=list)
    """All image columns detected in the schema."""

    label_column: str | None = None
    """Label column name for classification tasks."""

    num_classes: int = 0
    """Number of classes detected from value maps."""

    classification_columns: list[str] = field(default_factory=list)
    """All columns identified as classification-type."""

    detection_columns: list[str] = field(default_factory=list)
    """All columns identified as detection-type."""

    segmentation_columns: list[str] = field(default_factory=list)
    """All columns identified as segmentation-type."""

    all_columns: list[str] = field(default_factory=list)
    """All column names in the schema."""


# ---------------------------------------------------------------------------
# Core detection from a schema object
# ---------------------------------------------------------------------------


def detect_modality_from_schema(schema: Any) -> ModalityInfo:
    """Walk a schema object and detect modality.

    This is the core detection function. It does not load any table or URL —
    callers pass in ``table.rows_schema`` directly.

    Detection priority:
    1. ``sample_type`` attribute (canonical SDK identifier)
    2. Nested schema keys (``bb_list``, ``rles``, ``instance_properties``)
    3. Column name conventions (``bbs``, ``segmentations``, ``masks``)
    4. Python class names (``BoundingBoxListValue``, ``CategoricalLabel``)
    5. ``value.type`` string, ``number_role``, ``description``
    6. Heuristic: image column + label map → classification
    7. JSON fallback (``schema.to_json()``) if Python introspection fails

    Args:
        schema: A 3LC schema object with a ``values`` dict of column schemas.

    Returns:
        ModalityInfo with all detected fields.

    """
    info = ModalityInfo()

    if not schema or not hasattr(schema, "values"):
        return info

    try:
        return _detect_from_python(schema, info)
    except Exception:
        logger.debug("Python schema introspection failed, trying JSON fallback", exc_info=True)
        try:
            return _detect_from_json(schema, info)
        except Exception:
            logger.debug("JSON fallback also failed", exc_info=True)
            return info


def _detect_from_python(schema: Any, info: ModalityInfo) -> ModalityInfo:
    """Detect modality using Python attribute access on the schema."""
    values = schema.values
    info.all_columns = sorted(values.keys())

    best_cls_col: str | None = None
    best_det_col: str | None = None
    best_seg_col: str | None = None

    for key, col_schema in values.items():
        # 1. Check column-level sample_type
        col_task = _sample_type_to_modality(_get_sample_type(col_schema))
        if col_task:
            _assign_column(info, col_task, key)
            if col_task == "classification" and best_cls_col is None:
                best_cls_col = key
            elif col_task == "detection" and best_det_col is None:
                best_det_col = key
            elif col_task == "segmentation" and best_seg_col is None:
                best_seg_col = key
            continue

        # 1b. Walk nested schemas for sample_type (e.g. instance_properties)
        if hasattr(col_schema, "values") and isinstance(col_schema.values, dict):
            nested_hit = False
            for _sk, sub_schema in col_schema.values.items():
                sub_task = _sample_type_to_modality(_get_sample_type(sub_schema))
                if sub_task:
                    _assign_column(info, sub_task, key)
                    if sub_task == "classification" and best_cls_col is None:
                        best_cls_col = key
                    elif sub_task == "detection" and best_det_col is None:
                        best_det_col = key
                    elif sub_task == "segmentation" and best_seg_col is None:
                        best_seg_col = key
                    nested_hit = True
                    break
            if nested_hit:
                continue

        # 2. Nested schema keys: bb_list, rles, instance_properties, oriented_bbs, keypoints
        if hasattr(col_schema, "values") and isinstance(col_schema.values, dict):
            nested_keys = col_schema.values
            if "rles" in nested_keys or "instance_properties" in nested_keys:
                info.segmentation_columns.append(key)
                if best_seg_col is None:
                    best_seg_col = key
                continue
            if "bb_list" in nested_keys:
                info.detection_columns.append(key)
                if best_det_col is None:
                    best_det_col = key
                continue
            # Check nested instances.values for pose/obb keys
            all_nested = set(nested_keys.keys())
            instances_sub = nested_keys.get("instances")
            if instances_sub and hasattr(instances_sub, "values") and isinstance(instances_sub.values, dict):
                all_nested.update(instances_sub.values.keys())
            for nk in all_nested:
                nkl = nk.lower()
                if "oriented_bb" in nkl or nkl == "obb":
                    info.modality = "obb"
                    info.gt_col = key
                    return info
                if "keypoint" in nkl or "vertices_2d" in nkl or "vertices_3d" in nkl:
                    info.modality = "pose"
                    info.gt_col = key
                    return info

        # 2b. display_name / internal_name hints
        display_name = (getattr(col_schema, "display_name", "") or "").lower()
        internal_name = (getattr(col_schema, "internal_name", "") or "").lower()
        for hint_name in (display_name, internal_name):
            if not hint_name:
                continue
            if hint_name == "obb" or "oriented_b" in hint_name:
                info.modality = "obb"
                info.gt_col = key
                return info
            if "keypoint" in hint_name or "pose" in hint_name:
                info.modality = "pose"
                info.gt_col = key
                return info

        # 3. Column name conventions
        col_name_mod = _column_name_to_modality(key)
        if col_name_mod == "segmentation":
            info.segmentation_columns.append(key)
            if best_seg_col is None:
                best_seg_col = key
            continue
        if col_name_mod == "detection":
            info.detection_columns.append(key)
            if best_det_col is None:
                best_det_col = key
            continue
        if col_name_mod in ("pose", "obb"):
            info.modality = col_name_mod
            info.gt_col = key
            return info

        if not hasattr(col_schema, "value"):
            # Check description for bounding box hints
            desc = getattr(col_schema, "description", "") or ""
            if "bounding" in desc.lower():
                info.detection_columns.append(key)
                if best_det_col is None:
                    best_det_col = key
            continue

        value_obj = col_schema.value
        type_name = type(value_obj).__name__.lower()

        # 4. Python class names
        if "boundingbox" in type_name or "bounding_box" in type_name:
            info.detection_columns.append(key)
            if best_det_col is None:
                best_det_col = key
            continue
        if "segmentation" in type_name or "mask" in type_name:
            info.segmentation_columns.append(key)
            if best_seg_col is None:
                best_seg_col = key
            continue
        if "pose" in type_name or "keypoint" in type_name:
            # pose/obb don't go into column lists — just set modality directly
            info.modality = "pose"
            info.gt_col = key
            return info

        # 5a. value.type string
        if hasattr(value_obj, "type"):
            vtype = str(value_obj.type).lower()
            if "bounding_box" in vtype or "boundingbox" in vtype:
                info.detection_columns.append(key)
                if best_det_col is None:
                    best_det_col = key
                continue
            if "segmentation" in vtype or "mask" in vtype:
                info.segmentation_columns.append(key)
                if best_seg_col is None:
                    best_seg_col = key
                continue
            if "pose" in vtype or "keypoint" in vtype:
                info.modality = "pose"
                info.gt_col = key
                return info

        # 5b. sample_type on col_schema (not value) for classification
        sample_type = getattr(col_schema, "sample_type", "") or ""
        if "categorical" in str(sample_type).lower():
            info.classification_columns.append(key)
            if best_cls_col is None:
                best_cls_col = key
            continue

        # 5c. number_role == "label"
        number_role = getattr(value_obj, "number_role", "") or ""
        if "label" in str(number_role).lower():
            info.classification_columns.append(key)
            if best_cls_col is None:
                best_cls_col = key
            continue

        # 5d. value.map dict present with entries (classification)
        value_map = getattr(value_obj, "map", None)
        if isinstance(value_map, dict) and len(value_map) > 0:
            info.classification_columns.append(key)
            if best_cls_col is None:
                best_cls_col = key
            continue

        # 5e. Description containing "bounding"
        desc = getattr(col_schema, "description", "") or ""
        if "bounding" in desc.lower():
            info.detection_columns.append(key)
            if best_det_col is None:
                best_det_col = key
            continue

        # Track image column
        if hasattr(value_obj, "string_role"):
            role = str(value_obj.string_role).lower()
            if "image" in role:
                info.image_columns.append(key)
                if info.image_column is None:
                    info.image_column = key

    # Priority: segmentation > detection > classification
    if info.segmentation_columns:
        info.modality = "segmentation"
        info.gt_col = best_seg_col
    elif info.detection_columns:
        info.modality = "detection"
        info.gt_col = best_det_col
    elif info.classification_columns:
        info.modality = "classification"
        info.gt_col = best_cls_col
        info.label_column = best_cls_col
    # Assign pred_col by convention
    if info.gt_col:
        pred_candidate = info.gt_col + "_predicted"
        if pred_candidate in values:
            info.pred_col = pred_candidate
        elif "predicted" in values:
            info.pred_col = "predicted"
        elif info.gt_col + "Predicted" in values:
            info.pred_col = info.gt_col + "Predicted"

    return info


def _detect_from_json(schema: Any, info: ModalityInfo) -> ModalityInfo:
    """Fallback: detect modality by parsing schema.to_json()."""
    schema_json = json.loads(schema.to_json())
    vals = schema_json.get("values", {})
    info.all_columns = sorted(vals.keys())

    for key, col_def in vals.items():
        key_lower = key.lower()
        nested_vals = col_def.get("values", {})

        # Segmentation
        if isinstance(nested_vals, dict) and ("rles" in nested_vals or "instance_properties" in nested_vals):
            info.segmentation_columns.append(key)
            if info.gt_col is None:
                info.gt_col = key
            continue
        if key_lower in ("segmentations", "segmentation", "masks", "segmentations_predicted"):
            info.segmentation_columns.append(key)
            if info.gt_col is None:
                info.gt_col = key
            continue

        # Detection
        if isinstance(nested_vals, dict) and "bb_list" in nested_vals:
            info.detection_columns.append(key)
            if info.gt_col is None:
                info.gt_col = key
            continue
        if key_lower in ("bbs", "bounding_boxes"):
            info.detection_columns.append(key)
            if info.gt_col is None:
                info.gt_col = key
            continue

        # Classification
        if col_def.get("sample_type", "") == "categorical_label":
            info.classification_columns.append(key)
            if info.gt_col is None:
                info.gt_col = key
            continue
        val_def = col_def.get("value", {})
        if isinstance(val_def, dict):
            if val_def.get("number_role", "") == "label":
                info.classification_columns.append(key)
                if info.gt_col is None:
                    info.gt_col = key
                continue
            if isinstance(val_def.get("map"), dict) and val_def["map"]:
                info.classification_columns.append(key)
                if info.gt_col is None:
                    info.gt_col = key
                continue

    if info.segmentation_columns:
        info.modality = "segmentation"
    elif info.detection_columns:
        info.modality = "detection"
    elif info.classification_columns:
        info.modality = "classification"

    return info


# ---------------------------------------------------------------------------
# Higher-level entry points
# ---------------------------------------------------------------------------


def detect_modality_from_table(table: Any) -> ModalityInfo:
    """Detect modality from a loaded table object.

    Args:
        table: A loaded ``tlc.Table`` object.

    Returns:
        ModalityInfo with modality, columns, and class names extracted.

    """
    info = detect_modality_from_schema(table.rows_schema)

    # Extract class names from value map
    if info.gt_col:
        info.class_names = _extract_class_names(table, info.gt_col)
        if info.class_names:
            info.num_classes = len(info.class_names)

    return info


def detect_modality_from_url(url: str) -> ModalityInfo:
    """Load a table from URL and detect its modality.

    Convenience wrapper that loads the table first, then delegates to
    ``detect_modality_from_table``.

    Args:
        url: 3LC table URL (will be normalized).

    Returns:
        ModalityInfo with full detection results.

    """
    import tlc

    from tlc_plugin_sdk.shared.url_utils import normalize_url

    table = tlc.Table.from_url(normalize_url(url))
    return detect_modality_from_table(table)


def classify_metrics_columns(columns: set[str]) -> str:
    """Classify a metrics table by its column set.

    Used by run statistics to quickly determine the modality of a metrics
    table before doing full schema inspection.

    Args:
        columns: Set of column names from the table.

    Returns:
        One of: classification, detection_aggregate, detection_sample,
        segmentation_sample, or unknown.

    """
    if "example_id" in columns and "predicted" in columns:
        return "classification"
    if "label" in columns and ("mAP" in columns or "mAP50-95" in columns):
        return "detection_aggregate"
    if "segmentations_predicted" in columns:
        return "segmentation_sample"
    if "bbs_predicted" in columns:
        return "detection_sample"
    # Pose / keypoints
    for c in columns:
        cl = c.lower()
        if "keypoint" in cl or cl.startswith("pose") or "vertices_2d" in cl or "vertices_3d" in cl:
            return "pose"
        if cl in ("obb", "oriented_bounding_boxes") or cl.startswith("obb_"):
            return "obb"
    return "unknown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_sample_type(obj: Any) -> str:
    """Extract sample_type string from a schema object."""
    st = getattr(obj, "sample_type", None)
    if st and isinstance(st, str):
        return st.lower()
    return ""


def _sample_type_to_modality(sample_type: str) -> str:
    """Convert a sample_type string to a modality name, or empty string."""
    if not sample_type:
        return ""
    if sample_type in _SAMPLE_TYPE_MAP:
        return _SAMPLE_TYPE_MAP[sample_type]
    for key, modality in _SAMPLE_TYPE_MAP.items():
        if key in sample_type:
            return modality
    return ""


def _column_name_to_modality(col_name: str) -> str:
    """Detect modality from column name using conventions."""
    try:
        import tlc

        if col_name == getattr(tlc, "BOUNDING_BOXES", "bbs") or col_name == "bbs":
            return "detection"
        if col_name == getattr(tlc, "SEGMENTATIONS", "segmentations") or col_name == "segmentations":
            return "segmentation"
    except ImportError:
        pass

    cl = col_name.lower()
    if cl in ("bbs", "bounding_boxes", "bboxes"):
        return "detection"
    if cl in ("segmentations", "segmentation", "masks", "segmentations_predicted", "instance_segmentation"):
        return "segmentation"
    if "keypoint" in cl or cl.startswith("pose") or "vertices_2d" in cl or "vertices_3d" in cl:
        return "pose"
    if cl in ("obb", "oriented_bounding_boxes") or cl.startswith("obb_"):
        return "obb"
    return ""


def _assign_column(info: ModalityInfo, modality: str, col_name: str) -> None:
    """Add a column to the appropriate modality list in ModalityInfo."""
    if modality == "detection":
        info.detection_columns.append(col_name)
    elif modality == "segmentation":
        info.segmentation_columns.append(col_name)
    elif modality == "classification":
        info.classification_columns.append(col_name)
    elif modality == "pose":
        info.modality = "pose"
        info.gt_col = col_name
    elif modality == "obb":
        info.modality = "obb"
        info.gt_col = col_name


def _extract_class_names(table: Any, column_name: str) -> dict[str, str]:
    """Extract class names from a column's value map.

    Tries the column directly, then nested instance-label paths (centralized
    in ``tlc_plugin_sdk.shared.labels``).
    """
    from tlc_plugin_sdk.shared.labels import get_label_map

    return {str(index): name for index, name in get_label_map(table, column_name).items()}
