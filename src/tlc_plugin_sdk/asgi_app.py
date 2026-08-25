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

from typing import TYPE_CHECKING, Any

from litestar import Litestar, Request, get

from tlc_plugin_sdk.shared.job_tracker import JOB_TRACKER_JS
from tlc_plugin_sdk.shared.ui_inject import inject_scripts

if TYPE_CHECKING:
    from litestar.handlers import BaseRouteHandler

    from tlc_plugin_sdk.contract import ComputePlugin


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
        try:
            return inject_scripts(raw, JOB_TRACKER_JS)
        except ValueError:
            return raw

    @get("/compute", sync_to_thread=True)
    def compute(request: Request[Any, Any, Any]) -> dict[str, Any]:
        params: dict[str, Any] = dict(request.query_params)
        return plugin.compute(params)

    return [health, ui, compute]


def build_plugin_app(
    plugin: ComputePlugin,
    *,
    extra_handlers: list[BaseRouteHandler] | None = None,
    debug: bool = False,
) -> Litestar:
    """Build the Litestar app serving ``plugin``'s HTTP surface.

    Args:
        plugin: The plugin instance whose behavior the routes invoke.
        extra_handlers: The worker's job-channel handlers (the ``/jobs/{id}/run``
            stream, ``/jobs/{id}/cancel``, and ``/reclaim``).
        debug: Litestar debug flag.

    Returns:
        A Litestar app mounting, in trie-priority order: the plugin's own relative
        route handlers (most specific), the generic reserved routes, and any
        ``extra_handlers``.

    """
    handlers: list[Any] = [
        *plugin.get_route_handlers(),
        *_generic_handlers(plugin),
        *(extra_handlers or []),
    ]
    return Litestar(route_handlers=handlers, debug=debug)
