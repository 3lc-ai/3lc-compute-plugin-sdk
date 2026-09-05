# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for shared.data_source_routes — root confinement and upload hardening.

These routes expose the compute node's filesystem to every authenticated hub
user, so the properties under test are security properties: /browse must not
list outside the configured roots (however the path is spelled), and
/upload-temp must not let the client-supplied filename choose the location.
"""

import os
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from tlc_plugin_sdk.shared.data_source_routes import (
    MAX_UPLOAD_MB_ENV,
    ROOTS_ENV,
    _safe_upload_name,
    allowed_browse_roots,
    data_source_route_handlers,
)


@pytest.fixture()
def client() -> TestClient[Litestar]:
    return TestClient(Litestar(route_handlers=data_source_route_handlers()))


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A confinement root with a small tree, exported via ROOTS_ENV."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "table.yaml").write_text("x")
    (tmp_path / "data.csv").write_text("a,b")
    (tmp_path / ".hidden").write_text("")
    monkeypatch.setenv(ROOTS_ENV, str(tmp_path))
    return tmp_path


def _browse(client: TestClient[Litestar], **params: str) -> dict[str, Any]:
    response = client.get("/browse", params=params)
    body: dict[str, Any] = response.json()
    return body


class TestAllowedBrowseRoots:
    def test_defaults_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ROOTS_ENV, raising=False)
        assert allowed_browse_roots() == [os.path.realpath(os.path.expanduser("~"))]

    def test_nonexistent_entries_are_dropped(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ROOTS_ENV, os.pathsep.join([str(root), "/no/such/dir"]))
        assert allowed_browse_roots() == [str(root.resolve())]

    def test_all_entries_bad_falls_back_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ROOTS_ENV, "/no/such/dir")
        assert allowed_browse_roots() == [os.path.realpath(os.path.expanduser("~"))]


class TestBrowseConfinement:
    def test_empty_and_tilde_open_the_first_root(self, client: TestClient[Litestar], root: Path) -> None:
        for spelled in ("", "~"):
            body = _browse(client, path=spelled)
            assert body["path"] == str(root.resolve())
            assert body["parent"] is None  # clamped: no "up" from a root
            assert body["root"] == str(root.resolve())

    def test_listing_inside_root(self, client: TestClient[Litestar], root: Path) -> None:
        body = _browse(client, path=str(root))
        names = [e["name"] for e in body["entries"]]
        assert names == ["sub", "data.csv"]  # dirs first, no dotfiles

    def test_outside_root_is_denied(self, client: TestClient[Litestar], root: Path) -> None:
        body = _browse(client, path="/etc")
        assert "outside the allowed data-source roots" in body["error"]

    def test_dotdot_escape_is_denied(self, client: TestClient[Litestar], root: Path) -> None:
        body = _browse(client, path=f"{root}/sub/../../..")
        assert "outside the allowed data-source roots" in body["error"]

    @pytest.mark.skipif(os.name != "posix", reason="symlinks")
    def test_symlink_escape_is_denied(self, client: TestClient[Litestar], root: Path, tmp_path_factory: Any) -> None:
        outside = tmp_path_factory.mktemp("outside")
        (root / "link").symlink_to(outside)
        body = _browse(client, path=str(root / "link"))
        assert "outside the allowed data-source roots" in body["error"]

    def test_parent_clamped_to_root(self, client: TestClient[Litestar], root: Path) -> None:
        body = _browse(client, path=str(root / "sub"))
        assert body["parent"] == str(root.resolve())
        assert _browse(client, path=str(root))["parent"] is None

    def test_glob_filters_files_not_dirs(self, client: TestClient[Litestar], root: Path) -> None:
        body = _browse(client, path=str(root), glob="*.yaml")
        assert [e["name"] for e in body["entries"]] == ["sub"]
        body = _browse(client, path=str(root / "sub"), glob="*.yaml")
        assert [e["name"] for e in body["entries"]] == ["table.yaml"]

    def test_glob_accepts_comma_separated_patterns(self, client: TestClient[Litestar], root: Path) -> None:
        # Matches the widget's "accept" config verbatim (e.g. "*.yaml,*.yml") — a file
        # is kept if it matches ANY pattern in the list, not the joined string literally.
        body = _browse(client, path=str(root / "sub"), glob="*.yaml,*.yml")
        assert [e["name"] for e in body["entries"]] == ["table.yaml"]
        body = _browse(client, path=str(root), glob="*.csv,*.json")
        assert [e["name"] for e in body["entries"]] == ["sub", "data.csv"]


class TestBrowseAccessibility:
    """Each dir entry is tagged with whether it can actually be opened for listing."""

    def test_readable_dir_is_accessible(self, client: TestClient[Litestar], root: Path) -> None:
        entries = {e["name"]: e for e in _browse(client, path=str(root))["entries"]}
        assert entries["sub"]["accessible"] is True

    def test_files_do_not_carry_accessible(self, client: TestClient[Litestar], root: Path) -> None:
        entries = {e["name"]: e for e in _browse(client, path=str(root))["entries"]}
        assert "accessible" not in entries["data.csv"]

    @pytest.mark.skipif(os.name != "posix", reason="chmod 000 semantics")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses chmod 000, so the probe would report accessible",
    )
    def test_unreadable_dir_is_inaccessible(self, client: TestClient[Litestar], root: Path) -> None:
        locked = root / "locked"
        locked.mkdir()
        os.chmod(locked, 0o000)
        try:
            entries = {e["name"]: e for e in _browse(client, path=str(root))["entries"]}
            assert entries["locked"]["accessible"] is False
            # The readable sibling is unaffected.
            assert entries["sub"]["accessible"] is True
        finally:
            os.chmod(locked, 0o700)


class TestBrowseWritable:
    """The ``writable`` flag is reported only in output mode (``purpose=output``)."""

    def test_input_mode_omits_writable(self, client: TestClient[Litestar], root: Path) -> None:
        body = _browse(client, path=str(root))
        assert "writable" not in body  # top-level
        entries = {e["name"]: e for e in body["entries"]}
        assert "writable" not in entries["sub"]  # dir entry

    def test_default_mode_omits_writable(self, client: TestClient[Litestar], root: Path) -> None:
        # No purpose param at all behaves like input mode.
        body = _browse(client, path=str(root))
        assert "writable" not in body
        assert all("writable" not in e for e in body["entries"])

    def test_output_mode_reports_writable(self, client: TestClient[Litestar], root: Path) -> None:
        body = _browse(client, path=str(root), purpose="output")
        assert body["writable"] is True  # the listed dir itself
        entries = {e["name"]: e for e in body["entries"]}
        assert entries["sub"]["writable"] is True  # a normal, writable dir
        # Files still never carry the flag.
        assert "writable" not in entries["data.csv"]

    @pytest.mark.skipif(os.name != "posix", reason="chmod semantics")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses chmod, so a 0500 dir would still report writable",
    )
    def test_output_mode_flags_unwritable_dir(self, client: TestClient[Litestar], root: Path) -> None:
        locked = root / "readonly"
        locked.mkdir()
        os.chmod(locked, 0o500)  # readable + executable, but not writable
        try:
            entries = {e["name"]: e for e in _browse(client, path=str(root), purpose="output")["entries"]}
            assert entries["readonly"]["writable"] is False
            # A normal sibling is unaffected.
            assert entries["sub"]["writable"] is True
            # Listing inside the read-only dir reports the dir itself as unwritable.
            inside = _browse(client, path=str(locked), purpose="output")
            assert inside["writable"] is False
        finally:
            os.chmod(locked, 0o700)


class TestUploadTemp:
    def test_traversal_filename_cannot_choose_location(self, client: TestClient[Litestar]) -> None:
        response = client.post("/upload-temp", files={"data": ("../../evil.txt", b"payload")})
        body = response.json()
        assert body["filename"] == "evil.txt"
        dest = Path(body["path"])
        assert dest.name == "evil.txt"
        assert "tlc-uploads" in dest.parts
        assert dest.read_bytes() == b"payload"

    def test_windows_separators_stripped(self) -> None:
        assert _safe_upload_name("..\\..\\evil.txt") == "evil.txt"
        assert _safe_upload_name("C:\\Users\\x\\evil.txt") == "evil.txt"

    def test_dot_names_get_a_default(self) -> None:
        assert _safe_upload_name("..") == "upload"
        assert _safe_upload_name(".") == "upload"
        assert _safe_upload_name("") == "upload"
        assert _safe_upload_name(None) == "upload"

    def test_concurrent_same_name_uploads_do_not_clobber(self, client: TestClient[Litestar]) -> None:
        first = client.post("/upload-temp", files={"data": ("same.txt", b"one")}).json()
        second = client.post("/upload-temp", files={"data": ("same.txt", b"two")}).json()
        assert first["path"] != second["path"]
        assert Path(first["path"]).read_bytes() == b"one"
        assert Path(second["path"]).read_bytes() == b"two"

    def test_size_cap(self, client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MAX_UPLOAD_MB_ENV, "1")
        response = client.post("/upload-temp", files={"data": ("big.bin", b"x" * (1024 * 1024 + 1))})
        body = response.json()
        assert "larger than the 1 MB limit" in body["error"]

    def test_bad_cap_value_falls_back_to_default(
        self, client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MAX_UPLOAD_MB_ENV, "not-a-number")
        response = client.post("/upload-temp", files={"data": ("ok.txt", b"fine")})
        assert response.json()["filename"] == "ok.txt"


class TestSelfReferentialSymlink:
    """The /usr/bin/X11 -> '.' fossil: entering it must collapse, not nest forever."""

    @pytest.mark.skipif(os.name != "posix", reason="symlinks")
    def test_self_symlink_collapses_instead_of_nesting(self, client: TestClient[Litestar], root: Path) -> None:
        (root / "loop").symlink_to(".")
        body = _browse(client, path=str(root / "loop" / "loop" / "loop"))
        assert body["path"] == str(root.resolve())  # realpath collapsed the whole chain
        assert body["parent"] is None  # and it IS the root, so no phantom "up"


def test_project_root_is_the_plugins_own_tlc_root(client: TestClient[Litestar]) -> None:
    """The alias widget asks the writing plugin, not the infrastructure plugin, where tables land."""
    import tlc

    body = client.get("/project-root").json()
    assert body["url"] == str(tlc.config.project_root_url).rstrip("/") and body["url"]
