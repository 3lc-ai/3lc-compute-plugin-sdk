# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""``JobContext`` — the surface a plugin's ``run_job`` programs against.

This is the v2 job-execution contract from ``docs/plugin-isolation.md``. A plugin
implements ``run_job(ctx)`` and only ever touches ``ctx`` — it never grabs the
host GPU queue or polls a shared ``cancel_flag``. The same object is used in both
``host`` and ``venv`` modes; only the **sink** (where emitted events go) and the
**cancel signal** differ:

- ``venv`` mode: the worker harness gives a sink that enqueues events for the
  streamed control-channel response, and a ``threading.Event`` set by the worker's
  ``/cancel`` endpoint.
- ``host`` mode (later): the host gives a sink that relays straight to SocketIO and
  a cancel event tied to the host queue.

Import-light: stdlib only. Must not pull in the server stack.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


class JobContext:
    """Host-provided context a plugin uses to drive one job.

    Args:
        job_id: Unique id for this job.
        params: Job parameters (parsed request body / query).
        state_dir: Writable per-plugin scratch dir that survives a venv
            reinstall/reload (plugins must not write inside their package dir).
        sink: Callable invoked with each emitted event dict.
        cancel_event: Set by the host/worker to request cooperative cancellation.

    """

    def __init__(
        self,
        job_id: str,
        params: dict[str, Any],
        state_dir: Path,
        *,
        sink: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event,
    ) -> None:
        self.job_id = job_id
        self.params = params or {}
        self.state_dir = state_dir
        self._sink = sink
        self._cancel = cancel_event

    # ── plugin-facing API ────────────────────────────────────────────────
    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested (poll this at checkpoints)."""
        return self._cancel.is_set()

    def progress(self, *, percent: float, label: str = "", timing: dict[str, Any] | None = None) -> None:
        """Report progress (0-100) with an optional label and timing dict."""
        self._emit({"event": "progress", "percent": percent, "label": label, "timing": timing})

    def metric(self, label: str, value: str | float) -> None:
        """Report a scalar metric as a key/value card."""
        self._emit({"event": "metric", "label": label, "value": value})

    def log(self, message: str) -> None:
        """Emit a log line for the job."""
        self._emit({"event": "log", "message": message})

    def result(self, *, run_url: str) -> None:
        """Record the job's result link (e.g. the created table/run URL).

        The host stores it on the generic job record so the Queue & Progress
        panel can render it as an "open result" link; safe to call multiple times
        (last write wins). Use this for the one canonical artifact the job
        produced — richer per-plugin output still goes through :meth:`emit`.

        Args:
            run_url: URL of the artifact the job produced.

        """
        self._emit({"event": "result", "run_url": run_url})

    def emit(self, name: str, payload: dict[str, Any] | None = None) -> None:
        """Emit a custom, plugin-defined event for the plugin's OWN rich UI.

        The host relays it verbatim on the plugin's SocketIO namespace; the
        generic Queue & Progress panel ignores it. Use :meth:`progress` /
        :meth:`metric` / :meth:`log` for the generic panel, and this for
        plugin-specific UI (e.g. a training plugin's per-epoch loss curve) — so a
        plugin never opens its own SocketIO connection and stays host/venv
        portable (same ``run_job`` in either mode).

        Args:
            name: Event name the plugin's UI listens for.
            payload: JSON-serializable event body.

        Raises:
            ValueError: If ``name`` collides with a host-reserved event
                (``job_update``) used for the generic Queue & Progress channel.

        """
        if name == "job_update":
            msg = "'job_update' is reserved for the generic job channel; choose a different event name"
            raise ValueError(msg)
        self._emit({"event": "custom", "name": name, "payload": payload or {}})

    # ── host/worker-facing internals ─────────────────────────────────────
    def _emit(self, event: dict[str, Any]) -> None:
        event.setdefault("job_id", self.job_id)
        self._sink(event)

    def request_cancel(self) -> None:
        """Request cooperative cancellation (host/worker side)."""
        self._cancel.set()
