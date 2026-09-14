# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Folder downloads for storage plugins: bundle a prefix into one zip, in the background, with progress.

A browser cannot download a folder: it downloads files, one request each, and a dataset folder
is tens of thousands of them. What it can download is one archive. Every storage plugin (S3
buckets, RunPod volumes, anything with objects under a prefix) needs the same machinery for
that — list the objects, stream them into a zip, upload the zip next to the data, hand back a
link, report progress, allow a cancel — and only the three provider calls differ. This module is
the machinery; a plugin supplies the three calls:

    bundles = BundleRegistry(
        list_objects=lambda url: iter([("data/fire/a.jpg", 1234), ...]),   # (key, size) under url
        open_object=lambda key: s3.get_object(Bucket=b, Key=key)["Body"],   # a readable stream
        store_bundle=lambda local_path, name: upload_and_presign(...),      # -> download URL
    )
    bundle_id = bundles.start(url="s3://bucket/data/fire", name="fire")
    bundles.status(bundle_id)   # {state, files_done, files_total, bytes_done, bytes_total, download_url, error}
    bundles.cancel(bundle_id)

The zip is written with ``ZIP_STORED``: images and model files are already compressed, and
deflating them costs CPU for nothing. It is spooled to a temporary file on disk (a bundle can
be many GB; memory is not the place), uploaded by the plugin's ``store_bundle``, then deleted.
Finished bundles stay in the registry for :data:`BUNDLE_TTL_S` so a page can still read the
link; anything older is forgotten on the next call.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BUNDLE_TTL_S = 6 * 3600.0
# The cap keeps one careless click from filling the plugin host's disk; a dataset larger than
# this is copied with the CLI, which the status text says.
MAX_BUNDLE_BYTES = 50 * 1024**3
MAX_BUNDLE_FILES = 500_000
_CHUNK = 8 * 1024 * 1024


@dataclass
class Bundle:
    """One folder-to-zip job."""

    id: str
    url: str
    name: str
    state: str = "listing"  # listing | running | uploading | done | failed | cancelled
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    download_url: str = ""
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        """The status payload a route returns."""
        percent = 0.0
        if self.state in ("done",):
            percent = 100.0
        elif self.bytes_total > 0:
            percent = min(99.0, 100.0 * self.bytes_done / self.bytes_total)
        return {
            "bundle_id": self.id,
            "url": self.url,
            "name": self.name,
            "state": self.state,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "percent": round(percent, 1),
            "download_url": self.download_url,
            "error": self.error,
            "seconds": round((self.finished_at or time.time()) - self.started_at, 1),
        }


