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
    :func:`current_credential` (and ``ctx.credential``). A route the plugin's manifest lists under
    ``credential_routes`` receives the same value from the host in the host-owned
    :data:`BOUND_CREDENTIAL_HEADER`; :func:`credential_middleware` binds it for that one request.
    The SDK never writes the value into ``os.environ``: the environment is shared by every job and
    request in the worker, including ones acting for other people. Hand the value to the
    provider's client explicitly, or, for a tool that only reads its variables, to a subprocess's
    ``env`` (:func:`credential_environment`: ``huggingface`` → ``HF_TOKEN``, ``wandb`` →
    ``WANDB_API_KEY``, a ``kaggle`` value ``{"username", "key"}`` → ``KAGGLE_USERNAME`` +
    ``KAGGLE_KEY``).

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
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BOUND_CREDENTIAL_HEADER",
    "CONNECTION_HEADER",
    "CONNECTION_USE_HEADER",
    "CREDENTIAL_KEY",
    "ENV_VARS_FROM_JSON_BY_PROVIDER",
    "ENV_VAR_BY_PROVIDER",
    "Ambient",
    "AwsSession",
    "ConnectionBinding",
    "ConnectionUse",
    "CredentialUnavailable",
    "ResolvedCredential",
    "SecretToken",
    "bound_credential",
    "connection_middleware",
    "credential_environment",
    "credential_middleware",
    "current_connection",
    "current_credential",
    "current_use",
    "encode_binding",
    "encode_credential",
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

BOUND_CREDENTIAL_HEADER = "x-tlc-bound-credential"
"""The request header carrying a route's :class:`SecretToken` (host-owned): the JSON object
``{connection_id, provider, secret}``, the same shape as the run body's :data:`CREDENTIAL_KEY`."""

ENV_VAR_BY_PROVIDER: dict[str, str] = {"huggingface": "HF_TOKEN", "wandb": "WANDB_API_KEY"}
"""The environment variable a provider's own libraries read a token from (:func:`credential_environment`).

Never set by the SDK: a plugin passes it to a subprocess it starts for that job or request."""

ENV_VARS_FROM_JSON_BY_PROVIDER: dict[str, dict[str, str]] = {
    "kaggle": {"username": "KAGGLE_USERNAME", "key": "KAGGLE_KEY"},
}
"""Providers whose value is a JSON object: each field → its environment variable (:func:`credential_environment`).

Every listed field must be a non-empty string in the value; anything else is
:class:`CredentialUnavailable` when the credential is bound (so a job or route fails early)."""


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


def encode_credential(credential: SecretToken) -> str:
    """The :data:`BOUND_CREDENTIAL_HEADER` value for ``credential`` (for hosts and tests).

    Args:
        credential: The token to send.

    Returns:
        Compact JSON ``{connection_id, provider, secret}``.
    """
    wire = {"connection_id": credential.connection_id, "provider": credential.provider, "secret": credential.secret}
    return json.dumps(wire, separators=(",", ":"))


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


def credential_environment(credential: SecretToken) -> dict[str, str]:
    """The environment variables a provider's tools read ``credential`` from, by name.

    The SDK never sets them in its own process (``os.environ`` is shared by every job and request
    in the worker). Pass them to a subprocess that acts for this one job or request:
    ``subprocess.run(cmd, env={**os.environ, **credential_environment(token)})``.

    Args:
        credential: The token.

    Returns:
        ``{}`` for a provider with no variable; one entry for :data:`ENV_VAR_BY_PROVIDER`; one per
        field for :data:`ENV_VARS_FROM_JSON_BY_PROVIDER`.

    Raises:
        CredentialUnavailable: When a JSON provider's value is not an object with every field a
            non-empty string (the message never quotes the value).
    """
    var = ENV_VAR_BY_PROVIDER.get(credential.provider)
    if var:
        return {var: credential.secret}
    fields = ENV_VARS_FROM_JSON_BY_PROVIDER.get(credential.provider)
    if not fields:
        return {}
    try:
        value = json.loads(credential.secret)
    except ValueError:
        value = None
    names = ", ".join(repr(name) for name in fields)
    msg = f"The {credential.provider} Connection's value must be a JSON object with string fields {names}"
    if not isinstance(value, dict):
        raise CredentialUnavailable(msg)
    environment: dict[str, str] = {}
    for name, env_var in fields.items():
        item = value.get(name)
        if not isinstance(item, str) or not item:
            raise CredentialUnavailable(msg)
        environment[env_var] = item
    return environment


@contextlib.contextmanager
def bound_credential(credential: SecretToken | None) -> Iterator[None]:
    """Expose a token for the ``with`` block as :func:`current_credential`, in this context only.

    The worker wraps ``run_job`` in this on the job's own thread, and
    :func:`credential_middleware` wraps a bound route's handler in it, so the token is that job's
    or request's alone: a context variable, which concurrent requests, other jobs and threads the
    plugin starts do not see. Nothing process-wide changes — in particular no environment
    variable — so tokens of different Connections (and work with none) run side by side in one
    worker without seeing each other's. ``None`` changes nothing.

    Args:
        credential: The token the host granted, or ``None``.

    Yields:
        Nothing.

    Raises:
        CredentialUnavailable: When the token's value does not have its provider's shape
            (:data:`ENV_VARS_FROM_JSON_BY_PROVIDER`).
    """
    if credential is None:
        yield
        return
    credential_environment(credential)  # the value's shape is checked up front, as a route's 424
    token = _CREDENTIAL.set(credential)
    try:
        yield
    finally:
        _CREDENTIAL.reset(token)


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


def credential_middleware(app: Any) -> Any:
    """ASGI middleware: bind the request's :data:`BOUND_CREDENTIAL_HEADER` around the handler.

    The host sends the header only on a route the plugin's manifest lists under
    ``credential_routes``, after the Config Service granted the person's chosen Connection to this
    plugin. No header: passes through untouched. A repeated header, or one that is not
    ``{connection_id, provider, secret}`` with a non-empty ``secret``, answers 400; a value its
    provider cannot use (:func:`credential_environment`) 424. Inside the handler,
    :func:`current_credential` is the request's own token (context-variable scoped, so isolated
    from concurrent requests and jobs, also in ``def`` handlers run in a thread), restored
    afterwards. No environment variable is set (see :func:`bound_credential`).

    Args:
        app: The wrapped ASGI app.

    Returns:
        The wrapping ASGI app.
    """
    header = BOUND_CREDENTIAL_HEADER.encode()

    async def middleware(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        values = [value for name, value in scope.get("headers", []) if name == header]
        if not values:
            await app(scope, receive, send)
            return
        if len(values) > 1:
            await _reply(send, 400, f"Send one {BOUND_CREDENTIAL_HEADER} header, not {len(values)}")
            return
        try:
            raw = json.loads(values[0])
        except ValueError:
            raw = None
        credential = SecretToken.from_wire(raw)
        if credential is None:
            await _reply(send, 400, f"Invalid {BOUND_CREDENTIAL_HEADER} header: not a granted credential")
            return
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(bound_credential(credential))
            except CredentialUnavailable as exc:
                await _reply(send, 424, str(exc))
                return
            await app(scope, receive, send)

    return middleware
