# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Connections: which external account a request acts on, resolved outside the plugin's handler.

A *Connection* is a managed link to an external account (an AWS account, a RunPod team, …) that
the Config Service stores as a non-secret **binding**: a provider, a kind and kind-specific
metadata. A host that has authorized a plugin operation on a Connection sends the binding with
the request in the :data:`CONNECTION_HEADER`; the worker resolves it into a credential before the
plugin's route handler runs and exposes both for the duration of that one request:

.. code-block:: python

    from tlc_plugin_sdk import connections

    binding = connections.current_connection()  # ConnectionBinding | None
    credential = connections.current_credential()  # Ambient | AwsSession | None

Kinds:

``AMBIENT``
    Use the deployment's own identity as is (the default credential chain of the provider's SDK:
    an instance profile, a workload identity, a developer's profile). Resolved here, to
    :class:`Ambient`. A plugin that sees it must *not* fall back to credentials saved in its own
    settings: the Connection says which identity to use.
``KEYLESS``
    Delegated identity (for AWS: assume the customer's role with the stored external id, using the
    deployment's own identity). Resolved by a provider resolver the plugin registers with
    :func:`register_resolver` — the SDK itself carries no provider SDK.

The header is **host-owned**: the host sets it and must strip any copy a caller sent. A request
without it resolves to nothing, and the plugin behaves exactly as before Connections.
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CONNECTION_HEADER",
    "Ambient",
    "AwsSession",
    "ConnectionBinding",
    "CredentialUnavailable",
    "ResolvedCredential",
    "connection_middleware",
    "current_connection",
    "current_credential",
    "encode_binding",
    "register_resolver",
    "resolve",
]

CONNECTION_HEADER = "x-3lc-connection"
"""The request header carrying a JSON-encoded :class:`ConnectionBinding` (host-owned)."""

KIND_AMBIENT = "AMBIENT"
KIND_KEYLESS = "KEYLESS"


class CredentialUnavailable(Exception):
    """A Connection could not be resolved into a credential (unknown kind, no resolver, a refusal)."""


@dataclass(frozen=True)
class ConnectionBinding:
    """The non-secret description of a Connection a request acts on.

    Attributes:
        id: The Connection id.
        provider: The provider slug (``aws``, ``runpod``, …).
        kind: ``AMBIENT`` or ``KEYLESS`` (more kinds later).
        metadata: Kind-specific, non-secret binding data (for ``KEYLESS`` on AWS: ``role_arn``,
            ``external_id``, optionally ``region`` and ``session_duration_s``).
    """

    id: str
    provider: str
    kind: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: str | bytes) -> ConnectionBinding:
        """Parse the header value.

        Args:
            raw: The JSON object ``{id, provider, kind, metadata?}``.

        Returns:
            The binding.

        Raises:
            ValueError: When the value is not such an object.
        """
        data = json.loads(raw)
        if not isinstance(data, dict):
            msg = "a connection binding is a JSON object"
            raise ValueError(msg)
        metadata = data.get("metadata")
        metadata = {} if metadata is None else metadata
        values = [data.get(k) for k in ("id", "provider", "kind")]
        if not all(isinstance(v, str) and v for v in values) or not isinstance(metadata, dict):
            msg = "a connection binding needs string 'id', 'provider' and 'kind', and an object 'metadata'"
            raise ValueError(msg)
        return cls(id=str(values[0]), provider=str(values[1]), kind=str(values[2]).upper(), metadata=metadata)


