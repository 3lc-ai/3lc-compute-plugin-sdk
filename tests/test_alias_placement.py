# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Alias placement: copying data next to the table and registering the alias against the copy.

The case: a project root on a bucket, the images on this machine. The shared alias widget
offers one checkbox; the helper copies the folder through ``tlc.Url`` and the persisted alias
points at the copy while this session keeps resolving to the local folder.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk.shared import aliases
from tlc_plugin_sdk.shared.alias_ui import alias_ui_script
from tlc_plugin_sdk.shared.data_source_ui import data_source_ui_script


def _tree(root: Path) -> None:
    (root / "train").mkdir(parents=True)
    (root / "train" / "a.jpg").write_bytes(b"a" * 10)
    (root / "train" / "b.jpg").write_bytes(b"bb" * 10)
    (root / "c.txt").write_bytes(b"c")
    (root / ".DS_Store").write_bytes(b"junk")  # never copied


def test_copy_folder_to_url_copies_the_tree_and_reports_progress(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst" / "data" / "token"
    _tree(src)
    seen: list[tuple[int, int, int, int]] = []
    stats = aliases.copy_folder_to_url(str(src), str(dst), progress=lambda *a: seen.append(a), workers=2)
    assert stats == {"files": 3, "bytes": 31, "skipped": 0, "url": str(dst)}
    assert (dst / "train" / "a.jpg").read_bytes() == b"a" * 10 and (dst / "c.txt").read_bytes() == b"c"
    assert not (dst / ".DS_Store").exists()
    assert seen[-1] == (3, 3, 31, 31) and len(seen) == 3


def test_copy_folder_to_url_second_run_skips_what_is_there(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    _tree(src)
    aliases.copy_folder_to_url(str(src), str(dst))
    (dst / "train" / "b.jpg").unlink()  # one file missing → only that one is written again
    stats = aliases.copy_folder_to_url(str(src), str(dst))
    assert stats["files"] == 3 and stats["skipped"] == 2
    assert (dst / "train" / "b.jpg").exists()


def test_copy_folder_to_url_rejects_a_missing_folder(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        aliases.copy_folder_to_url(str(tmp_path / "nope"), str(tmp_path / "dst"))


def test_is_remote_url() -> None:
    assert aliases.is_remote_url("s3://bucket/projects") and aliases.is_remote_url(" runpod://vol/x ")
    assert not aliases.is_remote_url("/Users/me/data") and not aliases.is_remote_url("C:\\data")


def test_register_alias_persists_the_copy_and_keeps_the_session_local(monkeypatch: pytest.MonkeyPatch) -> None:
    import tlc

    calls: dict[str, Any] = {}
    monkeypatch.setattr(tlc.url, "get_registered_url_aliases", dict)
    monkeypatch.setattr(
        tlc.helpers.ProjectHelper,
        "register_project_url_alias",
        staticmethod(lambda **kw: calls.setdefault("project", kw)),
    )
    monkeypatch.setattr(tlc.url, "register_url_alias", lambda **kw: calls.setdefault("session", kw))

    out = aliases.register_alias("Fire", "~/data/fire", "FIRE", remote_path="s3://b/projects/Fire/data/fire/")
    assert out["remote_path"] == "s3://b/projects/Fire/data/fire" and out["primary_created"]
    assert calls["project"]["path"] == "s3://b/projects/Fire/data/fire" and calls["project"]["force"] is True
    assert calls["session"]["path"] == out["path"] and "~" not in out["path"]  # local, expanded

    calls.clear()
    aliases.register_alias("Fire", "/data/fire", "FIRE")
    assert calls["project"]["path"] == "/data/fire" and calls["project"]["force"] is False


def test_alias_widget_offers_the_copy_and_submits_it() -> None:
    js = alias_ui_script()
    for pin in (
        "function _tlcProjectLocationHtml(",  # "Create project in": this computer, or the bucket root
        "function _tlcBindProjectLocation(",
        "function _tlcGetProjectRoot(",
        "options.length > 1 ? '' : 'none'",  # hidden while there is nothing to choose
        "rootOverride",  # the copy offer follows the chosen root
        "-alias-copy-enabled",
        "function _tlcAliasReviewCopy(",
        "'/project-root'",  # the root this plugin's own tlc writes to — never the infra plugin's bucket
        "_tlcStorageOf(root) !== 'local' && _tlcStorageOf(folder) === 'local'",  # local data, bucket root — only then
        "'/data/' + token.toLowerCase()",
        "alias_copy_to_root:",
        "alias_copy_target:",
    ):
        assert pin in js, pin


def test_data_source_widget_browses_buckets_through_the_generic_storage_surface() -> None:
    js = data_source_ui_script()
    for pin in ("'/api/infra/storage'", "/list?url=", "data-ds-loc", "function _globToRegex(", "This compute"):
        assert pin in js, pin


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_shared_scripts_parse(tmp_path: Path) -> None:
    for name, js in (("alias.js", alias_ui_script()), ("ds.js", data_source_ui_script())):
        f = tmp_path / name
        f.write_text(js)
        subprocess.run(["node", "--check", str(f)], check=True)


def test_register_alias_persists_under_the_chosen_root(monkeypatch: pytest.MonkeyPatch) -> None:
    import tlc

    calls: dict[str, Any] = {}
    monkeypatch.setattr(tlc.url, "get_registered_url_aliases", dict)
    monkeypatch.setattr(
        tlc.helpers.ProjectHelper, "register_project_url_alias", staticmethod(lambda **kw: calls.update(kw))
    )
    monkeypatch.setattr(tlc.url, "register_url_alias", lambda **kw: None)
    aliases.register_alias("Fire", "/data/fire", "FIRE", root_url="s3://b/projects/")
    assert calls["root_url"] == "s3://b/projects"
    aliases.register_alias("Fire", "/data/fire", "FIRE")
    assert calls["root_url"] is None  # the plugin's default root
