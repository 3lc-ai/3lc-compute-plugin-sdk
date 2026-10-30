# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Canonical browser runtime served by a compatible Compute host."""

from functools import lru_cache
from importlib.resources import files

from tlc_plugin_sdk import SDK_CONTRACT_VERSION
from tlc_plugin_sdk.shared.job_tracker import JOB_TRACKER_JS

FRAME_PROTOCOL = 1


@lru_cache(maxsize=1)
def frame_runtime_script() -> str:
    """Return the versioned, self-contained frame script, including job helpers.

    Serving this public asset does not grant authority. The Hub must validate
    the handshake and authorize every operation on its private message port.
    """
    source = files("tlc_plugin_sdk").joinpath("contract/frame-runtime.js").read_text(encoding="utf-8")
    return source.replace("__SDK_FRAME_VERSION__", SDK_CONTRACT_VERSION).replace(
        "/* SDK_JOB_TRACKER */", JOB_TRACKER_JS
    )
