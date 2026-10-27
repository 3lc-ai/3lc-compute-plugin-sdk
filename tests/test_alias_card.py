# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""``parse_alias_card``: what the shared alias card put in a run body, read without registering anything.

The card's JS (``_tlcGetAliasValues``) and this parser are one contract, so the keys are pinned
against the JS source as well.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from tlc_plugin_sdk.shared.alias_ui import ALIAS_UI_JS, AliasCard, parse_alias_card


def test_a_card_filled_in_is_read_trimmed() -> None:
    body = {
        "alias_enabled": True,
        "alias_token": "  COCO ",
        "alias_folder": " s3://b/coco ",
        "alias_source_folder": "s3://b/coco/train2017",
        "unrelated": 1,
    }
    assert parse_alias_card(body) == AliasCard(
        enabled=True, token="COCO", alias_folder="s3://b/coco", source_folder="s3://b/coco/train2017"
    )


def test_a_body_without_the_card_reads_as_the_card_at_its_defaults() -> None:
    assert parse_alias_card({}) == AliasCard(enabled=True, token=None, alias_folder=None, source_folder=None)


@pytest.mark.parametrize("value", [True, "true", 1, "1"])
def test_values_that_mean_on(value: Any) -> None:
    assert parse_alias_card({"alias_enabled": value}).enabled


@pytest.mark.parametrize("value", [False, "false", 0, "0", "", None, "yes", "True"])
def test_anything_else_means_off(value: Any) -> None:
    assert not parse_alias_card({"alias_enabled": value}).enabled


def test_a_card_turned_off_still_says_what_it_held() -> None:
    """A caller that turns the alias on regardless (``enabled=True``) needs the rest."""
    card = parse_alias_card({"alias_enabled": "false", "alias_token": "X", "alias_folder": "/data"})
    assert card == AliasCard(enabled=False, token="X", alias_folder="/data", source_folder=None)


@pytest.mark.parametrize("blank", ["", "   ", None, False, 0])
def test_blank_values_read_as_none(blank: Any) -> None:
    card = parse_alias_card({"alias_token": blank, "alias_folder": blank, "alias_source_folder": blank})
    assert (card.token, card.alias_folder, card.source_folder) == (None, None, None)


def test_the_card_js_sends_the_keys_the_parser_reads() -> None:
    for key in ("alias_enabled", "alias_token", "alias_folder", "alias_source_folder"):
        assert f"    {key}:" in ALIAS_UI_JS


def test_alias_card_is_a_frozen_value() -> None:
    card = parse_alias_card({})
    with pytest.raises(AttributeError):
        card.token = "X"  # type: ignore[misc]


def test_parsing_never_imports_3lc() -> None:
    code = (
        "import sys;"
        "from tlc_plugin_sdk.shared.alias_ui import parse_alias_card;"
        "parse_alias_card({'alias_token': 'X', 'alias_folder': '/data'});"
        "sys.exit('tlc imported' if 'tlc' in sys.modules else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
