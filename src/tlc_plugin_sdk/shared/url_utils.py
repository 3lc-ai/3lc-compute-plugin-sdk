# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""URL normalization utilities for 3LC object URLs.

Ensures file-path URLs are absolute before passing to the tlc SDK,
preventing the CWD from being prepended to relative-looking paths.
"""

from __future__ import annotations

import logging
import os
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
