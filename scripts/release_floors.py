# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Write the release floors of first-party requirements into ``pyproject.toml`` before a release build.

In source, a first-party requirement names the line it builds against with a ``>=X.Y.Z.dev0`` floor, so
staged builds of that line resolve. A release must only require released versions, so on a release tag
this script:

- rewrites each first-party ``>=X.Y.Z.dev0`` floor to ``>=X.Y.Z``;
- refuses any first-party requirement left with a pre-release version, or a version with more than three
  release segments (a build stamp such as ``3.5.0.20261126151233``);
- refuses when a first-party floor ``X.Y.Z`` is not released on PyPI.

Run it with ``packaging`` available, for instance
``uv run --no-project --with packaging python scripts/release_floors.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

FIRST_PARTY = frozenset({"3lc"})
PYPI_JSON = "https://pypi.org/pypi/{name}/json"

_DEV0_FLOOR = re.compile(r">=\s*(\d+\.\d+\.\d+)\.dev0(?![\w.])")


class ReleaseFloorError(Exception):
    """A first-party requirement that a release must not carry."""


def _requirements(pyproject: dict[str, object]) -> list[str]:
    project = pyproject.get("project")
    if not isinstance(project, dict):
        return []
    found: list[str] = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        found.extend(extra)
    return found


def _is_first_party(requirement: str) -> bool:
    return canonicalize_name(Requirement(requirement).name) in FIRST_PARTY


def rewrite_floors(text: str) -> tuple[str, list[Requirement]]:
    """Return ``text`` with first-party ``>=X.Y.Z.dev0`` floors raised to ``>=X.Y.Z``, and those requirements.

    Raises:
        ReleaseFloorError: A first-party requirement still names a pre-release or a build-stamped version.
    """
    first_party: list[Requirement] = []
    for original in _requirements(tomllib.loads(text)):
        if not _is_first_party(original):
            continue
        released = _DEV0_FLOOR.sub(r">=\1", original)
        if released != original:
            quoted = re.compile(r"([\"'])" + re.escape(original) + r"\1")
            text, count = quoted.subn(lambda m: m.group(1) + released + m.group(1), text)
            if count != 1:
                msg = f"expected one occurrence of {original!r} in pyproject.toml, found {count}"
                raise ReleaseFloorError(msg)
        requirement = Requirement(released)
        for spec in requirement.specifier:
            version = Version(spec.version.removesuffix(".*"))
            if version.is_prerelease:
                msg = f"{released!r} requires a pre-release: {spec}"
                raise ReleaseFloorError(msg)
            if len(version.release) > 3:
                msg = f"{released!r} requires a version with more than three release segments: {spec}"
                raise ReleaseFloorError(msg)
        first_party.append(requirement)
    return text, first_party


def floors(requirements: Iterable[Requirement]) -> list[tuple[str, str]]:
    """Return ``(name, version)`` for each ``>=`` floor among ``requirements``."""
    return [(req.name, spec.version) for req in requirements for spec in req.specifier if spec.operator == ">="]


def pypi_releases(name: str) -> set[str]:
    """Return the versions of ``name`` on PyPI that have at least one file that is not yanked."""
    with urllib.request.urlopen(PYPI_JSON.format(name=name), timeout=30) as response:
        data = json.load(response)
    return {version for version, files in data["releases"].items() if any(not f.get("yanked", False) for f in files)}


def check_released(requirements: Iterable[Requirement], releases: Callable[[str], set[str]] = pypi_releases) -> None:
    """Refuse a first-party floor that is not released on PyPI.

    Raises:
        ReleaseFloorError: A floor names a version PyPI does not have.
    """
    for name, version in floors(requirements):
        available = {Version(v) for v in releases(name)}
        if Version(version) not in available:
            msg = f"{name} {version} is not released on PyPI; release it before this package"
            raise ReleaseFloorError(msg)


def main(argv: list[str] | None = None, releases: Callable[[str], set[str]] = pypi_releases) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pyproject", nargs="?", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args(argv)
    path: Path = args.pyproject
    try:
        text, first_party = rewrite_floors(path.read_text(encoding="utf-8"))
        check_released(first_party, releases)
    except ReleaseFloorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    path.write_text(text, encoding="utf-8")
    for requirement in first_party:
        print(f"release requirement: {requirement}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
