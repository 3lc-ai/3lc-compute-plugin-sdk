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

import pytest
from litestar import get

from tlc_plugin_sdk import connections
from tlc_plugin_sdk.connections import CONNECTION_HEADER, AwsSession, ConnectionBinding, encode_binding
from tlc_plugin_sdk.contract import HubPlugin
from tlc_plugin_sdk.harness import PluginHarness
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


def test_an_empty_name_is_resolved_from_the_url_the_registry_bundles() -> None:
    registry = sb.BundleRegistry(list_objects=lambda u: [], open_object=lambda k: None, store_bundle=lambda p, n: "")
    assert registry.start(url="s3://bucket/data/fire/", name="")["name"] == "fire"
    assert registry.start(url="s3://bucket")["name"] == "bucket"
    assert registry.start(url="s3://bucket/data", name="mine")["name"] == "mine"
    assert sb.default_bundle_name("volume://vol1") == "vol1"


def test_a_normalising_subclass_sees_an_empty_name_and_gets_the_normalised_default() -> None:
    seen: list[str] = []

    class Normalising(sb.BundleRegistry):
        def start(self, *, url: str, name: str = "") -> dict[str, Any]:
            seen.append(name)
            return super().start(url=url.replace("VOLUME://", "volume://").rstrip("/") + "/models", name=name)

    registry = Normalising(list_objects=lambda u: [], open_object=lambda k: None, store_bundle=lambda p, n: "")
    assert registry.start(url="VOLUME://vol1/")["name"] == "models"
    assert seen == [""]


class _Unprintable:
    def __str__(self) -> str:
        msg = "no text"
        raise RuntimeError(msg)


def test_a_describer_answering_a_non_str_or_raising_never_strands_the_job() -> None:
    def failing_store(path: Path, name: str) -> str:
        msg = "store failed"
        raise RuntimeError(msg)

    for describer, expected in ((lambda exc: 7, "7"), (lambda exc: _Unprintable(), "store failed")):
        registry = sb.BundleRegistry(
            list_objects=lambda u: [("p/a", 1)],
            open_object=lambda k: io.BytesIO(b"x"),
            store_bundle=failing_store,
            describe_error=describer,
        )
        status = _wait(registry, registry.start(url="s3://b/p")["bundle_id"])
        assert (status["state"], status["error"]) == ("failed", expected)


def _connection_seen() -> tuple[str | None, str | None]:
    binding = connections.current_connection()
    credential = connections.current_credential()
    return (binding.id if binding else None, getattr(credential, "session_token", None))


def test_a_bundle_started_by_a_request_keeps_the_requests_connection_in_its_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connections, "_RESOLVERS", {})
    connections.register_resolver("aws", "KEYLESS", lambda b: AwsSession("AKIA123", "secret", "session-1"))
    seen: list[tuple[str, str | None, str | None]] = []

    def list_objects(url: str) -> list[tuple[str, int]]:
        seen.append(("list", *_connection_seen()))
        return [("p/a", 1), ("p/b", 1)]

    def open_object(key: str) -> io.BytesIO:
        seen.append(("open", *_connection_seen()))
        return io.BytesIO(b"x")

    def store_bundle(path: Path, name: str) -> str:
        seen.append(("store", *_connection_seen()))
        return "https://signed.example/p.zip"

    registry = sb.BundleRegistry(list_objects=list_objects, open_object=open_object, store_bundle=store_bundle)

    # Sync, like the infrastructure routes: the handler runs in a thread with a copy of the context.
    @get("/bundle", sync_to_thread=True)
    def start_bundle() -> dict[str, Any]:
        return registry.start(url="s3://b/p")

    class _Probe(HubPlugin):
        def get_ui_fragment(self) -> str:
            return ""

        def get_route_handlers(self) -> list[Any]:
            return [start_bundle]

    header = {CONNECTION_HEADER: encode_binding(ConnectionBinding("conn-1", "aws", "KEYLESS", {}))}
    with PluginHarness(_Probe(), plugin_id="probe", config_root=tmp_path) as harness:
        bundle_id = harness.get("/bundle", headers=header).json()["bundle_id"]
        # The request has ended (its context variables are reset) before the bundle finishes.
        assert _wait(registry, bundle_id)["state"] == "done"
        assert seen == [
            ("list", "conn-1", "session-1"),
            ("open", "conn-1", "session-1"),
            ("open", "conn-1", "session-1"),
            ("store", "conn-1", "session-1"),
        ]

        # A request naming no Connection starts a bundle that sees none: nothing leaks between jobs.
        seen.clear()
        assert _wait(registry, harness.get("/bundle").json()["bundle_id"])["state"] == "done"
        assert {entry[1:] for entry in seen} == {(None, None)}
