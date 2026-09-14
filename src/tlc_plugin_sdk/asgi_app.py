# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Build a plugin's HTTP surface as a Litestar ASGI app.

This is the single route-authoring pattern: a plugin exposes its custom routes as
relative Litestar route handlers via :meth:`ComputePlugin.get_route_handlers`, and
the worker (``tlc_plugin_sdk.worker``) serves the app with uvicorn on a Unix socket
(or TCP, for a remote worker); the host reverse-proxies to it.

Because the worker runs a real Litestar app, a plugin's routes get a real router,
request validation, multipart, and binary/streaming responses. Litestar runs
``def`` handlers in a threadpool, so a synchronous, CPU-bound custom route (e.g.
preview inference) does not block the event loop.

The app also mounts the host-reserved generic routes (``/health``, ``/ui``,
``/compute``) so the worker can answer them over the socket.

Litestar is a base dependency of this SDK, but it is imported **here**, not in
the import-light :mod:`tlc_plugin_sdk` package surface — so
``import tlc_plugin_sdk`` stays cheap.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from litestar import Litestar, Request, get

from tlc_plugin_sdk.shared.catalog_table import CATALOG_MARKER, CATALOG_TABLE_JS
from tlc_plugin_sdk.shared.job_tracker import JOB_TRACKER_JS
from tlc_plugin_sdk.shared.ui_inject import inject_scripts

if TYPE_CHECKING:
    from litestar.handlers import BaseRouteHandler

    from tlc_plugin_sdk.contract import ComputePlugin


logger = logging.getLogger(__name__)


def _generic_handlers(plugin: ComputePlugin) -> list[BaseRouteHandler]:
    """The host-reserved generic routes, bound to ``plugin`` (served by the worker)."""

    @get("/health", sync_to_thread=False)
    def health() -> dict[str, Any]:
        # ``sdk_version`` is the worker half of a handshake: a venv worker imports its
        # *own* install of this SDK, so the host cannot know which contract is live inside
        # the venv unless the worker says so. The host compares it against its own on
        # MAJOR.MINOR and flags skew on the plugin card. One contract axis, one field.
        from tlc_plugin_sdk import SDK_CONTRACT_VERSION

        return {
            "ok": True,
            "plugin": getattr(plugin, "id", "?"),
            "sdk_version": SDK_CONTRACT_VERSION,
        }

    # def + sync_to_thread: get_ui_fragment()/compute() are synchronous and may do
    # blocking work, so Litestar runs them in a threadpool, off the event loop.
    @get("/ui", media_type="text/html", sync_to_thread=True)
    def ui() -> str:
        # Auto-inject the PluginJobs client so every fragment can drive the generic job
        # channel without a manual inject_scripts() call. The client is idempotent
        # (``if (window.PluginJobs) return;``), so a plugin that still injects it by hand
        # is harmless. A fragment with no inline <script> has nothing to drive PluginJobs
        # from — inject_scripts() raises there, so serve it unchanged.
        raw = plugin.get_ui_fragment()
        # The catalogue table rides along only for fragments that use it (a container with the
        # ``tlc-catalog`` class): most plugins never list provider offerings.
        scripts = [JOB_TRACKER_JS, CATALOG_TABLE_JS] if CATALOG_MARKER in raw else [JOB_TRACKER_JS]
        try:
            return inject_scripts(raw, *scripts)
        except ValueError:
            return raw

    @get("/compute", sync_to_thread=True)
    def compute(request: Request[Any, Any, Any]) -> dict[str, Any]:
        params: dict[str, Any] = dict(request.query_params)
        return plugin.compute(params)

    return [health, ui, compute]


