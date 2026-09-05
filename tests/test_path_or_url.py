# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Path-or-URL helpers: one vocabulary for local folders and bucket prefixes."""

from __future__ import annotations

from pathlib import Path

import pytest

from tlc_plugin_sdk.shared import url_utils as pu


def test_is_url_and_normalize() -> None:
    assert pu.is_url("s3://b/x") and pu.is_url(" abfs://c/x ") and not pu.is_url("/data/x") and not pu.is_url("C:\\x")
    assert pu.normalize_path_or_url(" s3://b/data/fire/ ") == "s3://b/data/fire"
    assert pu.normalize_path_or_url("s3://bucket") == "s3://bucket"
    assert pu.normalize_path_or_url("~/x").startswith("/") and pu.normalize_path_or_url("/x") == "/x"
    with pytest.raises(ValueError, match="absolute"):
        pu.normalize_path_or_url("relative/path")


def test_url_segments() -> None:
    assert pu.join_path_or_url("s3://b/data/", "images", "val") == "s3://b/data/images/val"
    assert pu.join_path_or_url("/data", "images") == str(Path("/data/images"))
    assert pu.parent_of("s3://b/data/images/") == "s3://b/data" and pu.parent_of("s3://b") == "s3://b"
    assert (
        pu.name_of("s3://b/data/images/") == "images" and pu.stem_of("s3://b/a/instances_val.json") == "instances_val"
    )
    assert pu.suffix_of("s3://b/a/X.JSON") == ".json" and pu.suffix_of("/a/b.yaml") == ".yaml"
    assert pu.is_absolute("s3://b/x") and pu.is_absolute("/x") and not pu.is_absolute("x/y")


def test_local_listing_and_reading(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "1.JPG").write_bytes(b"x")
    (tmp_path / "2.png").write_bytes(b"y")
    (tmp_path / ".hidden.png").write_bytes(b"z")
    (tmp_path / "notes.txt").write_text("hi")
    assert pu.is_folder(str(tmp_path)) and pu.is_file(str(tmp_path / "2.png")) and not pu.is_file(str(tmp_path / "a"))
    assert sorted(Path(p).name for p in pu.iter_files(str(tmp_path), extensions={".jpg", ".png"})) == ["1.JPG", "2.png"]
    assert [Path(p).name for p in pu.iter_files(str(tmp_path), recursive=False)] == ["2.png", "notes.txt"]
    assert pu.read_text(str(tmp_path / "notes.txt")) == "hi"


def test_url_listing_walks_the_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = {
        "s3://b/data": [("images", True), ("labels", True), ("readme.txt", False)],
        "s3://b/data/images": [("1.jpg", False), (".DS_Store", False), ("sub", True)],
        "s3://b/data/images/sub": [("2.PNG", False)],
        "s3://b/data/labels": [("1.txt", False)],
    }
    monkeypatch.setattr(pu, "list_folder", lambda v: [(v.rstrip("/") + "/" + n, d) for n, d in tree[v.rstrip("/")]])
    assert pu.iter_files("s3://b/data/", extensions={".jpg", ".png"}) == [
        "s3://b/data/images/1.jpg",
        "s3://b/data/images/sub/2.PNG",
    ]
    assert pu.iter_files("s3://b/data/images", recursive=False) == ["s3://b/data/images/1.jpg"]
