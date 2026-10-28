# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared URL alias utilities for plugins.

Three concerns:

1. **Registration** — when creating a new table, register a persistent project
   alias so image paths use a portable ``<TOKEN>`` prefix.
2. **Placement** — a cloud project must not default to a machine-local source.
   Deliberate permanent transfers belong in Storage, before choosing the import source.
   The copy utility remains available to legacy callers; registration never copies data.
3. **Override** — when consuming an existing table, temporarily override an
   alias so ``<TOKEN>`` resolves to a fast local path (e.g. SSD) instead of
   the default (e.g. S3).  Overrides are session-scoped and never persisted.

Deprecated in SDK 0.5.5: all three move to 3lc 3.5 (``tlc.helpers.ProjectHelper.register_data_alias``,
``tlc.integration.compute``, ``tlc.url``), and SDK 0.6 keeps only :func:`is_remote_url` and
:func:`default_alias_token` here. Each function's warning names its replacement. The alias card's
run-body keys are read by :func:`tlc_plugin_sdk.shared.alias_ui.parse_alias_card`.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from tlc_plugin_sdk.shared._deprecation import moved_to_3lc

logger = logging.getLogger(__name__)


def is_remote_url(value: str) -> bool:
    """True for ``scheme://…`` values (a bucket or volume), False for local paths."""
    from tlc_plugin_sdk.shared.url_utils import is_url

    return is_url(value)


CopyProgress = Callable[[int, int, int, int], None]
"""``(files_done, files_total, bytes_done, bytes_total)`` — called after every file.

Deprecated in SDK 0.5.5 with :func:`copy_folder_to_url`, which SDK 0.6 removes.
"""


@moved_to_3lc("it has no replacement")
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

    # Each upload runs in a copy of the caller's context, so a job's or request's Connection
    # (``connections.current_credential()``) holds in the pool threads as it does here.
    context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for path, fut in [(p, pool.submit(context.copy().run, put, p)) for p in files]:
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


#: Depth of a table below its project folder: ``<project>/datasets/<dataset>/tables/<table>``. The alias
#: for data inside the project is written as the hop from a table up to it, so this is how far up.
_TABLE_DEPTH_BELOW_PROJECT = 4


@moved_to_3lc(
    "use tlc.helpers.ProjectHelper.register_data_alias (3lc 3.5) instead, which writes a folder inside "
    "the project as a $./ value itself"
)
def relative_alias_value(root_url: str | None, project_name: str, target: str) -> str | None:
    """The value to register for *target* when it lives inside the project — else ``None``.

    ``s3://b/projects/p/data/tok`` under project ``p`` at root ``s3://b/projects`` becomes
    ``../../../../data/tok``: the hop from a table to the data.

    **Why relative.** tlc writes a media URL as a path relative to the table whenever the two share more
    than one path segment, and it expands aliases to absolute *before* deciding — so an absolute alias to
    a folder inside the project never survives into the row. Rows come out as
    ``../../../../data/tok/x.jpg``, and a relative row cannot be redirected: an alias override on a GPU
    node has no token to bind to, so the images can never be staged there. Registered relatively, the
    alias matches the relativized string that tlc produces, the row keeps ``<TOKEN>/x.jpg``, and
    overriding the token on a node points it at the staged copy.

    It also travels better than an absolute alias: the whole project can move to another bucket or
    machine and the rows still resolve, because the hop is relative to the table (Paul's idea; measured
    every way round 2026-09-08).

    Returns ``None`` when the target is not inside the project — data on other storage keeps an absolute
    alias, which is what makes *that* case redirectable.
    """
    if not root_url or not project_name or not target:
        return None
    project_dir = f"{root_url.strip().rstrip('/')}/{project_name.strip()}"
    inside = target.strip().rstrip("/")
    if not inside.startswith(project_dir + "/"):
        return None
    return "../" * _TABLE_DEPTH_BELOW_PROJECT + inside[len(project_dir) + 1 :]


@moved_to_3lc(
    "use tlc.helpers.ProjectHelper.register_data_alias (3lc 3.5) instead, which refuses a local folder "
    "for a cloud project itself (tlc.url.AliasLocationError)"
)
def validate_alias_location(source: str, root_url: str | None = None) -> None:
    """Refuse a machine-local default alias in a cloud project before any writes.

    Validate the durable source, not a temporary execution path. When no root is
    supplied, use the same configured default as the table writer.
    """
    import tlc

    from tlc_plugin_sdk.job_context import JobFailed

    root = root_url or str(tlc.config.project_root_url)
    cloud_root = is_remote_url(root) and not root.strip().lower().startswith("file://")
    local_source = not is_remote_url(source) or source.strip().lower().startswith("file://")
    if cloud_root and source.strip() and local_source:
        msg = (
            "This project is in cloud storage, but its alias would point to a local disk. "
            "Choose a local project location, or upload the folder to the bucket first "
            "(for example with aws s3 sync) and select the cloud source."
        )
        raise JobFailed(msg)


