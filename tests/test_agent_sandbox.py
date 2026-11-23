# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared policy and safe result-file handling for generated Python."""

import os
from pathlib import Path

import pytest

from tlc_plugin_sdk.agent_sandbox import Policy, environment, read_private, seatbelt_profile


def test_environment_never_inherits_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "secret")
    monkeypatch.setenv("TLC_WORKER_TOKEN", "secret")
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    assert not {"OPENAI_API_KEY", "AWS_SESSION_TOKEN", "TLC_WORKER_TOKEN", "PYTHONPATH"} & environment().keys()


def test_private_result_reads_reject_links_and_bound_size(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.write_bytes(b"outside")
    (root / "safe").write_bytes(b"safe")
    assert read_private(root, "safe", 4) == b"safe"
    with pytest.raises(ValueError):
        read_private(root, "safe", 3)
    (root / "symlink").symlink_to(secret)
    (root / "dir").symlink_to(tmp_path, target_is_directory=True)
    os.link(secret, root / "hardlink")
    for path in ("symlink", "dir/secret", "hardlink", "../secret", str(secret)):
        with pytest.raises((ValueError, OSError)):
            read_private(root, path, 100)


def test_policy_has_no_network_or_global_filesystem_grant(tmp_path: Path) -> None:
    profile = seatbelt_profile(Policy.for_workspace(tmp_path / "work", tmp_path / "state"))
    assert "(deny default)" in profile
    assert "allow network" not in profile
    assert '(subpath "/")' not in profile
    assert str(tmp_path / "state" / "conversations") in profile
