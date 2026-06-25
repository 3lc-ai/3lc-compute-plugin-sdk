# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Out-of-process plugin worker — the plugin's Litestar app served on a Unix socket.

Run by the host's worker supervisor inside the plugin's own venv::

    python -m tlc_plugin_sdk.worker --entry pkg:PluginClass --socket /run/.../id.sock

The bind transport is selectable: ``--socket`` (Unix domain socket, the default the
supervisor uses) or ``--host``/``--port`` (TCP, e.g. ``--host 127.0.0.1 --port 9100``)
for a worker reachable over the network. Exactly one of the two must be given.

The worker serves the **same** Litestar app the host runs in-process for a host
plugin (``tlc_plugin_sdk.asgi_app.build_plugin_app``): the plugin's own route handlers
plus the generic reserved routes (``/health``, ``/ui``, ``/compute``). On top of that
it adds the venv job channel the host supervisor drives:

- ``POST /jobs/{job_id}/run``      → runs ``run_job(ctx)`` on a thread; the response
                                     **streams NDJSON events** (progress/metric/log)
                                     ending in a terminal ``done``/``error`` event.
- ``POST /jobs/{job_id}/cancel``   → cooperative cancel (sets ``ctx.cancelled``).

Because the worker runs a real Litestar app, a plugin's custom routes get the same
router, validation, multipart, and binary/streaming behavior they get in host mode —
and Litestar runs synchronous ``def`` handlers in a threadpool, so CPU-bound routes
don't block the worker's event loop. Litestar + uvicorn are base ``tlc-compute``
dependencies (Litestar is also pulled transitively by ``3lc``); they are imported
here, not by the import-light :mod:`tlc_plugin_sdk` package surface.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from litestar import Request, Response, post
from litestar.response import Stream

from tlc_plugin_sdk.job_context import JobContext

if TYPE_CHECKING:
    from litestar.handlers import BaseRouteHandler

    from tlc_plugin_sdk.contract import ComputePlugin

logger = logging.getLogger(__name__)

# Terminal event names that end a streamed /run response.
_TERMINAL = ("done", "error")


