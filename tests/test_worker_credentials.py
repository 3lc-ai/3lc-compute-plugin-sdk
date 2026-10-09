# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The worker hands a job the credential the host granted it, for ``run_job``'s lifetime only.

The run body's host-owned ``_credential`` is popped before ``ctx.params`` exists; inside
``run_job`` it is ``ctx.credential`` and ``connections.current_credential()`` — never an
environment variable, which every job and request in the worker would see. Jobs with different
tokens run side by side, each with its own.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

from tlc_plugin_sdk import connections
from tlc_plugin_sdk.connections import SecretToken
from tlc_plugin_sdk.contract import ComputePlugin
from tlc_plugin_sdk.worker import _Worker

if TYPE_CHECKING:
    from pathlib import Path

    from tlc_plugin_sdk.job_context import JobContext

SECRET = "hf_" + "t" * 34


class _Plugin(ComputePlugin):
    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    def get_ui_fragment(self) -> str:
        return ""

    def run_job(self, ctx: JobContext) -> None:
        self.seen = {
            "env": os.environ.get("HF_TOKEN"),
            "current": connections.current_credential(),
            "ctx": ctx.credential,
            "in_params": "_credential" in ctx.params,
        }


def _run(tmp_path: Path, params: dict[str, Any]) -> tuple[_Plugin, list[dict[str, Any]]]:
    plugin = _Plugin()
    worker = _Worker(plugin, "p", tmp_path / "state")
    job = worker.start_job("j1", params)
    assert job.wait(5)
    events = []
    while not job.events.empty():
        events.append(job.events.get_nowait())
    assert events[-1]["event"] == "done", events
    return plugin, events


def _wire(provider: str = "huggingface") -> dict[str, str]:
    return {"connection_id": "c-1", "provider": provider, "secret": SECRET}


def test_a_job_sees_its_credential_for_its_lifetime_only_and_never_in_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_the_operators_own")
    plugin, events = _run(tmp_path, {"table_url": "s3://b/t", "_credential": _wire()})
    expected = SecretToken(provider="huggingface", secret=SECRET, connection_id="c-1")
    assert plugin.seen["env"] == "hf_the_operators_own"
    assert plugin.seen["current"] == expected and plugin.seen["ctx"] is plugin.seen["current"]
    assert plugin.seen["in_params"] is False
    assert os.environ["HF_TOKEN"] == "hf_the_operators_own"
    assert connections.current_credential() is None
    assert SECRET not in repr(events)


def test_a_job_without_a_credential_leaves_the_environment_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    plugin, _events = _run(tmp_path, {"table_url": "s3://b/t", "_credential": {"secret": ""}})
    assert plugin.seen == {"env": None, "current": None, "ctx": None, "in_params": False}
    assert "HF_TOKEN" not in os.environ


def test_a_provider_without_an_env_var_gets_the_credential_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    before = dict(os.environ)
    plugin, _events = _run(tmp_path, {"_credential": _wire(provider="example")})
    assert plugin.seen["env"] is None
    assert plugin.seen["current"] == SecretToken(provider="example", secret=SECRET, connection_id="c-1")
    assert dict(os.environ) == before


def _token(connection_id: str = "c-1", secret: str = SECRET) -> SecretToken:
    return SecretToken(provider="huggingface", secret=secret, connection_id=connection_id)


def test_a_different_token_binds_alongside_and_each_context_sees_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_the_operators_own")
    first, second = _token(), _token("c-2", "hf_" + "u" * 34)
    with connections.bound_credential(first):
        with connections.bound_credential(second):
            assert connections.current_credential() is second
        assert connections.current_credential() is first
        assert os.environ["HF_TOKEN"] == "hf_the_operators_own"
    assert connections.current_credential() is None


def test_an_exception_restores_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    msg = "the job raised"
    with pytest.raises(RuntimeError, match=msg), connections.bound_credential(_token()):
        raise RuntimeError(msg)
    assert connections.current_credential() is None
    assert "HF_TOKEN" not in os.environ


