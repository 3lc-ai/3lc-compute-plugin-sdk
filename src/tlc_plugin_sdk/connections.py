# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Connections: which external account a request acts on, resolved outside the plugin's handler.

A *Connection* is an account, the access method into it and what 3LC may do there. The Config
Service stores it as a non-secret **binding**: a provider, a kind, and maps of fields the provider
declares and nothing above the plugin interprets. A host that has authorized a plugin operation on
a Connection sends the binding with the request in the :data:`CONNECTION_HEADER`; the worker
resolves it into a credential before the plugin's route handler runs and exposes both for the
duration of that one request:

.. code-block:: python

    from tlc_plugin_sdk import connections

    binding = connections.current_connection()  # ConnectionBinding | None
    credential = connections.current_credential()  # Ambient | SecretToken | your own class | None
    session = connections.credential_as(MySession)  # your resolver's class, or CredentialUnavailable

Kinds:

``AMBIENT``
    Use the machine's own identity as is (the default credential chain of the provider's SDK:
    an instance profile, a workload identity, a developer's sign-in). Resolved here, to
    :class:`Ambient`. A plugin that sees it must *not* fall back to credentials saved in its own
    settings: the Connection says which identity to use.
``KEYLESS``
    Access the customer granted: the host's own identity is exchanged for short-lived credentials
    in the customer's account. Resolved by the resolver the plugin registers with
    :func:`register_resolver`, which returns the plugin's own credential class
    (:class:`ProviderCredential`). The SDK carries no provider SDK and names no vendor.
``SECRET``
    A stored value (an API key, a Hugging Face token). Never derived from the binding: the host
    obtains it for one job and puts it in the run body's host-owned :data:`CREDENTIAL_KEY`; the
    worker pops it and, for the duration of ``run_job``, exposes it as a :class:`SecretToken` from
    :func:`current_credential` (and ``ctx.credential``). A route the plugin's manifest lists under
    ``credential_routes`` receives the same value from the host in the host-owned
    :data:`BOUND_CREDENTIAL_HEADER`; :func:`credential_middleware` binds it for that one request.
    An infrastructure call that acts on a ``SECRET`` Connection carries both: the binding in
    :data:`CONNECTION_HEADER` (so :func:`current_connection` and :func:`current_use` answer as for
    any other kind) and the value in :data:`BOUND_CREDENTIAL_HEADER`, which is what
    :func:`current_credential` returns. A value of several fields is a JSON object, read with
    :meth:`SecretToken.fields`.
    The SDK never writes the value into ``os.environ``: the environment is shared by every job and
    request in the worker, including ones acting for other people. Hand the value to the
    provider's client explicitly, or, for a tool that only reads its variables, to a subprocess's
    ``env`` (:func:`credential_environment`: ``huggingface`` → ``HF_TOKEN``, ``wandb`` →
    ``WANDB_API_KEY``, a ``kaggle`` value ``{"username", "key"}`` → ``KAGGLE_USERNAME`` +
    ``KAGGLE_KEY``).

The headers are **host-owned**: the host sets them and must strip any copy a caller sent. A request
without :data:`CONNECTION_HEADER` resolves to nothing. A request to a route that reads this host's
own identity (:data:`AMBIENT_ONLY_PATHS`, :data:`AMBIENT_ONLY_PREFIXES`) is refused when it carries
any of them, so such a handler can never observe a Connection.

A host may also say what the Connection is being used *for* in the host-owned
:data:`CONNECTION_USE_HEADER`: the resource the call is about (a node id) and the person it acts
for. A resolver receives it in its :class:`ResolveRequest`, and a handler reads it with
:func:`current_use`, so a customer's audit trail can say which node and whose.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import warnings
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

__all__ = [
    "AMBIENT_ONLY_PATHS",
    "AMBIENT_ONLY_PREFIXES",
    "BOUND_CREDENTIAL_HEADER",
    "CONNECTION_HEADER",
    "CONNECTION_TOKEN_HEADER",
    "CONNECTION_USE_HEADER",
    "CREDENTIALS_KEY",
    "CREDENTIAL_KEY",
    "ENV_VARS_FROM_JSON_BY_PROVIDER",
    "ENV_VAR_BY_PROVIDER",
    "MAX_BINDING_BYTES",
    "MAX_CREDENTIAL_BYTES",
    "MAX_STORAGE_RULES",
    "MAX_STORAGE_RULE_LENGTH",
    "MAX_TOKEN_BYTES",
    "Ambient",
    "ConnectionBinding",
    "ConnectionKind",
    "ConnectionPermissions",
    "ConnectionUse",
    "CredentialUnavailable",
    "IdentityToken",
    "ProviderCredential",
    "ResolveRequest",
    "ResolvedCredential",
    "Resolver",
    "SecretToken",
    "StorageAccessMode",
    "StorageRule",
    "bound_credential",
    "bound_credentials",
    "connection_middleware",
    "credential_as",
    "credential_environment",
    "credential_middleware",
    "current_connection",
    "current_credential",
    "current_identity_token",
    "current_use",
    "encode_binding",
    "encode_credential",
    "encode_identity_token",
    "encode_use",
    "register_resolver",
    "resolve",
    "token_audience",
]

