# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared utility for saving and copying model checkpoints to Run folders.

Handles both local filesystem and cloud storage (S3, GCS, Azure) via the
``tlc.Url`` abstraction. The compute service is assumed to have write access
to the Run folder location.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _is_local_url(url_str: str) -> bool:
    """Check if a URL points to the local filesystem (not cloud storage)."""
    if "://" not in url_str:
        return True
    scheme = url_str.split("://", 1)[0].lower()
    return scheme in ("file", "")


def _resolve_to_local_path(url_str: str) -> Path | None:
    """Resolve a 3LC URL to a local filesystem path, or None if cloud."""
    import tlc

    try:
        abs_url = tlc.Url(url_str).to_absolute()
        url_s = str(abs_url)
        if _is_local_url(url_s):
            # Strip file:// scheme if present
            path = url_s.replace("file://", "")
            return Path(path)
    except Exception:
        logger.debug("Could not resolve model URL via tlc.Url: %s", url_str)
    # Try direct path interpretation
    if _is_local_url(url_str):
        return Path(url_str)
    return None


def save_model_to_run(
    run_url: str,
    model_data: Any,
    filename: str = "best_model.pt",
    source_file: str | Path | None = None,
    on_status: Any = None,
) -> str:
    """Save a model checkpoint to a Run's ``model/`` subdirectory.

    Supports both local and cloud (S3/GCS/Azure) run folders. For cloud storage,
    the file is first written to a temp directory, then uploaded via ``tlc.Url``.

    Args:
        run_url: The 3LC Run URL (local path or cloud URL).
        model_data: PyTorch state_dict to save (ignored if ``source_file`` is set).
        filename: Name for the model file in the run folder.
        source_file: If set, copy this existing file instead of saving ``model_data``.
        on_status: Optional callback for status messages.

    Returns:
        The relative path to the saved model file (e.g. ``model/best.pt``).
        This is relative to the run folder so it survives run renames.

    Raises:
        RuntimeError: If the model could not be saved.

    """
    import tlc

    if on_status is None:
        on_status = lambda m: None  # noqa: E731

    relative_path = f"model/{filename}"

    local_path = _resolve_to_local_path(run_url)

    if local_path is not None:
        # ── Local filesystem ──
        model_dir = local_path / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        dest = model_dir / filename

        if source_file is not None:
            shutil.copy2(str(source_file), str(dest))
            on_status(f"Copied model to run: {dest}")
        else:
            import torch

            torch.save(model_data, str(dest))
            on_status(f"Saved model to run: {dest}")

        return relative_path

    # ── Cloud storage (S3/GCS/Azure) ──
    on_status("Run is on cloud storage, uploading model...")
    run_abs = tlc.Url(run_url).to_absolute()
    model_url = run_abs / "model" / filename

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / filename

        if source_file is not None:
            shutil.copy2(str(source_file), str(tmp_path))
        else:
            import torch

            torch.save(model_data, str(tmp_path))

        # Upload using tlc.Url write facilities
        try:
            model_url.write_file(str(tmp_path))
            on_status(f"Uploaded model to: {model_url}")
        except AttributeError:
            # Fallback: use the Url's native copy mechanism
            src_url = tlc.Url(str(tmp_path))
            src_url.copy_to(model_url)
            on_status(f"Copied model to cloud: {model_url}")

    return relative_path


def store_model_info_in_run(
    run: Any,
    model_name: str,
    model_path: str,
    source_url: str = "",
    on_status: Any = None,
) -> None:
    """Store model metadata in a Run's parameters.

    Args:
        run: The ``tlc.Run`` object.
        model_name: Model architecture name (e.g. ``yolov8n.pt``, ``resnet50``).
        model_path: Path/URL to the saved model checkpoint.
        source_url: Original pretrained model URL (if fine-tuning).
        on_status: Optional callback for status messages.

    """
    if on_status is None:
        on_status = lambda m: None  # noqa: E731

    try:
        model_info: dict[str, str] = {
            "model_name": model_name,
            "model_path": model_path,
        }
        if source_url:
            model_info["model_source"] = source_url
        run.set_parameters(model_info)
        on_status(f"Stored model info in Run: {model_name} → {model_path}")
    except Exception as e:
        on_status(f"Warning: could not store model info in Run: {e}")
        logger.warning("Failed to store model info in Run: %s", e)
