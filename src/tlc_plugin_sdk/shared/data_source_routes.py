# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Reusable Litestar route handlers for data-source input widgets.

Plugins that use the shared ``data_source`` UI component (see
:mod:`~tlc_plugin_sdk.shared.data_source_ui`) should include these handlers in
their ``get_route_handlers()`` list so the frontend widget can browse the compute
node's filesystem and receive file uploads.

Usage in a plugin's ``routes.py``::

    from tlc_plugin_sdk.shared.data_source_routes import data_source_route_handlers

    def get_route_handlers():
        handlers = [my_handler_1, my_handler_2, ...]
        handlers.extend(data_source_route_handlers())
        return handlers

The routes are:

``GET /browse?path=<dir>&glob=<pattern>&show_hidden=false``
    List files and directories visible on the compute node.

``POST /upload-temp``  (multipart ``data`` field)
    Upload a file from the browser to a temp directory on the compute node and
    return the server-side path.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Annotated, Any

from litestar import get, post
from litestar.datastructures import UploadFile
from litestar.enums import RequestEncodingType
from litestar.params import Body

if TYPE_CHECKING:
    from litestar import Request
    from litestar.handlers import BaseRouteHandler

logger = logging.getLogger(__name__)


def data_source_route_handlers() -> list[BaseRouteHandler]:
    """Return browse + upload-temp route handlers for the data-source widget."""

    @get("/browse", sync_to_thread=True)
    def browse_filesystem(request: Request[Any, Any, Any]) -> dict[str, Any]:
        """List files and directories at a compute-node path.

        Query params:
            path: Directory to list (default ``~``).
            glob: Optional glob pattern to filter files (e.g. ``*.yaml``).
            show_hidden: Whether to include dotfiles (default ``false``).
        """
        import fnmatch

        raw_path = request.query_params.get("path", "~")
        glob_pattern = request.query_params.get("glob", "")
        show_hidden = request.query_params.get("show_hidden", "false").lower() == "true"

        expanded = os.path.expanduser(raw_path)
        if not os.path.isabs(expanded):
            return {"error": f"Path must be absolute (got {expanded!r})."}
        if not os.path.exists(expanded):
            return {"error": f"Path does not exist: {expanded}"}
        if not os.path.isdir(expanded):
            return {"error": f"Not a directory: {expanded}"}

        entries: list[dict[str, Any]] = []
        try:
            for entry in sorted(os.scandir(expanded), key=lambda e: (not e.is_dir(), e.name.lower())):
                if not show_hidden and entry.name.startswith("."):
                    continue
                if glob_pattern and not entry.is_dir() and not fnmatch.fnmatch(entry.name, glob_pattern):
                    continue
                try:
                    stat = entry.stat()
                    entries.append({
                        "name": entry.name,
                        "type": "dir" if entry.is_dir() else "file",
                        "size": stat.st_size if entry.is_file() else None,
                    })
                except OSError:
                    continue
        except PermissionError:
            return {"error": f"Permission denied: {expanded}"}

        return {
            "path": expanded,
            "parent": os.path.dirname(expanded) if expanded != "/" else None,
            "entries": entries,
        }

    @post("/upload-temp", status_code=200)
    async def upload_temp(
        data: Annotated[UploadFile, Body(media_type=RequestEncodingType.MULTI_PART)],
    ) -> dict[str, Any]:
        """Upload a file to a temp directory and return the server-side path.

        Used by data-source widgets that need to transfer a file from the
        browser to the compute node's filesystem.
        """
        import tempfile
        from pathlib import Path

        file_bytes = await data.read()
        filename = data.filename or "upload"

        tmp_dir = Path(tempfile.gettempdir()) / "tlc-uploads"
        tmp_dir.mkdir(exist_ok=True)
        dest = tmp_dir / filename
        dest.write_bytes(file_bytes)

        return {"path": str(dest), "filename": filename}

    return [browse_filesystem, upload_temp]
