# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Golden wire-shape tests for ``JobContext`` and the worker's terminal events.

These pin the exact event dicts a plugin's ``run_job`` emits — the contract the
host's ``JobManager._apply`` folds into the generic job record. If a shape changes
here, the host relay changes with it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk import AliasOverride, JobContext, JobFailed, JobIdentity
from tlc_plugin_sdk.connections import SecretToken


def test_identity_defaults_to_unknown() -> None:
    ctx = _ctx([])
    assert ctx.identity == JobIdentity()
    assert not ctx.identity.known
    assert ctx.identity.user_id is None and ctx.identity.org_id is None and ctx.identity.project_id is None


def test_identity_is_carried_when_given() -> None:
    identity = JobIdentity(user_id="u-1", org_id="o-1")
    ctx = JobContext("job-1", {}, Path("/tmp"), sink=lambda _e: None, cancel_event=threading.Event(), identity=identity)
    assert ctx.identity is identity
    assert ctx.identity.known


def test_a_context_carries_its_credential_or_none() -> None:
    assert _ctx([]).credential is None
    token = SecretToken(provider="huggingface", secret="hf_value", connection_id="c-1")
    ctx = JobContext("job-1", {}, Path("/tmp"), sink=lambda _e: None, cancel_event=threading.Event(), credential=token)
    assert ctx.credential is token


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, JobIdentity()),
        ("garbage", JobIdentity()),
        ({}, JobIdentity()),
        ({"user_id": "u-1", "org_id": "o-1", "project_id": "p-1"}, JobIdentity("u-1", "o-1", "p-1")),
        ({"user_id": "u-1", "unknown_key": 1}, JobIdentity(user_id="u-1")),  # a newer host may add keys
        ({"user_id": 42, "org_id": "", "project_id": None}, JobIdentity()),  # non-strings and empties read unknown
    ],
)
def test_identity_from_wire_tolerates_anything(raw: object, expected: JobIdentity) -> None:
    assert JobIdentity.from_wire(raw) == expected


def _ctx(events: list[dict]) -> JobContext:
    return JobContext(
        "job-1",
        {"a": 1},
        Path("/tmp"),
        sink=events.append,
        cancel_event=threading.Event(),
    )


def test_progress_wire_shape() -> None:
    events: list[dict] = []
    _ctx(events).progress(percent=42.0, label="half", timing={"eta_s": 3})
    assert events == [
        {"event": "progress", "percent": 42.0, "label": "half", "timing": {"eta_s": 3}, "job_id": "job-1"}
    ]


def test_progress_indeterminate_passes_minus_one() -> None:
    events: list[dict] = []
    _ctx(events).progress(percent=-1)
    assert events == [{"event": "progress", "percent": -1, "label": "", "timing": None, "job_id": "job-1"}]


def test_metric_wire_shape() -> None:
    events: list[dict] = []
    _ctx(events).metric("loss", 0.04)
    assert events == [{"event": "metric", "label": "loss", "value": 0.04, "job_id": "job-1"}]


def test_log_wire_shape() -> None:
    events: list[dict] = []
    _ctx(events).log("hello")
    assert events == [{"event": "log", "message": "hello", "job_id": "job-1"}]


def test_result_is_positional_and_emits_run_url() -> None:
    events: list[dict] = []
    _ctx(events).result("s3://bucket/run")  # positional url; the run_url= keyword is gone
    assert events == [{"event": "result", "run_url": "s3://bucket/run", "job_id": "job-1"}]


def test_result_rejects_run_url_keyword() -> None:
    with pytest.raises(TypeError):
        _ctx([]).result(run_url="s3://bucket/run")  # type: ignore[call-arg]


def test_emit_custom_wire_shape() -> None:
    events: list[dict] = []
    _ctx(events).emit("epoch_metrics", {"epoch": 3, "loss": 0.04})
    assert events == [
        {"event": "custom", "name": "epoch_metrics", "payload": {"epoch": 3, "loss": 0.04}, "job_id": "job-1"}
    ]


def test_emit_reserved_name_rejected() -> None:
    with pytest.raises(ValueError, match="reserved"):
        _ctx([]).emit("job_update", {})


