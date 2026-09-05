# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Copies, moves and renames inside one storage provider, in the background, with progress.

Object stores have no folders and no rename: a folder is every object under a prefix, and a
rename is a copy and a delete. Moving a dataset folder is therefore thousands of copy requests,
which is a job with a progress bar, not a request the browser waits for. Every storage plugin
needs the same machinery — plan the objects, copy them in parallel, delete the sources for a
move, report counts, allow a cancel — and only the provider calls differ. This module is the
machinery; a plugin supplies four calls that speak its client:

    transfers = TransferRegistry(
        list_objects=lambda url: iter([(key, size), ...]),     # every object under a prefix URL
        head_object=lambda url: size_or_None,                   # one object's size; None when absent
        copy_object=lambda src_url, dst_url: None,              # server-side copy of one object
        delete_object=lambda url: None,                         # one object
    )
    plan = transfers.plan(src_url, dst_url, items=["a.jpg", "train/"])            # counts, no side effects
    status = transfers.start(src_url, dst_url, items=[...], mode="move")          # {transfer_id, state, ...}
    transfers.status(transfer_id); transfers.cancel(transfer_id)

``items`` are paths relative to ``src_url`` as a listing shows them; a trailing ``/`` names a folder.
Each item lands under ``dst_url`` with its own name (``rename_to`` gives a single item a new name).
Existing objects at the destination are overwritten — the copy semantics of every object store —
and the caller's confirmation says so. A move deletes each source object only after its copy
succeeded, so a failure mid-way leaves the source complete. Finished transfers stay in the
registry for :data:`TRANSFER_TTL_S` so a page can still read the outcome.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

TRANSFER_TTL_S = 6 * 3600.0
MAX_TRANSFER_FILES = 500_000
MODES = ("copy", "move")


class TransferError(ValueError):
    """A request that cannot be carried out as asked (bad path, source inside destination, ...)."""


