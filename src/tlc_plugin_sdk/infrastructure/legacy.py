# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Request-carried credentials for the legacy owner-credentials facet (LEGACY).

The demo and hosted flows put a ``credentials`` object (top-level, or under ``workspace``) and
``workspace.provider_configs`` on ``POST /infra/nodes`` and ``POST /infra/storage``: the resource
is created in the requester's own account with the keys the request brings. These are never
fields of :class:`~tlc_plugin_sdk.infrastructure.CreateNodeRequest` or
:class:`~tlc_plugin_sdk.infrastructure.CreateStorageRequest`. The SDK's route handlers strip
them from the body and, for a plugin that implements
:class:`~tlc_plugin_sdk.infrastructure.LegacyOwnerCredentialsFacet`, expose them here for the
duration of the call — the same pattern as :mod:`tlc_plugin_sdk.connections`:

.. code-block:: python

    from tlc_plugin_sdk.infrastructure import legacy

    creds = legacy.current_request_credentials()  # dict | None
    configs = legacy.current_provider_configs()  # {plugin_id: {...}}
    owner = legacy.current_request_owner()  # the caller the host acts for

A plugin without that facet is sent a 400 for such a request before its method runs: a silently
dropped ``credentials`` object would create the resource in the host's own account.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TypeGuard

from tlc_plugin_sdk.infrastructure.errors import InvalidRequest

if TYPE_CHECKING:
    from tlc_plugin_sdk.infrastructure.types import OwnerCredentialsDescriptor

__all__ = [
    "current_provider_configs",
    "current_request_credentials",
    "current_request_owner",
    "has_values",
    "request_credentials",
    "secret_credential_values",
    "strip_request_credentials",
]

_CREDENTIALS: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "tlc_legacy_credentials", default=None
)
_PROVIDER_CONFIGS: contextvars.ContextVar[dict[str, dict[str, Any]]] = contextvars.ContextVar(
    "tlc_legacy_provider_configs", default={}
)
_OWNER: contextvars.ContextVar[str] = contextvars.ContextVar("tlc_legacy_owner", default="")


def current_request_credentials() -> dict[str, Any] | None:
    """The ``credentials`` object the current request carried, or ``None`` when it carried none."""
    return _CREDENTIALS.get()


def current_provider_configs() -> dict[str, dict[str, Any]]:
    """The ``workspace.provider_configs`` the current request carried (``{}`` when none)."""
    return _PROVIDER_CONFIGS.get()


def current_request_owner() -> str:
    """The ``owner`` the current request names (the caller the host acts for), or ``""``.

    An identity, not a credential: it is also a field of the typed request
    (``CreateNodeRequest.owner``, ``CreateStorageRequest.owner``); here it sits next to the
    credentials for a provider that derives something from both (an STS external id, say).
    """
    return _OWNER.get()


@contextmanager
def request_credentials(
    credentials: dict[str, Any] | None,
    provider_configs: dict[str, dict[str, Any]] | None = None,
    *,
    owner: str = "",
) -> Iterator[None]:
    """Expose request-carried credentials (and the owner) to the plugin for the duration of the block.

    Args:
        credentials: The request's ``credentials`` object, or ``None``.
        provider_configs: The request's ``workspace.provider_configs``, or ``None``.
        owner: The request's ``owner``.
    """
    credentials_token = _CREDENTIALS.set(credentials)
    configs_token = _PROVIDER_CONFIGS.set(dict(provider_configs or {}))
    owner_token = _OWNER.set(owner)
    try:
        yield
    finally:
        _OWNER.reset(owner_token)
        _PROVIDER_CONFIGS.reset(configs_token)
        _CREDENTIALS.reset(credentials_token)


#: Credential keys whose values stay readable in an error when the descriptor does not say otherwise:
#: identifiers and places, never keys. Every other key's value is scrubbed.
_READABLE_KEYS = frozenset((
    "region",
    "location",
    "role_arn",
    "start_url",
    "tenant_id",
    "client_id",
    "subscription_id",
    "account",
    "resource_group",
))