@moved_to_3lc("use tlc.helpers.ProjectHelper.register_data_alias (3lc 3.5) instead")
def register_alias(
    project_name: str,
    image_folder: str,
    alias_token: str | None = None,
    *,
    remote_path: str | None = None,
    root_url: str | None = None,
) -> dict[str, Any]:
    """Register a project URL alias for an image folder.

    Args:
        project_name: The 3LC project that owns the alias.
        image_folder: Absolute path to the image root folder — where the rows
            being written point today, so the SDK can fold it into ``<TOKEN>``.
        alias_token: Override token name.  If *None*, one is derived from
            *project_name* via :func:`default_alias_token`.
        remote_path: Durable source location when the execution path differs.
            When given, the *persisted* project alias points here — every reader
            of the project resolves ``<TOKEN>`` to the copy — while this session
            keeps resolving to *image_folder* until the job ends, so paths encode
            correctly from the local files.
        root_url: The project root the table is written under (``TableWriter(root_url=…)``); the
            alias is persisted in that project's config. ``None`` = the plugin's default root.

    Returns:
        Dict with ``token`` and ``path`` that were registered (plus
        ``remote_path`` when one was used), or ``error`` on registration failure.

    Raises:
        JobFailed: A cloud project would save a machine-local alias.

    """
    import tlc

    token = alias_token or default_alias_token(project_name)
    # Expand ~ before persisting — an alias stored with a literal tilde would
    # poison every future table that resolves through it.
    path = os.path.expanduser(image_folder.strip())
    copied = bool(remote_path and remote_path.strip())
    persisted = remote_path.strip().rstrip("/") if copied and remote_path else path
    validate_alias_location(persisted, root_url)
    # Data inside the project is aliased relative to the table — see relative_alias_value for why an
    # absolute alias to that folder is silently dropped when the table is written.
    relative = relative_alias_value(root_url, project_name, persisted)
    if relative:
        persisted = relative
        if not copied:
            # The rows are written from this same folder, so the session alias has to be the relative
            # form too: it is the relativized string that tlc will try to match when it writes them.
            path = relative

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
            root_url=(root_url.strip().rstrip("/") or None) if root_url else None,
            force=persisted != path,  # re-pointing at the copy is the intent, not a conflict
        )
        # 2. Also register as a session alias so it is active for the current
        #    process when the SDK encodes image paths.
        tlc.url.register_url_alias(token=token, path=path, force=True)
        logger.info("Registered alias <%s> → %s for project %r", token, persisted, project_name)
        result: dict[str, Any] = {"token": token, "path": path, "primary_created": not existed}
        if persisted != path:
            result["remote_path"] = persisted
        result["persisted"] = persisted  # what other machines and GPU nodes read as the baseline
        return result
    except Exception:
        logger.exception("Failed to register alias <%s> → %s", token, persisted)
        return {"error": f"Failed to register alias <{token}> → {persisted}"}


def _folder(value: str | None) -> str:
    """A folder as compared and registered: trimmed, ``~`` expanded, no trailing slash."""
    text = os.path.expanduser(str(value or "").strip())
    return text.rstrip("/") or text


def _is_under(path: str, folder: str) -> bool:
    """True when *path* is *folder* or inside it (local paths and URLs alike)."""
    return path == folder or path.startswith(folder + "/")


def _session_alias_folder(read_folder: str, source_folder: str, alias_folder: str, token: str) -> str:
    """Where the session alias points so rows read from *read_folder* resolve under *alias_folder*.

    A row read from ``<read_folder>/b.jpg`` is written as ``<TOKEN>/<rel>/b.jpg``, *rel* being where
    the picked folder sits below the alias folder, and the persisted alias resolves that to
    ``<alias_folder>/<rel>/b.jpg``. That needs a session folder S with ``S/<rel> == read_folder``:
    the read folder itself when the alias is the picked folder, else the read folder minus *rel*
    (which, for data read where it was picked, is the alias folder itself).

    Raises:
        JobFailed: The read folder does not end in *rel*: a copy that did not keep the layout.

    """
    from tlc_plugin_sdk.job_context import JobFailed

    rel = source_folder[len(alias_folder) :].strip("/")
    if not rel:
        return read_folder
    if read_folder.endswith("/" + rel):
        return read_folder[: -len(rel) - 1]
    msg = (
        f"This run reads the data from {read_folder}, a copy of {source_folder}, but the alias <{token}> "
        f"points at {alias_folder}, a folder above it; the table's paths would not resolve to the source. "
        "Point the alias at the data folder itself, or read the data where it is for this run."
    )
    raise JobFailed(msg)