class BundleRegistry:
    """Starts, tracks and cancels bundles for one plugin worker."""

    def __init__(
        self,
        *,
        list_objects: Callable[[str], Iterable[tuple[str, int]]],
        open_object: Callable[[str], Any],
        store_bundle: Callable[[Path, str], str],
        arcname: Callable[[str, str], str] | None = None,
        max_bytes: int = MAX_BUNDLE_BYTES,
        max_files: int = MAX_BUNDLE_FILES,
    ) -> None:
        """
        Args:
            list_objects: ``url -> iterable of (key, size)`` for every object under the prefix.
            open_object: ``key -> readable`` (an object with ``read(n)``; closed by the bundler
                when it has a ``close``).
            store_bundle: ``(local_zip_path, bundle_name) -> download_url``; the plugin decides
                where the archive lives and how long its link is valid.
            arcname: ``(url, key) -> path inside the zip``; default strips the prefix of ``url``.
            max_bytes: Refuse a folder larger than this (a CLI copy is the right tool then).
            max_files: Refuse a folder with more objects than this.

        """
        self._list = list_objects
        self._open = open_object
        self._store = store_bundle
        self._arcname = arcname
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._bundles: dict[str, Bundle] = {}
        self._lock = threading.Lock()

    # ── public ────────────────────────────────────────────────────────────

    def start(self, *, url: str, name: str) -> dict[str, Any]:
        """Begin bundling ``url``; returns the bundle's first status (``state: listing``)."""
        self._prune()
        bundle = Bundle(id=uuid.uuid4().hex[:12], url=url, name=_safe_name(name) or "download")
        with self._lock:
            self._bundles[bundle.id] = bundle
        threading.Thread(target=self._run, args=(bundle,), name=f"bundle-{bundle.id}", daemon=True).start()
        return bundle.public()

    def status(self, bundle_id: str) -> dict[str, Any] | None:
        """The bundle's status, or None when unknown (or expired)."""
        self._prune()
        with self._lock:
            bundle = self._bundles.get(bundle_id)
        return bundle.public() if bundle else None

    def cancel(self, bundle_id: str) -> bool:
        """Ask a running bundle to stop; True when it was known and not yet finished."""
        with self._lock:
            bundle = self._bundles.get(bundle_id)
        if bundle is None or bundle.state in ("done", "failed", "cancelled"):
            return False
        bundle._cancel.set()
        return True

    def active(self) -> int:
        """How many bundles are still being built (a worker's idle check may read this)."""
        with self._lock:
            return sum(1 for b in self._bundles.values() if b.state not in ("done", "failed", "cancelled"))

    # ── internals ─────────────────────────────────────────────────────────

    def _prune(self) -> None:
        cutoff = time.time() - BUNDLE_TTL_S
        with self._lock:
            for bid in [b.id for b in self._bundles.values() if b.finished_at and b.finished_at < cutoff]:
                self._bundles.pop(bid, None)

    def _run(self, bundle: Bundle) -> None:
        tmpdir = Path(tempfile.mkdtemp(prefix="tlc-bundle-"))
        zip_path = tmpdir / f"{bundle.name}.zip"
        try:
            objects = self._plan(bundle)
            if objects is None:
                return
            bundle.state = "running"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for key, size in objects:
                    if bundle._cancel.is_set():
                        self._finish(bundle, "cancelled", "")
                        return
                    self._add(archive, bundle, key, size)
            if bundle._cancel.is_set():
                self._finish(bundle, "cancelled", "")
                return
            bundle.state = "uploading"
            bundle.download_url = self._store(zip_path, bundle.name)
            self._finish(bundle, "done", "")
        except Exception as exc:
            logger.exception("Bundle %s of %s failed", bundle.id, bundle.url)
            self._finish(bundle, "failed", str(exc)[:400])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _plan(self, bundle: Bundle) -> list[tuple[str, int]] | None:
        objects: list[tuple[str, int]] = []
        total = 0
        for key, size in self._list(bundle.url):
            if bundle._cancel.is_set():
                self._finish(bundle, "cancelled", "")
                return None
            objects.append((str(key), int(size or 0)))
            total += int(size or 0)
            if len(objects) > self._max_files:
                self._finish(
                    bundle, "failed", f"More than {self._max_files:,} files. Copy a folder this large with the CLI."
                )
                return None
            if total > self._max_bytes:
                self._finish(
                    bundle,
                    "failed",
                    f"More than {self._max_bytes // 1024**3} GB. Copy a folder this large with the CLI.",
                )
                return None
        if not objects:
            self._finish(bundle, "failed", "The folder is empty.")
            return None
        bundle.files_total = len(objects)
        bundle.bytes_total = total
        return objects

    def _add(self, archive: zipfile.ZipFile, bundle: Bundle, key: str, size: int) -> None:
        arcname = self._arcname(bundle.url, key) if self._arcname else _default_arcname(bundle.url, key)
        body = self._open(key)
        try:
            with archive.open(zipfile.ZipInfo(arcname, date_time=time.localtime()[:6]), "w", force_zip64=True) as out:
                while True:
                    chunk = body.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    bundle.bytes_done += len(chunk)
                    if bundle._cancel.is_set():
                        return
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        bundle.files_done += 1

    @staticmethod
    def _finish(bundle: Bundle, state: str, error: str) -> None:
        bundle.state = state
        bundle.error = error
        bundle.finished_at = time.time()


def _default_arcname(url: str, key: str) -> str:
    """The object's path inside the zip: relative to the bundled prefix, under a folder named after it."""
    prefix = url.split("://", 1)[-1].split("/", 1)[1] if "/" in url.split("://", 1)[-1] else ""
    prefix = prefix.strip("/")
    rel = key[len(prefix) :].lstrip("/") if prefix and key.startswith(prefix) else key
    top = prefix.rsplit("/", 1)[-1] if prefix else url.split("://", 1)[-1].split("/", 1)[0]
    return f"{top}/{rel}" if rel else top


def _safe_name(name: str) -> str:
    """A file-name-safe bundle name (letters, digits, dot, dash, underscore; 80 characters)."""
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(name or "").strip())
    return cleaned.strip("-.")[:80]
