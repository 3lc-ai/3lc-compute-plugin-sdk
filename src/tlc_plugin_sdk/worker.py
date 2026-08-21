# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Out-of-process plugin worker — the plugin's Litestar app served on a Unix socket.

Run by the host's worker supervisor inside the plugin's own venv::

    python -m tlc_plugin_sdk.worker --entry pkg:PluginClass --socket /run/.../id.sock

The bind transport is selectable: ``--socket`` (Unix domain socket, the default the
supervisor uses) or ``--host``/``--port`` (TCP, e.g. ``--host 127.0.0.1 --port 9100``)
for a worker reachable over the network. Exactly one of the two must be given.

The worker serves the plugin's Litestar app
(``tlc_plugin_sdk.asgi_app.build_plugin_app``): the plugin's own route handlers
plus the generic reserved routes (``/health``, ``/ui``, ``/compute``). On top of that
it adds the job channel the host supervisor drives:

- ``POST /jobs/{job_id}/run``      → runs ``run_job(ctx)`` on a thread; the response
                                     **streams NDJSON events** (progress/metric/log)
                                     ending in a terminal ``done``/``error`` event.
- ``POST /jobs/{job_id}/cancel``   → cooperative cancel (sets ``ctx.cancelled``).
- ``POST /reclaim``                → release cached GPU memory now (see
                                     :func:`release_gpu_memory`).

GPU memory is reclaimed automatically after every job, before the terminal event is
emitted. ``/reclaim`` exists because only the host can see *across* workers: this
process cannot know that another plugin's worker needs the card. The host decides
when; the worker is the only side that can act, because a CUDA allocator is
per-process and the supervisor's only in-process lever is killing the worker.

Because the worker runs a real Litestar app, a plugin's custom routes get a real
router, validation, multipart, and binary/streaming behavior —
and Litestar runs synchronous ``def`` handlers in a threadpool, so CPU-bound routes
don't block the worker's event loop. Litestar + uvicorn are base dependencies of
this SDK; they are imported here, not by the import-light :mod:`tlc_plugin_sdk`
package surface.
"""

from __future__ import annotations

import argparse
import gc
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


def release_gpu_memory() -> bool:
    """Return this process's cached-but-unused GPU memory to the driver.

    PyTorch's caching allocator keeps freed blocks for reuse instead of handing them
    back, so a finished job's peak allocation stays charged to this process until it
    exits. A worker is long-lived and serves many jobs, so without this a completed
    job keeps pinning VRAM that nothing is using, and the next GPU job — here or in
    another plugin's worker — can fail to allocate against it. A job that died of
    ``OutOfMemoryError`` is the worst case: it has the largest cache to release, and
    the natural response to that error is an immediate retry.

    ``torch`` is deliberately **not** imported. It is not an SDK dependency (only an ML
    plugin's own venv brings it), and a worker whose plugin never loaded it has nothing
    to reclaim; the module is used only if the plugin already put it in
    :data:`sys.modules`. This keeps the SDK's import-light invariant intact.

    Safe to call at any time, including while a job runs: releasing cached blocks never
    touches memory that is still referenced.

    Returns:
        True if a CUDA cache was released; False if this worker has no loaded ``torch``,
        no usable CUDA device, or the release failed.

    """
    torch = sys.modules.get("torch")
    if torch is None:
        return False
    try:
        if not torch.cuda.is_available():
            return False
        # Collect first: tensors trapped in reference cycles are not freed until the
        # collector runs, and empty_cache() cannot reclaim a block still referenced.
        gc.collect()
        torch.cuda.empty_cache()
    except Exception:
        # Reclaim is an optimisation, and this runs on the path that reports a job's
        # outcome — a failure here must never turn a completed job into a failed one.
        logger.debug("GPU memory release failed", exc_info=True)
        return False
    return True


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
            terminal: dict[str, Any] = {"event": "done", "status": status, "job_id": self.job_id}
        except Exception as exc:  # surfaced to the host as a terminal error event
            logger.exception("run_job failed for job %s", self.job_id)
            terminal = {"event": "error", "message": f"{type(exc).__name__}: {exc}", "job_id": self.job_id}
        # Reclaim *before* announcing the terminal state, not after: the host may dispatch
        # the next GPU job the moment it sees this event, and it would then be racing this
        # job's still-cached allocation. Runs for the error path too — see
        # release_gpu_memory() on why a failed job is the case that matters most.
        release_gpu_memory()
        self.events.put(terminal)


def _control_handlers(worker: _Worker) -> list[BaseRouteHandler]:
    """The venv control channel the host supervisor drives (job lifecycle + reclaim)."""

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

    @post("/reclaim")
    async def reclaim() -> Response[dict[str, Any]]:
        # Off the event loop: a collect + empty_cache pass on a large heap is not
        # instant, and this worker still has to answer /health while it runs.
        released = await anyio.to_thread.run_sync(release_gpu_memory)
        return Response(content={"released": released, "plugin_id": worker.plugin_id}, status_code=200)

    return [run_job, cancel_job, reclaim]


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

    app = build_plugin_app(plugin, extra_handlers=_control_handlers(worker))

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