@moved_to_3lc("use tlc.integration.compute.register_source_alias (3lc 3.5) instead")
def register_source_alias(
    project_name: str,
    read_folder: str,
    *,
    source_folder: str | None = None,
    alias_folder: str | None = None,
    alias_token: str | None = None,
    root_url: str | None = None,
) -> dict[str, Any]:
    """Register the alias for a table written from a folder the person picked, before any row.

    Three folders take part, each defaulting to the one before it:

    - *read_folder*: where THIS run reads the data. The host may have rewritten the picked folder
      for one run (a copy staged on a GPU node, or a path the person named there).
    - *source_folder*: where the person picked the data.
    - *alias_folder*: the durable folder the alias points at — the picked folder, or a folder above
      it ("source root") when the table's paths should keep the subfolders between them.

    The persisted alias always points at *alias_folder*. The rows keep the subfolders from the alias
    folder down to the picked folder (``<TOKEN>/sub/a.jpg``), whether the data is read in place or
    from a copy that kept that layout. An alias folder that is not above the picked folder is a
    durable copy elsewhere: the rows are folded from the folder read and every reader resolves the
    token to the alias folder.

    Args:
        project_name: The project that owns the alias.
        read_folder: The folder this run reads.
        source_folder: The folder as picked (default: *read_folder*, the data is read in place).
        alias_folder: The folder the persisted alias points at (default: *source_folder*).
        alias_token: The token (default: derived from *project_name*).
        root_url: The project root the table is written under (``None`` = the default root).

    Returns:
        :func:`register_alias`'s result.

    Raises:
        JobFailed: The alias folder is above the picked one and the folder read (a copy) does not
            keep the layout below it, or a cloud project would save a machine-local alias.

    """
    token = alias_token or default_alias_token(project_name)
    read = _folder(read_folder)
    source = _folder(source_folder) or read
    durable = _folder(alias_folder) or source
    if _is_under(source, durable):
        session = _session_alias_folder(read, source, durable, token)
        remote = durable if session != durable else None
    else:
        session, remote = read, (durable if durable != read else None)
    return register_alias(
        project_name=project_name, image_folder=session, alias_token=token, remote_path=remote, root_url=root_url
    )


@moved_to_3lc(
    "use tlc.integration.compute.register_alias_from_card(ctx, read_folder, project_name=...) (3lc 3.5) instead, "
    "which takes the JobContext in place of its params"
)
def register_run_alias(
    params: dict[str, Any],
    read_folder: str,
    *,
    project_name: str,
    root_url: str | None = None,
    source_folder: str | None = None,
) -> dict[str, Any] | None:
    """:func:`register_source_alias` from a run body the alias card filled in.

    Reads ``alias_enabled`` (default on), ``alias_token``, ``alias_folder`` and
    ``alias_source_folder``, which the card sends as the folder its source field held when the run
    was submitted. The host never rewrites these keys, so they say where the data was picked even
    when the run reads a copy.

    Args:
        params: The run body.
        read_folder: The folder this run reads.
        project_name: The project that owns the alias.
        root_url: The project root the table is written under.
        source_folder: The folder as picked, when the plugin knows it better than the card.

    Returns:
        :func:`register_alias`'s result, or ``None`` when the person turned the alias off.

    Raises:
        JobFailed: As :func:`register_source_alias`.

    """
    from tlc_plugin_sdk.shared.alias_ui import parse_alias_card

    card = parse_alias_card(params)
    if not card.enabled:
        return None
    return register_source_alias(
        project_name,
        read_folder,
        source_folder=source_folder or card.source_folder,
        alias_folder=card.alias_folder,
        alias_token=card.token,
        root_url=root_url,
    )


# ---------------------------------------------------------------------------
# Alias override (for plugins that consume existing tables)
# ---------------------------------------------------------------------------

_ALIAS_TOKEN_RE = re.compile(r"<([A-Z][A-Z0-9_]*)>")


@moved_to_3lc("use tlc.url.get_table_url_aliases (3lc 3.5) instead")
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


@moved_to_3lc(
    "use tlc.url.override_url_aliases (3lc 3.5) instead, or in a job tlc.integration.compute.job_scope "
    "with ComputePlugin.applies_alias_overrides = True"
)
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


@moved_to_3lc(
    "use tlc.url.override_url_aliases (3lc 3.5) instead, or in a job tlc.integration.compute.job_scope "
    "with ComputePlugin.applies_alias_overrides = True"
)
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
