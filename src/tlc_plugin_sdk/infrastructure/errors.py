# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Errors an infrastructure plugin raises, and the HTTP status each one answers with.

A provider method raises one of these with the provider's own sentence; the SDK's route layer
turns it into ``{"detail": "<sentence>"}`` with the class's status, scrubbed of every value in
:meth:`~tlc_plugin_sdk.infrastructure.InfrastructurePlugin.secret_values`. Any other exception
answers 502 with its message, never an opaque 500: the host writes the sentence on the node record.
"""

from __future__ import annotations

__all__ = ["InvalidRequest", "NotConfigured", "NotFound", "NotSupported", "ProviderError"]


class ProviderError(Exception):
    """The provider answered something a person must read (HTTP 502).

    Attributes:
        status: The HTTP status the route answers with.
    """

    status: int = 502


class InvalidRequest(ProviderError):
    """A bad id, url, mode or name, or a half credential pair (HTTP 400)."""

    status = 400


class NotConfigured(ProviderError):
    """No key yet, no size configured, a corrupt settings file (HTTP 409)."""

    status = 409


class NotFound(ProviderError):
    """No such node, storage, transfer or bundle (HTTP 404)."""

    status = 404


class NotSupported(ProviderError):
    """A facet method the provider left at its default (HTTP 501)."""

    status = 501