class _Worker:
    """Holds the single plugin instance and its in-flight jobs."""

    def __init__(self, plugin: ComputePlugin, plugin_id: str, state_root: Path) -> None:
        self.plugin = plugin
        self.plugin_id = plugin_id
        self.state_root = state_root
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()

    def start_job(self, job_id: str, params: dict[str, Any]) -> _Job:
        state_dir = self.state_root / job_id
        state_dir.mkdir(parents=True, exist_ok=True)
        job = _Job(job_id, params, state_dir, self.plugin)
        with self._lock:
            self._jobs[job_id] = job
        job.start()
        return job

    def finish_job(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        job.ctx.request_cancel()
        return True


class _Job:
    """A single ``run_job`` invocation on a background thread, with an event queue."""

    def __init__(self, job_id: str, params: dict[str, Any], state_dir: Path, plugin: ComputePlugin) -> None:
        self.job_id = job_id
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._cancel = threading.Event()
        self.ctx = JobContext(job_id, params, state_dir, sink=self.events.put, cancel_event=self._cancel)
        self._plugin = plugin
        self._thread = threading.Thread(target=self._run, name=f"job-{job_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            self._plugin.run_job(self.ctx)
            status = "cancelled" if self.ctx.cancelled else "completed"
            self.events.put({"event": "done", "status": status, "job_id": self.job_id})
        except Exception as exc:  # surfaced to the host as a terminal error event
            logger.exception("run_job failed for job %s", self.job_id)
            self.events.put({"event": "error", "message": f"{type(exc).__name__}: {exc}", "job_id": self.job_id})


def _job_handlers(worker: _Worker) -> list[BaseRouteHandler]:
    """The venv job channel (``/jobs/{id}/run`` stream + ``/jobs/{id}/cancel``)."""

    @post("/jobs/{job_id:str}/run")
    async def run_job(job_id: str, request: Request[Any, Any, Any]) -> Stream:
        raw = await request.body()
        params: dict[str, Any] = json.loads(raw) if raw else {}
        job = worker.start_job(job_id, params)

        async def stream() -> AsyncIterator[bytes]:
            try:
                while True:
                    event = await anyio.to_thread.run_sync(job.events.get)
                    yield (json.dumps(event) + "\n").encode()
                    if event.get("event") in _TERMINAL:
                        break
            finally:
                worker.finish_job(job_id)

        return Stream(stream(), media_type="application/x-ndjson")

    @post("/jobs/{job_id:str}/cancel")
    async def cancel_job(job_id: str) -> Response[dict[str, Any]]:
        ok = worker.cancel_job(job_id)
        return Response(content={"cancelling": ok, "job_id": job_id}, status_code=200 if ok else 404)

    return [run_job, cancel_job]


def _load_plugin(entry: str) -> ComputePlugin:
    module_name, _, cls_name = entry.partition(":")
    if not cls_name:
        msg = f"--entry must be 'module:ClassName', got {entry!r}"
        raise ValueError(msg)
    module = __import__(module_name, fromlist=[cls_name])
    plugin: ComputePlugin = getattr(module, cls_name)()
    return plugin


def serve(
    entry: str,
    plugin_id: str,
    *,
    socket_path: str | None = None,
    host: str | None = None,
    port: int | None = None,
    state_root: str | None = None,
) -> None:
    """Load the plugin and serve its Litestar app (blocking).

    Bind to exactly one transport: ``socket_path`` (Unix domain socket, the
    supervisor's default) or ``host`` + ``port`` (TCP). The plugin's identity comes
    from ``plugin_id`` (passed by the supervisor from the manifest), not from a class
    attribute — venv plugins carry no metadata on the instance.

    Raises:
        ValueError: If not exactly one of ``socket_path`` or ``host``/``port`` is given.

    """
    if (socket_path is None) == (host is None):
        msg = "serve() requires exactly one of socket_path or (host & port)"
        raise ValueError(msg)
    if host is not None and port is None:
        msg = "serve() requires port when host is set"
        raise ValueError(msg)

    import uvicorn

    from tlc_plugin_sdk.asgi_app import build_plugin_app

    # Ensure the cwd is importable so a plugin laid out as a local package resolves.
    sys.path.insert(0, os.getcwd())
    plugin = _load_plugin(entry)
    plugin.id = plugin_id
    root = Path(state_root) if state_root else Path(os.getcwd()) / ".plugin-state" / plugin_id
    root.mkdir(parents=True, exist_ok=True)
    worker = _Worker(plugin, plugin_id, root)

    try:
        plugin.initialise_runtime()
    except Exception:
        logger.exception("initialise_runtime failed for plugin %s", plugin_id)

    if socket_path is not None and os.path.exists(socket_path):
        os.unlink(socket_path)

    app = build_plugin_app(plugin, extra_handlers=_job_handlers(worker))

    bind: dict[str, Any] = {"uds": socket_path} if socket_path is not None else {"host": host, "port": port}
    target = f"uds={socket_path}" if socket_path is not None else f"{host}:{port}"
    logger.info("Plugin worker '%s' serving on %s", plugin_id, target)
    uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on", **bind)).run()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tlc_plugin_sdk.worker")
    parser.add_argument("--entry", required=True, help="Plugin entry point as 'module:ClassName'")
    parser.add_argument("--socket", required=False, default=None, help="Unix socket path to serve on")
    parser.add_argument("--host", default=None, help="TCP host to bind (mutually exclusive with --socket)")
    parser.add_argument("--port", type=int, default=None, help="TCP port to bind (with --host)")
    parser.add_argument("--id", required=True, help="Plugin id (identity; from the manifest)")
    parser.add_argument("--state-root", default=None, help="Writable per-plugin state root")
    args = parser.parse_args(argv)
    serve(
        args.entry,
        args.id,
        socket_path=args.socket,
        host=args.host,
        port=args.port,
        state_root=args.state_root,
    )


if __name__ == "__main__":
    main()