CONNECTION_HEADER = "x-3lc-connection"
"""The request header carrying a JSON-encoded :class:`ConnectionBinding` (host-owned)."""

CONNECTION_USE_HEADER = "x-3lc-connection-use"
"""The request header carrying a JSON-encoded :class:`ConnectionUse` (host-owned, optional)."""

CONNECTION_TOKEN_HEADER = "x-3lc-connection-token"
"""The request header carrying a JSON-encoded :class:`IdentityToken` (host-owned): sent on a call for
a ``KEYLESS`` Connection whose access method declares ``identity_token``."""

MAX_TOKEN_BYTES = 8 * 1024
"""The largest :data:`CONNECTION_TOKEN_HEADER` value."""

ConnectionKind = Literal["AMBIENT", "KEYLESS", "SECRET"]
"""How a Compute Service obtains authority in an account: its own sign-in, granted access, a stored value."""

StorageAccessMode = Literal["read", "read_write_no_delete", "read_write"]
"""What a storage rule lets 3LC do at its location."""

KIND_AMBIENT: ConnectionKind = "AMBIENT"
KIND_KEYLESS: ConnectionKind = "KEYLESS"
KIND_SECRET: ConnectionKind = "SECRET"
_KINDS: tuple[str, ...] = (KIND_AMBIENT, KIND_KEYLESS, KIND_SECRET)
_ACCESS_MODES: tuple[str, ...] = ("read", "read_write_no_delete", "read_write")

MAX_BINDING_BYTES = 8 * 1024
"""The largest :data:`CONNECTION_HEADER` value. A larger one is refused with a sentence, by the host
before sending and by :func:`connection_middleware`, rather than by the HTTP parser without one."""

MAX_CREDENTIAL_BYTES = 4 * 1024
"""The largest :data:`BOUND_CREDENTIAL_HEADER` value."""

MAX_STORAGE_RULES = 20
"""How many storage rules a Connection carries at most (what keeps a binding under its limit)."""

MAX_STORAGE_RULE_LENGTH = 256
"""How long one storage rule's location is at most."""

AMBIENT_ONLY_PATHS: frozenset[str] = frozenset({"/infra/capabilities", "/infra/connection/grant"})
"""Routes that read this host's own identity: a request carrying any Connection header is refused."""

AMBIENT_ONLY_PREFIXES: tuple[str, ...] = ("/infra/dns/",)
"""Route prefixes refused like :data:`AMBIENT_ONLY_PATHS` (the launcher's DNS zone is never a customer's)."""

CREDENTIAL_KEY = "_credential"
"""The host-owned run-body key carrying one job token; popped by the worker."""
CREDENTIALS_KEY = "_credentials"
"""The host-owned run-body key carrying tokens by service; popped by the worker."""

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

T = TypeVar("T")


class CredentialUnavailable(Exception):
    """A Connection could not be turned into the credential a request needs (HTTP 424).

    Raised for a kind the plugin has no resolver for, a refused exchange, a value of the wrong
    shape, and by :func:`credential_as` when the request's credential is not the class asked for.

    Attributes:
        status: The HTTP status a route answers with.
        code: The error code a host branches on.
        remedy: What a person can do about it, in a sentence, or ``""``.
    """

    status: int = 424
    code: str = "credential_unavailable"

    def __init__(self, *args: object, remedy: str = "") -> None:
        super().__init__(*args)
        self.remedy = remedy


