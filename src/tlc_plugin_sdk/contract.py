# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The compute-service plugin contract — a behavior-only base class.

A plugin is a subclass of :class:`ComputePlugin`. The base declares the
*behavioral* surface the host (or an out-of-process worker) invokes; all
*metadata* (id, name, ui placement, gpu flag, socketio namespace, …) lives in
the plugin manifest (``[tool.tlc-compute]`` in ``plugin.toml`` / ``pyproject.toml``;
see ``plugins/manifest.py``), the single source of truth. There is **no
metadata on the class** and **no** ``register()`` call at import — the host
discovers a plugin via its manifest ``entrypoint`` and hydrates the instance's
display identity (``id``/``name``/``icon``/``version``) from the card after
construction.

Only ``compute`` and ``get_ui_fragment`` are required (abstract); everything
else is optional behavior with a safe default, so a plugin implements just what
it needs and the host calls every hook directly against the inherited defaults.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tlc_plugin_sdk.job_context import JobContext


# SVG icon prefix — 16x16, currentColor, standard stroke settings.
# Handy when composing an icon literal for a manifest's ``icon_svg`` field:
#   icon_svg = _ICON_SVG + '><path d="..."/></svg>'
_ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"'  # noqa: E501


class ComputePlugin(ABC):
    """Behavior-only base class for a compute-service plugin (host or venv).

    Subclass and implement at least :meth:`compute` and :meth:`get_ui_fragment`.
    The optional hooks below ship as no-op defaults, so the host can call any of
    them directly without probing for the method first.

    Attributes:
        id: Unique slug (e.g. ``run-insights``). Identity only, hydrated onto the
            instance from the manifest by the host; the rest of the plugin's
            metadata also comes from its manifest, never from the instance.

    """

    id: str

    @abstractmethod
    def get_ui_fragment(self) -> str:
        """Return a self-contained HTML+JS+CSS fragment for the plugin UI."""
        ...

    @abstractmethod
    def compute(self, params: dict[str, Any]) -> dict[str, Any]:
        """Execute the plugin's computation and return a JSON-serializable dict."""
        ...

    # ── Optional behavior (no-op defaults) ────────────────────────────────────

    def initialise_runtime(self) -> None:
        """Initialise the plugin's runtime resources (runners, stores, models).

        Called once after the shared GPU queue is ready. Default is a no-op.
        """

    def shutdown_runtime(self) -> None:
        """Tear down the plugin's runtime resources.

        Must be safe to call on a plugin that was never initialised. Default is a
        no-op.
        """

    def run_job(self, ctx: JobContext) -> None:
        """Run a long-running job against a host-provided context.

        The plugin reports progress/metrics and polls cancellation via ``ctx``;
        the same code runs in-process (host mode) or in a worker (venv mode).

        Raises:
            NotImplementedError: The default — a plugin that streams jobs must
                override this.

        """
        plugin_id = getattr(self, "id", "?")
        msg = f"Plugin '{plugin_id}' does not implement run_job()"
        raise NotImplementedError(msg)

    # Job listing (get_active_jobs), the busy check (has_running_jobs), and cancel
    # (cancel_job) deliberately do NOT live on the plugin: the host JobManager owns
    # every job's lifecycle (it started it via run_job), so it reports active jobs
    # (GET /api/plugins/jobs), gates reload/unload (registry busy-check seam), and
    # cancels (POST /jobs/{id}/cancel). A plugin only implements run_job.

    def get_route_handlers(self) -> list[Any]:
        """Return the plugin's custom routes as relative Litestar route handlers.

        Each handler's path is **relative** to the plugin's mount point
        ``/api/plugins/{plugin_id}/`` (e.g. a ``@get("/models")`` handler serves
        ``GET /api/plugins/{plugin_id}/models``). The handlers are served by the
        plugin's own Litestar app — invoked in-process for host plugins,
        reverse-proxied to the worker for venv plugins (see
        ``tlc_plugin_sdk/asgi_app.py``); Litestar runs ``def`` handlers in a threadpool,
        so a synchronous, blocking custom route does not block the event loop. The
        reserved routes (``/run``, ``/health``, ``/ui``, ``/compute``, ``/jobs/*``)
        are host-owned — a plugin must not define them. Empty by default.
        """
        return []
