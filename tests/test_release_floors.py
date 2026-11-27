# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The release job's floor rewrite: a released wheel requires only released first-party versions."""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_floors.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_floors", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rf = _load()


def _pyproject(*deps: str) -> str:
    lines = "\n".join(f'    "{dep}",' for dep in deps)
    return textwrap.dedent(
        """\
        [project]
        name = "demo"
        version = "0.7.0"
        dependencies = [
        {lines}
        ]
        """
    ).format(lines=lines)


def _released(*versions: str) -> object:
    return lambda name: set(versions) if name == "3lc" else set()


def test_dev0_floor_becomes_the_release_floor() -> None:
    text, reqs = rf.rewrite_floors(_pyproject("uvicorn>=0.34.0", "3lc[pandas]>=3.5.0.dev0,<4.0.0"))
    assert '"3lc[pandas]>=3.5.0,<4.0.0",' in text
    assert '"uvicorn>=0.34.0",' in text
    assert [str(r) for r in reqs] == ["3lc[pandas]<4.0.0,>=3.5.0"]


def test_released_floor_is_left_as_it_is() -> None:
    source = _pyproject("3lc[pandas]>=3.5.0,<4.0.0")
    text, _ = rf.rewrite_floors(source)
    assert text == source


def test_other_packages_are_not_touched() -> None:
    source = _pyproject("litestar>=2.22.0.dev0,<3.0.0", "3lc>=3.5.0.dev0")
    text, reqs = rf.rewrite_floors(source)
    assert '"litestar>=2.22.0.dev0,<3.0.0",' in text
    assert '"3lc>=3.5.0",' in text
    assert [r.name for r in reqs] == ["3lc"]


def test_optional_dependencies_are_rewritten() -> None:
    source = _pyproject("uvicorn") + '\n[project.optional-dependencies]\nfull = ["3lc>=3.5.0.dev0"]\n'
    text, _ = rf.rewrite_floors(source)
    assert 'full = ["3lc>=3.5.0"]' in text


@pytest.mark.parametrize(
    "dep",
    [
        "3lc[pandas]>=3.5.0.dev154037,<4.0.0",
        "3lc[pandas]>=3.5.0b1,<4.0.0",
        "3lc[pandas]>=3.5.0rc1",
        "3lc[pandas]>=3.5.0.20261126151233",
        "3lc[pandas]>=3.5.0.1",
        "3lc==3.5.0.dev3",
    ],
)
def test_refuses_a_floor_that_is_not_a_release(dep: str) -> None:
    with pytest.raises(rf.ReleaseFloorError):
        rf.rewrite_floors(_pyproject(dep))


def test_refuses_a_floor_not_on_pypi() -> None:
    _, reqs = rf.rewrite_floors(_pyproject("3lc[pandas]>=3.5.0.dev0,<4.0.0"))
    with pytest.raises(rf.ReleaseFloorError, match=r"3lc 3\.5\.0 is not released on PyPI"):
        rf.check_released(reqs, _released("3.4.2", "3.5.0b3"))
    rf.check_released(reqs, _released("3.4.2", "3.5.0"))


def test_main_writes_the_release_floor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(_pyproject("3lc[pandas]>=3.5.0.dev0,<4.0.0"), encoding="utf-8")
    assert rf.main([str(path)], _released("3.5.0")) == 0
    assert '"3lc[pandas]>=3.5.0,<4.0.0",' in path.read_text(encoding="utf-8")
    assert "release requirement: 3lc[pandas]<4.0.0,>=3.5.0" in capsys.readouterr().out


def test_main_leaves_the_file_alone_on_refusal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "pyproject.toml"
    source = _pyproject("3lc[pandas]>=3.5.0.dev0,<4.0.0")
    path.write_text(source, encoding="utf-8")
    assert rf.main([str(path)], _released("3.4.2")) == 1
    assert path.read_text(encoding="utf-8") == source
    assert "not released on PyPI" in capsys.readouterr().err


def test_repo_pyproject_has_a_release_floor() -> None:
    _, reqs = rf.rewrite_floors((_SCRIPT.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert [r.name for r in reqs] == ["3lc"]
    assert all(".dev" not in str(r) for r in reqs)
