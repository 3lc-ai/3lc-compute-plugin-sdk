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

A plugin without that facet is sent a 400 for such a request before its method runs: a silently
dropped ``credentials`` object would create the resource in the host's own account.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

__all__ = [
    "current_provider_configs",
    "current_request_credentials",
    "request_credentials",
    "strip_request_credentials",
]

_CREDENTIALS: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "tlc_legacy_credentials", default=None
)
_PROVIDER_CONFIGS: contextvars.ContextVar[dict[str, dict[str, Any]]] = contextvars.ContextVar(
    "tlc_legacy_provider_configs", default={}
)


def current_request_credentials() -> dict[str, Any] | None:
    """The ``credentials`` object the current request carried, or ``None`` when it carried none."""
    return _CREDENTIALS.get()


def current_provider_configs() -> dict[str, dict[str, Any]]:
    """The ``workspace.provider_configs`` the current request carried (``{}`` when none)."""
    return _PROVIDER_CONFIGS.get()


@contextmanager
def request_credentials(
    credentials: dict[str, Any] | None, provider_configs: dict[str, dict[str, Any]] | None = None
) -> Iterator[None]:
    """Expose request-carried credentials to the plugin for the duration of the block.

    Args:
        credentials: The request's ``credentials`` object, or ``None``.
        provider_configs: The request's ``workspace.provider_configs``, or ``None``.
    """
    credentials_token = _CREDENTIALS.set(credentials)
    configs_token = _PROVIDER_CONFIGS.set(dict(provider_configs or {}))
    try:
        yield
    finally:
        _PROVIDER_CONFIGS.reset(configs_token)
        _CREDENTIALS.reset(credentials_token)


def strip_request_credentials(
    data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """Split a create body into the typed part and the transient legacy part.

    Args:
        data: The request body.

    Returns:
        ``(body, credentials, provider_configs)``: the body without the transient keys (a copy;
        the ``workspace`` object is copied too), the ``credentials`` object found top-level or
        under ``workspace`` (``None`` when none), and ``workspace.provider_configs`` (``{}`` when
        none).
    """
    body = dict(data)
    credentials: dict[str, Any] | None = None
    top = body.pop("credentials", None)
    if isinstance(top, dict):
        credentials = top
    provider_configs: dict[str, dict[str, Any]] = {}
    workspace = body.get("workspace")
    if isinstance(workspace, dict):
        workspace = dict(workspace)
        nested = workspace.pop("credentials", None)
        if credentials is None and isinstance(nested, dict):
            credentials = nested
        configs = workspace.pop("provider_configs", None)
        if isinstance(configs, dict):
            provider_configs = {str(k): v for k, v in configs.items() if isinstance(v, dict)}
        body["workspace"] = workspace
    return body, credentials, provider_configs