def _marked_keys(descriptor: OwnerCredentialsDescriptor | None) -> tuple[set[str], set[str]]:
    """``(secret, readable)``: the keys the descriptor explicitly marks ``secret`` true or false."""
    secret: set[str] = set()
    readable: set[str] = set()
    if descriptor is None:
        return secret, readable
    for f in descriptor.workspace_credentials:
        (secret if f.secret else readable).add(f.key)
    marked_fields = list(descriptor.login.fields) if descriptor.login is not None else []
    if descriptor.role is not None:
        marked_fields.append(descriptor.role.field)
    for spec in marked_fields:
        if "secret" in spec and spec.get("key"):
            (secret if spec["secret"] else readable).add(str(spec["key"]))
    return secret, readable


def secret_credential_values(
    credentials: dict[str, Any] | None, descriptor: OwnerCredentialsDescriptor | None = None
) -> list[str]:
    """The values of ``credentials`` an error message must not echo — every string value but the readable ones.

    Fail-closed: a value is scrubbed unless its key is one the descriptor explicitly marks
    non-secret (``secret: False``) or is on the readable allowlist (``region``, ``location``,
    ``role_arn``, ``start_url``, ``tenant_id``, ``client_id``, ``subscription_id``, ``account``,
    ``resource_group``). A key the descriptor marks ``secret: True`` is always scrubbed.

    Args:
        credentials: The request's ``credentials`` object, or ``None``.
        descriptor: The provider's
            :meth:`~tlc_plugin_sdk.infrastructure.LegacyOwnerCredentialsFacet.credential_descriptor`,
            when it could be read.

    Returns:
        The non-empty string values of the secret keys.
    """
    if not credentials:
        return []
    secret, readable = _marked_keys(descriptor)
    return [
        v
        for k, v in credentials.items()
        if isinstance(v, str) and v and (k in secret or not (k in readable or k in _READABLE_KEYS))
    ]


def has_values(credentials: Any) -> TypeGuard[dict[str, Any]]:
    """Whether ``credentials`` is an object with at least one value that is not empty or blank (the host's rule)."""
    return isinstance(credentials, dict) and any(str(v or "").strip() for v in credentials.values())


def _filled(credentials: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in credentials.items() if str(v or "").strip()}


def strip_request_credentials(
    data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """Split a create body into the typed part and the transient legacy part.

    The ``credentials`` object may sit top-level or under ``workspace``. The one with a non-empty
    value wins, so an empty top-level object never hides a filled ``workspace.credentials``; two
    filled objects that disagree are refused. When neither has a value, the all-blank object is
    still returned as given (not ``None``): the request meant "use my account", and a provider
    must be able to refuse it rather than fall back to its own keys.

    Args:
        data: The request body.

    Returns:
        ``(body, credentials, provider_configs)``: the body without the transient keys (a copy;
        the ``workspace`` object is copied too), the ``credentials`` object (``None`` only when the
        request carries none), and ``workspace.provider_configs`` (``{}`` when none).

    Raises:
        InvalidRequest: When a ``credentials`` or ``provider_configs`` key is present but not an
            object — silently dropping it would create the resource in the host's own account —
            or when both ``credentials`` objects carry values and disagree.
    """
    body = dict(data)
    top: dict[str, Any] | None = None
    nested: dict[str, Any] | None = None
    if "credentials" in body:
        value = body.pop("credentials")
        if not isinstance(value, dict):
            msg = "'credentials' must be an object of key/value pairs"
            raise InvalidRequest(msg)
        top = value
    provider_configs: dict[str, dict[str, Any]] = {}
    workspace = body.get("workspace")
    if isinstance(workspace, dict):
        workspace = dict(workspace)
        if "credentials" in workspace:
            value = workspace.pop("credentials")
            if not isinstance(value, dict):
                msg = "'workspace.credentials' must be an object of key/value pairs"
                raise InvalidRequest(msg)
            nested = value
        if "provider_configs" in workspace:
            configs = workspace.pop("provider_configs")
            if not isinstance(configs, dict) or not all(isinstance(v, dict) for v in configs.values()):
                msg = "'workspace.provider_configs' must be an object of per-plugin objects"
                raise InvalidRequest(msg)
            provider_configs = {str(k): v for k, v in configs.items()}
        body["workspace"] = workspace
    if has_values(top) and has_values(nested) and _filled(top) != _filled(nested):
        msg = "The request carries two different credentials objects (top-level and under 'workspace'). Send one."
        raise InvalidRequest(msg)
    if has_values(top):
        return body, top, provider_configs
    if has_values(nested):
        return body, nested, provider_configs
    return body, top if top is not None else nested, provider_configs
