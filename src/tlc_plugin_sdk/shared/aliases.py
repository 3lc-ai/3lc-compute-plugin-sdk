# =============================================================================
# <copyright>
# Copyright (c) 2026 3LC Inc. All rights reserved.
#
# All rights are reserved. Reproduction or transmission in whole or in part, in
# any form or by any means, electronic, mechanical or otherwise, is prohibited
# without the prior written permission of the copyright owner.
# </copyright>
# =============================================================================
"""Shared URL alias utilities for plugins.

Two concerns:

1. **Registration** — when creating a new table, register a persistent project
   alias so image paths use a portable ``<TOKEN>`` prefix.
2. **Override** — when consuming an existing table, temporarily override an
   alias so ``<TOKEN>`` resolves to a fast local path (e.g. SSD) instead of
   the default (e.g. S3).  Overrides are session-scoped and never persisted.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)


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
) -> dict[str, Any]:
    """Register a project URL alias for an image folder.

    Args:
        project_name: The 3LC project that owns the alias.
        image_folder: Absolute path to the image root folder.
        alias_token: Override token name.  If *None*, one is derived from
            *project_name* via :func:`default_alias_token`.

    Returns:
        Dict with ``token`` and ``path`` that were registered, or ``error``
        on failure.

    """
    import tlc

    token = alias_token or default_alias_token(project_name)
    path = image_folder.strip()

    try:
        # Track whether a session alias for this token already existed, so the
        # caller (e.g. the importer) knows whether it created one and should
        # clean it up afterwards. 3.x dropped the PRIMARY/SECONDARY precedence
        # model — aliases are now a single flat namespace — so we compare against
        # the public alias snapshot instead of the old private precedence dict.
        existed = f"<{token}>" in tlc.url.get_registered_url_aliases()

        # 1. Persist the alias in the project config.
        tlc.helpers.ProjectHelper.register_project_url_alias(
            token=token,
            path=path,
            project_name=project_name,
        )
        # 2. Also register as a session alias so it is active for the current
        #    process when the SDK encodes image paths.
        tlc.url.register_url_alias(token=token, path=path, force=True)
        logger.info("Registered alias <%s> → %s for project %r", token, path, project_name)
        return {"token": token, "path": path, "primary_created": not existed}
    except Exception:
        logger.exception("Failed to register alias <%s> → %s", token, path)
        return {"error": f"Failed to register alias <{token}> → {path}"}


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

    # Check a sample row from URL columns. `_url_columns` is a private 3lc attribute that
    # is not part of the typed public API and may be absent depending on the 3lc version,
    # so reach it defensively via getattr. It can be [['image']] (nested) or ['image'] (flat).
    url_col_names: list[str] = []
    try:
        raw_cols = list(getattr(table, "_url_columns", []))
        for entry in raw_cols:
            if isinstance(entry, list):
                url_col_names.extend(entry)
            else:
                url_col_names.append(str(entry))
    except Exception:
        logger.debug("Could not extract URL column names from schema", exc_info=True)

    if url_col_names and len(table) > 0:
        try:
            row = table[0]
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