def _field_map(value: Any, what: str) -> dict[str, str]:
    """A ``{key: string}`` object as the binding carries it; scalars are read as their text.

    Raises:
        ValueError: When the value is not an object.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        msg = f"connection {what} must be an object"
        raise ValueError(msg)
    out: dict[str, str] = {}
    for key, item in value.items():
        if item is None or isinstance(item, (dict, list)):
            continue
        out[str(key)] = json.dumps(item) if isinstance(item, bool) else str(item)
    return out


@dataclass
class StorageRule:
    """One storage location a Connection may reach, and how.

    Attributes:
        location: A URL or URL pattern in the provider's own grammar (``s3://bucket/prefix``,
            ``abfss://container@account.dfs.core.windows.net/path``), at most
            :data:`MAX_STORAGE_RULE_LENGTH` characters.
        access: What 3LC may do there.
    """

    location: str
    access: StorageAccessMode = "read"

    @classmethod
    def from_dict(cls, data: Any) -> StorageRule:
        """Read one rule; an unreadable access mode reads as ``read``."""
        d = data if isinstance(data, Mapping) else {}
        access = d.get("access")
        location = d.get("location")
        return cls(
            location="" if location is None else str(location),
            access=access if access in _ACCESS_MODES else "read",
        )

    def to_dict(self) -> dict[str, Any]:
        """Both keys, always."""
        return {"location": self.location, "access": self.access}


@dataclass
class ConnectionPermissions:
    """What 3LC may do in the account: deployments, nodes, storage, plus provider-declared options.

    Attributes:
        deployments: 3LC may start managed deployments.
        nodes: 3LC may start nodes.
        storage: The locations 3LC may reach, at most :data:`MAX_STORAGE_RULES`. The upper bound
            on what a project root or a data location may be.
        options: Provider-declared switches, keyed as the provider's ``PermissionSchema.options``.
    """

    deployments: bool = False
    nodes: bool = False
    storage: list[StorageRule] = field(default_factory=list)
    options: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> ConnectionPermissions:
        """Read the permissions; anything else than an object reads as none."""
        d = data if isinstance(data, Mapping) else {}
        storage = d.get("storage")
        options = d.get("options")
        return cls(
            deployments=d.get("deployments") is True,
            nodes=d.get("nodes") is True,
            storage=[StorageRule.from_dict(r) for r in storage if isinstance(r, Mapping)]
            if isinstance(storage, (list, tuple))
            else [],
            options={str(k): v is True for k, v in options.items()} if isinstance(options, Mapping) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        """All four keys, always."""
        return {
            "deployments": self.deployments,
            "nodes": self.nodes,
            "storage": [rule.to_dict() for rule in self.storage],
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class ConnectionBinding:
    """The non-secret description of a Connection a request acts on.

    The binding names no vendor concept. ``account``, ``access`` and ``defaults`` are maps of
    fields the provider declares in its capabilities and reads here; the host and the Config
    Service store and pass them without interpreting them.

    Attributes:
        id: The Connection id.
        provider: The provider slug (``aws``, ``azure``, ``runpod``, …).
        kind: The access method.
        account: The account the Connection names, as the provider's ``AccountSchema.fields``.
        access: The method's non-secret fields, server-generated ones included. Never the value
            of a ``SECRET`` Connection.
        defaults: The provider's ``AccountSchema.defaults`` (a default location).
        permissions: What is installed in the account, or ``None`` before a first verification.
            Never pending editor state.
        grant_ref: The provider's reference to the installed grant
            (``GrantObservation.resource_ref``), or ``""``.
    """

    id: str
    provider: str
    kind: ConnectionKind
    account: dict[str, str] = field(default_factory=dict)
    access: dict[str, str] = field(default_factory=dict)
    defaults: dict[str, str] = field(default_factory=dict)
    permissions: ConnectionPermissions | None = None
    grant_ref: str = ""
    # ── transitional: the installed configuration in the Config Service's own shape ──
    configuration: dict[str, Any] | None = None
    # ── end transitional ──

    @classmethod
    def from_json(cls, raw: str | bytes) -> ConnectionBinding:
        """Parse the header value.

        Args:
            raw: The JSON object ``{id, provider, kind, account?, access?, defaults?, permissions?,
                grant_ref?}``.

        Returns:
            The binding.

        Raises:
            ValueError: When the value is not such an object, or names a kind that does not exist.
        """
        data = json.loads(raw)
        if not isinstance(data, dict):
            msg = "a connection binding is a JSON object"
            raise ValueError(msg)
        values = [data.get(k) for k in ("id", "provider", "kind")]
        if not all(isinstance(v, str) and v for v in values):
            msg = "a connection binding needs string 'id', 'provider' and 'kind'"
            raise ValueError(msg)
        kind = _kind(str(values[2]))
        permissions = data.get("permissions")
        if permissions is not None and not isinstance(permissions, dict):
            msg = "connection permissions must be an object"
            raise ValueError(msg)
        grant_ref = data.get("grant_ref")
        # ── transitional ──
        configuration = data.get("configuration")
        if configuration is not None and not isinstance(configuration, dict):
            msg = "connection configuration must be an object"
            raise ValueError(msg)
        # ── end transitional ──
        return cls(
            id=str(values[0]),
            provider=str(values[1]),
            kind=kind,
            account=_field_map(data.get("account"), "account"),
            access=_field_map(data.get("access"), "access"),
            defaults=_field_map(data.get("defaults"), "defaults"),
            permissions=None if permissions is None else ConnectionPermissions.from_dict(permissions),
            grant_ref=grant_ref if isinstance(grant_ref, str) else "",
            configuration=configuration,
        )

    def to_dict(self) -> dict[str, Any]:
        """The JSON object :meth:`from_json` reads; ``permissions`` is ``null`` before a first verification."""
        wire: dict[str, Any] = {
            "id": self.id,
            "provider": self.provider,
            "kind": self.kind,
            "account": dict(self.account),
            "access": dict(self.access),
            "defaults": dict(self.defaults),
            "permissions": None if self.permissions is None else self.permissions.to_dict(),
            "grant_ref": self.grant_ref,
        }
        # ── transitional ──
        if self.configuration is not None:
            wire["configuration"] = self.configuration
        # ── end transitional ──
        return wire


def _kind(value: str) -> ConnectionKind:
    text = value.upper()
    if text == KIND_AMBIENT:
        return KIND_AMBIENT
    if text == KIND_KEYLESS:
        return KIND_KEYLESS
    if text == KIND_SECRET:
        return KIND_SECRET
    msg = f"a connection kind is one of {', '.join(_KINDS)}"
    raise ValueError(msg)


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

    def to_dict(self) -> dict[str, Any]:
        """Both keys, always."""
        return {"resource_id": self.resource_id, "source_identity": self.source_identity}


def encode_use(use: ConnectionUse) -> str:
    """The header value for ``use`` (for hosts and tests).

    Args:
        use: What the request uses its Connection for.

    Returns:
        Compact JSON.
    """
    return json.dumps(use.to_dict(), separators=(",", ":"))


def encode_binding(binding: ConnectionBinding) -> str:
    """The header value for ``binding`` (for hosts and tests).

    A host checks the result against :data:`MAX_BINDING_BYTES` before sending it.

    Args:
        binding: The binding to send.

    Returns:
        Compact JSON.
    """
    return json.dumps(binding.to_dict(), separators=(",", ":"))


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
    """Use the machine's own identity (the provider SDK's default chain).

    Attributes:
        provider: The provider slug.
        account: The account the Connection names. Empty for a host's built-in own-identity
            Connection: the plugin then takes the account from its own settings.
        defaults: The Connection's defaults (a default location).
    """

    provider: str
    account: dict[str, str] = field(default_factory=dict)
    defaults: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class ProviderCredential(Protocol):
    """What a resolver returns for a ``KEYLESS`` Connection.

    The class is the provider plugin's own (session keys, an access token); the SDK holds it for
    the request and scrubs its :meth:`secret_values` from every error answer.
    """

    @property
    def expires_at(self) -> str:
        """ISO-8601 expiry, or ``""`` when the credential does not expire."""
        ...

    def secret_values(self) -> list[str]:
        """Every value an error message must never echo."""
        ...


@dataclass(frozen=True, repr=False)
class SecretToken:
    """A SECRET Connection's value, granted to one job or request.

    Attributes:
        provider: What the token is for (``huggingface``, ``runpod``).
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

    def fields(self) -> dict[str, str]:
        """A value of several fields: the JSON object whose keys are the access method's secret fields.

        Returns:
            The fields, each a non-empty string.

        Raises:
            CredentialUnavailable: When the value is not a JSON object of strings (the message
                never quotes the value).
        """
        msg = f"The {self.provider} Connection's value must be a JSON object of string fields"
        try:
            value = json.loads(self.secret)
        except ValueError:
            raise CredentialUnavailable(msg) from None
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            raise CredentialUnavailable(msg)
        return {str(k): v for k, v in value.items() if v}

    def secret_values(self) -> list[str]:
        """The value, and each of its fields when it is a JSON object of them."""
        values = [self.secret]
        try:
            values.extend(self.fields().values())
        except CredentialUnavailable:
            pass
        return values


