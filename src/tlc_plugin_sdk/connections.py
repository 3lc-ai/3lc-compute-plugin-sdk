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
    credential = connections.current_credential()  # Ambient | AwsSession | SecretToken | None

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
``SECRET``
    A stored value (a Hugging Face token). Never sent in a header: the host obtains it for one job
    and puts it in the run body's host-owned :data:`CREDENTIAL_KEY`; the worker pops it and, for
    the duration of ``run_job``, exposes it as a :class:`SecretToken` from
    :func:`current_credential` (and ``ctx.credential``) and, when the provider has one, as its
    environment variable (:data:`ENV_VAR_BY_PROVIDER`: ``huggingface`` → ``HF_TOKEN``).

The header is **host-owned**: the host sets it and must strip any copy a caller sent. A request
without it resolves to nothing, and the plugin behaves exactly as before Connections.

A host may also say what the Connection is being used *for* in the host-owned
:data:`CONNECTION_USE_HEADER`: the resource the call is about (a node id) and the person it acts
for. A resolver reads it with :func:`current_use` — the AWS resolver names the role session after
the resource and stamps the person as the session's source identity, so the customer's audit
trail says which node and whose.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CONNECTION_HEADER",
    "CONNECTION_USE_HEADER",
    "CREDENTIAL_KEY",
    "ENV_VAR_BY_PROVIDER",
    "Ambient",
    "AwsSession",
    "ConnectionBinding",
    "ConnectionUse",
    "CredentialInUse",
    "CredentialUnavailable",
    "ResolvedCredential",
    "SecretToken",
    "bound_credential",
    "connection_middleware",
    "current_connection",
    "current_credential",
    "current_use",
    "encode_binding",
    "encode_use",
    "register_resolver",
    "resolve",
]

CONNECTION_HEADER = "x-3lc-connection"
"""The request header carrying a JSON-encoded :class:`ConnectionBinding` (host-owned)."""

CONNECTION_USE_HEADER = "x-3lc-connection-use"
"""The request header carrying a JSON-encoded :class:`ConnectionUse` (host-owned, optional)."""

KIND_AMBIENT = "AMBIENT"
KIND_KEYLESS = "KEYLESS"
KIND_SECRET = "SECRET"

CREDENTIAL_KEY = "_credential"
"""The host-owned top-level run-body key carrying a job's :class:`SecretToken`; popped by the worker."""

ENV_VAR_BY_PROVIDER: dict[str, str] = {"huggingface": "HF_TOKEN"}
"""The environment variable a provider's own libraries read a token from, set while a job runs."""


class CredentialUnavailable(Exception):
    """A Connection could not be resolved into a credential (unknown kind, no resolver, a refusal)."""


class CredentialInUse(RuntimeError):
    """Another job's different token is bound in this process; the job must not run with either."""


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


@dataclass(frozen=True)
class ConnectionUse:
    """What a request uses its Connection for, as the host says.

    Attributes:
        resource_id: The host's id for the resource the call is about (a node id), or ``""``.
        source_identity: Who the call acts for (the person who created the node), or ``""``.
    """

    resource_id: str = ""
    source_identity: str = ""

    @classmethod
    def from_json(cls, raw: str | bytes) -> ConnectionUse:
        """Parse the header value.

        Args:
            raw: The JSON object ``{resource_id?, source_identity?}``.

        Returns:
            The use.

        Raises:
            ValueError: When the value is not such an object.
        """
        data = json.loads(raw)
        if not isinstance(data, dict):
            msg = "a connection use is a JSON object"
            raise ValueError(msg)
        values = [data.get(k, "") for k in ("resource_id", "source_identity")]
        if not all(isinstance(v, str) for v in values):
            msg = "a connection use has string 'resource_id' and 'source_identity'"
            raise ValueError(msg)
        return cls(resource_id=str(values[0]), source_identity=str(values[1]))


def encode_use(use: ConnectionUse) -> str:
    """The header value for ``use`` (for hosts and tests).

    Args:
        use: What the request uses its Connection for.

    Returns:
        Compact JSON.
    """
    return json.dumps({"resource_id": use.resource_id, "source_identity": use.source_identity}, separators=(",", ":"))


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


@dataclass(frozen=True, repr=False)
class SecretToken:
    """A SECRET Connection's value, granted to one job.

    Attributes:
        provider: What the token is for (``huggingface``).
        secret: The value. Never in a repr.
        connection_id: The Connection it came from.
    """

    provider: str
    secret: str
    connection_id: str = ""

    def __repr__(self) -> str:
        return f"SecretToken(provider={self.provider!r}, connection_id={self.connection_id!r}, secret='***')"

    @classmethod
    def from_wire(cls, raw: object) -> SecretToken | None:
        """Build a token from the run body's :data:`CREDENTIAL_KEY` value, tolerating anything.

        Args:
            raw: ``{connection_id, provider, secret}`` as the host sends it, or anything else.

        Returns:
            The token; ``None`` unless ``raw`` is a mapping with a non-empty string ``secret``.
        """
        if not isinstance(raw, dict):
            return None
        secret, provider, connection_id = (raw.get(k) for k in ("secret", "provider", "connection_id"))
        if not isinstance(secret, str) or not secret:
            return None
        return cls(
            provider=provider if isinstance(provider, str) else "",
            secret=secret,
            connection_id=connection_id if isinstance(connection_id, str) else "",
        )