@dataclass
class Transfer:
    """One copy/move job."""

    id: str
    src_url: str
    dst_url: str
    mode: str
    state: str = "planning"  # planning | running | deleting | done | failed | cancelled
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    deleted: int = 0
    error: str = ""
    failures: list[dict[str, str]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        """The status payload a route returns."""
        if self.state == "done":
            percent = 100.0
        elif self.bytes_total > 0:
            percent = min(99.0, 100.0 * self.bytes_done / self.bytes_total)
        elif self.files_total > 0:
            percent = min(99.0, 100.0 * self.files_done / self.files_total)
        else:
            percent = 0.0
        return {
            "transfer_id": self.id,
            "src_url": self.src_url,
            "dst_url": self.dst_url,
            "mode": self.mode,
            "state": self.state,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "deleted": self.deleted,
            "percent": round(percent, 1),
            "error": self.error,
            "failures": self.failures[:20],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _norm_item(raw: str) -> tuple[str, bool]:
    """``("train", True)`` for ``"train/"``, ``("a.jpg", False)`` for a file; refuses empty and ``..`` paths."""
    text = str(raw or "").replace("\\", "/")
    is_folder = text.endswith("/")
    rel = text.strip("/")
    if not rel:
        msg = "an empty path names the folder itself; pick files or folders inside it"
        raise TransferError(msg)
    if ".." in rel.split("/"):
        msg = f"'{rel}' has a '..' segment, which cannot be resolved under the folder"
        raise TransferError(msg)
    return rel, is_folder


def _join(url: str, rel: str) -> str:
    return url.rstrip("/") + "/" + rel.strip("/")


def _check_not_inside(src: str, dst: str) -> None:
    """A folder cannot be copied into itself (the listing would grow while it is read)."""
    s, d = src.rstrip("/") + "/", dst.rstrip("/") + "/"
    if d.startswith(s):
        msg = f"'{dst}' is inside '{src}': a folder cannot be copied into itself"
        raise TransferError(msg)


class TransferRegistry:
    """Plans, starts, tracks and cancels transfers for one plugin worker."""

    def __init__(
        self,
        *,
        list_objects: Callable[[str], Iterable[tuple[str, int]]],
        head_object: Callable[[str], int | None],
        copy_object: Callable[[str, str], None],
        delete_object: Callable[[str], None],
        workers: int = 16,
        max_files: int = MAX_TRANSFER_FILES,
    ) -> None:
        """
        Args:
            list_objects: ``url -> iterable of (key_or_url, size)`` for every object under the prefix.
                The first element may be a full URL or a key under ``url``'s bucket; only the part
                after ``url`` is used to place the copy, so either works.
            head_object: ``url -> size`` for one object, ``None`` when it does not exist.
            copy_object: ``(src_url, dst_url) -> None``; server-side, overwriting.
            delete_object: ``url -> None``.
            workers: Copies in flight at once.
            max_files: Refuse a transfer larger than this (the CLI is the tool then).
        """
        self._list = list_objects
        self._head = head_object
        self._copy = copy_object
        self._delete = delete_object
        self._workers = max(1, workers)
        self._max_files = max_files
        self._transfers: dict[str, Transfer] = {}
        self._lock = threading.Lock()

    # ── public ────────────────────────────────────────────────────────────

    def plan(
        self, src_url: str, dst_url: str, items: list[str], *, rename_to: str = ""
    ) -> tuple[list[tuple[str, str, int]], dict[str, Any]]:
        """Resolve ``items`` to ``[(src_object_url, dst_object_url, size)]`` plus counts; no side effects."""
        if rename_to and len(items) != 1:
            msg = "a new name applies to exactly one item"
            raise TransferError(msg)
        new_name = rename_to.strip().strip("/") if rename_to else ""
        if new_name and ("/" in new_name or new_name in (".", "..")):
            msg = f"'{rename_to}' is not a name: no slashes, not '.' or '..'"
            raise TransferError(msg)
        pairs: list[tuple[str, str, int]] = []
        files = folders = 0
        missing: list[str] = []
        for raw in items:
            rel, is_folder = _norm_item(raw)
            name = new_name or rel.rsplit("/", 1)[-1]
            src = _join(src_url, rel)
            dst = _join(dst_url, name)
            if is_folder:
                folders += 1
                _check_not_inside(src, dst)
                base = src.rstrip("/") + "/"
                for key_or_url, size in self._list(src):
                    text = str(key_or_url)
                    tail = text[len(base) :] if text.startswith(base) else text.split(rel.rstrip("/") + "/", 1)[-1]
                    if not tail or tail.endswith("/"):
                        continue
                    pairs.append((base + tail, dst.rstrip("/") + "/" + tail, int(size or 0)))
                    if len(pairs) > self._max_files:
                        msg = f"more than {self._max_files:,} objects; copy a folder that large with your cloud's tools"
                        raise TransferError(msg)
            else:
                files += 1
                size = self._head(src)
                if size is None:
                    missing.append(rel)
                    continue
                pairs.append((src, dst, int(size)))
        seen: set[str] = set()
        unique = [p for p in pairs if not (p[0] in seen or seen.add(p[0]))]
        summary = {
            "files": files,
            "folders": folders,
            "objects": len(unique),
            "bytes": sum(p[2] for p in unique),
            "missing": missing,
            "src_url": src_url,
            "dst_url": dst_url,
        }
        return unique, summary

    def start(
        self, src_url: str, dst_url: str, items: list[str], *, mode: str = "copy", rename_to: str = ""
    ) -> dict[str, Any]:
        """Begin the transfer; returns its first status. Planning runs on the job's own thread."""
        if mode not in MODES:
            msg = f"'{mode}' is not a transfer mode; use copy or move"
            raise TransferError(msg)
        for raw in items:
            _norm_item(raw)  # fail fast on a bad path, before a thread exists
        self._prune()
        transfer = Transfer(id=uuid.uuid4().hex[:12], src_url=src_url, dst_url=dst_url, mode=mode)
        with self._lock:
            self._transfers[transfer.id] = transfer
        threading.Thread(
            target=self._run, args=(transfer, list(items), rename_to), name=f"transfer-{transfer.id}", daemon=True
        ).start()
        return transfer.public()

    def status(self, transfer_id: str) -> dict[str, Any] | None:
        self._prune()
        with self._lock:
            transfer = self._transfers.get(transfer_id)
        return transfer.public() if transfer else None

    def cancel(self, transfer_id: str) -> bool:
        with self._lock:
            transfer = self._transfers.get(transfer_id)
        if transfer is None or transfer.state in ("done", "failed", "cancelled"):
            return False
        transfer._cancel.set()
        return True

    def active(self) -> int:
        with self._lock:
            return sum(1 for t in self._transfers.values() if t.state not in ("done", "failed", "cancelled"))

    # ── internals ─────────────────────────────────────────────────────────

    def _prune(self) -> None:
        cutoff = time.time() - TRANSFER_TTL_S
        with self._lock:
            for tid in [t.id for t in self._transfers.values() if t.finished_at and t.finished_at < cutoff]:
                self._transfers.pop(tid, None)

    def _run(self, transfer: Transfer, items: list[str], rename_to: str) -> None:
        try:
            pairs, summary = self.plan(transfer.src_url, transfer.dst_url, items, rename_to=rename_to)
            transfer.files_total = len(pairs)
            transfer.bytes_total = int(summary["bytes"])
            if summary["missing"]:
                transfer.failures.extend({"path": m, "reason": "no such file"} for m in summary["missing"])
            if not pairs:
                self._finish(
                    transfer,
                    "done" if not summary["missing"] else "failed",
                    "nothing to copy" if summary["missing"] else "",
                )
                return
            transfer.state = "running"
            copied: list[tuple[str, str, int]] = []
            lock = threading.Lock()

            def one(pair: tuple[str, str, int]) -> None:
                if transfer._cancel.is_set():
                    return
                src, dst, size = pair
                try:
                    self._copy(src, dst)
                except Exception as exc:
                    with lock:
                        transfer.failures.append({"path": src, "reason": str(exc)[:200]})
                    return
                with lock:
                    copied.append(pair)
                    transfer.files_done += 1
                    transfer.bytes_done += size

            with ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix=f"transfer-{transfer.id}") as pool:
                list(pool.map(one, pairs))
            if transfer._cancel.is_set():
                self._finish(
                    transfer, "cancelled", f"{len(copied)} of {len(pairs)} objects were copied before the cancel"
                )
                return
            if transfer.mode == "move":
                transfer.state = "deleting"
                for src, _dst, _size in copied:
                    if transfer._cancel.is_set():
                        break
                    try:
                        self._delete(src)
                        transfer.deleted += 1
                    except Exception as exc:
                        transfer.failures.append({
                            "path": src,
                            "reason": f"copied, but not removed from the source: {exc!s:.160}",
                        })
            if transfer.failures:
                verb = "moved" if transfer.mode == "move" else "copied"
                self._finish(transfer, "failed", f"{len(transfer.failures)} object(s) could not be {verb}")
                return
            self._finish(transfer, "done", "")
        except Exception as exc:
            logger.exception("Transfer %s (%s → %s) failed", transfer.id, transfer.src_url, transfer.dst_url)
            self._finish(transfer, "failed", str(exc)[:400])

    @staticmethod
    def _finish(transfer: Transfer, state: str, error: str) -> None:
        transfer.state = state
        transfer.error = error
        transfer.finished_at = time.time()
