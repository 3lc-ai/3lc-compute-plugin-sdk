# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""URL normalization utilities for 3LC object URLs.

Ensures file-path URLs are absolute before passing to the tlc SDK,
preventing the CWD from being prepended to relative-looking paths.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Collection
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def normalize_url(url: str) -> str:
    """Normalize a 3LC URL for use with the tlc SDK.

    - If the URL is a protocol URL (e.g. api://, s3://, gs://), return as-is.
    - If the URL looks like a file path, expand ``~`` and ensure it's absolute.
    - Handles URL-decoded paths that may have lost their leading slash.
    """
    if not url:
        return url

    # Protocol URLs — pass through
    if "://" in url:
        scheme = url.split("://", 1)[0].lower()
        # File paths on macOS/Linux look like /Users/... not a protocol
        if scheme in ("api", "s3", "gs", "http", "https", "3lc"):
            return url

    # File path — expand tilde, then ensure absolute
    url = os.path.expanduser(url)
    if not os.path.isabs(url):
        # Common case: path like "Users/paul/..." that lost its leading /
        if url.startswith(("Users/", "home/")):
            return "/" + url
        # Project-relative URL (e.g. "tinycoco/runs/demo1") — resolve
        # against the 3LC project root directory.
        try:
            import tlc

            project_root = str(tlc.config.project_root_url).rstrip("/")
            candidate = os.path.join(project_root, url)
            if os.path.exists(candidate):
                return candidate
        except Exception:
            logger.debug("Could not resolve relative URL against project root: %s", url)
        # Fallback: return as-is and let the SDK try
        return url

    return url


def normalize_local_path(path: str) -> str:
    """Normalize a user-typed local filesystem path.

    Strips whitespace and expands ``~``/``~user``. Plugins run with the plugin
    venv as CWD, so a bare-relative path would silently resolve somewhere no
    user ever looks — reject it instead.

    Args:
        path: Raw path string as typed by the user.

    Returns:
        The expanded, absolute path.

    Raises:
        ValueError: If the path is empty or not absolute after expansion.

    """
    expanded = os.path.expanduser(path.strip())
    if not expanded:
        msg = "Path is empty."
        raise ValueError(msg)
    if not os.path.isabs(expanded):
        msg = (
            f"Path must be absolute (got {expanded!r}). "
            "Relative paths would resolve against the plugin's working directory, not yours."
        )
        raise ValueError(msg)
    return expanded


def get_url_column_names(table: Any) -> list[str]:
    """Return the names of a table's URL/path-valued columns.

    Reads the table's ``_url_columns`` attribute, which is a private 3lc
    attribute that is not part of the typed public API and may be absent
    depending on the 3lc version, so reach it defensively. It can be
    ``[['image']]`` (nested) or ``['image']`` (flat).

    Args:
        table: A ``tlc.Table``.

    Returns:
        Flat list of column names; empty if none could be determined.

    """
    names: list[str] = []
    try:
        for entry in list(getattr(table, "_url_columns", [])):
            if isinstance(entry, list):
                names.extend(str(col) for col in entry)
            else:
                names.append(str(entry))
    except Exception:
        logger.debug("Could not extract URL column names from table", exc_info=True)
    return names


# ---------------------------------------------------------------------------
# Paths or URLs — one vocabulary for "where the data is"
#
# Import and export plugins take folders and files from people who may point at this
# machine (``/data/coco``) or at a bucket (``s3://bucket/data/coco``): the shared
# data-source picker offers both. These helpers let a plugin treat the two alike. Local
# paths go through ``pathlib``; URLs go through ``tlc.Url`` and its adapters — the same
# transport and credentials that read and write tables, so no extra cloud SDKs.
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def is_url(value: str) -> bool:
    """True for ``scheme://…`` values (a bucket, volume or http location), False for local paths."""
    return bool(_URL_RE.match(value.strip()))


def normalize_path_or_url(value: str) -> str:
    """Normalize a user-typed location: a URL is trimmed, a local path goes through :func:`normalize_local_path`.

    Raises:
        ValueError: The value is empty, or a local path that is not absolute.

    """
    text = value.strip()
    if is_url(text):
        return text.rstrip("/") if text.count("/") > 2 else text  # keep ``s3://bucket`` intact
    return normalize_local_path(text)


