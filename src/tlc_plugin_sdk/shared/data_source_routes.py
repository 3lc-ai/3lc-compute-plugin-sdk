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

``GET /browse?path=<dir>&glob=<pattern>&show_hidden=false&purpose=input``
    List files and directories visible on the compute node, **confined to the
    allowed roots**. An empty or ``~`` path opens the first root. Directory entries
    carry an ``accessible`` flag so the UI can disable folders it cannot open. When
    ``purpose=output`` the response also carries a ``writable`` flag on each directory
    entry and for the listed directory itself, so an output picker can flag folders it
    cannot save into; ``purpose=input`` (the default) omits ``writable`` and skips the
    extra writability syscall.

``POST /upload-temp``  (multipart ``data`` field)
    Upload a file from the browser to a fresh private temp directory on the
    compute node and return the server-side path.

Confinement — these routes expose the compute node's filesystem to every
authenticated hub user, so what they can reach is an operator decision, not the
caller's:

- ``/browse`` only lists paths inside the roots named by ``TLC_DATA_SOURCE_ROOTS``
  (``os.pathsep``-separated directories; default: the service user's home). Every
  requested path is ``realpath``-resolved before the containment check, so ``..``
  segments and symlinks pointing outside a root are denied rather than followed.
- ``/upload-temp`` ignores directory components of the client-supplied filename
  (a ``../``-laden name cannot escape the upload dir) and rejects bodies larger
  than ``TLC_DATA_SOURCE_MAX_UPLOAD_MB`` (default ``512``).
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

#: Directories ``/browse`` may list, ``os.pathsep``-separated. Default: the user's home.
ROOTS_ENV = "TLC_DATA_SOURCE_ROOTS"

#: Upload size ceiling in MB for ``/upload-temp``. Default: :data:`DEFAULT_MAX_UPLOAD_MB`.
MAX_UPLOAD_MB_ENV = "TLC_DATA_SOURCE_MAX_UPLOAD_MB"

DEFAULT_MAX_UPLOAD_MB = 512


def allowed_browse_roots() -> list[str]:
    """Return the directories ``/browse`` may list, resolved and existing.

    Read from :data:`ROOTS_ENV` (``os.pathsep``-separated). Entries are
    tilde-expanded and ``realpath``-resolved; ones that are not existing
    directories are dropped. Falls back to the user's home directory when the
    variable is unset or nothing survives — a picker with zero roots is just a
    broken picker, and home is the least surprising default.

    Returns:
        Resolved root directories, first one being the default browse target.

    """
    raw = os.environ.get(ROOTS_ENV, "")
    candidates = [c for c in (part.strip() for part in raw.split(os.pathsep)) if c] or ["~"]
    roots: list[str] = []
    for candidate in candidates:
        resolved = os.path.realpath(os.path.expanduser(candidate))
        if os.path.isdir(resolved) and resolved not in roots:
            roots.append(resolved)
    return roots or [os.path.realpath(os.path.expanduser("~"))]


def _confine_to_roots(path: str, roots: list[str]) -> str | None:
    """Resolve ``path`` and return it when it lies inside one of ``roots``, else ``None``.

    ``realpath`` first, contain second: a ``..`` segment or a symlink that points
    outside a root resolves to its real target and is then denied, rather than the
    literal path passing a prefix test it does not deserve.
    """
    resolved = os.path.realpath(os.path.expanduser(path))
    for root in roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    return None


def _max_upload_mb() -> int:
    """The upload ceiling in MB — :data:`MAX_UPLOAD_MB_ENV`, else the default."""
    try:
        value = int(os.environ.get(MAX_UPLOAD_MB_ENV, ""))
    except ValueError:
        value = 0
    return value if value > 0 else DEFAULT_MAX_UPLOAD_MB


def _dir_accessible(path: str) -> bool:
    """Whether the directory at ``path`` can actually be opened for listing.

    Probes with ``os.scandir`` rather than ``os.access``: on macOS a TCC-protected
    package (e.g. ``Photos Library.photoslibrary``) passes an ``os.access`` read/exec
    check but still raises ``PermissionError`` at ``opendir`` time. ``os.scandir``
    opens the directory eagerly, so it surfaces that denial here — before the user
    clicks into a dead end.

    Args:
        path: Absolute path to a directory.

    Returns:
        ``True`` if the directory can be opened for listing, ``False`` otherwise.
    """
    try:
        with os.scandir(path):
            pass
        return True
    except OSError:
        return False


def _dir_writable(path: str) -> bool:
    """Best-effort check that a directory can be written to.

    Uses ``os.access(path, os.W_OK)``. This is advisory only: on macOS a
    TCC-protected or cloud-provider-backed directory can report writable yet
    still reject a write, and a ``True`` here never guarantees a later write
    succeeds — it is a hint for the output picker, not a contract.
    """
    try:
        return os.access(path, os.W_OK)
    except OSError:
        return False


def _safe_upload_name(raw: str | None) -> str:
    """Reduce a client-supplied filename to a bare, safe basename.

    Directory components are the client reaching for a *location*, which is not
    theirs to choose — strip them (both separator flavors, whichever OS serves)
    and refuse the dot names that survive ``basename``.
    """
    name = os.path.basename((raw or "").replace("\\", "/").strip())
    if name in ("", ".", ".."):
        return "upload"
    return name


def data_source_route_handlers() -> list[BaseRouteHandler]:
    """Return the browse, upload-temp and project-root route handlers for the shared widgets."""

    @get("/browse", sync_to_thread=True)
    def browse_filesystem(request: Request[Any, Any, Any]) -> dict[str, Any]:
        """List files and directories at a compute-node path inside the allowed roots.

        Query params:
            path: Directory to list. Empty or ``~`` opens the first allowed root.
            glob: Optional glob pattern(s) to filter files, comma-separated for more than
                one (e.g. ``*.yaml`` or ``*.yaml,*.yml`` — matches the widget's ``accept``
                config verbatim). A file is kept if it matches *any* pattern in the list.
            show_hidden: Whether to include dotfiles (default ``false``).
            purpose: ``input`` (default) or ``output``. In ``output`` mode the response
                additionally reports writability (see below); ``input`` mode skips that
                extra syscall.

        Each directory entry carries an ``accessible`` bool: ``False`` marks a folder
        that cannot be opened for listing (e.g. a macOS TCC-protected package), so the
        UI can render it disabled rather than as a navigable dead end. File entries
        do not carry this flag.

        When ``purpose=output``, each directory entry also carries a ``writable`` bool
        and the top-level payload carries a ``writable`` bool for the listed directory
        itself, so an output picker can flag folders it cannot save into. This flag is
        omitted entirely in ``input`` mode.
        """
        import fnmatch

        roots = allowed_browse_roots()
        raw_path = request.query_params.get("path", "").strip()
        # Comma-separated, matching the widget's "accept" config (e.g. "*.yaml,*.yml") —
        # a bare fnmatch against the joined string would only match a literal comma.
        glob_patterns = [g.strip() for g in request.query_params.get("glob", "").split(",") if g.strip()]
        show_hidden = request.query_params.get("show_hidden", "false").lower() == "true"
        purpose = request.query_params.get("purpose", "input")
        want_writable = purpose == "output"

        if not raw_path or raw_path == "~":
            expanded = roots[0]
        else:
            candidate = os.path.expanduser(raw_path)
            if not os.path.isabs(candidate):
                return {"error": f"Path must be absolute (got {candidate!r})."}
            confined = _confine_to_roots(candidate, roots)
            if confined is None:
                return {"error": f"Path is outside the allowed data-source roots: {candidate}"}
            expanded = confined

        if not os.path.exists(expanded):
            return {"error": f"Path does not exist: {expanded}"}
        if not os.path.isdir(expanded):
            return {"error": f"Not a directory: {expanded}"}

        active_root = next(r for r in roots if expanded == r or expanded.startswith(r + os.sep))

        entries: list[dict[str, Any]] = []
        try:
            for entry in sorted(os.scandir(expanded), key=lambda e: (not e.is_dir(), e.name.lower())):
                if not show_hidden and entry.name.startswith("."):
                    continue
                if (
                    glob_patterns
                    and not entry.is_dir()
                    and not any(fnmatch.fnmatch(entry.name, pattern) for pattern in glob_patterns)
                ):
                    continue
                try:
                    stat = entry.stat()
                    is_dir = entry.is_dir()
                    item: dict[str, Any] = {
                        "name": entry.name,
                        "type": "dir" if is_dir else "file",
                        "size": stat.st_size if entry.is_file() else None,
                    }
                    if is_dir:
                        # Probe openability now so the UI can disable folders it cannot
                        # descend into (e.g. macOS TCC-protected packages), instead of
                        # letting the user click into a "Permission denied" dead end.
                        item["accessible"] = _dir_accessible(entry.path)
                        # Output pickers also need to know where a save can land; input
                        # pickers skip the extra syscall entirely.
                        if want_writable:
                            item["writable"] = _dir_writable(entry.path)
                    entries.append(item)
                except OSError:
                    continue
        except PermissionError:
            return {"error": f"Permission denied: {expanded}"}

        result: dict[str, Any] = {
            "path": expanded,
            # Clamped at the containing root: "up" from a root is not a place this
            # widget can go, so the UI gets no parent to offer.
            "parent": os.path.dirname(expanded) if expanded != active_root else None,
            "root": active_root,
            "roots": roots,
            "entries": entries,
        }
        if want_writable:
            # Whether the listed directory itself can be saved into — lets the output
            # picker disable its "Select This Folder" button.
            result["writable"] = _dir_writable(expanded)
        return result

    @post("/upload-temp", status_code=200)
    async def upload_temp(
        data: Annotated[UploadFile, Body(media_type=RequestEncodingType.MULTI_PART)],
    ) -> dict[str, Any]:
        """Upload a file to a fresh temp directory and return the server-side path.

        Used by data-source widgets that need to transfer a file from the
        browser to the compute node's filesystem. Each upload lands in its own
        ``mkdtemp`` directory (mode 0700), so concurrent uploads of the same
        filename never clobber each other.
        """
        import tempfile
        from pathlib import Path

        max_mb = _max_upload_mb()
        file_bytes = await data.read()
        if len(file_bytes) > max_mb * 1024 * 1024:
            return {"error": f"Upload is larger than the {max_mb} MB limit; set {MAX_UPLOAD_MB_ENV} to raise it."}
        filename = _safe_upload_name(data.filename)

        tmp_base = Path(tempfile.gettempdir()) / "tlc-uploads"
        tmp_base.mkdir(exist_ok=True)
        dest = Path(tempfile.mkdtemp(dir=tmp_base)) / filename
        dest.write_bytes(file_bytes)

        return {"path": str(dest), "filename": filename}

    @get("/project-root", sync_to_thread=True)
    def project_root(request: Request[Any, Any, Any]) -> dict[str, str]:
        """Where this plugin's ``tlc`` writes tables — its effective project root.

        The shared alias widget asks this before offering to copy data next to a new table: the
        answer must come from the process that will write the table (a local controller writes to
        its local projects folder even when an infrastructure plugin has a bucket root for nodes).
        ``{"url": ""}`` when ``tlc`` cannot say.
        """
        try:
            import tlc

            return {"url": str(tlc.config.project_root_url).rstrip("/")}
        except Exception:
            return {"url": ""}

    return [browse_filesystem, upload_temp, project_root]
