# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared URL alias utilities for plugins.

Three concerns:

1. **Registration** — when creating a new table, register a persistent project
   alias so image paths use a portable ``<TOKEN>`` prefix.
2. **Placement** — when the table lands on other storage than the data (a project
   root on a bucket, images on a laptop), copy the data next to the table first
   (:func:`copy_folder_to_url`) and register the alias against the copy, so every
   reader of the table — GPU nodes, the Dashboard, this machine — resolves
   ``<TOKEN>`` to one place. The copy goes through ``tlc.Url``: whatever can write
   the table can write the data, with the same credentials.
3. **Override** — when consuming an existing table, temporarily override an
   alias so ``<TOKEN>`` resolves to a fast local path (e.g. SSD) instead of
   the default (e.g. S3).  Overrides are session-scoped and never persisted.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def is_remote_url(value: str) -> bool:
    """True for ``scheme://…`` values (a bucket or volume), False for local paths."""
    from tlc_plugin_sdk.shared.url_utils import is_url

    return is_url(value)


CopyProgress = Callable[[int, int, int, int], None]
"""``(files_done, files_total, bytes_done, bytes_total)`` — called after every file."""


def copy_folder_to_url(
    src_dir: str,
    dst_url: str,
    *,
    progress: CopyProgress | None = None,
    workers: int = 8,
) -> dict[str, Any]:
    """Copy a local folder, recursively, to a URL prefix through ``tlc.Url``.

    Files land at ``<dst_url>/<relative path>``. When the first file already exists
    at the destination the folder is treated as a repeat (the same data imported
    again) and files that exist are skipped; otherwise nothing is probed and every
    file is written, which keeps a first copy at one request per file.

    Args:
        src_dir: Local folder to copy.
        dst_url: Destination prefix, e.g. ``s3://bucket/projects/p/data/token``.
        progress: Optional callback, see :data:`CopyProgress`.
        workers: Parallel uploads.

    Returns:
        ``{"files": n, "bytes": b, "skipped": k, "url": dst_url}``.

    Raises:
        FileNotFoundError: *src_dir* is not a folder.
        RuntimeError: A file could not be written (the first failure, after the
            other uploads in flight have finished).

    """
    import tlc

    root = Path(os.path.expanduser(src_dir.strip()))
    if not root.is_dir():
        msg = f"Not a folder: {root}"
        raise FileNotFoundError(msg)
    base = dst_url.strip().rstrip("/")
    files = sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith("."))
    total_bytes = sum(p.stat().st_size for p in files)
    if not files:
        return {"files": 0, "bytes": 0, "skipped": 0, "url": base}

    def target(path: Path) -> Any:
        return tlc.Url(base + "/" + path.relative_to(root).as_posix())

    # One probe decides the mode for the whole folder: repeat copies skip what is there.
    skip_existing = bool(target(files[0]).exists())

    done_files = 0
    done_bytes = 0
    skipped = 0
    first_error: Exception | None = None

    def put(path: Path) -> tuple[int, bool]:
        size = path.stat().st_size
        url = target(path)
        if skip_existing and url.exists():
            return size, True
        url.write_bytes(path.read_bytes())
        return size, False

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for path, fut in [(p, pool.submit(put, p)) for p in files]:
            try:
                size, was_skipped = fut.result()
            except Exception as exc:  # surfaced once, below
                if first_error is None:
                    first_error = exc
                    logger.warning("Copy failed for %s → %s: %s", path, base, exc)
                continue
            done_files += 1
            done_bytes += size
            skipped += int(was_skipped)
            if progress is not None:
                progress(done_files, len(files), done_bytes, total_bytes)

    if first_error is not None:
        msg = f"Could not copy {root} to {base}: {first_error}"
        raise RuntimeError(msg) from first_error
    logger.info("Copied %s → %s (%d files, %d bytes, %d already there)", root, base, done_files, done_bytes, skipped)
    return {"files": done_files, "bytes": done_bytes, "skipped": skipped, "url": base}


def _sanitize_token(name: str) -> str:
    """Convert a project name to a valid alias token.

    Alias tokens must match ``[A-Z][A-Z0-9_]*``.  We upper-case the input,
    replace non-alphanumeric characters with underscores, collapse runs of
    underscores, and ensure it starts with a letter.
    """
    token = re.sub(r"[^A-Z0-9]", "_", name.upper())
    token = re.sub(r"_+", "_", token).strip("_")
    if not token or not token[0].isalpha():
        token = "PROJECT_" + token
    return token


def default_alias_token(project_name: str) -> str:
    """Generate a default alias token from a project name.

    Args:
        project_name: Human-readable project name (e.g. "My COCO Dataset").

    Returns:
        A valid alias token like ``MY_COCO_DATASET``.

    """
    return _sanitize_token(project_name)


