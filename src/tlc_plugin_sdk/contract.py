# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The compute-service plugin contract — a behavior-only base class.

A plugin is a subclass of :class:`ComputePlugin`. The base declares the
*behavioral* surface the host (or an out-of-process worker) invokes; all
*metadata* (id, name, ui placement, gpu flag, socketio namespace, …) lives in
the plugin manifest — a standalone ``plugin.toml`` (bare, un-namespaced keys) or,
equivalently, a ``[tool.tlc-compute]`` table in the plugin's ``pyproject.toml`` —
the single source of truth. There is **no metadata on the class** and **no**
``register()`` call at import — the host discovers a plugin via its manifest
``entrypoint`` and hydrates the instance's display identity
(``id``/``name``/``icon``/``version``) from the card after construction.

Only ``get_ui_fragment`` is required (abstract); everything else is optional
behavior with a safe default, so a plugin implements just what it needs and the
host calls every hook directly against the inherited defaults.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tlc_plugin_sdk.job_context import JobContext


class ComputePlugin(ABC):
    """Behavior-only base class for a compute-service plugin (host or venv).

    Subclass and implement at least :meth:`get_ui_fragment`. The optional hooks
    below (``compute``, ``run_job``, lifecycle, routes) ship as safe defaults, so
    the host can call any of them directly without probing for the method first.

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

    # ── Optional behavior (safe defaults) ─────────────────────────────────────

    def compute(self, params: dict[str, Any]) -> dict[str, Any]:
        """Execute the plugin's synchronous ``GET /compute`` computation.

        Override to expose a synchronous compute endpoint; the default returns an
        error dict so a plugin that only serves a UI and/or jobs need not implement
        it. Long-running work belongs in :meth:`run_job`, not here.

        Returns:
            A JSON-serializable dict. The default is
            ``{"error": f"{id} does not implement compute()"}``.

        """
        plugin_id = getattr(self, "id", "?")
        return {"error": f"{plugin_id} does not implement compute()"}

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
        the code runs in the plugin's worker and only ever touches ``ctx``.

        Raises:
            NotImplementedError: The default — a plugin that streams jobs must
                override this.

        """
        plugin_id = getattr(self, "id", "?")
        msg = f"Plugin '{plugin_id}' does not implement run_job()"
        raise NotImplementedError(msg)

    # Job listing, busy checks, and cancellation deliberately do NOT live on the
    # plugin: the host owns every job's lifecycle (it started the job via run_job),
    # so it lists, gates, and cancels. A plugin only implements run_job.

    def get_route_handlers(self) -> list[Any]:
        """Return the plugin's custom routes as relative Litestar route handlers.

        Each handler's path is **relative** to the plugin's mount point
        ``/api/plugins/{plugin_id}/`` (e.g. a ``@get("/models")`` handler serves
        ``GET /api/plugins/{plugin_id}/models``). The handlers are served by the
        plugin's own Litestar app in its worker, reverse-proxied by the host (see
        ``tlc_plugin_sdk/asgi_app.py``); Litestar runs ``def`` handlers in a threadpool,
        so a synchronous, blocking custom route does not block the event loop. The
        reserved routes (``/run``, ``/health``, ``/ui``, ``/compute``, ``/busy``,
        ``/reclaim``, ``/jobs/*`` including ``/jobs/cancel-all``, and the host admin routes
        ``/provision`` ``/reload`` ``/venv`` ``/worker/stop``) are host-owned — a plugin must
        not define them; a handler on one of them is not mounted (logged as an error).
        Empty by default.
        """
        return []
