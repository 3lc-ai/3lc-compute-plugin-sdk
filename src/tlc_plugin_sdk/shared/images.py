# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for discovering and reading image data from 3LC tables.

Centralizes the image-column discovery and image-reading pattern used across
plugins. Raw paths are read from ``table.table_rows`` (the row view — paths as
stored, no sample-view decoding) and resolved with ``tlc.Url.to_absolute``
against the table URL, mirroring what ``Table.__getitem__`` does internally.
This makes relative, aliased (``<TOKEN>/...``), and cloud (S3/GCS/Azure) paths
all work — rather than being passed straight to ``PIL.Image.open``, which only
handles local filesystem paths.

Porting to tlc core
-------------------
This functionality belongs in the tlc SDK; this module is shaped so the port
is mechanical. Plugins import only from here, so the port touches exactly this
file. Intended core mapping:

- ``get_image_column(table, ...)`` → ``Table.resolve_image_column(name)``
  plus a ``Table.image_columns`` property (schema walk over
  ``STRING_ROLE_IMAGE_URL``).
- ``resolve_image_url(path, table_url)`` → already in core:
  ``Url(path).to_absolute(owner)`` (= ``Table.absolute_url_from_relative``).
- ``load_image(path, table_url)`` → ``ImageHelper.open_image(url)``
  (local file → PIL directly, else ``BytesIO(url.read_bytes())``; faithful
  mode by default — the RGB conversion is this app's policy and stays here).
- ``read_image_from_table(table, idx, col)`` → ``Table.read_image(idx, column)``.
- ``get_image_paths(table, col)`` → ``Table.get_image_urls(column)``.
- ``list_image_urls(folder)`` → needs a **public recursive listing API** in
  core first: ``UrlAdapterRegistry.list_dir`` is not exported from
  ``tlc.url``, and ``Url._list_dir`` is private, single-level, and drops the
  ``is_dir`` flag needed to recurse. Suggested core addition:
  ``Url.list_dir()`` / ``Url.walk()`` returning ``UrlAdapterDirEntry``.

Once those land, each function body here becomes a one-line delegation and
plugin code is untouched.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import tlc
    from PIL.Image import Image

logger = logging.getLogger(__name__)

# Common image-column names, tried as a fallback when schema-role detection
# does not identify an image column.
_IMAGE_COLUMN_CANDIDATES = ("image", "image_path", "file_name", "filename")

# File extensions treated as images when listing folders.
_IMAGE_FILE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")


def get_image_column(table: Any, override: str | None = None) -> str:
    """Discover the name of the image column in a table.

    Detection order:
        1. ``override`` if given (validated against the table's columns).
        2. Schema ``string_role == STRING_ROLE_IMAGE_URL`` (the canonical SDK
           signal).
        3. Common column-name candidates (``image``, ``image_path``, ...).

    Args:
        table: A loaded ``tlc.Table``.
        override: An explicit column name to use, if provided.

    Returns:
        The image column name.

    Raises:
        ValueError: If no image column can be found, listing the available
            columns so the caller can diagnose the table.

    """
    from tlc.constants import STRING_ROLE_IMAGE_URL

    columns = _table_columns(table)

    if override:
        if override in columns:
            return override
        msg = f"Image column '{override}' not found in table. Available columns: {columns}"
        raise ValueError(msg)

    # Schema-role based detection.
    try:
        for name, col_schema in table.rows_schema.values.items():
            value_obj = getattr(col_schema, "value", None)
            if getattr(value_obj, "string_role", None) == STRING_ROLE_IMAGE_URL:
                return str(name)
    except Exception:
        pass

    # Name-candidate fallback (case-insensitive).
    lower_to_actual = {c.lower(): c for c in columns}
    for candidate in _IMAGE_COLUMN_CANDIDATES:
        if candidate in lower_to_actual:
            return lower_to_actual[candidate]

    msg = (
        "Could not find an image column in the table. "
        f"Available columns: {columns}. "
        f"Expected a column with an image URL role, or one named one of {list(_IMAGE_COLUMN_CANDIDATES)}."
    )
    raise ValueError(msg)


def resolve_image_url(img_path: str, table_url: Any = None) -> tlc.Url:
    """Resolve an image path (possibly relative or aliased) against a table URL.

    Uses ``tlc.Url.to_absolute`` — the same resolution ``Table.__getitem__``
    applies to URL columns — so absolute paths pass through unchanged, alias
    paths (``<TOKEN>/...``) are expanded, and relative paths are resolved
    against the table URL.

    One deliberate deviation from core: aliases are expanded **strictly**.
    Core's lenient ``to_absolute`` keeps an unregistered alias in the path and
    joins it onto the owner (``.../tables/t/<TOKEN>/img.jpg``), which turns
    "alias not registered" into a confusing FileNotFoundError — or silently-NaN
    metrics — far from the cause. Raising here fails jobs fast with the actual
    problem. (Whether core should do the same is tracked as a core ask in the
    deployment doc roadmap.)

    Args:
        img_path: The image path stored in the table.
        table_url: URL of the table the image belongs to (``tlc.Url`` or
            string; used as the owner for relative paths). May be omitted for
            absolute/alias paths.

    Returns:
        An absolute ``tlc.Url`` readable through 3LC's URL adapters.

    Raises:
        ValueError: If ``img_path`` contains an alias that is not registered.

    """
    import tlc

    owner = tlc.Url(str(table_url)) if table_url else None
    return tlc.Url(img_path).expand_aliases(allow_unexpanded=False).to_absolute(owner)


def load_image(img_path: str, table_url: Any = None) -> Image:
    """Load an image from a stored path as an RGB PIL image.

    Resolves the path via :func:`resolve_image_url`, then opens local files
    directly with PIL and everything else through the URL adapters
    (``BytesIO(url.read_bytes())``).

    Args:
        img_path: The image path stored in the table.
        table_url: URL of the table the image belongs to (base for relative
            paths). May be omitted for absolute/alias paths.

    Returns:
        The decoded image, converted to RGB.

    """
    import io

    from PIL import Image as PILImage
    from tlc.url import Scheme

    url = resolve_image_url(str(img_path), table_url)
    if url.scheme == Scheme.FILE:
        image = PILImage.open(url.to_str())
    else:
        image = PILImage.open(io.BytesIO(url.read_bytes()))
    return image.convert("RGB")


def read_image_size(img_path: str, table_url: Any = None) -> tuple[int, int]:
    """Return a stored image's ``(width, height)`` without decoding its pixels.

    Resolves the path the same way as :func:`load_image` (through the URL
    adapters, so any storage backend works), but reads only the image header
    rather than the full image — much cheaper when only the dimensions are
    needed (e.g. populating ``image_width``/``image_height`` for empty
    annotations on a table built from images alone).

    Args:
        img_path: The image path stored in the table.
        table_url: URL of the table the image belongs to (base for relative
            paths). May be omitted for absolute/alias paths.

    Returns:
        The image ``(width, height)`` in pixels.

    """
    import io

    from PIL import Image as PILImage
    from tlc.url import Scheme

    url = resolve_image_url(str(img_path), table_url)
    if url.scheme == Scheme.FILE:
        with PILImage.open(url.to_str()) as image:
            return image.width, image.height
    with PILImage.open(io.BytesIO(url.read_bytes())) as image:
        return image.width, image.height


def read_image_from_table(table: Any, idx: int, image_column: str | None = None) -> Image:
    """Read a single image from a table row as an RGB PIL image.

    Reads the raw path from ``table.table_rows`` (``table[idx]`` would return
    a decoded image rather than the path) and opens it via :func:`load_image`.

    Args:
        table: A loaded ``tlc.Table``.
        idx: Row index.
        image_column: Image column name; discovered via :func:`get_image_column`
            if not provided.

    Returns:
        The decoded image, converted to RGB.

    Raises:
        IndexError: If ``idx`` is out of range.
        ValueError: If the row has no image path.

    """
    column = image_column or get_image_column(table)
    if idx < 0 or idx >= len(table):
        msg = f"Row index {idx} out of range for table with {len(table)} rows"
        raise IndexError(msg)
    img_path = table.table_rows[idx][column]
    if not img_path:
        msg = f"Row {idx} has no image path in column '{column}'"
        raise ValueError(msg)
    return load_image(str(img_path), table.url)


def get_image_paths(table: Any, image_column: str | None = None) -> list[str]:
    """Read all image paths from a table, resolved to absolute URLs.

    Iterates ``table.table_rows`` (raw row view) and absolutizes each path
    against the table URL, so the result is safe to read from anywhere or to
    write into a new table at a different location. Rows without a path yield
    an empty string. This is the single bulk-read entry point — if it ever
    becomes a bottleneck, optimize here rather than at call sites.

    Args:
        table: A loaded ``tlc.Table``.
        image_column: Image column name; discovered via :func:`get_image_column`
            if not provided (an explicit name is validated the same way).

    Returns:
        One absolute URL string per row, in row order.

    Raises:
        ValueError: If the image column cannot be found.

    """
    column = get_image_column(table, override=image_column)
    table_url = table.url
    paths: list[str] = []
    for row in table.table_rows:
        img_path = row[column]
        paths.append(resolve_image_url(str(img_path), table_url).to_str() if img_path else "")
    return paths


def list_image_urls(folder: Any, max_count: int = 10000) -> list[str]:
    """List image files under a folder, recursively, on any storage backend.

    Resolves the folder through ``tlc.Url`` (so aliased ``<TOKEN>/...`` and
    relative paths work) and walks it via the URL adapter registry, so local,
    S3/GCS/Azure, and any custom-adapter folders all list correctly — unlike
    ``pathlib``, which silently returns nothing for non-local paths.

    The full tree is walked before sorting and capping, so the result is
    deterministic (the lexicographically first ``max_count`` paths).

    Args:
        folder: Folder path or URL (``str`` or ``tlc.Url``).
        max_count: Maximum number of paths to return.

    Returns:
        Sorted list of image URLs/paths. Empty if the folder does not exist or
        exists but contains no images.

    Raises:
        ValueError: If ``folder`` contains an alias that is not registered.
        OSError: If the folder exists but cannot be listed — e.g. a cloud auth,
            region, or permission misconfiguration. (A merely non-existent
            folder returns an empty list, not an error.)

    """
    import tlc

    # No public listing API in tlc.url yet (Url._list_dir is private and
    # single-level) — see the porting notes in the module docstring.
    from tlcurl.url_adapters._registry import UrlAdapterRegistry

    # Expand aliases strictly before absolutizing: to_absolute() on an
    # unregistered alias would silently join the still-aliased path onto cwd
    # ("/cwd/<TOKEN>/images") — never what you want. Failing loudly gives the
    # caller a diagnosable "could not expand alias" error instead of an empty
    # listing.
    root = tlc.Url(str(folder)).expand_aliases(allow_unexpanded=False).to_absolute()
    found: list[str] = []
    stack = [root]
    is_root = True
    while stack:
        current = stack.pop()
        try:
            entries = list(UrlAdapterRegistry.list_dir(current))
        except FileNotFoundError:
            # The folder simply does not exist — a benign empty result, same as
            # the old Path.exists() guard. (Deeper in the walk this would be a
            # race: a subfolder vanished between listing and visiting it.)
            logger.debug("Folder does not exist: %s", current)
            is_root = False
            continue
        except Exception as exc:
            if is_root:
                # The top-level folder exists but could not be listed: a real
                # misconfiguration (cloud auth, bad region, denied access). Raise
                # it — returning [] here would read as a benign "no images found"
                # and send the caller hunting for the wrong problem.
                msg = f"Could not list folder {current}: {exc}"
                raise OSError(msg) from exc
            # Tolerate an unlistable subfolder mid-walk so a partial listing is
            # still useful, but log why it was skipped.
            logger.warning("Skipping unlistable subfolder %s: %s", current, exc)
            continue
        is_root = False
        for entry in entries:
            if entry.is_dir():
                stack.append(tlc.Url(entry.path))
            elif entry.name.lower().endswith(_IMAGE_FILE_EXTENSIONS):
                found.append(str(entry.path))
    found.sort()
    return found[:max_count]


def _table_columns(table: Any) -> list[str]:
    """Best-effort list of a table's column names, for detection and errors."""
    try:
        return list(table.columns)
    except Exception:
        try:
            return list(table.rows_schema.values.keys())
        except Exception:
            return []