def _bearer_guard(token: str) -> Any:
    """An ASGI middleware factory rejecting requests without ``Authorization: Bearer <token>``.

    Installed only when a token is configured — a worker on a Unix socket runs with no
    token and never pays for this. Guards every HTTP route, ``/health`` included, and every
    websocket a plugin declares (closed with code 1008 before accept). Callers that own the
    token (the controller's supervisor, the node-agent) send it on probes too.

    Litestar applies app middleware per matched route, so an unknown path still answers 404
    and a wrong method 405 without the token: the guard protects content, not the route
    table. ``lifespan`` scopes pass through.
    """
    import hmac

    expected = f"Bearer {token}".encode()

    def factory(app: Any) -> Any:
        async def guard(scope: Any, receive: Any, send: Any) -> None:
            kind = scope["type"]
            if kind not in ("http", "websocket"):
                await app(scope, receive, send)
                return
            auth = b""
            for name, value in scope.get("headers", []):
                if name == b"authorization":
                    auth = value
                    break
            # Constant-time compare: a timing oracle on the token would defeat it.
            if hmac.compare_digest(auth, expected):
                await app(scope, receive, send)
                return
            if kind == "websocket":
                # 1008 = policy violation; closing before accept makes the handshake fail.
                await send({"type": "websocket.close", "code": 1008})
                return
            body = b'{"detail":"unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})

        return guard

    return factory


def build_plugin_app(
    plugin: ComputePlugin,
    *,
    extra_handlers: list[BaseRouteHandler] | None = None,
    debug: bool = False,
    token: str | None = None,
) -> Litestar:
    """Build the Litestar app serving ``plugin``'s HTTP surface.

    Args:
        plugin: The plugin instance whose behavior the routes invoke.
        extra_handlers: The worker's job-channel handlers (the ``/jobs/{id}/run``
            stream, ``/jobs/{id}/cancel``, and ``/reclaim``).
        debug: Litestar debug flag.
        token: When set, every request must carry ``Authorization: Bearer <token>``
            (401 otherwise). Set for TCP workers on remote nodes; ``None`` (the
            default) leaves local/UDS behavior untouched — no middleware installed.

    Returns:
        A Litestar app mounting, in trie-priority order: the plugin's own relative
        route handlers (most specific), the generic reserved routes, and any
        ``extra_handlers``.

    """
    handlers: list[Any] = [
        *_without_reserved(plugin.get_route_handlers(), plugin),
        *_generic_handlers(plugin),
        *(extra_handlers or []),
    ]
    middleware: list[Any] = [_bearer_guard(token)] if token else []
    # No generated OpenAPI/Swagger routes: a worker is an internal endpoint, and on a node the
    # schema would describe the job channel to anyone who reached the port.
    return Litestar(route_handlers=handlers, debug=debug, middleware=middleware, openapi_config=None)


# Paths the host and the worker own. A plugin handler on one of them would shadow the
# worker's (``/busy`` shadowed = a node-agent's self-destruct guard answers whatever the
# plugin says). Plugins mount first for trie priority, so collisions are removed here.
RESERVED_WORKER_PATHS: frozenset[str] = frozenset({"/health", "/ui", "/compute", "/busy", "/reclaim"})
RESERVED_WORKER_PREFIXES: tuple[str, ...] = ("/jobs",)


def _without_reserved(handlers: list[Any], plugin: ComputePlugin) -> list[Any]:
    kept: list[Any] = []
    for handler in handlers:
        paths = {"/" + str(p).strip("/") for p in (getattr(handler, "paths", None) or ())}
        clash = [
            p
            for p in paths
            if p in RESERVED_WORKER_PATHS or any(p == r or p.startswith(r + "/") for r in RESERVED_WORKER_PREFIXES)
        ]
        if clash:
            logger.error(
                "Plugin %s declares reserved route(s) %s; the handler is not mounted (host-owned paths: %s, %s/*)",
                getattr(plugin, "id", "?"),
                ", ".join(sorted(clash)),
                ", ".join(sorted(RESERVED_WORKER_PATHS)),
                ", ".join(RESERVED_WORKER_PREFIXES),
            )
            continue
        kept.append(handler)
    return kept