def test_fail_raises_jobfailed_with_bare_message() -> None:
    with pytest.raises(JobFailed) as excinfo:
        _ctx([]).fail("bad input")
    assert str(excinfo.value) == "bad input"


def test_job_id_stamped_on_every_event() -> None:
    events: list[dict] = []
    ctx = _ctx(events)
    ctx.progress(percent=1)
    ctx.metric("m", 1)
    ctx.log("l")
    ctx.result("u")
    ctx.emit("e", {})
    assert [e["job_id"] for e in events] == ["job-1"] * 5


# ── worker terminal-event formatting (A2) ─────────────────────────────────────


class _FakePlugin:
    def __init__(self, run_job):  # type: ignore[no-untyped-def]
        self._run_job = run_job

    def run_job(self, ctx: JobContext) -> None:
        self._run_job(ctx)


def _terminal_event(run_job) -> dict:  # type: ignore[no-untyped-def]
    from tlc_plugin_sdk.worker import _Job

    job = _Job("j", {}, Path("/tmp"), _FakePlugin(run_job))
    job._run()  # run synchronously on this thread
    events: list[dict] = []
    while not job.events.empty():
        events.append(job.events.get())
    return events[-1]


def test_worker_jobfailed_is_reported_as_bare_message() -> None:
    def run_job(ctx: JobContext) -> None:
        ctx.fail("nothing selected")

    assert _terminal_event(run_job) == {"event": "error", "message": "nothing selected", "job_id": "j"}


def test_worker_other_exception_keeps_type_prefix() -> None:
    def run_job(ctx: JobContext) -> None:
        msg = "boom"
        raise ValueError(msg)

    term = _terminal_event(run_job)
    assert term == {"event": "error", "message": "ValueError: boom", "job_id": "j"}


def test_worker_success_is_done_completed() -> None:
    def run_job(ctx: JobContext) -> None:
        ctx.progress(percent=100)

    assert _terminal_event(run_job) == {"event": "done", "status": "completed", "job_id": "j"}


def _ctx_with(params: dict[str, Any]) -> JobContext:
    return JobContext("job-1", params, Path("/tmp"), sink=lambda _e: None, cancel_event=threading.Event())


def test_project_root_is_the_stamped_key_cleaned() -> None:
    ctx = _ctx_with({"project_root_url": "s3://team/projects/"})
    assert ctx.project_root_url == "s3://team/projects"
    assert ctx.params["project_root_url"] == "s3://team/projects/"  # left in params for plugins reading it there


def test_project_root_falls_back_to_the_workers_own_tlc_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body from a host that predates the key: the worker's ``tlc`` says, once."""
    import sys
    import types

    fake = types.ModuleType("tlc")
    fake.config = types.SimpleNamespace(project_root_url="/Users/me/3LC/projects/")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tlc", fake)
    ctx = _ctx_with({"project_root_url": ""})
    assert ctx.project_root_url == "/Users/me/3LC/projects"
    fake.config.project_root_url = "changed"  # cached: read once per job
    assert ctx.project_root_url == "/Users/me/3LC/projects"


def test_project_root_is_empty_only_when_nobody_can_say(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_tlc(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "tlc":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_tlc)
    assert _ctx_with({}).project_root_url == ""


# ── alias overrides: parsed from the run body, never applied here ─────────────


def _overrides(raw: object) -> tuple[AliasOverride, ...]:
    return _ctx_with({"_alias_overrides": raw}).alias_overrides


def test_alias_overrides_are_parsed_from_the_run_body() -> None:
    raw = {
        "enabled": True,
        "overrides": [{"token": "COCO_IMAGES", "path": "/staged/coco"}, {"token": "LABELS", "path": "s3://b/l"}],
    }
    assert _overrides(raw) == (
        AliasOverride(token="COCO_IMAGES", path="/staged/coco"),
        AliasOverride(token="LABELS", path="s3://b/l"),
    )


def test_alias_overrides_are_empty_when_nothing_was_staged() -> None:
    assert _ctx_with({}).alias_overrides == ()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "garbage",
        [{"token": "DATA", "path": "/x"}],
        {"overrides": [{"token": "DATA", "path": "/x"}]},  # enabled missing
        {"enabled": False, "overrides": [{"token": "DATA", "path": "/x"}]},
        {"enabled": "true", "overrides": [{"token": "DATA", "path": "/x"}]},  # only True itself
        {"enabled": True},
        {"enabled": True, "overrides": "not-a-list"},
        {"enabled": True, "overrides": []},
    ],
)
def test_absent_disabled_or_malformed_alias_overrides_read_as_none(raw: object) -> None:
    assert _overrides(raw) == ()


