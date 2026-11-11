# =============================================================================
# <copyright>
# Copyright (c) 2026 3LC Inc. All rights reserved.
#
# All rights are reserved. Reproduction or transmission in whole or in part, in
# any form or by any means, electronic, mechanical or otherwise, is prohibited
# without the prior written permission of the copyright owner.
# </copyright>
# =============================================================================
"""Who a plugin route is called for.

The host authenticates every request before it forwards one to the worker, and the person's bearer travels no
further: no worker, local or on a node, holds a person's token. What a route may still need is who the call is
for. The host stamps that in :data:`CALLER_HEADER`, the same ids it stamps into a job's ``_identity``
(:class:`~tlc_plugin_sdk.job_context.JobIdentity`), and :func:`current_caller` reads it inside the handler.

The header carries no authority: a plugin keys per-person state on it (settings, conversations, a spend ledger)
and decides nothing else by it. Only the host sets it; a worker never hears from anyone else.
"""

from __future__ import annotations

import contextvars
import copy
import json
from dataclasses import dataclass
from typing import Any

from tlc_plugin_sdk.job_context import JobIdentity

__all__ = [
    "CALLER_HEADER",
    "CallerProfile",
    "caller_middleware",
    "current_ai_settings",
    "current_caller",
    "current_profile",
]

#: The host-owned request header naming who a route call is for: the ``_identity`` wire shape as JSON.
CALLER_HEADER = "x-3lc-caller"

_CALLER: contextvars.ContextVar[JobIdentity | None] = contextvars.ContextVar("tlc_plugin_sdk.caller", default=None)


@dataclass(frozen=True)
class CallerProfile:
    """Optional organization membership verified by the host's Config Service.

    Requested with ``runtime.caller_profile = true``. No bearer is forwarded.
    A service's access level is separate from organization membership: being
    a team administrator does not make someone an organization administrator.
    """

    org_role: str = ""
    team_ids: tuple[str, ...] = ()
    email: str = ""


_PROFILE: contextvars.ContextVar[CallerProfile] = contextvars.ContextVar(
    "tlc_plugin_sdk.caller_profile", default=CallerProfile()
)


_AI: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("tlc_plugin_sdk.ai_settings", default=None)


def current_ai_settings() -> dict[str, Any] | None:
    """Non-secret organization policy verified by the host for an AI consumer's route."""
    return copy.deepcopy(_AI.get())


def current_profile() -> CallerProfile:
    """The host-verified membership for this route, empty when unavailable."""
    return _PROFILE.get()


def _profile(raw: object) -> CallerProfile:
    if not isinstance(raw, dict):
        return CallerProfile()
    role = raw.get("org_role")
    teams = raw.get("team_ids")
    email = raw.get("email")
    return CallerProfile(
        org_role=role if role in ("org_admin", "org_member") else "",
        team_ids=tuple(t for t in teams[:1024] if isinstance(t, str) and 0 < len(t) <= 128)
        if isinstance(teams, list)
        else (),
        email=email if isinstance(email, str) and len(email) <= 320 else "",
    )


def current_caller() -> JobIdentity:
    """The identity the host stamped on the current route call.

    Returns:
        The caller; empty outside a route call, or when the host (a keyless dev build) knows no one.

    """
    return _CALLER.get() or JobIdentity()


async def _reply(send: Any, status: int, detail: str) -> None:
    body = json.dumps({"status_code": status, "detail": detail}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def caller_middleware(app: Any) -> Any:
    """ASGI middleware: bind the request's :data:`CALLER_HEADER` around the handler.

    No header: passes through untouched, and :func:`current_caller` is empty. A repeated header, or one that is
    not a JSON object, answers 400. Inside the handler, :func:`current_caller` is the request's own identity
    (context-variable scoped, so isolated from concurrent requests and from jobs, also in ``def`` handlers run in
    a thread), restored afterwards.

    Args:
        app: The wrapped ASGI app.

    Returns:
        The wrapping ASGI app.

    """
    header = CALLER_HEADER.encode()

    async def middleware(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        values = [value for name, value in scope.get("headers", []) if name == header]
        if not values:
            await app(scope, receive, send)
            return
        if len(values) > 1:
            await _reply(send, 400, f"Send one {CALLER_HEADER} header, not {len(values)}")
            return
        try:
            raw = json.loads(values[0])
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            await _reply(send, 400, f"Invalid {CALLER_HEADER} header: not an identity")
            return
        token = _CALLER.set(JobIdentity.from_wire(raw))
        profile_token = _PROFILE.set(_profile(raw.get("profile")))
        ai_token = _AI.set(raw["ai_settings"] if isinstance(raw.get("ai_settings"), dict) else None)
        try:
            await app(scope, receive, send)
        finally:
            _AI.reset(ai_token)
            _PROFILE.reset(profile_token)
            _CALLER.reset(token)

    return middleware
