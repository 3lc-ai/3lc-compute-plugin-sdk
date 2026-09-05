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
- ``POST /jobs/{job_id}/cancel``   → cooperative cancel (sets ``ctx.cancelled``), escalated
                                     after a grace period (see :func:`_escalate_cancel`).
- ``POST /jobs/cancel-all``        → cancel every job still running here (the host asks
                                     after a restart it could not re-attach to).
- ``GET /busy``                    → ``{"active_jobs": n}`` for a node-agent's self-destruct guard.
- ``POST /reclaim``                → release cached GPU memory now (see
                                     :func:`release_gpu_memory`).

Job ids come from the host and are used as a directory name under the state root, so
they must match ``[A-Za-z0-9_-]{1,64}``; a run for an id that is still live answers 409.

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
import re
import sys
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from litestar import Request, Response, get, post
from litestar.exceptions import HTTPException
from litestar.response import Stream

from tlc_plugin_sdk.job_context import JobContext, JobFailed

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


_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class JobAlreadyRunning(RuntimeError):
    """A ``/jobs/{id}/run`` arrived for an id whose job is still live here."""


class _Worker:
    """Holds the single plugin instance and its in-flight jobs."""

    def __init__(self, plugin: ComputePlugin, plugin_id: str, state_root: Path) -> None:
        self.plugin = plugin
        self.plugin_id = plugin_id
        self.state_root = state_root
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()

    def start_job(self, job_id: str, params: dict[str, Any]) -> _Job:
        if not _JOB_ID_RE.match(job_id):
            # The id names a directory under the state root; anything else (``..``, a slash, a
            # 4 KB string) must not reach the filesystem.
            msg = f"job id must match {_JOB_ID_RE.pattern}, got {job_id!r}"
            raise ValueError(msg)
        state_dir = self.state_root / job_id
        job = _Job(job_id, params, state_dir, self.plugin, on_end=self.finish_job)
        with self._lock:
            live = self._jobs.get(job_id)
            if live is not None and live.is_alive():
                # Overwriting would leave the earlier thread running but uncancellable and
                # uncounted by /busy (the node could be terminated under it).
                msg = f"job {job_id!r} is still running on this worker"
                raise JobAlreadyRunning(msg)
            self._jobs[job_id] = job
        state_dir.mkdir(parents=True, exist_ok=True)
        job.start()
        return job

    def live_jobs(self) -> list[_Job]:
        """The jobs whose threads are still running."""
        with self._lock:
            return [j for j in self._jobs.values() if j.is_alive()]

    def active_jobs(self) -> int:
        """How many jobs are currently in flight (running threads)."""
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.is_alive())

    def finish_job(self, job_id: str) -> None:
        """Drop a job whose thread has ended. A job whose host stream went away but whose thread still
        runs STAYS registered — otherwise a later cancel answers 404 while the GPU keeps working
        (found live 2026-09-04: a training kept going for 40 minutes after every cancel said "unknown")."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and not job.is_alive():
                self._jobs.pop(job_id, None)

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        job.ctx.request_cancel()
        _escalate_cancel(job, self)
        return True

    def cancel_all(self) -> list[str]:
        """Cancel every job still running here (the host asks after a restart it cannot re-attach to)."""
        jobs = self.live_jobs()
        for job in jobs:
            job.ctx.request_cancel()
            _escalate_cancel(job, self)
        return [j.job_id for j in jobs]


def _cancel_grace_seconds() -> float:
    """How long a cooperative cancel may take before the worker ends itself: ``TLC_WORKER_CANCEL_GRACE_S``.

    A training loop that never looks at ``ctx.cancelled`` would otherwise run to the end on a GPU nobody
    is watching. After the grace the worker exits hard (status 3); the supervisor or node-agent spawns
    a fresh one on the next job. Non-positive disables the escalation.
    """
    raw = os.environ.get("TLC_WORKER_CANCEL_GRACE_S", "").strip()
    try:
        value = float(raw) if raw else 60.0
    except ValueError:
        logger.warning("Ignoring invalid TLC_WORKER_CANCEL_GRACE_S=%r", raw)
        value = 60.0
    return value


def _escalate_cancel(job: _Job, worker: _Worker) -> None:
    """Give the job's own code a grace period to honour the cancel; then end the process.

    The process exits (status 3) only when the stubborn job is the last live one: a worker may
    host another job or an in-flight custom route, and ending those without a terminal event
    would trade one orphan for several. When other jobs are live the stubborn thread is left
    running and reported; it is still counted by ``/busy`` and ends with the process.
    One watchdog per job: repeated cancels do not stack clocks.
    """
    grace = _cancel_grace_seconds()
    if grace <= 0 or not job.mark_escalated():
        return

    def watch() -> None:
        if job.wait(grace):
            return
        others = [j.job_id for j in worker.live_jobs() if j.job_id != job.job_id]
        if others:
            logger.error(
                "Job %s ignored its cancel for %.0fs; the worker keeps running because jobs %s are live "
                "here. The thread ends with the process.",
                job.job_id,
                grace,
                ", ".join(others),
            )
            return
        logger.error(
            "Job %s ignored its cancel for %.0fs; the worker exits so the GPU is released (a new worker is "
            "spawned for the next job)",
            job.job_id,
            grace,
        )
        logging.shutdown()
        os._exit(3)

    threading.Thread(target=watch, name=f"cancel-watch-{job.job_id}", daemon=True).start()


class _Job:
    """A single ``run_job`` invocation on a background thread, with an event queue."""

    def __init__(
        self,
        job_id: str,
        params: dict[str, Any],
        state_dir: Path,
        plugin: ComputePlugin,
        on_end: Callable[[str], None] | None = None,
    ) -> None:
        self.job_id = job_id
        self._on_end = on_end
        self._ended = threading.Event()
        self._escalated = threading.Event()
        self._abandoned = threading.Event()
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._cancel = threading.Event()
        self.ctx = JobContext(job_id, params, state_dir, sink=self._put_event, cancel_event=self._cancel)
        self._plugin = plugin
        self._thread = threading.Thread(target=self._run, name=f"job-{job_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        # An Event rather than Thread.is_alive(): the registry callback runs on the job's own
        # thread, right after the terminal event, when the thread is still technically alive.
        return not self._ended.is_set()

    def wait(self, timeout: float) -> bool:
        """True when the job ended within ``timeout`` seconds."""
        return self._ended.wait(timeout)

    def mark_escalated(self) -> bool:
        """Claim the single cancel watchdog for this job; False when one already runs."""
        if self._escalated.is_set():
            return False
        self._escalated.set()
        return True

    def abandon(self) -> None:
        """The host's stream is gone: stop buffering events nobody will read.

        Without this a lost-stream training emitting per-step progress accumulates every event
        in memory for the rest of the run. The thread keeps running and stays registered.
        """
        self._abandoned.set()
        # Drain what was queued for the consumer that left.
        try:
            while True:
                self.events.get_nowait()
        except queue.Empty:
            pass

    def _put_event(self, event: dict[str, Any]) -> None:
        if self._abandoned.is_set():
            return
        self.events.put(event)

    def _run(self) -> None:
        try:
            self._plugin.run_job(self.ctx)
            status = "cancelled" if self.ctx.cancelled else "completed"
            terminal: dict[str, Any] = {"event": "done", "status": status, "job_id": self.job_id}
        except JobFailed as exc:  # a clean, user-facing failure (ctx.fail / raise JobFailed)
            logger.info("run_job for job %s failed: %s", self.job_id, exc)
            terminal = {"event": "error", "message": str(exc), "job_id": self.job_id}
        except Exception as exc:  # any other exception: surfaced with its type prefix
            logger.exception("run_job failed for job %s", self.job_id)
            terminal = {"event": "error", "message": f"{type(exc).__name__}: {exc}", "job_id": self.job_id}
        # Reclaim *before* announcing the terminal state, not after: the host may dispatch
        # the next GPU job the moment it sees this event, and it would then be racing this
        # job's still-cached allocation. Runs for the error path too — see
        # release_gpu_memory() on why a failed job is the case that matters most.
        release_gpu_memory()
        self._put_event(terminal)
        self._ended.set()
        if self._on_end is not None:
            self._on_end(self.job_id)


def _stream_keepalive_seconds() -> float | None:
    """Keepalive cadence for the job stream, from ``TLC_WORKER_STREAM_KEEPALIVE_S``.

    ``None`` (unset/invalid/non-positive) disables keepalives — the default, and the
    unchanged local behavior. A remote worker behind a provider's HTTP proxy sets it
    (the node-agent injects ~30) because such proxies kill streams that stay silent
    longer than their idle window (~100 s on Cloudflare-fronted proxies), and a
    training epoch is routinely longer than that.
    """
    raw = os.environ.get("TLC_WORKER_STREAM_KEEPALIVE_S", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid TLC_WORKER_STREAM_KEEPALIVE_S=%r", raw)
        return None
    return value if value > 0 else None


def _control_handlers(worker: _Worker) -> list[BaseRouteHandler]:
    """The venv control channel the host supervisor drives (job lifecycle + reclaim)."""

    @post("/jobs/{job_id:str}/run")
    async def run_job(job_id: str, request: Request[Any, Any, Any]) -> Stream:
        raw = await request.body()
        params: dict[str, Any] = json.loads(raw) if raw else {}
        try:
            job = worker.start_job(job_id, params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except JobAlreadyRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        keepalive = _stream_keepalive_seconds()

        def _next_event() -> dict[str, Any] | None:
            """Block for the next job event; ``None`` = keepalive interval elapsed."""
            try:
                return job.events.get(timeout=keepalive)  # timeout=None blocks forever
            except queue.Empty:
                return None

        async def stream() -> AsyncIterator[bytes]:
            try:
                while True:
                    # abandon_on_cancel: when the host disconnects, do not wait for the next job
                    # event before the ``finally`` runs (it could be an epoch away).
                    event = await anyio.to_thread.run_sync(_next_event, abandon_on_cancel=True)
                    if event is None:
                        # Not a job event — hosts (>= the ping-aware supervisor) drop it;
                        # its only purpose is bytes on the wire inside proxy idle windows.
                        yield (json.dumps({"event": "ping", "job_id": job_id}) + "\n").encode()
                        continue
                    yield (json.dumps(event) + "\n").encode()
                    if event.get("event") in _TERMINAL:
                        break
            finally:
                if job.is_alive():
                    job.abandon()  # the thread runs on, registered and cancellable; events are dropped
                worker.finish_job(job_id)  # a no-op while the thread still runs (see finish_job)

        return Stream(stream(), media_type="application/x-ndjson")

    @post("/jobs/{job_id:str}/cancel")
    async def cancel_job(job_id: str) -> Response[dict[str, Any]]:
        ok = worker.cancel_job(job_id)
        return Response(content={"cancelling": ok, "job_id": job_id}, status_code=200 if ok else 404)

    @post("/jobs/cancel-all", status_code=200)
    async def cancel_all() -> dict[str, Any]:
        # The host asks this after a restart it could not re-attach to: whatever still runs here is
        # work nobody can see, so it is stopped (with the same escalation as a single cancel).
        return {"cancelling": worker.cancel_all()}

    @get("/busy", sync_to_thread=False)
    def busy() -> dict[str, Any]:
        # Read by a node-agent deciding whether self-destruct is safe: a worker with
        # in-flight jobs must not have its node terminated under it just because the
        # controller went quiet (a sleeping laptop mid-training).
        return {"active_jobs": worker.active_jobs()}

    @post("/reclaim")
    async def reclaim() -> Response[dict[str, Any]]:
        # Off the event loop: a collect + empty_cache pass on a large heap is not
        # instant, and this worker still has to answer /health while it runs.
        released = await anyio.to_thread.run_sync(release_gpu_memory)
        return Response(content={"released": released, "plugin_id": worker.plugin_id}, status_code=200)

    return [run_job, cancel_job, cancel_all, busy, reclaim]


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
    token: str | None = None,
) -> None:
    """Load the plugin and serve its Litestar app (blocking).

    Bind to exactly one transport: ``socket_path`` (Unix domain socket, the
    supervisor's default) or ``host`` + ``port`` (TCP). The plugin's identity comes
    from ``plugin_id`` (passed by the supervisor from the manifest), not from a class
    attribute — venv plugins carry no metadata on the instance.

    Args:
        entry: Plugin entry point as ``module:ClassName``.
        plugin_id: The plugin's identity from the manifest.
        socket_path: Unix-socket path to bind (the local default).
        host: TCP host to bind (mutually exclusive with ``socket_path``).
        port: TCP port (required with ``host``).
        state_root: Writable per-plugin state root.
        token: When set, every request must carry ``Authorization: Bearer <token>``.
            Meant for TCP workers reachable over a network (a GPU node); pointless —
            and left unset — on a Unix socket, which file permissions already guard.

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

    app = build_plugin_app(plugin, extra_handlers=_control_handlers(worker), token=token)

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
    parser.add_argument(
        "--token",
        default=None,
        help="Require 'Authorization: Bearer <token>' on every request (defaults to $TLC_WORKER_TOKEN)",
    )
    args = parser.parse_args(argv)
    serve(
        args.entry,
        args.id,
        socket_path=args.socket,
        host=args.host,
        port=args.port,
        state_root=args.state_root,
        token=args.token or os.environ.get("TLC_WORKER_TOKEN") or None,
    )


if __name__ == "__main__":
    main()
