# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The shared transfer engine: plan, copy, move, rename, cancel — against a fake object store."""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import pytest

from tlc_plugin_sdk.shared.storage_transfer import TransferError, TransferRegistry


class _Store:
    """A bucket as a dict of url -> size, with the four calls the registry needs."""

    def __init__(self, objects: dict[str, int]) -> None:
        self.objects = dict(objects)
        self.copies: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.fail_on: set[str] = set()
        self.gate: threading.Event | None = None

    def list_objects(self, url: str) -> list[tuple[str, int]]:
        prefix = url.rstrip("/") + "/"
        return sorted((k, v) for k, v in self.objects.items() if k.startswith(prefix))

    def head_object(self, url: str) -> int | None:
        return self.objects.get(url)

    def copy_object(self, src: str, dst: str) -> None:
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if src in self.fail_on:
            msg = "AccessDenied"
            raise RuntimeError(msg)
        self.copies.append((src, dst))
        self.objects[dst] = self.objects[src]

    def delete_object(self, url: str) -> None:
        self.deletes.append(url)
        self.objects.pop(url, None)


def _registry(store: _Store, **kw: Any) -> TransferRegistry:
    return TransferRegistry(
        list_objects=store.list_objects,
        head_object=store.head_object,
        copy_object=store.copy_object,
        delete_object=store.delete_object,
        **kw,
    )


def _wait(registry: TransferRegistry, tid: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        st = registry.status(tid)
        assert st is not None
        if st["state"] in ("done", "failed", "cancelled"):
            return st
        time.sleep(0.01)
    msg = "transfer never finished"
    raise AssertionError(msg)


_OBJECTS = {
    "s3://b/data/fire/a.jpg": 10,
    "s3://b/data/fire/train/1.jpg": 100,
    "s3://b/data/fire/train/deep/2.jpg": 200,
    "s3://b/data/fire/keep.txt": 5,
    "s3://b/other/x.jpg": 999,
}


def test_plan_resolves_files_and_folders_to_object_pairs() -> None:
    store = _Store(_OBJECTS)
    pairs, summary = _registry(store).plan("s3://b/data/fire", "s3://b/backup/fire", ["a.jpg", "train/", "missing.jpg"])
    assert pairs == [
        ("s3://b/data/fire/a.jpg", "s3://b/backup/fire/a.jpg", 10),
        ("s3://b/data/fire/train/1.jpg", "s3://b/backup/fire/train/1.jpg", 100),
        ("s3://b/data/fire/train/deep/2.jpg", "s3://b/backup/fire/train/deep/2.jpg", 200),
    ]
    assert summary["files"] == 2 and summary["folders"] == 1 and summary["objects"] == 3 and summary["bytes"] == 310
    assert summary["missing"] == ["missing.jpg"]
    with pytest.raises(TransferError, match="into itself"):
        _registry(store).plan("s3://b/data/fire", "s3://b/data/fire/train", ["train/"])
    with pytest.raises(TransferError, match=re.escape("'..'")):
        _registry(store).plan("s3://b/data/fire", "s3://b/x", ["../etc"])
    with pytest.raises(TransferError, match="empty path"):
        _registry(store).plan("s3://b/data/fire", "s3://b/x", [""])


def test_copy_then_move_then_rename() -> None:
    store = _Store(_OBJECTS)
    registry = _registry(store, workers=4)
    st = registry.start("s3://b/data/fire", "s3://b/backup", ["train/"], mode="copy")
    done = _wait(registry, st["transfer_id"])
    assert (
        done["state"] == "done" and done["files_done"] == 2 and done["bytes_done"] == 300 and done["percent"] == 100.0
    )
    assert "s3://b/backup/train/deep/2.jpg" in store.objects and store.deletes == []

    st = registry.start("s3://b/data/fire", "s3://b/moved", ["a.jpg"], mode="move")
    done = _wait(registry, st["transfer_id"])
    assert done["state"] == "done" and done["deleted"] == 1
    assert "s3://b/moved/a.jpg" in store.objects and "s3://b/data/fire/a.jpg" not in store.objects

    # A rename is a move to the same folder with a new name — one item only.
    st = registry.start("s3://b/data/fire", "s3://b/data/fire", ["keep.txt"], mode="move", rename_to="notes.txt")
    done = _wait(registry, st["transfer_id"])
    assert done["state"] == "done" and "s3://b/data/fire/notes.txt" in store.objects
    with pytest.raises(TransferError, match="exactly one item"):
        registry.plan("s3://b/data/fire", "s3://b/data/fire", ["a", "b"], rename_to="c")
    with pytest.raises(TransferError, match="not a name"):
        registry.plan("s3://b/data/fire", "s3://b/data/fire", ["train/"], rename_to="a/b")


def test_a_failed_copy_is_reported_and_its_source_is_never_deleted() -> None:
    store = _Store(_OBJECTS)
    store.fail_on.add("s3://b/data/fire/train/1.jpg")
    registry = _registry(store, workers=2)
    st = registry.start("s3://b/data/fire", "s3://b/moved", ["train/"], mode="move")
    done = _wait(registry, st["transfer_id"])
    assert done["state"] == "failed" and "1 object(s) could not be moved" in done["error"]
    assert done["failures"] == [{"path": "s3://b/data/fire/train/1.jpg", "reason": "AccessDenied"}]
    assert "s3://b/data/fire/train/1.jpg" in store.objects  # the failed one stays put
    assert "s3://b/data/fire/train/deep/2.jpg" not in store.objects  # the successful one moved


def test_cancel_stops_copying_and_says_how_far_it_got() -> None:
    store = _Store(_OBJECTS)
    store.gate = threading.Event()
    registry = _registry(store, workers=1)
    st = registry.start("s3://b/data/fire", "s3://b/copy", ["train/", "a.jpg"], mode="copy")
    time.sleep(0.05)
    assert registry.cancel(st["transfer_id"]) is True
    store.gate.set()
    done = _wait(registry, st["transfer_id"])
    assert done["state"] == "cancelled" and "before the cancel" in done["error"]
    assert registry.cancel(st["transfer_id"]) is False  # finished: nothing to cancel


def test_bad_requests_fail_before_a_thread_exists() -> None:
    registry = _registry(_Store({}))
    with pytest.raises(TransferError, match="transfer mode"):
        registry.start("s3://b/a", "s3://b/c", ["x"], mode="teleport")
    with pytest.raises(TransferError, match=re.escape("'..'")):
        registry.start("s3://b/a", "s3://b/c", ["../x"])
    assert registry.status("nope") is None and registry.active() == 0