def test_alias_override_entries_are_cleaned_and_bad_ones_skipped() -> None:
    raw = {
        "enabled": True,
        "overrides": [
            "not-a-dict",
            {"token": " <DATA> ", "path": " /staged/data/ "},  # bracketed, padded: bare token, trimmed path
            {"token": "", "path": "/x"},
            {"token": "EMPTY", "path": "  "},
            {"token": "NUMBER", "path": 3},
            {"token": None, "path": "/x"},
            {"path": "/x"},
            {"token": "DATA", "path": "/second/wins/not"},  # the first entry for a token is kept
            {"token": "OTHER", "path": "/other"},
        ],
    }
    assert _overrides(raw) == (
        AliasOverride(token="DATA", path="/staged/data/"),
        AliasOverride(token="OTHER", path="/other"),
    )


def test_reading_alias_overrides_is_recorded() -> None:
    ctx = _ctx_with({"_alias_overrides": {"enabled": True, "overrides": [{"token": "DATA", "path": "/x"}]}})
    assert not ctx._alias_overrides_read
    assert ctx.alias_overrides
    assert ctx._alias_overrides_read


def test_alias_overrides_stay_in_params_on_the_05_line() -> None:
    raw = {"enabled": True, "overrides": [{"token": "DATA", "path": "/x"}]}
    ctx = _ctx_with({"_alias_overrides": raw})
    assert ctx.params["_alias_overrides"] is raw  # plugins that read the key themselves keep working


def test_alias_override_is_a_frozen_value() -> None:
    override = AliasOverride(token="DATA", path="/x")
    with pytest.raises(AttributeError):
        override.path = "/y"  # type: ignore[misc]
    assert override == AliasOverride("DATA", "/x")
    assert hash(override) == hash(AliasOverride("DATA", "/x"))


# ── user-facing errors: one rule, the class attribute ──────────────────────────


class _UserFacingError(ValueError):
    user_facing = True


class _TruthyButNotTrue(ValueError):
    user_facing = "yes"


def test_jobfailed_declares_itself_user_facing() -> None:
    assert JobFailed.user_facing is True


def test_worker_shows_any_user_facing_exception_verbatim() -> None:
    def run_job(ctx: JobContext) -> None:
        msg = "The alias <DATA> in '<DATA>/a.jpg' is not registered in this process."
        raise _UserFacingError(msg)

    assert _terminal_event(run_job) == {
        "event": "error",
        "message": "The alias <DATA> in '<DATA>/a.jpg' is not registered in this process.",
        "job_id": "j",
    }


def test_a_subclass_of_a_user_facing_error_is_user_facing() -> None:
    class _Narrower(_UserFacingError):
        pass

    def run_job(ctx: JobContext) -> None:
        msg = "narrower"
        raise _Narrower(msg)

    assert _terminal_event(run_job)["message"] == "narrower"


def test_a_user_facing_exception_without_a_message_shows_its_type_name() -> None:
    def run_job(ctx: JobContext) -> None:
        raise _UserFacingError

    assert _terminal_event(run_job)["message"] == "_UserFacingError"


@pytest.mark.parametrize("make", [lambda: _TruthyButNotTrue("x"), lambda: _instance_flagged("x")])
def test_only_true_on_the_class_counts(make: Any) -> None:
    exc = make()

    def run_job(ctx: JobContext) -> None:
        raise exc

    assert _terminal_event(run_job)["message"] == f"{type(exc).__name__}: x"


def _instance_flagged(message: str) -> ValueError:
    exc = ValueError(message)
    exc.user_facing = True  # type: ignore[attr-defined]
    return exc
