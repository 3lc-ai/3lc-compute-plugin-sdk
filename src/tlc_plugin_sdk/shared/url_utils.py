# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""URL normalization utilities for 3LC object URLs.

Ensures file-path URLs are absolute before passing to the tlc SDK,
preventing the CWD from being prepended to relative-looking paths.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def normalize_url(url: str) -> str:
    """Normalize a 3LC URL for use with the tlc SDK.

    - If the URL is a protocol URL (e.g. api://, s3://, gs://), return as-is.
    - If the URL looks like a file path, ensure it's absolute.
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

    # File path — ensure absolute
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
