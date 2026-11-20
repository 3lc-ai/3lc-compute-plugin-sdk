# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for shared.url_utils path normalization (no `tlc` required)."""

import os
import sys

import pytest

from tlc_plugin_sdk.shared.url_utils import get_url_column_names, normalize_local_path, normalize_url

HOME = os.path.expanduser("~")
# An absolute local file path on this platform: on Windows "/data/out.csv" has no drive, so it is not absolute.
OUT_CSV = "C:\\data\\out.csv" if sys.platform == "win32" else "/data/out.csv"


class TestNormalizeUrl:
    def test_protocol_urls_pass_through(self) -> None:
        assert normalize_url("s3://bucket/key") == "s3://bucket/key"
        assert normalize_url("api://tables/foo") == "api://tables/foo"

    def test_absolute_path_passes_through(self) -> None:
        assert normalize_url("/data/tables/foo") == "/data/tables/foo"

    def test_tilde_expands(self) -> None:
        assert normalize_url("~/tables/foo") == f"{HOME}/tables/foo"

    def test_empty_passes_through(self) -> None:
        assert normalize_url("") == ""


class TestNormalizeLocalPath:
    def test_tilde_expands(self) -> None:
        assert normalize_local_path("~/pyro.csv") == f"{HOME}/pyro.csv"

    def test_strips_whitespace(self) -> None:
        assert normalize_local_path(f"  {OUT_CSV} ") == OUT_CSV

    def test_absolute_unchanged(self) -> None:
        assert normalize_local_path(OUT_CSV) == OUT_CSV

    def test_bare_relative_rejected(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            normalize_local_path("pyro.csv")

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            normalize_local_path("   ")


class _FakeTable:
    def __init__(self, url_columns: object) -> None:
        self._url_columns = url_columns


class TestGetUrlColumnNames:
    def test_flat(self) -> None:
        assert get_url_column_names(_FakeTable(["image"])) == ["image"]

    def test_nested(self) -> None:
        assert get_url_column_names(_FakeTable([["image", "mask"]])) == ["image", "mask"]

    def test_absent_attribute(self) -> None:
        assert get_url_column_names(object()) == []
