# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The worker hands a job the credential the host granted it, for ``run_job``'s lifetime only.

The run body's host-owned ``_credential`` is popped before ``ctx.params`` exists; inside
``run_job`` it is ``ctx.credential`` and ``connections.current_credential()``, and a Hugging Face
token is also ``HF_TOKEN``. When the job ends the environment is as it was. A worker binds one
token at a time: a job whose token differs from the bound one fails.
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


def test_a_job_sees_its_credential_and_hf_token_for_its_lifetime_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_the_operators_own")
    plugin, events = _run(tmp_path, {"table_url": "s3://b/t", "_credential": _wire()})
    expected = SecretToken(provider="huggingface", secret=SECRET, connection_id="c-1")
    assert plugin.seen["env"] == SECRET
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


def test_a_different_token_is_refused_while_one_is_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_the_operators_own")
    first = _token()
    with connections.bound_credential(first):
        with (
            pytest.raises(connections.CredentialInUse, match="'c-1'"),
            connections.bound_credential(_token("c-2", "hf_" + "u" * 34)),
        ):
            pytest.fail("a second token must not bind")
        assert os.environ["HF_TOKEN"] == SECRET
        assert connections.current_credential() is first
    assert os.environ["HF_TOKEN"] == "hf_the_operators_own"
    assert connections.current_credential() is None


def test_the_same_token_nests_and_the_last_holder_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with connections.bound_credential(_token()):
        with connections.bound_credential(_token()):
            assert os.environ["HF_TOKEN"] == SECRET
        assert os.environ["HF_TOKEN"] == SECRET
    assert "HF_TOKEN" not in os.environ


def test_a_new_token_binds_once_the_first_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    msg = "the job raised"
    with pytest.raises(RuntimeError, match=msg), connections.bound_credential(_token()):
        raise RuntimeError(msg)
    assert "HF_TOKEN" not in os.environ
    second = _token("c-2", "hf_" + "u" * 34)
    with connections.bound_credential(second):
        assert os.environ["HF_TOKEN"] == second.secret
        assert connections.current_credential() is second
    assert "HF_TOKEN" not in os.environ


def test_a_job_whose_token_differs_from_the_bound_one_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    plugin = _Plugin()
    worker = _Worker(plugin, "p", tmp_path / "state")
    other = "hf_" + "u" * 34
    with connections.bound_credential(_token("c-0", other)):
        job = worker.start_job("j1", {"_credential": _wire()})
        assert job.wait(5)
        events = []
        while not job.events.empty():
            events.append(job.events.get_nowait())
        assert os.environ["HF_TOKEN"] == other
    assert events[-1]["event"] == "error" and "CredentialInUse" in events[-1]["message"], events
    assert plugin.seen == {}
    assert SECRET not in repr(events) and other not in repr(events)
    assert "HF_TOKEN" not in os.environ
