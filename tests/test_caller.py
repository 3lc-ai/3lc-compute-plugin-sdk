# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The caller the host names on a forwarded route call, read inside the handler."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from litestar import get
from litestar.testing import TestClient

from tlc_plugin_sdk.asgi_app import build_plugin_app
from tlc_plugin_sdk.caller import CALLER_HEADER, current_caller, current_profile
from tlc_plugin_sdk.contract import ComputePlugin

if TYPE_CHECKING:
    from tlc_plugin_sdk.job_context import JobContext


class _Plugin(ComputePlugin):
    def get_ui_fragment(self) -> str:
        return "<div></div>"

    def run_job(self, ctx: JobContext) -> None:
        return None

    def get_route_handlers(self) -> list[Any]:
        @get("/whoami", sync_to_thread=True)
        def whoami() -> dict[str, Any]:
            caller = current_caller()
            return {"user_id": caller.user_id, "org_id": caller.org_id, "known": caller.known}

        @get("/membership", sync_to_thread=True)
        def membership() -> dict[str, Any]:
            profile = current_profile()
            return {"role": profile.org_role, "teams": profile.team_ids, "email": profile.email}

        return [whoami, membership]


def _client() -> TestClient[Any]:
    plugin: Any = _Plugin()
    plugin.id = "p"
    return TestClient(app=build_plugin_app(plugin))


def test_a_route_reads_the_caller_the_host_named_and_nobody_otherwise() -> None:
    with _client() as client:
        named = client.get("/whoami", headers={CALLER_HEADER: json.dumps({"user_id": "u-1", "org_id": "o-1"})})
        assert named.status_code == 200
        assert named.json() == {"user_id": "u-1", "org_id": "o-1", "known": True}
        nobody = client.get("/whoami")
        assert nobody.status_code == 200
        assert nobody.json() == {"user_id": None, "org_id": None, "known": False}
        # A host that knows no one (a keyless dev build) still sends the header, with nothing in it.
        empty = client.get("/whoami", headers={CALLER_HEADER: json.dumps({"user_id": None, "org_id": None})})
        assert empty.json()["known"] is False
    # Outside any request the caller is nobody: the binding did not leak.
    assert current_caller().known is False


def test_a_header_that_is_not_an_identity_is_refused() -> None:
    with _client() as client:
        assert client.get("/whoami", headers={CALLER_HEADER: "not json"}).status_code == 400
        assert client.get("/whoami", headers={CALLER_HEADER: '["u-1"]'}).status_code == 400
        two = client.get("/whoami", headers=[(CALLER_HEADER, "{}"), (CALLER_HEADER, "{}")])
        assert two.status_code == 400


def test_profile_is_scoped_to_one_request_and_unknown_roles_grant_nothing() -> None:
    with _client() as client:
        header = json.dumps({
            "user_id": "u",
            "org_id": "o",
            "profile": {"org_role": "org_member", "team_ids": ["t", None, {"id": "fake"}], "email": "u@example.com"},
        })
        assert client.get("/membership", headers={CALLER_HEADER: header}).json() == {
            "role": "org_member",
            "teams": ["t"],
            "email": "u@example.com",
        }
        assert client.get("/membership").json() == {"role": "", "teams": [], "email": ""}
        header = json.dumps({"profile": {"org_role": "team_admin"}})
        assert client.get("/membership", headers={CALLER_HEADER: header}).json()["role"] == ""
    assert current_profile().org_role == ""
