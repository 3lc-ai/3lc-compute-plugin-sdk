# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The shared data-source widget names a Connection on its bucket calls.

A host with a Config Service refuses a storage call that names no Connection, so the
widget must keep the ``connection_id`` the ``/api/infra/storage`` listing tags each
bucket with, and forward it to the provider's ``/list`` route. These are substring
pins on the shipped JS, in the spirit of ``test_js_contract_surface``: they break
loudly if the plumbing is dropped in a rewrite.
"""

from __future__ import annotations

from tlc_plugin_sdk.shared.data_source_ui import DATA_SOURCE_UI_JS


def test_locations_keep_the_connection_tag() -> None:
    assert "connection_id: s.connection_id || ''" in DATA_SOURCE_UI_JS
    assert "connection_name: s.connection_name || ''" in DATA_SOURCE_UI_JS


def test_bucket_browse_names_its_connection() -> None:
    assert "'&connection_id=' + encodeURIComponent(_loc.connection_id)" in DATA_SOURCE_UI_JS
