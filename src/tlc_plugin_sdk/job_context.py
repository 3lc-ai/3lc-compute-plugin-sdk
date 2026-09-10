# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""``JobContext`` — the surface a plugin's ``run_job`` programs against.

A plugin implements ``run_job(ctx)`` and only ever touches ``ctx`` — it never
grabs a host queue or polls a shared ``cancel_flag``. The **sink** (where emitted
events go) and the **cancel signal** are injected: the worker harness gives a sink
that enqueues events for the streamed control-channel response, and a
``threading.Event`` set by the worker's ``/cancel`` endpoint. Tests inject their
own sink and event and drive ``run_job`` directly.

Import-light: stdlib only. Must not pull in the server stack.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

#: The host-owned top-level run-body key that carries :class:`JobIdentity` to the worker.
#: The worker pops it before ``ctx.params`` is built; a plugin never reads or sets it.
IDENTITY_KEY = "_identity"


@dataclass(frozen=True)
class JobIdentity:
    """Who a job runs for: the tenant identity the host stamped when it started the job.

    Every field is the canonical string form of the id the host's identity provider uses
    (UUIDs in the hosted service) or ``None`` when the host did not know it — a keyless local
    dev host knows no user, and no host knows a project id yet. Plugins read this for
    attribution and for authorization decisions made on their behalf later (a credential
    lease is issued to a *job's* identity, never to a plugin); they never set it.

    Attributes:
        user_id: The user who started the job.
        org_id: The organization (tenant) the job belongs to.
        project_id: The project the job belongs to, when the host resolved one.

    """

    user_id: str | None = None
    org_id: str | None = None
    project_id: str | None = None

    @classmethod
    def from_wire(cls, raw: object) -> JobIdentity:
        """Build an identity from the run body's ``_identity`` value, tolerating anything.

        Unknown keys are ignored and non-string values read as unknown, so a host of any
        version can stamp whatever it knows and an old worker never fails a job over it.

        Args:
            raw: The value found under :data:`IDENTITY_KEY`, or ``None``.

        Returns:
            The identity; empty when ``raw`` is not a mapping.

        """
        if not isinstance(raw, dict):
            return cls()
        mapping: Mapping[str, object] = raw
        return cls(
            user_id=_str_or_none(mapping.get("user_id")),
            org_id=_str_or_none(mapping.get("org_id")),
            project_id=_str_or_none(mapping.get("project_id")),
        )

    @property
    def known(self) -> bool:
        """Whether the host stamped any identity at all."""
        return any((self.user_id, self.org_id, self.project_id))


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class JobFailed(Exception):
    """Fail the current job with a clean, user-facing message.

    Raised by :meth:`JobContext.fail`. The worker turns a ``JobFailed`` into the
    terminal ``error`` event carrying the message **verbatim** (no ``TypeName:``
    prefix); any *other* exception is reported as ``f"{type}: {exc}"``. So a plugin
    reaches for this (or ``ctx.fail``) when it has a message worth showing the user,
    and lets ordinary exceptions propagate for genuine faults.
    """


class JobContext:
    """Host-provided context a plugin uses to drive one job.

    Args:
        job_id: Unique id for this job.
        params: Job parameters (parsed request body / query).
        state_dir: Writable per-plugin scratch dir that survives a venv
            reinstall/reload (plugins must not write inside their package dir).
        sink: Callable invoked with each emitted event dict.
        cancel_event: Set by the host/worker to request cooperative cancellation.
        identity: Who the job runs for (see :class:`JobIdentity`); empty when omitted.

    """

    def __init__(
        self,
        job_id: str,
        params: dict[str, Any],
        state_dir: Path,
        *,
        sink: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event,
        identity: JobIdentity | None = None,
    ) -> None:
        self.job_id = job_id
        self.params = params or {}
        self.state_dir = state_dir
        self.identity = identity if identity is not None else JobIdentity()
        self._sink = sink
        self._cancel = cancel_event

    # ── plugin-facing API ────────────────────────────────────────────────
    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested (poll this at checkpoints)."""
        return self._cancel.is_set()

    def progress(self, *, percent: float, label: str = "", timing: dict[str, Any] | None = None) -> None:
        """Report progress with an optional label and timing dict.

        Args:
            percent: Completion 0-100. Pass ``-1`` for **indeterminate** — the
                generic panel then shows an activity indicator rather than a filled
                bar (use it when total work is unknown).
            label: Short status line for the generic progress view.
            timing: Optional ``{elapsed_s, eta_s, avg_step_s, step_label}`` dict.

        """
        self._emit({"event": "progress", "percent": percent, "label": label, "timing": timing})

    def metric(self, label: str, value: str | float) -> None:
        """Report a scalar metric as a key/value card."""
        self._emit({"event": "metric", "label": label, "value": value})

    def log(self, message: str) -> None:
        """Emit a log line for the job."""
        self._emit({"event": "log", "message": message})

    def result(self, url: str) -> None:
        """Record the job's result link — the thing the Open button opens.

        The host stores it on the generic job record so the Queue & Progress panel
        can render it as an "open result" link; safe to call multiple times (last
        write wins). Pass the one canonical artifact the job produced — a run *or* a
        table URL. Richer per-plugin output still goes through :meth:`emit`.

        Args:
            url: The URL the Open button opens (a run or a table URL).

        """
        self._emit({"event": "result", "run_url": url})

    def fail(self, message: str) -> NoReturn:
        """Fail the job with a clean, user-facing message.

        Raises :class:`JobFailed`, which the worker reports as the terminal
        ``error`` event carrying ``message`` **verbatim** — no exception-type
        prefix, unlike an ordinary exception (reported as ``f"{type}: {exc}"``).
        Use it for validation / precondition failures where the message is meant
        for the user; let ordinary exceptions propagate for genuine faults.

        Args:
            message: The failure message shown on the job's generic error card.

        Raises:
            JobFailed: Always.

        """
        raise JobFailed(message)

    def emit(self, name: str, payload: dict[str, Any] | None = None) -> None:
        """Emit a custom, plugin-defined event for the plugin's OWN rich UI.

        The host relays it verbatim on the plugin's SocketIO namespace; the
        generic Queue & Progress panel ignores it. Use :meth:`progress` /
        :meth:`metric` / :meth:`log` for the generic panel, and this for
        plugin-specific UI (e.g. a training plugin's per-epoch loss curve) — so a
        plugin never opens its own SocketIO connection; the host owns the transport.

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