ResolvedCredential = Ambient | SecretToken | ProviderCredential
"""What :func:`current_credential` returns for a request that names a Connection."""


@dataclass(frozen=True, repr=False)
class IdentityToken:
    """A short-lived token the host's Config Service signed for one Connection.

    A JWT signed by the Connection's issuer, with ``sub`` its subject and ``aud`` the audience the
    access method declares, living at most 10 minutes. A plugin exchanges it with its provider and
    never caches it; it is never logged and never in a repr.

    Attributes:
        connection_id: The Connection it was minted for.
        audience: Its ``aud``.
        token: The signed JWT.
        expires_at: When it expires, ISO-8601.
    """

    connection_id: str
    audience: str
    token: str
    expires_at: str

    def __repr__(self) -> str:
        return f"IdentityToken(connection_id={self.connection_id!r}, audience={self.audience!r}, token=<redacted>)"

    @classmethod
    def from_json(cls, raw: str | bytes) -> IdentityToken:
        """Parse the header value.

        Args:
            raw: The JSON object ``{connection_id, audience, token, expires_at}``.

        Returns:
            The token.

        Raises:
            ValueError: When it is not that object with four non-empty strings. The message never
                quotes the value.
        """
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        keys = ("connection_id", "audience", "token", "expires_at")
        if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data.get(k) for k in keys):
            msg = "expected a JSON object {connection_id, audience, token, expires_at} of non-empty strings"
            raise ValueError(msg)
        return cls(**{k: str(data[k]) for k in keys})


