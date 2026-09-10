# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The worker turns the run body's host-owned ``_identity`` into ``ctx.identity``.

The key is popped before ``ctx.params`` exists, so a plugin that persists its params never
persists who ran them, and a body without the key (an older host, a keyless local one) gives
an identity with every field ``None`` rather than a failed job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tlc_plugin_sdk import JobIdentity
from tlc_plugin_sdk.contract import ComputePlugin
from tlc_plugin_sdk.worker import _Worker

if TYPE_CHECKING:
    from pathlib import Path

    from tlc_plugin_sdk.job_context import JobContext


class _Plugin(ComputePlugin):
    def __init__(self) -> None:
        self.seen_identity: JobIdentity | None = None
        self.seen_params: dict[str, Any] | None = None

    def get_ui_fragment(self) -> str:
        return ""

    def run_job(self, ctx: JobContext) -> None:
        self.seen_identity = ctx.identity
        self.seen_params = dict(ctx.params)


def _run(tmp_path: Path, plugin: ComputePlugin, params: dict[str, Any]) -> dict[str, Any]:
    worker = _Worker(plugin, "p", tmp_path / "state")
    job = worker.start_job("j1", params)
    assert job.wait(5)
    events = []
    while not job.events.empty():
        events.append(job.events.get_nowait())
    return events[-1]


def test_identity_is_popped_from_params_and_exposed_on_ctx(tmp_path: Path) -> None:
    plugin = _Plugin()
    body = {"table_url": "s3://b/t", "_identity": {"user_id": "u-1", "org_id": "o-1", "project_id": "p-1"}}
    assert _run(tmp_path, plugin, body)["event"] == "done"
    assert plugin.seen_identity == JobIdentity("u-1", "o-1", "p-1")
    assert plugin.seen_params == {"table_url": "s3://b/t"}, "the host-owned key never reaches the plugin"


def test_a_body_without_identity_runs_with_an_unknown_one(tmp_path: Path) -> None:
    plugin = _Plugin()
    assert _run(tmp_path, plugin, {"table_url": "s3://b/t"})["event"] == "done"
    assert plugin.seen_identity == JobIdentity()
    assert plugin.seen_params == {"table_url": "s3://b/t"}
