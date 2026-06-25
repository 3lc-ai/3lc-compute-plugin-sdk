# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Build a plugin's HTTP surface as a Litestar ASGI app.

This is the single route-authoring pattern for Layer B (transport unification): a
plugin exposes its custom routes as relative Litestar route handlers via
:meth:`ComputePlugin.get_route_handlers`, and the **same** handlers are served two
ways from the **same** builder:

- **host (in-process):** the compute service builds the app once per plugin
  instance and invokes it directly through the ASGI interface (no socket) — see
  ``plugins/endpoint.py``;
- **venv (out-of-process):** the worker (``plugin_sdk.worker``) serves the app with
  uvicorn on a Unix socket and the host reverse-proxies to it.

Because both modes serve the identical app, a plugin's routes get a real router,
request validation, multipart, and binary/streaming responses in **either** mode,
with no host/venv divergence. Litestar runs ``def`` handlers in a threadpool, so a
synchronous, CPU-bound custom route (e.g. SAM3's preview inference) does not block
the event loop.

The app also mounts the host-reserved generic routes (``/health``, ``/ui``,
``/compute``) so the worker can answer them over the socket; for the in-process host
app these are harmless (the host serves ``/ui``/``/compute`` from its own reserved
param routes and never forwards them here).

Litestar is a base dependency of ``tlc-compute`` (the SDK contract), but it is
imported **here**, not in the import-light :mod:`tlc_plugin_sdk` package
surface — so ``import tlc_plugin_sdk`` stays cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from litestar import Litestar, Request, get

if TYPE_CHECKING:
    from litestar.handlers import BaseRouteHandler

    from tlc_plugin_sdk.contract import ComputePlugin


def _generic_handlers(plugin: ComputePlugin) -> list[BaseRouteHandler]:
    """The host-reserved generic routes, bound to ``plugin`` (served by the worker)."""

    @get("/health", sync_to_thread=False)
    def health() -> dict[str, Any]:
        return {"ok": True, "plugin": getattr(plugin, "id", "?")}

    # def + sync_to_thread: get_ui_fragment()/compute() are synchronous and may do
    # blocking work, so Litestar runs them in a threadpool, off the event loop.
    @get("/ui", media_type="text/html", sync_to_thread=True)
    def ui() -> str:
        return plugin.get_ui_fragment()

    @get("/compute", sync_to_thread=True)
    def compute(request: Request[Any, Any, Any]) -> dict[str, Any]:
        params: dict[str, Any] = dict(request.query_params)
        params.setdefault("url", "")
        return plugin.compute(params)

    return [health, ui, compute]


def build_plugin_app(
    plugin: ComputePlugin,
    *,
    extra_handlers: list[BaseRouteHandler] | None = None,
    debug: bool = False,
) -> Litestar:
    """Build the Litestar app serving ``plugin``'s HTTP surface (host + venv).

    Args:
        plugin: The plugin instance whose behavior the routes invoke.
        extra_handlers: Worker-only handlers (the ``/jobs/{id}/run`` stream and
            ``/jobs/{id}/cancel``); omitted for the host in-process app, whose job
            lifecycle is owned by the host ``JobManager``.
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