def encode_identity_token(token: IdentityToken) -> str:
    """The :data:`CONNECTION_TOKEN_HEADER` value for ``token``."""
    return json.dumps(
        {
            "connection_id": token.connection_id,
            "audience": token.audience,
            "token": token.token,
            "expires_at": token.expires_at,
        },
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class ResolveRequest:
    """What a resolver is asked to turn into a credential.

    Attributes:
        binding: The Connection's binding.
        use: What the host says the Connection is used for, or ``None``.
        identity_token: The token the host sent for this Connection, when the access method
            declares ``identity_token``; ``None`` otherwise.
    """

    binding: ConnectionBinding
    use: ConnectionUse | None = None
    identity_token: IdentityToken | None = None


Resolver = Callable[[ResolveRequest], Ambient | ProviderCredential]
"""A plugin's ``KEYLESS`` resolver. It returns :class:`Ambient` when this machine is already the
identity the Connection grants (a deployment acting on the Connection it was launched through)."""

_RESOLVERS: dict[str, Resolver] = {}
_TOKEN_AUDIENCES: dict[str, str] = {}
_IDENTITY_TOKEN: contextvars.ContextVar[IdentityToken | None] = contextvars.ContextVar(
    "tlc_identity_token", default=None
)
_CONNECTION: contextvars.ContextVar[ConnectionBinding | None] = contextvars.ContextVar("tlc_connection", default=None)
_CREDENTIAL: contextvars.ContextVar[ResolvedCredential | None] = contextvars.ContextVar(
    "tlc_connection_credential", default=None
)
_CREDENTIALS: contextvars.ContextVar[Mapping[str, SecretToken]] = contextvars.ContextVar(
    "tlc_job_credentials", default=MappingProxyType({})
)
_USE: contextvars.ContextVar[ConnectionUse | None] = contextvars.ContextVar("tlc_connection_use", default=None)


def register_resolver(provider: str, resolver: Resolver, *, token_audience: str = "") -> None:
    """Resolve ``KEYLESS`` Connections of ``provider`` with ``resolver`` in this process.

    A plugin registers its resolver at import. ``AMBIENT`` is resolved by the SDK and ``SECRET``
    arrives with the request, so neither has one.

    Args:
        provider: The provider slug.
        resolver: Called with a :class:`ResolveRequest` in a worker thread; returns the plugin's
            own credential or raises :class:`CredentialUnavailable`.
        token_audience: The ``KEYLESS`` access method's ``identity_token.audience``, when it
            declares one: every ``KEYLESS`` request of ``provider`` must then carry a
            :data:`CONNECTION_TOKEN_HEADER` for its Connection and this audience, or answers 424.
    """
    _RESOLVERS[provider] = resolver
    if token_audience:
        _TOKEN_AUDIENCES[provider] = token_audience
    else:
        _TOKEN_AUDIENCES.pop(provider, None)


def token_audience(provider: str) -> str:
    """The audience a ``KEYLESS`` request of ``provider`` needs a token for, or ``""`` for none."""
    return _TOKEN_AUDIENCES.get(provider, "")


def current_identity_token() -> IdentityToken | None:
    """The identity token the current request carries, for scrubbing; a plugin reads its resolver's."""
    return _IDENTITY_TOKEN.get()


def resolve(
    binding: ConnectionBinding, use: ConnectionUse | None = None, identity_token: IdentityToken | None = None
) -> ResolvedCredential:
    """Resolve a binding into the credential a request uses.

    Args:
        binding: The Connection's binding.
        use: What the host says the Connection is used for (default: the current request's).
        identity_token: The token the host sent for a ``KEYLESS`` Connection, if any.

    Returns:
        :class:`Ambient` for ``AMBIENT``; whatever the registered resolver returns for ``KEYLESS``.

    Raises:
        CredentialUnavailable: When no resolver handles the provider, and for a ``SECRET``
            binding: its value is not derived from the binding, the host sends it beside it.
    """
    if binding.kind == KIND_SECRET:
        msg = f"A {KIND_SECRET} Connection's value arrives with the request, in {BOUND_CREDENTIAL_HEADER}"
        raise CredentialUnavailable(msg)
    if binding.kind == KIND_AMBIENT:
        return Ambient(provider=binding.provider, account=dict(binding.account), defaults=dict(binding.defaults))
    resolver = _RESOLVERS.get(binding.provider)
    if resolver is None:
        msg = f"This plugin cannot use {binding.kind} Connections for {binding.provider}"
        raise CredentialUnavailable(msg)
    return resolver(
        ResolveRequest(binding=binding, use=use if use is not None else _USE.get(), identity_token=identity_token)
    )


def current_connection() -> ConnectionBinding | None:
    """The Connection the current request acts on, or ``None`` when it names none."""
    return _CONNECTION.get()


def current_credential(service: str | None = None) -> ResolvedCredential | None:
    """Return a service's token, or the request's or job's single credential when no service is given.

    A multi-service job has no default credential: pass the service explicitly. Without one there,
    this returns ``None`` and warns (a ``UserWarning``). An infrastructure request's credential is
    returned when called without a service.

    Args:
        service: The service slug (``huggingface``), or ``None`` for the single credential.

    Returns:
        The token for ``service``; without ``service``, the request's or job's only credential.
        ``None`` when there is none.
    """
    current = _CREDENTIAL.get()
    if service is None:
        services = _CREDENTIALS.get()
        if current is None and len(services) > 1:
            warnings.warn(
                f"current_credential() is None: this job has tokens for {len(services)} services "
                f"({', '.join(sorted(services))}). Use current_credential(service).",
                stacklevel=2,
            )
        return current
    if not service:
        return None  # a token that names no service is only ever the single credential
    if isinstance(current, SecretToken) and current.provider == service:
        return current
    return _CREDENTIALS.get().get(service)


def credential_as(cls: type[T]) -> T:
    """The request's credential as ``cls``: how a plugin reads back what its resolver returned.

    Args:
        cls: The class expected (the plugin's own credential class, :class:`Ambient`,
            :class:`SecretToken`).

    Returns:
        The credential.

    Raises:
        CredentialUnavailable: When the request has no credential, or one of another class.
    """
    current = _CREDENTIAL.get()
    if isinstance(current, cls):
        return current
    found = "no credential" if current is None else f"a {type(current).__name__}"
    msg = f"This call needs a {cls.__name__} and the request carries {found}"
    raise CredentialUnavailable(msg)


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


@contextlib.contextmanager
def bound_credentials(credentials: Mapping[str, SecretToken]) -> Iterator[None]:
    """Bind a job's service tokens for this context only, restoring both contexts on exit."""
    for service, credential in credentials.items():
        if service != credential.provider:
            msg = "A job credential does not match its service"
            raise CredentialUnavailable(msg)
        credential_environment(credential)
    snapshot = MappingProxyType(dict(credentials))
    many_token = _CREDENTIALS.set(snapshot)
    single_token = _CREDENTIAL.set(next(iter(snapshot.values())) if len(snapshot) == 1 else None)
    try:
        yield
    finally:
        _CREDENTIAL.reset(single_token)
        _CREDENTIALS.reset(many_token)


def current_use() -> ConnectionUse | None:
    """What the current request uses its Connection for, or ``None`` when the host did not say.

    Set before the binding is resolved; a resolver receives the same value in its request.
    """
    return _USE.get()


def _error(detail: str, code: str, remedy: str = "", retry_after_s: float | None = None) -> dict[str, Any]:
    """The error answer every infrastructure route and both middlewares give."""
    return {"detail": detail, "code": code, "remedy": remedy, "retry_after_s": retry_after_s}


async def _reply(
    send: Any, status: int, detail: str, code: str, remedy: str = "", retry_after_s: float | None = None
) -> None:
    body = json.dumps(_error(detail, code, remedy, retry_after_s)).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


def _secret_refusal(binding: ConnectionBinding, scope: Any) -> str:
    """Why a request's ``SECRET`` binding cannot be used with the value it carries; "" when it can.

    The binding and the value are two host-owned headers about one Connection, so they must agree:
    a value granted for another Connection or another service is never bound under this binding.
    A value that is missing or malformed is left to :func:`credential_middleware`'s own answer.
    """
    header = BOUND_CREDENTIAL_HEADER.encode()
    values = [value for name, value in scope.get("headers", []) if name == header]
    if not values:
        return f"This request names a {KIND_SECRET} Connection and carries no value for it"
    if len(values) > 1:
        return ""
    try:
        credential = SecretToken.from_wire(json.loads(values[0]))
    except ValueError:
        return ""
    if credential is None:
        return ""
    if credential.connection_id != binding.id or credential.provider != binding.provider:
        return "This request's Connection and the value it carries are for different Connections"
    return ""


def _ambient_only(path: str) -> bool:
    trimmed = path.rstrip("/") or "/"
    return trimmed in AMBIENT_ONLY_PATHS or any(path.startswith(prefix) for prefix in AMBIENT_ONLY_PREFIXES)


def _oversized(raw: bytes) -> str:
    """The sentence for a binding over :data:`MAX_BINDING_BYTES`, naming the Connection when it can be read."""
    try:
        data = json.loads(raw)
        name = str(data.get("id") or "") if isinstance(data, dict) else ""
    except ValueError:
        name = ""
    which = f"Connection {name[:80]}" if name else "This Connection"
    return (
        f"{which} is too large to send to its provider ({len(raw)} bytes; the limit is {MAX_BINDING_BYTES}). "
        "Shorten its storage rules."
    )


def connection_middleware(app: Any) -> Any:
    """ASGI middleware: resolve the request's :data:`CONNECTION_HEADER` around the handler.

    A request to a route that reads this host's own identity (:data:`AMBIENT_ONLY_PATHS`,
    :data:`AMBIENT_ONLY_PREFIXES`) answers 400 when it carries any of the three Connection
    headers. No :data:`CONNECTION_HEADER`: passes through untouched. A malformed or repeated
    header (either of :data:`CONNECTION_HEADER` and :data:`CONNECTION_USE_HEADER`), and a binding
    over :data:`MAX_BINDING_BYTES`, answer 400; a binding this process cannot resolve answers 424
    (the request depends on a Connection it cannot use), or the status of the typed error its
    resolver raised. A ``SECRET`` binding is not resolved here: its value is the request's
    :data:`BOUND_CREDENTIAL_HEADER`, which :func:`credential_middleware` binds. Such a binding
    without that header, or with one granted for another Connection or service, answers 424.
    A ``KEYLESS`` binding of a provider whose resolver was registered with a ``token_audience``
    needs one :data:`CONNECTION_TOKEN_HEADER` for that Connection and audience: without it, or with
    one for another Connection or audience, it answers 424. The token reaches the resolver as
    :attr:`ResolveRequest.identity_token` and is scrubbed from every refusal; a repeated, oversized
    or malformed one answers 400 without quoting it, as does one sent without a Connection.
    Resolution runs in a worker thread (a provider resolver may call its provider), with
    :func:`current_use` already set. The resolution is set in context variables for the handler,
    including ``def`` handlers run in a thread, which receive a copy of the context, and reset
    afterwards. Every refusal is ``{detail, code, remedy, retry_after_s}``.

    Args:
        app: The wrapped ASGI app.

    Returns:
        The wrapping ASGI app.
    """
    import anyio

    header = CONNECTION_HEADER.encode()
    use_header = CONNECTION_USE_HEADER.encode()
    token_header = CONNECTION_TOKEN_HEADER.encode()
    owned = (header, use_header, BOUND_CREDENTIAL_HEADER.encode(), token_header)

    async def middleware(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        if _ambient_only(str(scope.get("path") or "")) and any(name in owned for name, _ in headers):
            detail = "This route reads the host's own identity and is never sent a Connection"
            await _reply(send, 400, detail, "invalid_request")
            return
        values = [value for name, value in headers if name == header]
        tokens = [value for name, value in headers if name == token_header]
        if not values:
            if tokens:
                detail = f"A {CONNECTION_TOKEN_HEADER} header is sent only with a {CONNECTION_HEADER} header"
                await _reply(send, 400, detail, "invalid_request")
                return
            await app(scope, receive, send)
            return
        if len(tokens) > 1:
            await _reply(send, 400, f"Send at most one {CONNECTION_TOKEN_HEADER} header", "invalid_request")
            return
        if tokens and len(tokens[0]) > MAX_TOKEN_BYTES:
            detail = f"The {CONNECTION_TOKEN_HEADER} header is over its limit of {MAX_TOKEN_BYTES} bytes"
            await _reply(send, 400, detail, "invalid_request")
            return
        try:
            identity_token = IdentityToken.from_json(tokens[0]) if tokens else None
        except ValueError as exc:
            await _reply(send, 400, f"Invalid {CONNECTION_TOKEN_HEADER} header: {exc}", "invalid_request")
            return
        if len(values) > 1:
            await _reply(send, 400, f"Send one {CONNECTION_HEADER} header, not {len(values)}", "invalid_request")
            return
        if len(values[0]) > MAX_BINDING_BYTES:
            await _reply(send, 400, _oversized(values[0]), "invalid_request")
            return
        try:
            binding = ConnectionBinding.from_json(values[0])
        except ValueError as exc:
            await _reply(send, 400, f"Invalid {CONNECTION_HEADER} header: {exc}", "invalid_request")
            return
        uses = [value for name, value in headers if name == use_header]
        if len(uses) > 1:
            detail = f"Send at most one {CONNECTION_USE_HEADER} header, not {len(uses)}"
            await _reply(send, 400, detail, "invalid_request")
            return
        try:
            use = ConnectionUse.from_json(uses[0]) if uses else None
        except ValueError as exc:
            await _reply(send, 400, f"Invalid {CONNECTION_USE_HEADER} header: {exc}", "invalid_request")
            return
        use_token = _USE.set(use)
        try:
            if binding.kind == KIND_SECRET:
                refusal = _secret_refusal(binding, scope)
                if refusal:
                    await _reply(send, 424, refusal, CredentialUnavailable.code)
                    return
                connection_token = _CONNECTION.set(binding)
                try:
                    await app(scope, receive, send)  # the value: credential_middleware, inside this one
                finally:
                    _CONNECTION.reset(connection_token)
                return
            if binding.kind != KIND_KEYLESS:
                identity_token = None
            refusal = _token_refusal(binding, identity_token)
            if refusal:
                await _reply(send, 424, refusal, CredentialUnavailable.code)
                return
            hidden = [identity_token.token] if identity_token is not None else []
            id_token = _IDENTITY_TOKEN.set(identity_token)
            try:
                context = contextvars.copy_context()
                try:
                    credential = await anyio.to_thread.run_sync(context.run, resolve, binding, use, identity_token)
                except CredentialUnavailable as exc:
                    await _reply(send, 424, _hide(str(exc), hidden), exc.code, _hide(exc.remedy, hidden))
                    return
                except Exception as exc:
                    status, detail, code, remedy, retry = _resolver_failure(binding, exc)
                    await _reply(send, status, _hide(detail, hidden), code, _hide(remedy, hidden), retry)
                    return
                connection_token = _CONNECTION.set(binding)
                credential_token = _CREDENTIAL.set(credential)
                try:
                    await app(scope, receive, send)
                finally:
                    _CREDENTIAL.reset(credential_token)
                    _CONNECTION.reset(connection_token)
            finally:
                _IDENTITY_TOKEN.reset(id_token)
        finally:
            _USE.reset(use_token)

    return middleware


def _token_refusal(binding: ConnectionBinding, token: IdentityToken | None) -> str:
    """Why a ``KEYLESS`` binding cannot be resolved with the identity token it came with, or ``""``."""
    audience = token_audience(binding.provider) if binding.kind == KIND_KEYLESS else ""
    if not audience:
        return ""
    if token is None:
        return f"This {binding.provider} Connection needs an identity token, and the request carried none"
    if token.connection_id != binding.id:
        return f"The request's identity token was minted for another Connection than {binding.id}"
    if token.audience != audience:
        return f"The request's identity token is for another audience than {audience}"
    return ""


def _hide(text: str, hidden: list[str]) -> str:
    for value in hidden:
        if value:
            text = text.replace(value, "***")
    return text


def _resolver_failure(binding: ConnectionBinding, exc: Exception) -> tuple[int, str, str, str, float | None]:
    """``(status, detail, code, remedy, retry_after_s)`` for another exception a resolver raised.

    A typed provider error keeps its status, code, sentence and remedy. Anything else is a 424
    that names the Connection and the exception's class, never its text: no credential exists yet
    whose values could be scrubbed from it.
    """
    from tlc_plugin_sdk.infrastructure.errors import ProviderError

    if isinstance(exc, ProviderError):
        return exc.status, str(exc).strip() or type(exc).__name__, exc.code, exc.remedy, exc.retry_after_s
    detail = f"The {binding.provider} Connection {binding.id} could not be resolved ({type(exc).__name__})"
    return 424, detail, CredentialUnavailable.code, "", None


def credential_middleware(app: Any) -> Any:
    """ASGI middleware: bind the request's :data:`BOUND_CREDENTIAL_HEADER` around the handler.

    The host sends the header on a route the plugin's manifest lists under ``credential_routes``,
    after the Config Service granted the person's chosen Connection to this plugin, and on an
    infrastructure call that acts on a ``SECRET`` Connection. No header: passes through untouched.
    A repeated header, one over :data:`MAX_CREDENTIAL_BYTES`, or one that is not
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
            detail = f"Send one {BOUND_CREDENTIAL_HEADER} header, not {len(values)}"
            await _reply(send, 400, detail, "invalid_request")
            return
        if len(values[0]) > MAX_CREDENTIAL_BYTES:
            detail = f"The {BOUND_CREDENTIAL_HEADER} header is over its limit of {MAX_CREDENTIAL_BYTES} bytes"
            await _reply(send, 400, detail, "invalid_request")
            return
        try:
            raw = json.loads(values[0])
        except ValueError:
            raw = None
        credential = SecretToken.from_wire(raw)
        if credential is None:
            detail = f"Invalid {BOUND_CREDENTIAL_HEADER} header: not a granted credential"
            await _reply(send, 400, detail, "invalid_request")
            return
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(bound_credential(credential))
            except CredentialUnavailable as exc:
                await _reply(send, 424, str(exc), exc.code, exc.remedy)
                return
            await app(scope, receive, send)

    return middleware