def test_concurrent_jobs_with_different_tokens_each_see_only_their_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job running with a token never leaks it to a concurrent job with another, or with none."""
    import threading

    monkeypatch.delenv("HF_TOKEN", raising=False)
    entered, release = threading.Event(), threading.Event()
    seen: dict[str, Any] = {}

    class _Concurrent(ComputePlugin):
        def get_ui_fragment(self) -> str:
            return ""

        def run_job(self, ctx: JobContext) -> None:
            if ctx.job_id == "holder":
                entered.set()
                release.wait(5)
            seen[ctx.job_id] = (connections.current_credential(), os.environ.get("HF_TOKEN"))

    worker = _Worker(_Concurrent(), "p", tmp_path / "state")
    other = "hf_" + "u" * 34
    holder = worker.start_job("holder", {"_credential": _wire()})
    assert entered.wait(5)
    second = worker.start_job(
        "second", {"_credential": {"connection_id": "c-2", "provider": "huggingface", "secret": other}}
    )
    bystander = worker.start_job("bystander", {})
    assert second.wait(5) and bystander.wait(5)
    release.set()
    assert holder.wait(5)
    assert seen["holder"] == (_token(), None)
    assert seen["second"] == (_token("c-2", other), None)
    assert seen["bystander"] == (None, None)


def test_multiple_services_are_available_by_name_without_a_default_or_saved_secrets(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    class Multi(_Plugin):
        def run_job(self, ctx: JobContext) -> None:
            seen["services"] = list(ctx.credentials)
            seen["hf"] = ctx.get_credential("huggingface")
            seen["wandb"] = connections.current_credential("wandb")
            seen["missing"] = ctx.get_credential("missing")
            seen["legacy"] = (ctx.credential, connections.current_credential())
            seen["params"] = dict(ctx.params)
            with pytest.raises(TypeError):
                ctx.credentials["wandb"] = _token()

    worker = _Worker(Multi(), "p", tmp_path / "state")
    second = {"provider": "wandb", "connection_id": "c-2", "secret": "wandb-second"}
    job = worker.start_job(
        "multi", {"_credentials": {"huggingface": _wire(), "wandb": second}, "table_url": "s3://b/t"}
    )
    assert job.wait(5)
    events = list(job.events.queue)
    assert events[-1]["event"] == "done", events
    assert seen["services"] == ["huggingface", "wandb"]
    assert seen["hf"] == _token()
    assert seen["wandb"] == SecretToken.from_wire(second)
    assert seen["missing"] is None
    assert seen["legacy"] == (None, None)
    assert seen["params"] == {"table_url": "s3://b/t"}
    assert connections.current_credential("huggingface") is None
    assert connections.current_credential("wandb") is None


@pytest.mark.parametrize(
    "wire", [[], {"wandb": _wire()}, {"huggingface": {"provider": "huggingface", "secret": SECRET}}]
)
def test_invalid_service_map_never_starts_a_job(tmp_path: Path, wire: Any) -> None:
    worker = _Worker(_Plugin(), "p", tmp_path / "state")
    with pytest.raises(ValueError, match=r"[Cc]redential"):
        worker.start_job("invalid", {"_credentials": wire})
    assert worker.active_jobs() == 0


def test_nested_service_context_restores_all_tokens_after_an_exception() -> None:
    first = _token()
    second = SecretToken(provider="wandb", secret="wandb-second", connection_id="c-2")
    with connections.bound_credentials({"huggingface": first, "wandb": second}):
        assert connections.current_credential() is None
        with pytest.raises(RuntimeError), connections.bound_credentials({}):
            assert connections.current_credential("huggingface") is None
            msg = "job failed"
            raise RuntimeError(msg)
        assert connections.current_credential("huggingface") is first
        assert connections.current_credential("wandb") is second
    assert connections.current_credential("wandb") is None


def test_concurrent_multi_service_jobs_do_not_share_tokens(tmp_path: Path) -> None:
    import threading

    entered, release = threading.Event(), threading.Event()
    seen: dict[str, tuple[object, object]] = {}

    class Concurrent(_Plugin):
        def run_job(self, ctx: JobContext) -> None:
            if ctx.job_id == "holder":
                entered.set()
                release.wait(5)
            seen[ctx.job_id] = (connections.current_credential("huggingface"), connections.current_credential("wandb"))

    worker = _Worker(Concurrent(), "p", tmp_path / "state")
    second = {"provider": "wandb", "connection_id": "c-2", "secret": "second"}
    holder = worker.start_job("holder", {"_credentials": {"huggingface": _wire(), "wandb": second}})
    try:
        assert entered.wait(5)
        other = worker.start_job("other", {"_credential": {**second, "secret": "other"}})
        empty = worker.start_job("empty", {})
        assert other.wait(5) and empty.wait(5)
    finally:
        release.set()
    assert holder.wait(5)
    assert seen["holder"] == (_token(), SecretToken.from_wire(second))
    assert seen["other"] == (None, SecretToken.from_wire({**second, "secret": "other"}))
    assert seen["empty"] == (None, None)
