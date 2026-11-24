# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Errors an infrastructure plugin raises, and the HTTP answer each one becomes.

A provider method raises one of these with the provider's own sentence; the SDK's route layer
answers the class's status with ``{"detail", "code", "remedy", "retry_after_s"}``, scrubbed of
every value in :meth:`~tlc_plugin_sdk.infrastructure.InfrastructurePlugin.secret_values`, of the
request's credential and of the request's own secrets. ``detail`` is the sentence a person reads;
``code`` is what a host branches on, never the words of a sentence; ``remedy`` says what a person
can do about it. Any other exception answers 502 with ``code = "provider_error"`` and the sentence
:meth:`~tlc_plugin_sdk.infrastructure.InfrastructurePlugin.describe_error` makes of it, never an
opaque 500: the host writes the sentence on the node record.

- :class:`InvalidRequest`: 400, ``invalid_request``. The host reports it.
- :class:`PermissionDenied`: 403, ``permission_denied``. The host reports it; permanent until granted.
- :class:`NotFound`: 404, ``not_found``. The host reports it.
- :class:`NotConfigured`: 409, ``not_configured``. The host reports it.
- :class:`Conflict`: 409, ``conflict``. The host reports it.
- ``CredentialUnavailable``: 424, ``credential_unavailable``. The host reports it.
- :class:`CredentialExpired`: 424, ``credential_expired``. The host asks for a fresh grant and
  repeats the call once.
- :class:`RateLimited`: 429, ``rate_limited``. The host repeats the call after ``retry_after_s``,
  at most three times.
- :class:`NotSupported`: 501, ``not_supported``. The host reports it.
- :class:`ProviderError`: 502, ``provider_error``. The host reports it.

``CredentialUnavailable`` lives in :mod:`tlc_plugin_sdk.connections`, where a resolver raises it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = [
    "ERROR_CODES",
    "Conflict",
    "CredentialExpired",
    "InvalidRequest",
    "NotConfigured",
    "NotFound",
    "NotSupported",
    "PermissionDenied",
    "ProviderError",
    "RateLimited",
    "code_for_status",
    "error_body",
    "scrub",
]

#: A value shorter than this is not scrubbed: no key or token is that short, and replacing a
#: one-letter "secret" everywhere would mangle every word of the sentence.
_MIN_SECRET_LEN = 4

#: Every ``code`` an error answer carries.
ERROR_CODES: tuple[str, ...] = (
    "invalid_request",
    "permission_denied",
    "not_found",
    "not_configured",
    "conflict",
    "credential_unavailable",
    "credential_expired",
    "rate_limited",
    "not_supported",
    "provider_error",
)

_CODE_BY_STATUS: dict[int, str] = {
    400: "invalid_request",
    403: "permission_denied",
    404: "not_found",
    409: "conflict",
    424: "credential_unavailable",
    429: "rate_limited",
    501: "not_supported",
}


def scrub(text: str, secrets: Iterable[str]) -> str:
    """``text`` with every value in ``secrets`` (of :data:`_MIN_SECRET_LEN` characters or more) replaced by ``***``.

    Args:
        text: The message.
        secrets: The values that must not appear in it.

    Returns:
        The scrubbed message.
    """
    for value in sorted({s for s in secrets if s and len(s) >= _MIN_SECRET_LEN}, key=len, reverse=True):
        text = text.replace(value, "***")
    return text


def code_for_status(status: int) -> str:
    """The ``code`` of an answer that only has a status (a plugin's own HTTP error, a malformed body).

    Args:
        status: The HTTP status.

    Returns:
        The matching code; ``provider_error`` for a status no class answers.
    """
    return _CODE_BY_STATUS.get(status, "provider_error")


def error_body(detail: str, code: str, remedy: str = "", retry_after_s: float | None = None) -> dict[str, Any]:
    """The JSON object of an error answer.

    Args:
        detail: The sentence a person reads.
        code: One of :data:`ERROR_CODES`.
        remedy: What a person can do about it, or ``""``.
        retry_after_s: When a repeat may succeed, or ``None``.

    Returns:
        ``{"detail", "code", "remedy", "retry_after_s"}``, all four always.
    """
    return {"detail": detail, "code": code, "remedy": remedy, "retry_after_s": retry_after_s}


class ProviderError(Exception):
    """The provider answered something a person must read (HTTP 502).

    Attributes:
        status: The HTTP status the route answers with.
        code: What a host branches on.
        remedy: What a person can do about it, in a sentence; may be empty.
        retry_after_s: Seconds after which a repeat may succeed, or ``None``.
    """

    status: int = 502
    code: str = "provider_error"

    def __init__(self, *args: object, remedy: str = "", retry_after_s: float | None = None) -> None:
        super().__init__(*args)
        self.remedy = remedy
        self.retry_after_s = retry_after_s


class InvalidRequest(ProviderError):
    """A bad id, url, mode or name (HTTP 400). The remedy says what to change in the request."""

    status = 400
    code = "invalid_request"


class PermissionDenied(ProviderError):
    """The account refused the call (HTTP 403). Permanent until granted.

    The remedy says which permission is missing and where it is granted.
    """

    status = 403
    code = "permission_denied"


class NotFound(ProviderError):
    """No such node, deployment, storage, transfer or bundle (HTTP 404). The detail names what is gone."""

    status = 404
    code = "not_found"


class NotConfigured(ProviderError):
    """No key yet, no size configured, a corrupt settings file (HTTP 409).

    The remedy says which setting or setup step is missing.
    """

    status = 409
    code = "not_configured"


class Conflict(ProviderError):
    """The request clashes with what is there (HTTP 409): a bucket that still holds data, a DNS name already taken.

    The remedy says what is in the way.
    """

    status = 409
    code = "conflict"


class CredentialExpired(ProviderError):
    """A credential that was valid when the call started stopped being so during it (HTTP 424).

    The host asks for a fresh grant and repeats the call once, which is safe because every create
    is idempotent.
    """

    status = 424
    code = "credential_expired"


class RateLimited(ProviderError):
    """The provider is throttling (HTTP 429). Raise it with ``retry_after_s``; the host repeats the call."""

    status = 429
    code = "rate_limited"


class NotSupported(ProviderError):
    """An operation the provider does not implement (HTTP 501)."""

    status = 501
    code = "not_supported"
