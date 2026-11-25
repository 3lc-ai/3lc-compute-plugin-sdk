# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The shared deployment boot script: each parameter, quoting, and that every variant is valid bash."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk.infrastructure import (
    DeploymentBootstrap,
    DeploymentHostnames,
    DeploymentRequest,
    DeploymentStorage,
    deployment_boot,
)
from tlc_plugin_sdk.infrastructure.deployment_boot import (
    BOOT_STATUS_FILE,
    InvalidBootInput,
    object_service_allowed_urls,
    pin_wheel,
    render_deployment_script,
    tls_hostnames_for_ip,
)

_TOKEN = "tlc_boot_" + "x" * 24
_ADDRESS = "curl -s http://metadata.test/public-ip"
_NAMES = DeploymentHostnames(
    hostname="acme.deploy.example.test",
    object_service="os.acme.deploy.example.test",
    compute_service="compute.acme.deploy.example.test",
)


def _request(**overrides: Any) -> DeploymentRequest:
    fields: dict[str, Any] = {
        "deployment_id": "dep_1",
        "name": "Acme",
        "bootstrap": DeploymentBootstrap(
            record_id="ws_1", config_service_url="https://config.example.test/", token=_TOKEN
        ),
        "hostnames": _NAMES,
        "env": {"TLC_RUNTIME_CONFIG": "prod"},
    }
    fields.update(overrides)
    return DeploymentRequest(**fields)


def _render(request: DeploymentRequest | None = None, **kwargs: Any) -> str:
    kwargs.setdefault("default_compute_spec", "3lc-compute")
    kwargs.setdefault("public_address_command", _ADDRESS)
    return render_deployment_script(request or _request(), **kwargs)


def _env_file(script: str) -> dict[str, str]:
    body = script.split("cat > /etc/3lc/env <<'__3LC_ENV__'\n", 1)[1].split("\n__3LC_ENV__\n", 1)[0]
    return dict(line.split("=", 1) for line in body.splitlines() if line)


