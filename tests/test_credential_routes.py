# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""A route the host binds a credential to: the request's token, for the handler only.

The host forwards a granted SECRET value on a ``credential_routes`` route in the host-owned
``x-tlc-bound-credential`` header; the worker's middleware binds it around the handler exactly as
the worker binds a job's token around ``run_job``. Also: the per-service environment variables and
the manifest's ``credentials`` / ``credential_routes`` declarations.
"""

from __future__ import annotations

import json
import os
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from litestar import get

from tlc_plugin_sdk import connections
from tlc_plugin_sdk.connections import (
    BOUND_CREDENTIAL_HEADER,
    CredentialUnavailable,
    SecretToken,
    credential_environment,
    credential_middleware,
    encode_credential,
)
from tlc_plugin_sdk.contract import ComputePlugin, HubPlugin
from tlc_plugin_sdk.harness import CredentialRequirement, PluginHarness, read_manifest
from tlc_plugin_sdk.worker import _Worker

if TYPE_CHECKING:
    from tlc_plugin_sdk.job_context import JobContext

SECRET = "hf_" + "r" * 34
OTHER = "hf_" + "o" * 34


def _token(secret: str = SECRET, connection_id: str = "c-1", provider: str = "huggingface") -> SecretToken:
    return SecretToken(provider=provider, secret=secret, connection_id=connection_id)


def _header(token: SecretToken) -> dict[str, str]:
    return {BOUND_CREDENTIAL_HEADER: encode_credential(token)}


def _seen() -> dict[str, Any]:
    credential = connections.current_credential()
    return {
        "credential": repr(credential) if credential else None,
        "connection_id": getattr(credential, "connection_id", None),
        "env": os.environ.get("HF_TOKEN"),
    }


@get("/threaded", sync_to_thread=True)
def _threaded() -> dict[str, Any]:
    return _seen()


@get("/async")
async def _async() -> dict[str, Any]:
    return _seen()


@get("/kaggle")
async def _kaggle() -> dict[str, Any]:
    return {"username": os.environ.get("KAGGLE_USERNAME"), "key": os.environ.get("KAGGLE_KEY")}


class _Probe(HubPlugin):
    def get_ui_fragment(self) -> str:
        return ""

    def get_route_handlers(self) -> list[Any]:
        return [_threaded, _async, _kaggle]


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PluginHarness]:
    monkeypatch.setenv("HF_TOKEN", "hf_the_operators_own")
    with PluginHarness(_Probe(), plugin_id="probe", config_root=tmp_path) as h:
        yield h


@pytest.mark.parametrize("route", ["/threaded", "/async"])
def test_a_bound_route_sees_its_token_and_env_for_the_request_only(harness: PluginHarness, route: str) -> None:
    answer = harness.get(route, headers=_header(_token())).json()
    assert answer == {"credential": repr(_token()), "connection_id": "c-1", "env": SECRET}
    assert SECRET not in answer["credential"]
    assert os.environ["HF_TOKEN"] == "hf_the_operators_own"
    assert connections.current_credential() is None
    after = harness.get(route).json()
    assert after == {"credential": None, "connection_id": None, "env": "hf_the_operators_own"}


@pytest.mark.parametrize(
    "value",
    ["not json", "[]", json.dumps({"connection_id": "c-1", "provider": "huggingface"}), json.dumps({"secret": ""})],
)
def test_a_malformed_header_is_400(harness: PluginHarness, value: str) -> None:
    response = harness.get("/async", headers={BOUND_CREDENTIAL_HEADER: value})
    assert response.status_code == 400
    assert SECRET not in response.text


def test_a_different_token_while_one_is_bound_is_409_and_the_same_one_nests(harness: PluginHarness) -> None:
    with connections.bound_credential(_token()):
        refused = harness.get("/async", headers=_header(_token(OTHER, "c-2")))
        assert refused.status_code == 409
        assert "c-1" not in refused.text and SECRET not in refused.text
        assert harness.get("/async", headers=_header(_token())).json()["env"] == SECRET
        assert os.environ["HF_TOKEN"] == SECRET
    assert os.environ["HF_TOKEN"] == "hf_the_operators_own"


def test_kaggle_json_sets_username_and_key_and_a_bad_value_is_424(
    harness: PluginHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    value = json.dumps({"username": "ada", "key": "k" * 32})
    answer = harness.get("/kaggle", headers=_header(_token(value, provider="kaggle"))).json()
    assert answer == {"username": "ada", "key": "k" * 32}
    assert "KAGGLE_USERNAME" not in os.environ and "KAGGLE_KEY" not in os.environ
    bad = harness.get("/kaggle", headers=_header(_token("k" * 32, provider="kaggle")))
    assert bad.status_code == 424 and "k" * 32 not in bad.text
    assert "'username', 'key'" in bad.json()["detail"]


def test_credential_environment_per_service() -> None:
    assert credential_environment(_token()) == {"HF_TOKEN": SECRET}
    assert credential_environment(_token("w" * 40, provider="wandb")) == {"WANDB_API_KEY": "w" * 40}
    assert credential_environment(_token("x", provider="example")) == {}
    kaggle = json.dumps({"username": "ada", "key": "abc"})
    assert credential_environment(_token(kaggle, provider="kaggle")) == {"KAGGLE_USERNAME": "ada", "KAGGLE_KEY": "abc"}
    for bad in ("abc", "[]", json.dumps({"username": "ada"}), json.dumps({"username": "ada", "key": 3})):
        with pytest.raises(CredentialUnavailable) as exc:
            credential_environment(_token(bad, provider="kaggle"))
        expected = "The kaggle Connection's value must be a JSON object with string fields 'username', 'key'"
        assert str(exc.value) == expected


def test_concurrent_requests_each_see_only_their_own_token() -> None:
    """One request holds its token across an await; a concurrent one without a header sees none."""
    seen: dict[str, Any] = {}
    holding = anyio.Event()
    release = anyio.Event()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        name = scope["path"]
        if name == "/holder":
            holding.set()
            await release.wait()
        seen[name] = connections.current_credential()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wrapped = credential_middleware(app)

    async def call(path: str, headers: list[tuple[bytes, bytes]]) -> None:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message: dict[str, Any]) -> None:
            return None

        await wrapped({"type": "http", "path": path, "headers": headers}, receive, send)

    async def main() -> None:
        header = [(BOUND_CREDENTIAL_HEADER.encode(), encode_credential(_token()).encode())]
        async with anyio.create_task_group() as group:
            group.start_soon(call, "/holder", header)
            await holding.wait()
            await call("/bystander", [])
            release.set()

    anyio.run(main)
    assert seen["/holder"] == _token()
    assert seen["/bystander"] is None
    assert connections.current_credential() is None


class _KagglePlugin(ComputePlugin):
    def __init__(self) -> None:
        self.env: Any = "unset"

    def get_ui_fragment(self) -> str:
        return ""

    def run_job(self, ctx: JobContext) -> None:
        self.env = (os.environ.get("KAGGLE_USERNAME"), os.environ.get("KAGGLE_KEY"))


def test_a_job_with_a_kaggle_value_of_the_wrong_shape_fails_without_quoting_it(tmp_path: Path) -> None:
    plugin = _KagglePlugin()
    worker = _Worker(plugin, "p", tmp_path / "state")
    job = worker.start_job("j1", {"_credential": {"connection_id": "c", "provider": "kaggle", "secret": "s3cr3t"}})
    assert job.wait(5)
    events = []
    while not job.events.empty():
        events.append(job.events.get_nowait())
    assert events[-1]["event"] == "error" and "CredentialUnavailable" in events[-1]["message"]
    assert "s3cr3t" not in repr(events) and plugin.env == "unset"


def _manifest(tmp_path: Path, runtime: str) -> Path:
    (tmp_path / "plugin.toml").write_text(
        textwrap.dedent(
            """
            id = "probe"
            [runtime]
            entrypoint = "probe:Probe"
            """
        )
        + runtime
    )
    return tmp_path


def test_the_manifest_declares_credentials_and_routes(tmp_path: Path) -> None:
    manifest = read_manifest(
        _manifest(
            tmp_path,
            'credentials = [{ service = "huggingface", required = true }, { service = "wandb" }]\n'
            'credential_routes = ["preview", "/model-warmup/", "/preview"]\n',
        )
    )
    assert manifest.credentials == (CredentialRequirement("huggingface", True), CredentialRequirement("wandb", False))
    assert manifest.credential_routes == ("/preview", "/model-warmup")


def test_a_manifest_without_them_declares_nothing(tmp_path: Path) -> None:
    manifest = read_manifest(_manifest(tmp_path, ""))
    assert manifest.credentials == () and manifest.credential_routes == ()


@pytest.mark.parametrize(
    ("runtime", "match"),
    [
        ('credentials = "huggingface"\n', "list"),
        ('credentials = ["huggingface"]\n', "tables"),
        ('credentials = [{ service = "Hugging Face" }]\n', "slug"),
        ('credentials = [{ service = "huggingface", required = "yes" }]\n', "true or false"),
        ('credentials = [{ service = "hf" }, { service = "hf" }]\n', "twice"),
        ('credentials = [{ service = "hf" }]\ncredential_routes = "/preview"\n', "route prefixes"),
        ('credentials = [{ service = "hf" }]\ncredential_routes = [""]\n', "route prefixes"),
        ('credential_routes = ["/preview"]\n', "needs \\[runtime\\] credentials"),
    ],
)
def test_a_wrong_declaration_is_named(tmp_path: Path, runtime: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        read_manifest(_manifest(tmp_path, runtime))