def join_path_or_url(base: str, *parts: str) -> str:
    """Join child segments onto a folder path or URL."""
    if is_url(base):
        return "/".join([base.rstrip("/"), *(p.strip("/") for p in parts if p)])
    return str(Path(base, *parts))


def parent_of(value: str) -> str:
    """The folder containing *value* (for a URL: everything before the last segment)."""
    if is_url(value):
        trimmed = value.rstrip("/")
        scheme, _, rest = trimmed.partition("://")
        head, _, _ = rest.rpartition("/")
        return f"{scheme}://{head}" if head else trimmed
    return str(Path(value).parent)


def name_of(value: str) -> str:
    """The last segment of a path or URL (``images`` for ``s3://b/data/images/``)."""
    return value.rstrip("/").rpartition("/")[2] if is_url(value) else Path(value).name


def stem_of(value: str) -> str:
    """The last segment without its extension."""
    return Path(name_of(value)).stem


def suffix_of(value: str) -> str:
    """The extension of the last segment, lower-cased, with its dot (``.json``)."""
    return Path(name_of(value)).suffix.lower()


def is_absolute(value: str) -> bool:
    """True for a URL or an absolute local path."""
    return is_url(value) or Path(value).is_absolute()


def is_folder(value: str) -> bool:
    """True when *value* is a local directory or a URL prefix with content under it."""
    if not is_url(value):
        return Path(value).is_dir()
    import tlc
    from tlcurl.url_adapters._registry import UrlAdapterRegistry

    url = tlc.Url(value.rstrip("/") + "/")
    try:
        if UrlAdapterRegistry.is_dir(url):
            return True
    except Exception:
        logger.debug("is_dir failed for %s", value, exc_info=True)
    try:
        return next(iter(UrlAdapterRegistry.list_dir(url)), None) is not None
    except Exception:
        return False


def is_file(value: str) -> bool:
    """True when *value* is a local file or an object that exists at the URL."""
    if not is_url(value):
        return Path(value).is_file()
    import tlc

    try:
        return bool(tlc.Url(value).exists())
    except Exception:
        return False


def read_bytes(value: str) -> bytes:
    """Read a local file or a URL."""
    if not is_url(value):
        return Path(value).read_bytes()
    import tlc

    data: bytes = tlc.Url(value).read_bytes()
    return data


def read_text(value: str, *, encoding: str = "utf-8") -> str:
    """Read a local file or a URL as text."""
    return read_bytes(value).decode(encoding)


def list_folder(value: str) -> list[tuple[str, bool]]:
    """One level of a folder: ``(child path or URL, is_dir)`` pairs, sorted by name."""
    if not is_url(value):
        root = Path(value)
        return sorted(((str(p), p.is_dir()) for p in root.iterdir()), key=lambda t: t[0])
    import tlc
    from tlcurl.url_adapters._registry import UrlAdapterRegistry

    base = value.rstrip("/")
    out: list[tuple[str, bool]] = []
    for entry in UrlAdapterRegistry.list_dir(tlc.Url(base + "/")):
        flag = entry.is_dir
        is_dir = bool(flag()) if callable(flag) else bool(flag)
        name = str(entry.name).rstrip("/")
        if name:
            out.append((base + "/" + name, is_dir))
    return sorted(out, key=lambda t: t[0])


def iter_files(folder: str, *, extensions: Collection[str] | None = None, recursive: bool = True) -> list[str]:
    """Files under a folder path or URL, sorted; hidden files skipped, optionally filtered by extension.

    Args:
        folder: Local directory or URL prefix.
        extensions: Lower-case extensions with the dot (``{".jpg", ".png"}``); ``None`` for all files.
        recursive: Descend into subfolders.

    """
    wanted = {e.lower() for e in extensions} if extensions else None
    found: list[str] = []
    stack = [folder]
    while stack:
        current = stack.pop()
        for child, is_dir in list_folder(current):
            if name_of(child).startswith("."):
                continue
            if is_dir:
                if recursive:
                    stack.append(child)
            elif wanted is None or suffix_of(child) in wanted:
                found.append(child)
    return sorted(found)
