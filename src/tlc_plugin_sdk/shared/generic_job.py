# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared helper for translating training progress into the generic progress schema.

Used by the YOLO and timm plugins inside ``run_job``: they compute an
epoch/batch progress dict and call :func:`epoch_progress` to render the generic
``{percent, label, timing}`` shape the frontend understands, which they then push
through their own ``ctx.emit`` channel. (The host owns job *listing* and *cancel*;
this module only shapes the progress payload.)
"""

from __future__ import annotations

from typing import Any


def epoch_progress(
    progress: dict[str, Any],
    *,
    phase_key: str = "phase",
    epoch_key: str = "epoch",
    total_key: str = "total_epochs",
    batch_frac_key: str = "batch_frac",
    step_label: str = "epoch",
) -> dict[str, Any] | None:
    """Build progress dict from epoch/batch-based training progress.

    Common to YOLO and timm plugins.
    """
    if not progress:
        return None

    epoch = progress.get(epoch_key, 0)
    total = progress.get(total_key, 0)
    if total <= 0:
        return None

    batch_frac = progress.get(batch_frac_key, 0)
    percent = int(((epoch - 1 + batch_frac) / total) * 100) if total > 0 else 0
    percent = max(0, min(100, percent))

    phase = progress.get(phase_key, "")
    label = f"Epoch {epoch}/{total}" if total > 0 else ""
    if phase:
        label = f"{label} · {phase}" if label else phase

    result: dict[str, Any] = {"percent": percent, "label": label}

    timing = progress.get("timing")
    if timing:
        result["timing"] = {
            "elapsed_s": timing.get("elapsed_s"),
            "eta_s": timing.get("eta_s"),
            "avg_step_s": timing.get("avg_epoch_s", timing.get("avg_step_s")),
            "step_label": step_label,
        }

    return result
