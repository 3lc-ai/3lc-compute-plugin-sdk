# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The shared folder-to-zip bundler: progress, layout, cancel, caps — with fake provider calls."""

from __future__ import annotations

import io
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from tlc_plugin_sdk.shared import storage_bundle as sb


def _wait(registry: sb.BundleRegistry, bundle_id: str, *, timeout: float = 10.0) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = registry.status(bundle_id)
        assert status is not None
        if status["state"] in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.01)
    msg = f"bundle did not finish: {registry.status(bundle_id)}"
    raise AssertionError(msg)


def _registry(
    objects: dict[str, bytes], stored: list[Path], *, keep: Path | None = None, **kw: Any
) -> sb.BundleRegistry:
    def list_objects(url: str) -> list[tuple[str, int]]:
        prefix = url.split("://", 1)[1].split("/", 1)[1]
        return [(k, len(v)) for k, v in sorted(objects.items()) if k.startswith(prefix)]

    def store(path: Path, name: str) -> str:
        # The bundler deletes its temp dir after storing: keep a copy where the test can read it.
        copy = (keep or path.parent.parent) / (name + ".copy.zip")
        copy.write_bytes(path.read_bytes())
        stored.append(copy)
        return f"https://signed.example/{name}.zip"

    return sb.BundleRegistry(
        list_objects=list_objects, open_object=lambda k: io.BytesIO(objects[k]), store_bundle=store, **kw
    )


def test_bundle_zips_the_prefix_with_relative_paths_and_reports_progress(tmp_path: Path) -> None:
    objects = {"data/fire/train/a.jpg": b"a" * 1000, "data/fire/train/b.jpg": b"b" * 500, "data/other/c.jpg": b"c"}
    stored: list[Path] = []
    registry = _registry(objects, stored, keep=tmp_path)
    first = registry.start(url="s3://bucket/data/fire", name="fire")
    assert first["state"] in ("listing", "running") and first["bundle_id"]
    status = _wait(registry, first["bundle_id"])
    assert status["state"] == "done", status
    assert status["files_total"] == 2 and status["files_done"] == 2
    assert status["bytes_total"] == 1500 and status["bytes_done"] == 1500 and status["percent"] == 100.0
    assert status["download_url"] == "https://signed.example/fire.zip"
    with zipfile.ZipFile(stored[0]) as archive:
        assert sorted(archive.namelist()) == ["fire/train/a.jpg", "fire/train/b.jpg"]
        assert archive.getinfo("fire/train/a.jpg").compress_type == zipfile.ZIP_STORED
        assert archive.read("fire/train/b.jpg") == b"b" * 500


def test_bundle_refuses_empty_and_oversized_folders() -> None:
    stored: list[Path] = []
    registry = _registry({}, stored)
    status = _wait(registry, registry.start(url="s3://b/nothing", name="x")["bundle_id"])
    assert status["state"] == "failed" and "empty" in status["error"]
    registry = _registry({"p/a": b"12345", "p/b": b"12345"}, stored, max_bytes=6)
    status = _wait(registry, registry.start(url="s3://b/p", name="x")["bundle_id"])
    assert status["state"] == "failed" and "CLI" in status["error"]
    registry = _registry({"p/a": b"1", "p/b": b"1"}, stored, max_files=1)
    status = _wait(registry, registry.start(url="s3://b/p", name="x")["bundle_id"])
    assert status["state"] == "failed" and "files" in status["error"]
    assert stored == []


def test_bundle_can_be_cancelled_while_streaming() -> None:
    gate = threading.Event()

    class Slow(io.BytesIO):
        def read(self, n: int = -1) -> bytes:  # type: ignore[override]
            gate.wait(5)
            return super().read(n)

    registry = sb.BundleRegistry(
        list_objects=lambda url: [("p/a", 10), ("p/b", 10)],
        open_object=lambda k: Slow(b"x" * 10),
        store_bundle=lambda path, name: "https://never",
    )
    bundle_id = registry.start(url="s3://b/p", name="x")["bundle_id"]
    assert registry.cancel(bundle_id) is True
    gate.set()
    status = _wait(registry, bundle_id)
    assert status["state"] == "cancelled" and status["download_url"] == ""
    assert registry.cancel(bundle_id) is False  # already finished
    assert registry.status("nope") is None


def test_bundle_names_and_arcnames_are_safe() -> None:
    assert sb._safe_name("../weird name?.zip") == "weird-name-.zip" or sb._safe_name("../weird name?.zip").startswith(
        "weird"
    )
    assert "/" not in sb._safe_name("a/b/c")
    assert sb._default_arcname("s3://bucket/data/fire", "data/fire/x/y.jpg") == "fire/x/y.jpg"
    assert sb._default_arcname("s3://bucket", "y.jpg") == "bucket/y.jpg"
    assert sb._default_arcname("volume://vol1/models", "models/best.pt") == "models/best.pt"
