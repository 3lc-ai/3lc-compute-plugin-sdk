# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""One alias rule for tables written from a picked folder: the saved alias, the session alias, the refusal."""

from __future__ import annotations

from typing import Any

import pytest

from tlc_plugin_sdk import JobFailed
from tlc_plugin_sdk.shared import aliases

# These helpers move to 3lc and SDK 0.6 removes them; their warnings are pinned in test_moved_to_3lc.py.
pytestmark = pytest.mark.filterwarnings("ignore:tlc_plugin_sdk.shared:DeprecationWarning")


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"token": kwargs["alias_token"], "path": kwargs["image_folder"]}

    monkeypatch.setattr(aliases, "register_alias", fake)
    return calls


def _one(calls: list[dict[str, Any]]) -> tuple[str, str | None]:
    (call,) = calls
    return call["image_folder"], call["remote_path"]


@pytest.mark.parametrize(
    ("read", "source", "alias", "session", "remote"),
    [
        # Read in place, alias on the picked folder: today's behaviour.
        ("s3://b/imgs", None, None, "s3://b/imgs", None),
        ("s3://b/imgs/", "s3://b/imgs", "s3://b/imgs/", "s3://b/imgs", None),
        # Read in place, alias on a parent: rows keep the subfolders (<TOKEN>/sub/a.jpg).
        ("s3://b/imgs/sub", None, "s3://b/imgs", "s3://b/imgs", None),
        ("/data/imgs/a/b", "/data/imgs/a/b", "/data/imgs", "/data/imgs", None),
        # A node copy of the picked folder, alias on that folder.
        ("/srv/stage/imgs-1a2b", "s3://b/imgs", "s3://b/imgs", "/srv/stage/imgs-1a2b", "s3://b/imgs"),
        # A node copy that kept the layout below a parent alias.
        ("/srv/stage/imgs/sub", "s3://b/imgs/sub", "s3://b/imgs", "/srv/stage/imgs", "s3://b/imgs"),
        # The alias folder is a durable copy elsewhere, not above the picked folder.
        ("/srv/stage/x", None, "s3://b/imgs", "/srv/stage/x", "s3://b/imgs"),
        ("/data/local", "/data/local", "s3://b/mirror", "/data/local", "s3://b/mirror"),
    ],
)
def test_the_session_alias_folds_the_rows_so_the_saved_alias_resolves_them(
    registered: list[dict[str, Any]], read: str, source: str | None, alias: str | None, session: str, remote: str | None
) -> None:
    aliases.register_source_alias("proj", read, source_folder=source, alias_folder=alias, alias_token="IMGS")
    assert _one(registered) == (session, remote)
    assert registered[0]["alias_token"] == "IMGS"


def test_a_copy_that_lost_the_layout_below_a_parent_alias_is_refused_before_registering(
    registered: list[dict[str, Any]],
) -> None:
    with pytest.raises(JobFailed, match=r"<IMGS> points at s3://b/imgs, a folder above it"):
        aliases.register_source_alias(
            "proj",
            "/srv/stage/imgs-1a2b",
            source_folder="s3://b/imgs/sub",
            alias_folder="s3://b/imgs",
            alias_token="IMGS",
        )
    assert registered == []


def test_a_sibling_with_a_common_prefix_is_not_under_the_alias(registered: list[dict[str, Any]]) -> None:
    aliases.register_source_alias("proj", "s3://b/imgs2", alias_folder="s3://b/imgs", alias_token="IMGS")
    assert _one(registered) == ("s3://b/imgs2", "s3://b/imgs")


def test_tilde_folders_are_expanded_before_comparing(registered: list[dict[str, Any]]) -> None:
    import os

    aliases.register_source_alias("proj", "~/data/imgs/sub", alias_folder="~/data/imgs", alias_token="IMGS")
    assert _one(registered) == (os.path.expanduser("~/data/imgs"), None)


def test_the_token_defaults_from_the_project_and_the_root_is_passed_on(registered: list[dict[str, Any]]) -> None:
    aliases.register_source_alias("my proj", "s3://b/imgs", root_url="s3://b/root")
    assert registered[0]["alias_token"] == aliases.default_alias_token("my proj")
    assert registered[0]["root_url"] == "s3://b/root"


def test_the_run_body_supplies_the_card_values(registered: list[dict[str, Any]]) -> None:
    body = {
        "alias_enabled": True,
        "alias_token": " IMGS ",
        "alias_folder": "s3://b/imgs",
        "alias_source_folder": "s3://b/imgs/sub",
    }
    result = aliases.register_run_alias(body, "/srv/stage/imgs/sub", project_name="proj", root_url="s3://b/root")
    assert result is not None
    assert _one(registered) == ("/srv/stage/imgs", "s3://b/imgs")
    assert registered[0]["alias_token"] == "IMGS" and registered[0]["root_url"] == "s3://b/root"


def test_a_plugins_own_source_folder_wins_over_the_card(registered: list[dict[str, Any]]) -> None:
    body = {"alias_folder": "s3://b/imgs", "alias_source_folder": "s3://b/other"}
    aliases.register_run_alias(body, "/srv/stage/imgs/sub", project_name="proj", source_folder="s3://b/imgs/sub")
    assert _one(registered) == ("/srv/stage/imgs", "s3://b/imgs")


@pytest.mark.parametrize("enabled", [False, "false", "0"])
def test_an_alias_turned_off_registers_nothing(registered: list[dict[str, Any]], enabled: Any) -> None:
    assert aliases.register_run_alias({"alias_enabled": enabled}, "s3://b/imgs", project_name="proj") is None
    assert registered == []


def test_a_body_without_card_values_aliases_the_folder_read(registered: list[dict[str, Any]]) -> None:
    aliases.register_run_alias({}, "s3://b/imgs/", project_name="proj")
    assert _one(registered) == ("s3://b/imgs", None)
