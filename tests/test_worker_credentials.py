# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The worker hands a job the credential the host granted it, for ``run_job``'s lifetime only.

The run body's host-owned ``_credential`` is popped before ``ctx.params`` exists; inside
``run_job`` it is ``ctx.credential`` and ``connections.current_credential()``, and a Hugging Face
token is also ``HF_TOKEN``. When the job ends the environment is as it was.
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