def register_alias(
    project_name: str,
    image_folder: str,
    alias_token: str | None = None,
    *,
    remote_path: str | None = None,
) -> dict[str, Any]:
    """Register a project URL alias for an image folder.

    Args:
        project_name: The 3LC project that owns the alias.
        image_folder: Absolute path to the image root folder — where the rows
            being written point today, so the SDK can fold it into ``<TOKEN>``.
        alias_token: Override token name.  If *None*, one is derived from
            *project_name* via :func:`default_alias_token`.
        remote_path: Where the data was copied (see :func:`copy_folder_to_url`).
            When given, the *persisted* project alias points here — every reader
            of the project resolves ``<TOKEN>`` to the copy — while this session
            keeps resolving to *image_folder* until the job ends, so paths encode
            correctly from the local files.

    Returns:
        Dict with ``token`` and ``path`` that were registered (plus
        ``remote_path`` when one was used), or ``error`` on failure.

    """
    import tlc

    token = alias_token or default_alias_token(project_name)
    # Expand ~ before persisting — an alias stored with a literal tilde would
    # poison every future table that resolves through it.
    path = os.path.expanduser(image_folder.strip())
    persisted = remote_path.strip().rstrip("/") if remote_path and remote_path.strip() else path

    try:
        # Track whether a session alias for this token already existed, so the
        # caller knows whether it created one and should clean it up afterwards.
        # Aliases are a single flat namespace in 3.x, so the public alias
        # snapshot is the thing to compare against.
        existed = f"<{token}>" in tlc.url.get_registered_url_aliases()

        # 1. Persist the alias in the project config.
        tlc.helpers.ProjectHelper.register_project_url_alias(
            token=token,
            path=persisted,
            project_name=project_name,
            force=persisted != path,  # re-pointing at the copy is the intent, not a conflict
        )
        # 2. Also register as a session alias so it is active for the current
        #    process when the SDK encodes image paths.
        tlc.url.register_url_alias(token=token, path=path, force=True)
        logger.info("Registered alias <%s> → %s for project %r", token, persisted, project_name)
        result: dict[str, Any] = {"token": token, "path": path, "primary_created": not existed}
        if persisted != path:
            result["remote_path"] = persisted
        return result
    except Exception:
        logger.exception("Failed to register alias <%s> → %s", token, persisted)
        return {"error": f"Failed to register alias <{token}> → {persisted}"}


# ---------------------------------------------------------------------------
# Alias override (for plugins that consume existing tables)
# ---------------------------------------------------------------------------

_ALIAS_TOKEN_RE = re.compile(r"<([A-Z][A-Z0-9_]*)>")


def get_table_aliases(table_url: str) -> list[dict[str, str]]:
    """Discover which URL aliases a table uses.

    Loads the table, reads image-path columns from the first row, and
    returns every alias token that appears together with its current
    resolved path.

    Args:
        table_url: 3LC table URL.

    Returns:
        List of ``{"token": "MY_DATA", "current_path": "/data/images", "is_local": true}``.

    """
    import tlc

    table = tlc.Table.from_url(table_url)
    all_aliases = tlc.url.get_registered_url_aliases()  # {"<TOKEN>": "/path", ...}

    # Collect alias tokens referenced by the table
    found_tokens: set[str] = set()

    # Check input_url (creation source, often has alias)
    input_url = str(getattr(table, "input_url", "")) or ""
    for m in _ALIAS_TOKEN_RE.finditer(input_url):
        found_tokens.add(m.group(1))

    # Check a sample row from URL columns.
    from tlc_plugin_sdk.shared.url_utils import get_url_column_names

    url_col_names = get_url_column_names(table)

    if url_col_names and len(table) > 0:
        try:
            # STORED values, not the sample view: table[i] resolves aliases through
            # this machine's registry, so tokens would already be expanded away here.
            row = table.table_rows[0]
            for col in url_col_names:
                val = str(row.get(col, ""))
                for m in _ALIAS_TOKEN_RE.finditer(val):
                    found_tokens.add(m.group(1))
        except Exception:
            logger.debug("Could not scan first row for alias tokens", exc_info=True)

    # Also scan the table URL itself
    for m in _ALIAS_TOKEN_RE.finditer(str(table.url)):
        found_tokens.add(m.group(1))

    # Build result with current resolved paths
    result: list[dict[str, Any]] = []
    for token in sorted(found_tokens):
        key = f"<{token}>"
        path = all_aliases.get(key, "")
        if not path:
            # Try get_alias_path as fallback
            path = tlc.url.get_alias_path(token) or ""
        result.append({
            "token": token,
            "current_path": path,
            "is_local": bool(path) and os.path.isdir(path),
        })

    return result


def apply_alias_overrides(overrides: list[dict[str, str]]) -> list[dict[str, str]]:
    """Temporarily override alias paths for the current session.

    Uses ``tlc.url.register_url_alias`` (session-only, not persisted) so that
    ``<TOKEN>`` resolves to a different path during processing.

    Args:
        overrides: List of ``{"token": "TOKEN", "path": "/local/fast/path"}``.
            Entries with empty *path* are skipped.

    Returns:
        List of ``{"token": "TOKEN", "original_path": "/original/path"}``
        needed by :func:`restore_aliases` to undo the overrides.

    """
    import tlc

    originals: list[dict[str, str]] = []
    for entry in overrides:
        token = entry.get("token", "").strip()
        new_path = entry.get("path", "").strip()
        if not token or not new_path:
            continue

        # Save original path before overriding
        original = tlc.url.get_alias_path(token) or ""
        if new_path == original:
            continue  # No change needed

        try:
            tlc.url.register_url_alias(token=token, path=new_path, force=True)
            originals.append({"token": token, "original_path": original})
            logger.info("Override alias <%s>: %s → %s", token, original, new_path)
        except Exception:
            logger.exception("Failed to override alias <%s>", token)

    return originals


def restore_aliases(originals: list[dict[str, str]]) -> None:
    """Restore aliases to their original paths after an override.

    Args:
        originals: List returned by :func:`apply_alias_overrides`.

    """
    import tlc

    for entry in originals:
        token = entry.get("token", "")
        original_path = entry.get("original_path", "")
        try:
            if original_path:
                tlc.url.register_url_alias(token=token, path=original_path, force=True)
            else:
                tlc.url.unregister_url_alias(token=token)
            logger.info("Restored alias <%s> → %s", token, original_path or "(unregistered)")
        except Exception:
            logger.exception("Failed to restore alias <%s>", token)