def encode_binding(binding: ConnectionBinding) -> str:
    """The header value for ``binding`` (for hosts and tests).

    Args:
        binding: The binding to send.

    Returns:
        Compact JSON.
    """
    return json.dumps(
        {"id": binding.id, "provider": binding.provider, "kind": binding.kind, "metadata": binding.metadata},
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class Ambient:
    """Use the deployment's own identity (the provider SDK's default chain).

    Attributes:
        provider: The provider slug.
        region: A region the binding names, or ``""``.
    """

    provider: str
    region: str = ""


@dataclass(frozen=True, repr=False)
class AwsSession:
    """Temporary AWS credentials for one request (a ``KEYLESS`` resolution).

    Attributes:
        access_key_id: ``AccessKeyId``.
        secret_access_key: ``SecretAccessKey``.
        session_token: ``SessionToken``.
        region: A region the binding names, or ``""``.
        expires_at: ISO-8601 expiry, or ``""`` when unknown.
    """

    access_key_id: str
    secret_access_key: str
    session_token: str
    region: str = ""
    expires_at: str = ""

    def __repr__(self) -> str:
        key = f"{self.access_key_id[:4]}…"
        return f"AwsSession(access_key_id={key!r}, region={self.region!r}, expires_at={self.expires_at!r})"


ResolvedCredential = Ambient | AwsSession
Resolver = Callable[[ConnectionBinding], ResolvedCredential]

_RESOLVERS: dict[tuple[str, str], Resolver] = {}
_CONNECTION: contextvars.ContextVar[ConnectionBinding | None] = contextvars.ContextVar("tlc_connection", default=None)
_CREDENTIAL: contextvars.ContextVar[ResolvedCredential | None] = contextvars.ContextVar(
    "tlc_connection_credential", default=None
)


def register_resolver(provider: str, kind: str, resolver: Resolver) -> None:
    """Resolve ``kind`` Connections of ``provider`` with ``resolver`` in this process.

    A plugin registers its provider's resolvers at import (the AWS plugin: ``KEYLESS`` → assume
    the role). ``AMBIENT`` is built in and cannot be replaced.

    Args:
        provider: The provider slug.
        kind: The Connection kind.
        resolver: Called with the binding; returns the credential or raises
            :class:`CredentialUnavailable`.

    Raises:
        ValueError: When registering ``AMBIENT``.
    """
    if kind.upper() == KIND_AMBIENT:
        msg = "AMBIENT Connections are resolved by the SDK"
        raise ValueError(msg)
    _RESOLVERS[(provider, kind.upper())] = resolver


def resolve(binding: ConnectionBinding) -> ResolvedCredential:
    """Resolve a binding into the credential a request uses.

    Args:
        binding: The Connection's binding.

    Returns:
        :class:`Ambient` for ``AMBIENT``; whatever the registered resolver returns otherwise.

    Raises:
        CredentialUnavailable: When no resolver handles the provider and kind.
    """
    if binding.kind == KIND_AMBIENT:
        return Ambient(provider=binding.provider, region=str(binding.metadata.get("region", "") or ""))
    resolver = _RESOLVERS.get((binding.provider, binding.kind))
    if resolver is None:
        msg = f"This plugin cannot use {binding.kind} Connections for {binding.provider}"
        raise CredentialUnavailable(msg)
    return resolver(binding)


def current_connection() -> ConnectionBinding | None:
    """The Connection the current request acts on, or ``None`` when it names none."""
    return _CONNECTION.get()


def current_credential() -> ResolvedCredential | None:
    """The resolved credential for the current request, or ``None`` when it names no Connection."""
    return _CREDENTIAL.get()


async def _reply(send: Any, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


def connection_middleware(app: Any) -> Any:
    """ASGI middleware: resolve the request's :data:`CONNECTION_HEADER` around the handler.

    No header: passes through untouched. A malformed or repeated header answers 400; a binding
    this process cannot resolve answers 424 (the request depends on a Connection it cannot use).
    Resolution runs in a worker thread (a provider resolver may call its provider). The
    resolution is set in context variables for the handler — including ``def`` handlers run in a
    thread, which receive a copy of the context — and reset afterwards.

    Args:
        app: The wrapped ASGI app.

    Returns:
        The wrapping ASGI app.
    """
    import anyio

    header = CONNECTION_HEADER.encode()

    async def middleware(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        values = [value for name, value in scope.get("headers", []) if name == header]
        if not values:
            await app(scope, receive, send)
            return
        if len(values) > 1:
            await _reply(send, 400, f"Send one {CONNECTION_HEADER} header, not {len(values)}")
            return
        try:
            binding = ConnectionBinding.from_json(values[0])
        except ValueError as exc:
            await _reply(send, 400, f"Invalid {CONNECTION_HEADER} header: {exc}")
            return
        try:
            credential = await anyio.to_thread.run_sync(resolve, binding)
        except CredentialUnavailable as exc:
            await _reply(send, 424, str(exc))
            return
        connection_token = _CONNECTION.set(binding)
        credential_token = _CREDENTIAL.set(credential)
        try:
            await app(scope, receive, send)
        finally:
            _CREDENTIAL.reset(credential_token)
            _CONNECTION.reset(connection_token)

    return middleware
