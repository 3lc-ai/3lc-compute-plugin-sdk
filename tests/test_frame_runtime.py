# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Execute the assembled public runtime against a hostile/mocked parent boundary."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tlc_plugin_sdk import SDK_CONTRACT_VERSION
from tlc_plugin_sdk.shared.frame_runtime import FRAME_PROTOCOL, frame_runtime_script


def test_packaged_runtime_has_version_and_helpers() -> None:
    source = frame_runtime_script()
    assert FRAME_PROTOCOL == 1
    assert f"var FRAME_VERSION = '{SDK_CONTRACT_VERSION}'" in source
    assert "/* SDK_JOB_TRACKER */" not in source
    assert "__SDK_FRAME_VERSION__" not in source


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("close_mode", ["unload", "host-close"])
def test_runtime_boundary_and_lifecycle(tmp_path: Path, close_mode: str) -> None:
    script = tmp_path / "frame.js"
    script.write_text(frame_runtime_script(), encoding="utf-8")
    subprocess.run(
        ["node", str(Path(__file__).with_name("frame_runtime.cjs")), str(script), close_mode],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
