# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Job-scoped connection reuse without shared cookies, authorization or request bodies.

Each provider call gets its own HTTP client/session. Only the underlying TLS
connection pool is reused, on one event loop, and closed when the job exits.
All callers still validate their endpoint before constructing a request.
"""

from __future__ import annotations

import asyncio
import contextvars
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

_ACTIVE: contextvars.ContextVar[ClientPool | None] = contextvars.ContextVar("ai_http_pool", default=None)


class _Lease(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self.transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.transport.handle_async_request(request)

    async def aclose(self) -> None:
        # Client teardown must not close other concurrent calls' connections.
        pass


class ClientPool:
    def __init__(self, *, max_connections: int = 64, max_keepalive_connections: int = 16) -> None:
        if (
            type(max_connections) is not int
            or not 1 <= max_connections <= 4096
            or type(max_keepalive_connections) is not int
            or not 0 <= max_keepalive_connections <= max_connections
        ):
            msg = "Invalid provider connection pool capacity"
            raise ValueError(msg)
        self._limits = httpx.Limits(
            max_connections=max_connections, max_keepalive_connections=max_keepalive_connections, keepalive_expiry=30
        )
        self._transports: dict[ssl.SSLContext | str | bool, httpx.AsyncBaseTransport] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    @contextmanager
    def bind(self) -> Iterator[None]:
        loop = asyncio.get_running_loop()
        if self._closed or (self._loop is not None and self._loop is not loop):
            msg = "Provider connections belong to one active job event loop"
            raise RuntimeError(msg)
        self._loop = loop
        token = _ACTIVE.set(self)
        try:
            yield
        finally:
            _ACTIVE.reset(token)

    def transport(self, verify: ssl.SSLContext | str | bool) -> httpx.AsyncBaseTransport | None:
        if self._closed or self._loop is not asyncio.get_running_loop():
            msg = "Provider connection pool is no longer available"
            raise RuntimeError(msg)
        if verify not in self._transports:
            if len(self._transports) >= 8:
                return None  # uncommon custom CAs use an isolated, self-closing client
            self._transports[verify] = httpx.AsyncHTTPTransport(
                verify=verify,
                trust_env=False,
                retries=0,
                limits=self._limits,
            )
        return _Lease(self._transports[verify])

    async def aclose(self) -> None:
        self._closed = True
        transports, self._transports = list(self._transports.values()), {}
        await asyncio.gather(*(transport.aclose() for transport in transports))


def client(
    *, transport: httpx.AsyncBaseTransport | None = None, verify: ssl.SSLContext | str | bool = True, **options: Any
) -> httpx.AsyncClient:
    """A fresh request session with job-local TLS reuse and no ambient proxy or redirects."""
    pool = _ACTIVE.get()
    if transport is None and pool is not None:
        transport = pool.transport(verify)
    options.update(follow_redirects=False, trust_env=False)
    return httpx.AsyncClient(transport=transport, verify=verify, **options)
