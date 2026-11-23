# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Parse plugin placement requirements from a decoded TOML manifest table."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["PlacementRequirements", "parse_placement_requirements"]


@dataclass(frozen=True)
class PlacementRequirements:
    """The resources and browser access a plugin needs on its run target.

    Attributes:
        memory_gb: Minimum total RAM in whole GiB. Zero means no minimum.
        gpu_memory_gb: Minimum memory on one GPU in whole GiB. Zero means no GPU
            required; None means a GPU is required with no memory minimum.
        browser_access: Whether the browser must reach the app served by the job.
    """

    memory_gb: int = 0
    gpu_memory_gb: int | None = 0
    browser_access: bool = False

    @property
    def requires_gpu(self) -> bool:
        """Whether the plugin needs a GPU, with or without a memory minimum."""
        return self.gpu_memory_gb != 0


def _parse_memory(value: object, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        msg = f"[requirements] {key} must be a non-negative integer"
        raise ValueError(msg)
    return value


def parse_placement_requirements(manifest: Mapping[str, object]) -> PlacementRequirements:
    """Read ``[requirements]`` and fall back to the legacy GPU flag when needed.

    Unknown keys in the requirements table are ignored.

    Args:
        manifest: The decoded plugin manifest table (standalone or inside ``[tool.tlc-compute]``).

    Returns:
        The parsed requirements. An explicit GPU memory minimum, including zero,
        takes precedence over ``[runtime] requires_gpu``. The legacy ``remote_capable``
        flag describes eligibility on older hosts.

    Raises:
        ValueError: If the requirements value is not a table, a known field has an
            invalid value, or the legacy GPU flag is used and is not a boolean.
    """
    requirements = manifest.get("requirements", {})
    if not isinstance(requirements, Mapping):
        msg = "[requirements] must be a table"
        raise ValueError(msg)

    memory_gb = _parse_memory(requirements.get("memory_gb", 0), "memory_gb")
    browser_access = requirements.get("browser_access", False)
    if not isinstance(browser_access, bool):
        msg = "[requirements] browser_access must be true or false"
        raise ValueError(msg)

    gpu_memory_gb: int | None
    if "gpu_memory_gb" in requirements:
        gpu_memory_gb = _parse_memory(requirements["gpu_memory_gb"], "gpu_memory_gb")
    else:
        runtime = manifest.get("runtime")
        requires_gpu = runtime.get("requires_gpu", False) if isinstance(runtime, Mapping) else False
        if not isinstance(requires_gpu, bool):
            msg = "[runtime] requires_gpu must be true or false"
            raise ValueError(msg)
        gpu_memory_gb = None if requires_gpu else 0

    return PlacementRequirements(
        memory_gb=memory_gb,
        gpu_memory_gb=gpu_memory_gb,
        browser_access=browser_access,
    )
