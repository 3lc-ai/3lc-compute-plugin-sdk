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

import math
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from tlc_plugin_sdk.connections import SecretToken

#: The host-owned top-level run-body key that carries :class:`JobIdentity` to the worker.
#: The worker pops it before ``ctx.params`` is built; a plugin never reads or sets it.
IDENTITY_KEY = "_identity"
#: The run-body key carrying the project root the job writes to; the host stamps it at submit.
PROJECT_ROOT_KEY = "project_root_url"


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
        credential: The legacy single token the host granted this job, or ``None``. One that
            names a service is keyed under it in ``credentials``; one that names none is only
            ``ctx.credential`` (and no-argument ``connections.current_credential()``).
        credentials: Tokens keyed by service; copied into a read-only map. Read one with
            ``get_credential(service)``. ``ctx.credential`` is the only token of a single-token
            job, and ``None`` (with a warning) for several. Token values never belong in saved
            parameters or output.

    ``project_root_url`` (a property) is the root the job writes to — see there.

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
        credential: SecretToken | None = None,
        credentials: Mapping[str, SecretToken] | None = None,
        host_request: Callable[[str, dict[str, Any], float], Any] | None = None,
    ) -> None:
        self.job_id = job_id
        self.params = params or {}
        self.state_dir = state_dir
        self.identity = identity if identity is not None else JobIdentity()
        tokens = dict(credentials or {})
        single: SecretToken | None = None
        if credential is not None and not credential.provider:
            # A legacy token that names no service is the job's single credential, never keyed
            # under ``""``: ``credentials`` only ever maps service names, as the service wire does.
            if tokens:
                msg = "A credential without a service cannot accompany service tokens"
                raise ValueError(msg)
            single = credential
        elif credential is not None:
            if credential.provider in tokens and tokens[credential.provider] != credential:
                msg = "Conflicting credentials for one service"
                raise ValueError(msg)
            tokens[credential.provider] = credential
        if any(not service or service != token.provider for service, token in tokens.items()):
            msg = "A job credential does not match its service"
            raise ValueError(msg)
        self.credentials: Mapping[str, SecretToken] = MappingProxyType(tokens)
        if single is None and len(tokens) == 1:
            single = next(iter(tokens.values()))
        self._credential = single
        self.credentials_valid_until = 0.0
        self._credential_connections = {service: token.connection_id for service, token in tokens.items()}
        self._secret_lock = threading.Lock()
        self._redaction_secrets = {token.secret for token in tokens.values() if token.secret}
        if single is not None and single.secret:
            self._redaction_secrets.add(single.secret)
        self._host_request = host_request
        self._sink = sink
        self._cancel = cancel_event
        self._project_root: str | None = None

    def request_host(self, operation: str, payload: dict[str, Any], timeout: float = 120) -> Any:
        """Call an operation explicitly offered by the host, using this job's control channel.

        The host decides whether this plugin/job may use it. No host credentials
        or network address are required on the node. An unknown outcome must not
        be retried as a new mutating operation.
        """
        if self._host_request is None:
            msg = "This worker has no host request channel"
            raise RuntimeError(msg)
        return self._host_request(operation, payload, timeout)

    def protect_secret(self, value: str) -> None:
        """Redact a runtime-acquired credential from this job's events and snapshots.

        Call before using a renewed grant. Values stay only in the trusted worker's
        memory, including prior rotated values until the job is discarded.
        """
        if not value:
            return
        with self._secret_lock:
            if len(value) > 8192 or (value not in self._redaction_secrets and len(self._redaction_secrets) >= 256):
                msg = "Job credential redaction limit reached"
                raise ValueError(msg)
            self._redaction_secrets.add(value)

    def _mask_secrets(self, value: Any) -> Any:
        with self._secret_lock:
            secrets = sorted(self._redaction_secrets, key=len, reverse=True)

        def mask(item: Any) -> Any:
            if isinstance(item, str):
                for secret in secrets:
                    item = item.replace(secret, "***")
            elif isinstance(item, dict):
                return {mask(k): mask(v) for k, v in item.items()}
            elif isinstance(item, list):
                return [mask(v) for v in item]
            return item

        return mask(value)

    def refresh_credentials(self) -> Mapping[str, SecretToken]:
        """Renew only this job's already authorized Connections through host control.

        This exchanges grant metadata/credentials, never a model request or dataset.
        A refusal clears current credentials; previous values remain redacted.
        """
        from tlc_plugin_sdk.connections import SecretToken

        self.credentials = MappingProxyType({})
        self._credential = None
        self.credentials_valid_until = 0.0
        started = time.monotonic()
        answer = self.request_host("credentials", {}, 30)
        raw = answer.get("credentials") if isinstance(answer, dict) else None
        if not isinstance(raw, dict):
            msg = "Invalid credential renewal"
            raise PermissionError(msg)
        valid_for = answer.get("valid_for")
        if (
            isinstance(valid_for, bool)
            or not isinstance(valid_for, (int, float))
            or not math.isfinite(valid_for)
            or not 0 < valid_for <= 15
        ):
            msg = "Invalid credential grant lifetime"
            raise PermissionError(msg)
        deadline = started + valid_for
        tokens: dict[str, SecretToken] = {}
        for service, wire in raw.items():
            token = SecretToken.from_wire(wire)
            if (
                token is None
                or token.provider != service
                or self._credential_connections.get(service) != token.connection_id
            ):
                msg = "Credential renewal changed a job Connection"
                raise PermissionError(msg)
            self.protect_secret(token.secret)
            tokens[service] = token
        if set(tokens) != set(self._credential_connections):
            msg = "Credential renewal omitted a job Connection"
            raise PermissionError(msg)
        if deadline <= time.monotonic():
            msg = "Credential renewal expired in transit"
            raise PermissionError(msg)
        self.credentials_valid_until = deadline
        self.credentials = MappingProxyType(tokens)
        self._credential = next(iter(tokens.values())) if len(tokens) == 1 else None
        return self.credentials

    @property
    def credential(self) -> SecretToken | None:
        """The job's only token, or ``None`` when it has none or tokens for several services.

        Kept for single-token plugins. A job with tokens for several services has no default, so
        this is ``None`` there and warns (a ``UserWarning``) to use :meth:`get_credential` instead.
        """
        if len(self.credentials) > 1:
            warnings.warn(
                f"ctx.credential is None: this job has tokens for {len(self.credentials)} services "
                f"({', '.join(sorted(self.credentials))}). Use ctx.get_credential(service).",
                stacklevel=2,
            )
        return self._credential

    def get_credential(self, service: str) -> SecretToken | None:
        """Return the job's token for ``service``, or None when none was supplied.

        Args:
            service: The service slug the plugin declares (``huggingface``).

        Returns:
            The token, or ``None`` when the job was not given one for ``service``.
        """
        return self.credentials.get(service)

    # ── plugin-facing API ────────────────────────────────────────────────
    @property
    def project_root_url(self) -> str:
        """The project root this job writes to, without a trailing slash.

        The host resolves it at submit (the person's choice, else the host's configured root) and
        stamps it into the run body as ``project_root_url``; a plugin that creates tables or runs
        passes it as ``root_url`` to the core library. A body from a host that predates the key
        falls back to this worker's own ``tlc`` root, and to ``""`` only when ``tlc`` cannot say.
        """
        raw = self.params.get(PROJECT_ROOT_KEY)
        if isinstance(raw, str) and raw.strip():
            return raw.strip().rstrip("/")
        if self._project_root is None:
            try:
                import tlc

                self._project_root = str(tlc.config.project_root_url).rstrip("/")
            except Exception:
                self._project_root = ""
        return self._project_root

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

        When creating a run, call this as soon as the run exists, before training,
        prediction, or collection starts. This associates live queue progress with
        the actual run and its name. It does not complete the job: progress, metrics,
        errors, and cancellation continue normally. Pass ``str(run.url)`` from the
        created object, never construct a URL from the requested name (the library
        may choose a unique name). Trainer adapters should forward an ``on_run(url)``
        callback to this method instead of communicating identity through log text.

        Args:
            url: The URL the Open button opens (a run or a table URL).

        """
        self._emit({"event": "result", "run_url": url})

    def output(self, name: str, value: Any) -> None:
        """Record a named output of the job in its record, such as the URL of a table it wrote.

        Outputs are what an MCP caller (or anything reading ``GET /api/plugins/jobs``) gets back
        when the job is done, without listening to the job's events: a plugin that today
        reports its result only in a custom ``emit`` event records it here as well. ``name`` is
        a short identifier; ``value`` is JSON (a string, a number, a list or a dict). The last
        value per name wins; ``ctx.result(url)`` is the Open link and is always the output
        ``run_url``.
        """
        if not isinstance(name, str) or not name:
            msg = "An output needs a name"
            raise ValueError(msg)
        self._emit({"event": "output", "name": name, "value": value})

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