def _assert_bash(script: str) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash")
    result = subprocess.run([bash, "-n"], input=script, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_the_module_imports_only_the_standard_library() -> None:
    code = (
        "import sys, tlc_plugin_sdk.infrastructure.deployment_boot as m; "
        "bad = [n for n in ('litestar', 'uvicorn', 'tlc', 'socketio') if n in sys.modules]; "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_the_script_reports_its_stages_installs_and_starts_both_services() -> None:
    script = _render()
    assert script.startswith("#!/bin/bash\nset -e\n")
    assert "report() { printf '3LC_BOOT %s\\n' \"$1\" > /dev/console; }" in script
    for stage in ("installing the services", "redeeming the workspace's keys", "starting the services"):
        assert f'stage="{stage}"\nreport "$stage"' in script
    assert "uv venv /opt/3lc --python 3.12" in script
    assert "uv pip install --python /opt/3lc/bin/python 3lc 3lc-compute\n" in script
    assert "ExecStart=/opt/3lc/bin/3lc service --host 0.0.0.0 --port 5015 --no-public-examples" in script
    assert "ExecStart=/opt/3lc/bin/3lc-compute\n" in script
    assert "systemctl enable --now 3lc-object-service 3lc-compute" in script
    assert script.endswith('echo "3lc workspace ready"\nreport "boot finished"\n')
    _assert_bash(script)


def test_the_request_spec_wins_over_the_default_and_extra_specs_are_quoted() -> None:
    script = _render(_request(compute_spec="3lc-compute>=1.2,<1.3"), extra_specs=["pkg[extra]>=1"])
    assert "uv pip install --python /opt/3lc/bin/python 3lc '3lc-compute>=1.2,<1.3' 'pkg[extra]>=1'\n" in script
    _assert_bash(script)


def test_index_settings_in_the_request_reach_the_install_and_not_the_services() -> None:
    env = {
        "TLC_NODE_PIP_INDEX_URLS": "https://staging.example.test/simple",
        "TLC_NODE_PIP_INDEX_STAGING_URL": "https://staging.example.test/simple",
        "TLC_NODE_PIP_INDEX_STAGING_USERNAME": "reader",
        "TLC_NODE_PIP_INDEX_STAGING_PASSWORD": "it's $ecret",
        "TLC_NODE_STAGING_CATALOG": "1",
        "TLC_NODE_CORE_SPEC": "3lc==3.5.0.dev4",
    }
    script = _render(_request(env=env))
    assert "export UV_INDEX_STAGING_PASSWORD='it'\"'\"'s $ecret'\n" in script
    assert (
        "uv pip install --python /opt/3lc/bin/python --index-strategy unsafe-first-match "
        "--index staging=https://staging.example.test/simple 3lc==3.5.0.dev4 3lc-compute\n"
    ) in script
    written = _env_file(script)
    assert "TLC_NODE_STAGING_CATALOG" not in written and "TLC_NODE_CORE_SPEC" not in written
    assert written["TLC_COMPUTE_PLUGIN_INDEX_URLS"] == '"staging=https://staging.example.test/simple"'
    _assert_bash(script)
    with pytest.raises(InvalidBootInput, match="TLC_NODE_CORE_SPEC"):
        _render(_request(env={"TLC_NODE_CORE_SPEC": "numpy; rm -rf /"}))
    with pytest.raises(InvalidBootInput, match="staging index"):
        _render(_request(env={"TLC_NODE_STAGING_CATALOG": "1"}))


def test_identity_env_is_laid_over_the_request_and_the_launchers_own_values_are_dropped() -> None:
    env = {
        "AZURE_CLIENT_ID": "from-request",
        "TLC_PROJECT_ROOT_URL": "s3://launcher",
        "TLC_SCAN_URLS": "s3://launcher-scan",
        "TLC_SERVICE_ALLOWED_URLS": "everything",
        "TLC_API_KEY": "launcher-key",
        "TLC_NODE_TOKEN": "t",
    }
    written = _env_file(_render(_request(env=env), identity_env={"AZURE_CLIENT_ID": "machine"}))
    assert written["AZURE_CLIENT_ID"] == '"machine"'
    for key in ("TLC_PROJECT_ROOT_URL", "TLC_SCAN_URLS", "TLC_API_KEY", "TLC_NODE_TOKEN"):
        assert key not in written
    assert written["TLC_CONFIG_SERVICE_URL"] == '"https://config.example.test/"'
    assert written["TLC_COMPUTE_MODE"] == '"full"'


def test_env_values_are_escaped_and_unusable_ones_refused() -> None:
    written = _env_file(_render(_request(env={"A": 'say "hi" \\ $HOME'})))
    assert written["A"] == '"say \\"hi\\" \\\\ $HOME"'
    with pytest.raises(InvalidBootInput, match="line break"):
        _render(_request(env={"A": "one\n__3LC_ENV__\nrm -rf /"}))
    with pytest.raises(InvalidBootInput, match="environment variable name"):
        _render(_request(env={"a b": "x"}))


def test_storage_settings_and_allowed_urls() -> None:
    storage = DeploymentStorage(root="abfs://projects/root", scan=["abfs://data/a", "abfs://data/b"])
    written = _env_file(_render(_request(storage=storage), storage_schemes=["abfs://", "az://"]))
    assert written["TLC_PROJECT_ROOT_URL"] == '"abfs://projects/root"'
    assert written["TLC_SCAN_URLS"] == '"abfs://data/a,abfs://data/b"'
    assert written["TLC_SERVICE_ALLOWED_URLS"] == '"abfs://,az://,/srv/3lc/projects,/root/.local/share/3LC/projects"'
    assert "TLC_SERVICE_ALLOWED_URLS" not in _env_file(_render(confine_object_service=False))
    assert object_service_allowed_urls("/data/mine/", schemes=["s3://"]).endswith(",/data/mine")
    assert "/srv/3lc/projects/x" not in object_service_allowed_urls("/srv/3lc/projects/x")


def test_demo_mode_has_a_local_root_the_public_examples_and_no_default_plugins() -> None:
    script = _render(_request(mode="demo"))
    written = _env_file(script)
    assert written["TLC_PROJECT_ROOT_URL"] == '"/srv/3lc/projects"'
    assert written["TLC_COMPUTE_DEFAULT_PLUGINS"] == '""'
    assert "TLC_SERVICE_ALLOWED_URLS" not in written
    assert "--port 5015\n" in script
    assert "TLC_COMPUTE_DEFAULT_PLUGINS" not in _env_file(_render())


def test_plugins_are_the_install_sources_of_the_first_start() -> None:
    written = _env_file(_render(_request(plugins=["a==1", "b"])))
    assert written["TLC_COMPUTE_BOOTSTRAP_PLUGINS"] == '"a==1;b"'


def test_the_token_is_redeemed_once_and_skipped_without_a_record() -> None:
    script = _render()
    assert "-X POST https://config.example.test/v1/workspaces/ws_1/bootstrap" in script
    assert f"""-d '{{"token": "{_TOKEN}"}}'""" in script
    assert script.count(_TOKEN) == 1
    assert "rm -f /etc/3lc/bootstrap.json" in script
    assert "EnvironmentFile=-/etc/3lc/env.object-service" in script
    bare = _render(_request(bootstrap=DeploymentBootstrap(record_id="", config_service_url="", token="")))
    assert "redeeming" not in bare and "bootstrap.json" not in bare.replace("EnvironmentFile", "")
    assert "TLC_CONFIG_SERVICE_URL" not in _env_file(bare)
    _assert_bash(bare)


def test_tls_on_the_machine_waits_for_dns_before_caddy_asks_for_certificates() -> None:
    script = _render()
    wait = script.index('stage="waiting for DNS to name this machine"')
    caddyfile = script.index("cat > /etc/caddy/Caddyfile <<__3LC_CADDY__")
    assert wait < caddyfile
    assert f'PUBIP=$({_ADDRESS}) || PUBIP=""' in script
    assert "for name in os.acme.deploy.example.test compute.acme.deploy.example.test; do" in script
    assert "os.acme.deploy.example.test {\n    reverse_proxy 127.0.0.1:5015\n}" in script
    assert "compute.acme.deploy.example.test {\n    reverse_proxy 127.0.0.1:5020\n}" in script
    assert f'dns=slow: $name did not resolve to $PUBIP in 300s" >> {BOOT_STATUS_FILE}' in script
    _assert_bash(script)
    for quiet in (_render(wait_for_dns=False), _render(public_address_command="")):
        assert "waiting for DNS" not in quiet and "Caddyfile" in quiet


def test_names_derived_from_the_address_are_filled_in_on_the_machine() -> None:
    script = _render(_request(hostnames=None))
    assert f'PUBIP=$({_ADDRESS})\nDASHED=$(echo "$PUBIP" | tr . -)\n' in script
    assert "os.${DASHED}.sslip.io {" in script and "compute.${DASHED}.sslip.io {" in script
    assert "waiting for DNS" not in script
    _assert_bash(script)
    assert tls_hostnames_for_ip("1.2.3.4") == {"object": "os.1-2-3-4.sslip.io", "compute": "compute.1-2-3-4.sslip.io"}
    with pytest.raises(InvalidBootInput, match="public address"):
        _render(_request(hostnames=None), public_address_command="")


def test_behind_a_load_balancer_caddy_asks_for_no_certificate() -> None:
    plain = _render(tls="load_balancer")
    assert ":80 {\n    @health path /__3lc_health\n    respond @health 200\n" in plain
    assert "@object host os.acme.deploy.example.test" in plain
    assert "header_up X-Forwarded-Proto https" in plain
    assert "waiting for DNS" not in plain and "openssl" not in plain
    _assert_bash(plain)
    backend = _render(tls="load_balancer", backend_tls=True)
    assert "auto_https off" in backend and ":443 {\n    tls /etc/caddy/backend/cert.pem" in backend
    assert "openssl req -x509" in backend
    _assert_bash(backend)
    with pytest.raises(InvalidBootInput, match="load balancer"):
        _render(_request(hostnames=None), tls="load_balancer")


def test_without_tls_there_is_no_caddy() -> None:
    script = _render(tls="none")
    assert "caddy" not in script.lower()
    _assert_bash(script)


def test_a_hostname_that_is_not_one_is_refused() -> None:
    names = DeploymentHostnames(hostname="", object_service="os.x.test {\n evil", compute_service="c.x.test")
    with pytest.raises(InvalidBootInput, match="not a valid hostname"):
        _render(_request(hostnames=names))


def test_provider_snippets_run_before_the_install_and_after_the_services() -> None:
    script = _render(before_install="echo before", after_services="echo after\n\n")
    assert script.index("echo before\n") < script.index("$APT update")
    assert script.index("systemctl enable --now 3lc-object-service") < script.index("echo after\n")
    assert "echo after\n\n" not in script
    _assert_bash(script)


def test_wheels_are_downloaded_with_the_providers_command_and_fall_back_to_the_spec() -> None:
    script = _render(
        _request(compute_spec="3lc-compute>=1.2"),
        wheel_urls=[pin_wheel("s3://bucket/a b.whl", "a" * 64), pin_wheel("s3://bucket/c.whl", "B" * 64)],
        download_command='aws s3 cp "$1" "$2"',
    )
    assert 'fetch_wheel() { aws s3 cp "$1" "$2"; }' in script
    assert f"for pin in '{'a' * 64} s3://bucket/a b.whl' '{'b' * 64} s3://bucket/c.whl'; do" in script
    assert 'verify_wheel "$wheel_dir/${url##*/}" "${pin%% *}"' in script
    assert "INSTALL=('3lc-compute>=1.2')" in script
    assert 'uv pip install --python /opt/3lc/bin/python 3lc "${INSTALL[@]}"\n' in script
    _assert_bash(script)
    with pytest.raises(InvalidBootInput, match="downloads"):
        _render(wheel_urls=[pin_wheel("s3://bucket/c.whl", "a" * 64)])


@pytest.mark.parametrize(
    "address",
    ["s3://bucket/c.whl", "s3://bucket/c.whl#sha256=abc", "s3://bucket/c.whl#md5=" + "a" * 32, "#sha256=" + "a" * 64],
)
def test_a_wheel_without_a_digest_is_refused(address: str) -> None:
    with pytest.raises(InvalidBootInput, match="no digest"):
        _render(wheel_urls=[address], download_command='cp "$1" "$2"')
    with pytest.raises(InvalidBootInput, match="64 hex"):
        pin_wheel("s3://bucket/c.whl", "abc")


def _run_wheel_block(tmp_path: Path, payload: bytes, digest: str) -> tuple[int, str]:
    """Run the wheel block alone, with ``cp`` as the download and ``report`` printing to stdout."""
    from tlc_plugin_sdk.infrastructure.deployment_boot import _wheels_block

    source = tmp_path / "agent-1.0-py3-none-any.whl"
    source.write_bytes(payload)
    block = _wheels_block([pin_wheel(str(source), digest)], 'cp "$1" "$2"', fallback="3lc-compute")
    script = "set -e\nreport() { printf '3LC_BOOT %s\\n' \"$1\"; }\n" + block + 'echo "INSTALL=${INSTALL[*]}"\n'
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs sha256sum, as the machine has")
def test_a_wheel_with_another_digest_fails_the_boot_and_never_falls_back(tmp_path: Path) -> None:
    payload = b"the wheel the host built"
    code, out = _run_wheel_block(tmp_path, payload, hashlib.sha256(payload).hexdigest())
    assert code == 0 and "agent-1.0-py3-none-any.whl" in out.splitlines()[-1]
    code, out = _run_wheel_block(tmp_path, b"another file", hashlib.sha256(payload).hexdigest())
    assert code == 1
    assert "3LC_BOOT failed: wheel agent-1.0-py3-none-any.whl is not the file the host built" in out
    assert "INSTALL=" not in out and "installing from the index" not in out


def test_the_health_wait_is_opt_in() -> None:
    script = _render(wait_for_health=True)
    assert 'stage="waiting for the services to answer"' in script
    assert "for port in 5015 5020; do" in script
    assert script.index("waiting for the services to answer") < script.index('report "boot finished"')
    assert "waiting for the services to answer" not in _render()
    _assert_bash(script)


def _run_health_block(tmp_path: Path, *, curl_exit: int) -> tuple[int, str, str]:
    """Run the health wait with ``curl``, ``sleep`` and ``journalctl`` stubbed; return (exit, console, status)."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash")
    console, status = tmp_path / "console", tmp_path / "status"
    block = (
        deployment_boot
        ._health_block(True)
        .replace("> /dev/console", f">> {console}")  # a device: a file stands in, appended to
        .replace(BOOT_STATUS_FILE, str(status))
    )
    prelude = f"""set -e
report() {{ printf '3LC_BOOT %s\\n' "$1" >> {console}; }}
curl() {{ return {curl_exit}; }}
sleep() {{ :; }}
journalctl() {{ echo "2026-11-24T10:00:00+0000 vm 3lc[7]: boom TLC_API_KEY=tlc_svc_secret"; }}
"""
    script = prelude + block + 'report "boot finished"\n'
    result = subprocess.run([bash], input=script, text=True, capture_output=True, check=False)

    def read(path: Path) -> str:
        return path.read_text() if path.exists() else ""

    return result.returncode, read(console), read(status)


def test_services_that_never_answer_fail_the_boot_with_their_journals(tmp_path: Path) -> None:
    code, console, status = _run_health_block(tmp_path, curl_exit=7)
    assert code == 1
    assert "3LC_BOOT failed: waiting for the services to answer (exit 1)" in console
    assert "boot finished" not in console
    assert "3LC_JOURNAL_BEGIN 3lc-object-service unanswered" in console
    assert "3LC_JOURNAL_END 3lc-compute" in console
    assert "3LC_JOURNAL 3lc-compute 2026-11-24T10:00:00+0000 vm 3lc[7]: boom" in console
    assert "tlc_svc_secret" not in console
    assert "port(s) 5015 5020" in status


def test_services_that_answer_finish_the_boot(tmp_path: Path) -> None:
    code, console, _ = _run_health_block(tmp_path, curl_exit=0)
    assert code == 0
    assert console.endswith("3LC_BOOT boot finished\n")
    assert "3LC_JOURNAL" not in console