ResolvedCredential = Ambient | AwsSession | SecretToken
Resolver = Callable[[ConnectionBinding], ResolvedCredential]

_RESOLVERS: dict[tuple[str, str], Resolver] = {}
_CONNECTION: contextvars.ContextVar[ConnectionBinding | None] = contextvars.ContextVar("tlc_connection", default=None)
_CREDENTIAL: contextvars.ContextVar[ResolvedCredential | None] = contextvars.ContextVar(
    "tlc_connection_credential", default=None
)
_USE: contextvars.ContextVar[ConnectionUse | None] = contextvars.ContextVar("tlc_connection_use", default=None)


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
    """The resolved credential for the current request or job, or ``None`` when it names no Connection."""
    return _CREDENTIAL.get()


class _ProcessBinding:
    """The one token bound in this process, how many jobs hold it, and the env value it replaced."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.credential: SecretToken | None = None
        self.holders = 0
        self.previous: str | None = None


_BINDING = _ProcessBinding()


@contextlib.contextmanager
def bound_credential(credential: SecretToken | None) -> Iterator[None]:
    """Expose a job's token for the ``with`` block: :func:`current_credential` and its env var.

    The worker wraps ``run_job`` in this on the job's own thread, so the context variable is the
    job's alone. The environment variable is process-wide, so one token is bound per process at a
    time: binding the same token again nests, a different one raises :class:`CredentialInUse`
    and changes nothing. The previous value of the variable (or its absence) is restored when the
    last holder exits. ``None`` changes nothing.

    Args:
        credential: The token the host granted the job, or ``None``.

    Yields:
        Nothing.

    Raises:
        CredentialInUse: When a different token is bound in this process.
    """
    if credential is None:
        yield
        return
    var = ENV_VAR_BY_PROVIDER.get(credential.provider)
    with _BINDING.lock:
        if _BINDING.credential is not None and _BINDING.credential != credential:
            msg = (
                f"Connection {_BINDING.credential.connection_id!r} is bound in this worker; "
                f"a job for {credential.connection_id!r} cannot run until it is released"
            )
            raise CredentialInUse(msg)
        if _BINDING.holders == 0:
            _BINDING.credential = credential
            _BINDING.previous = os.environ.get(var) if var else None
            if var:
                os.environ[var] = credential.secret
        _BINDING.holders += 1
    token = _CREDENTIAL.set(credential)
    try:
        yield
    finally:
        _CREDENTIAL.reset(token)
        with _BINDING.lock:
            _BINDING.holders -= 1
            if _BINDING.holders == 0:
                if var:
                    if _BINDING.previous is None:
                        os.environ.pop(var, None)
                    else:
                        os.environ[var] = _BINDING.previous
                _BINDING.credential = None
                _BINDING.previous = None


def current_use() -> ConnectionUse | None:
    """What the current request uses its Connection for, or ``None`` when the host did not say.

    Set before the binding is resolved, so a resolver reads it too.
    """
    return _USE.get()


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

    No header: passes through untouched. A malformed or repeated header (either of
    :data:`CONNECTION_HEADER` and :data:`CONNECTION_USE_HEADER`) answers 400; a binding this
    process cannot resolve answers 424 (the request depends on a Connection it cannot use).
    Resolution runs in a worker thread (a provider resolver may call its provider), with
    :func:`current_use` already set. The resolution is set in context variables for the handler —
    including ``def`` handlers run in a thread, which receive a copy of the context — and reset
    afterwards.

    Args:
        app: The wrapped ASGI app.

    Returns:
        The wrapping ASGI app.
    """
    import anyio

    header = CONNECTION_HEADER.encode()
    use_header = CONNECTION_USE_HEADER.encode()

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
        uses = [value for name, value in scope.get("headers", []) if name == use_header]
        if len(uses) > 1:
            await _reply(send, 400, f"Send at most one {CONNECTION_USE_HEADER} header, not {len(uses)}")
            return
        try:
            use = ConnectionUse.from_json(uses[0]) if uses else None
        except ValueError as exc:
            await _reply(send, 400, f"Invalid {CONNECTION_USE_HEADER} header: {exc}")
            return
        use_token = _USE.set(use)
        try:
            context = contextvars.copy_context()
            try:
                credential = await anyio.to_thread.run_sync(context.run, resolve, binding)
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
        finally:
            _USE.reset(use_token)

    return middleware
