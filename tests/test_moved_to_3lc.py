# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The ``shared.*`` helpers that move to 3lc warn, naming their replacement; the old code still runs.

SDK 0.6 removes them (it never imports 3lc). 0.5.5 keeps each one as it was and adds a
``DeprecationWarning`` attributed to the plugin's call, naming the 3lc 3.5 replacement exactly. A
helper called from another SDK module does not warn.
"""

from __future__ import annotations

import importlib
import logging
import subprocess
import sys
import threading
import types
import warnings
from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk.shared import _deprecation, aliases, images, labels, modality, model_storage, url_utils
from tlc_plugin_sdk.shared._deprecation import moved_to_3lc
from tlc_plugin_sdk.shared.storage_transfer import TransferRegistry

# Every name SDK 0.6 removes, with the replacement its warning must name (api-spec section 10,
# decisions 2026-10-28).
MOVED: dict[str, str] = {
    "url_utils.normalize_url": "3lc has no replacement for it yet",
    "url_utils.get_url_column_names": "tlc.helpers.SchemaHelper.nested_url_columns(table.rows_schema)",
    "url_utils.is_folder": "tlc.Url.is_dir",
    "url_utils.is_file": "tlc.Url.is_file",
    "url_utils.read_bytes": "tlc.Url.read_bytes",
    "url_utils.read_text": "tlc.Url.read_text",
    "url_utils.list_folder": "tlc.Url.list_dir",
    "url_utils.iter_files": "tlc.Url.iter_files",
    "images.get_image_column": "tlc.helpers.ImageHelper.find_image_column",
    "images.resolve_image_url": "tlc.Url(img_path).to_absolute(table_url, allow_unexpanded=False)",
    "images.load_image": "tlc.helpers.ImageHelper.open",
    "images.read_image_size": "tlc.helpers.ImageHelper.get_exif_image_dimensions",
    "images.read_image_from_table": "tlc.helpers.ImageHelper.open with tlc.helpers.ImageHelper.get_image_url",
    "images.get_image_paths": "tlc.helpers.ImageHelper.get_image_urls",
    "images.list_image_urls": "tlc.Url.iter_files with extensions=tlc.constants.IMAGE_FILE_EXTENSIONS",
    "labels.get_label_map": "tlc.Table.get_simple_value_map with the label_path of tlc.helpers.AnnotationHelper.get",
    "labels.get_label_names": "tlc.Table.get_simple_value_map with the label_path of tlc.helpers.AnnotationHelper.get",
    "labels.get_class_name_lookup": "tlc.Table.get_simple_value_map",
    "labels.get_display_value_map": "tlc.Table.get_simple_value_map with use_display_names=True",
    "labels.find_label_column": (
        "tlc.helpers.AnnotationHelper.find_all with type=tlc.helpers.AnnotationType.CATEGORICAL_LABEL"
    ),
    "modality.detect_modality_from_table": "tlc.helpers.AnnotationHelper.detect_task",
    "model_storage.save_model_to_run": "tlc.helpers.ModelHelper.save_to_run",
    "model_storage.store_model_info_in_run": "tlc.helpers.ModelHelper.set_run_model_info",
    "aliases.copy_folder_to_url": "it has no replacement",
    "aliases.relative_alias_value": "tlc.helpers.ProjectHelper.register_data_alias",
    "aliases.validate_alias_location": "tlc.helpers.ProjectHelper.register_data_alias",
    "aliases.register_alias": "tlc.helpers.ProjectHelper.register_data_alias",
    "aliases.register_source_alias": "tlc.integration.compute.register_source_alias",
    "aliases.register_run_alias": "tlc.integration.compute.register_run_alias",
    "aliases.get_table_aliases": "tlc.url.get_table_url_aliases",
    "aliases.apply_alias_overrides": "tlc.url.override_url_aliases",
    "aliases.restore_aliases": "tlc.url.override_url_aliases",
    "storage_transfer.notify_storage_deletes": "tlc.discovery.notify_storage_changes",
}

# What stays in the SDK at 0.6, pure: none of it may warn.
STAYS: dict[str, list[str]] = {
    "url_utils": [
        "is_url",
        "normalize_local_path",
        "normalize_path_or_url",
        "join_path_or_url",
        "parent_of",
        "name_of",
        "stem_of",
        "suffix_of",
        "is_absolute",
    ],
    "aliases": ["is_remote_url", "default_alias_token"],
}


def _resolve(dotted: str) -> Any:
    module, name = dotted.split(".")
    return getattr(importlib.import_module(f"tlc_plugin_sdk.shared.{module}"), name)


@pytest.mark.parametrize(("dotted", "replacement"), MOVED.items())
def test_every_moved_name_says_where_it_went(dotted: str, replacement: str) -> None:
    func = _resolve(dotted)
    module, name = dotted.split(".")
    assert func.__name__ == name  # the wrapper keeps the name, signature and docstring
    assert func.__wrapped__.__doc__.split("\n")[0] in func.__doc__
    assert ".. deprecated:: 0.5.5" in func.__doc__
    expected = f"tlc_plugin_sdk.shared.{module}.{name} is deprecated and is removed in SDK 0.6: "
    assert expected in func.__doc__
    assert replacement in func.__doc__


@pytest.mark.parametrize(("module", "names"), STAYS.items())
def test_the_pure_helpers_that_stay_are_not_deprecated(module: str, names: list[str]) -> None:
    mod = importlib.import_module(f"tlc_plugin_sdk.shared.{module}")
    for name in names:
        assert not hasattr(getattr(mod, name), "__wrapped__"), name


def test_nothing_else_in_the_moved_modules_is_public_and_unmarked() -> None:
    """A public function added to a moving module later must be classified here."""
    stays = {f"{m}.{n}" for m, names in STAYS.items() for n in names}
    for mod in (aliases, images, labels, model_storage, modality, url_utils):
        short = mod.__name__.rsplit(".", 1)[-1]
        for name, value in vars(mod).items():
            if name.startswith("_") or not isinstance(value, types.FunctionType):
                continue
            if value.__module__ != mod.__name__:
                continue  # imported, not defined here
            assert f"{short}.{name}" in MOVED or f"{short}.{name}" in stays, f"{short}.{name}"


# ── the warning, from real calls ──────────────────────────────────────────────────────────


def test_a_call_warns_once_at_the_plugins_line_and_still_works(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    with pytest.warns(DeprecationWarning) as record:
        text = url_utils.read_text(str(tmp_path / "a.txt"))  # read_text calls read_bytes inside the SDK
    assert text == "hello"
    assert len(record) == 1
    assert str(record[0].message) == (
        "tlc_plugin_sdk.shared.url_utils.read_text is deprecated and is removed in SDK 0.6: "
        "use tlc.Url.read_text instead."
    )
    assert record[0].filename == __file__


def test_a_helper_built_on_another_warns_only_for_the_plugins_call(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.jpg").write_bytes(b"")
    (tmp_path / "a.jpg").write_bytes(b"")
    with pytest.warns(DeprecationWarning) as record:
        found = url_utils.iter_files(str(tmp_path), extensions={".jpg"})  # walks with list_folder
    assert found == [str(tmp_path / "a.jpg"), str(tmp_path / "sub" / "b.jpg")]
    assert [str(w.message).split(" is ")[0] for w in record] == ["tlc_plugin_sdk.shared.url_utils.iter_files"]


def test_a_pure_moved_helper_runs_unchanged() -> None:
    with pytest.warns(DeprecationWarning, match="register_data_alias"):
        value = aliases.relative_alias_value("s3://b/projects", "p", "s3://b/projects/p/data/tok")
    assert value == "../../../../data/tok"


def test_the_pure_helpers_that_stay_do_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert url_utils.join_path_or_url("s3://b/x", "y") == "s3://b/x/y"
        assert aliases.default_alias_token("My COCO") == "MY_COCO"
        assert aliases.is_remote_url("s3://b")


# ── the decorator ─────────────────────────────────────────────────────────────────────────


def _module_calling(func: Any, module_name: str) -> Any:
    """A function defined in a module called *module_name* that calls *func*."""
    namespace: dict[str, Any] = {"__name__": module_name, "func": func}
    exec("def call():\n    return func()\n", namespace)
    return namespace["call"]


def _helper() -> str:
    """Do a thing."""
    return "done"


_moved = moved_to_3lc("use tlc.thing (3lc 3.5) instead")(_helper)


@pytest.mark.parametrize("module_name", ["tlc_plugin_sdk", "tlc_plugin_sdk.shared.images", "tlc_plugin_sdk.worker"])
def test_calls_from_inside_the_sdk_do_not_warn(module_name: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert _module_calling(_moved, module_name)() == "done"


@pytest.mark.parametrize("module_name", ["tlc_plugin_yolo", "tlc_plugin_sdk_extras", "__main__"])
def test_calls_from_anywhere_else_warn(module_name: str) -> None:
    with pytest.warns(DeprecationWarning, match=r"_helper is deprecated and is removed in SDK 0\.6: use tlc\.thing"):
        assert _module_calling(_moved, module_name)() == "done"


def test_the_docstring_gains_the_deprecation() -> None:
    assert _moved.__doc__ == (
        "Do a thing.\n\n.. deprecated:: 0.5.5\n   "
        f"{__name__}._helper is deprecated and is removed in SDK 0.6: use tlc.thing (3lc 3.5) instead.\n"
    )


# ── the log line: once per name per process, so worker logs show what still needs migrating ────


@pytest.fixture
def fresh_log(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """A process in which no deprecated name has been used yet, with the SDK's log captured."""
    monkeypatch.setattr(_deprecation, "_logged", set())
    caplog.set_level(logging.WARNING, logger="tlc_plugin_sdk.shared._deprecation")
    return caplog


def _deprecation_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "tlc_plugin_sdk.shared._deprecation"]


def test_the_first_use_of_a_name_is_logged_with_the_plugins_call_site(
    fresh_log: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    (tmp_path / "a.txt").write_text("hello")
    with pytest.warns(DeprecationWarning) as record:
        line = sys._getframe().f_lineno + 1
        url_utils.read_text(str(tmp_path / "a.txt"))
    [log] = _deprecation_logs(fresh_log)
    assert log == f"{record[0].message} (first use in this process: {__file__}:{line})"
    assert [r.levelno for r in fresh_log.records] == [logging.WARNING]


def test_later_uses_warn_but_are_not_logged_again(fresh_log: pytest.LogCaptureFixture) -> None:
    with pytest.warns(DeprecationWarning) as record:
        for _ in range(3):
            aliases.relative_alias_value("s3://b/projects", "p", "s3://b/projects/p/data/tok")
    assert len(record) == 3
    assert len(_deprecation_logs(fresh_log)) == 1


def test_each_name_is_logged_once(fresh_log: pytest.LogCaptureFixture) -> None:
    with pytest.warns(DeprecationWarning):
        for _ in range(2):
            aliases.relative_alias_value("s3://b/projects", "p", "s3://b/projects/p/data/tok")
            url_utils.get_url_column_names(object())
    logs = _deprecation_logs(fresh_log)
    assert len(logs) == 2
    assert logs[0].startswith("tlc_plugin_sdk.shared.aliases.relative_alias_value is deprecated")
    assert logs[1].startswith("tlc_plugin_sdk.shared.url_utils.get_url_column_names is deprecated")


def test_concurrent_first_uses_log_once(fresh_log: pytest.LogCaptureFixture) -> None:
    moved = moved_to_3lc("use tlc.thing (3lc 3.5) instead")(_helper)
    start = threading.Barrier(16)

    def call() -> None:
        start.wait()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            moved()

    threads = [threading.Thread(target=call) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(_deprecation_logs(fresh_log)) == 1


def test_calls_from_inside_the_sdk_are_not_logged(fresh_log: pytest.LogCaptureFixture) -> None:
    moved = moved_to_3lc("use tlc.thing (3lc 3.5) instead")(_helper)
    _module_calling(moved, "tlc_plugin_sdk.shared.images")()
    assert _deprecation_logs(fresh_log) == []


def test_the_log_line_is_written_even_when_warnings_are_errors(fresh_log: pytest.LogCaptureFixture) -> None:
    moved = moved_to_3lc("use tlc.thing (3lc 3.5) instead")(_helper)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        with pytest.raises(DeprecationWarning):
            moved()
    assert len(_deprecation_logs(fresh_log)) == 1


def test_a_transfer_registry_without_on_change_is_logged_once(fresh_log: pytest.LogCaptureFixture) -> None:
    calls = {
        "list_objects": lambda _url: [],
        "head_object": lambda _url: None,
        "copy_object": lambda _src, _dst: None,
        "delete_object": lambda _url: None,
    }
    with pytest.warns(DeprecationWarning):
        TransferRegistry(**calls)
        TransferRegistry(**calls)
        TransferRegistry(**calls, notify_change=lambda _u, _o: None)
    logs = _deprecation_logs(fresh_log)
    assert len(logs) == 2
    assert logs[0].startswith("TransferRegistry without on_change")
    assert logs[1].startswith("TransferRegistry(notify_change=...) is deprecated")
    assert all(f"first use in this process: {__file__}:" in log for log in logs)


def test_warning_and_logging_never_import_3lc() -> None:
    code = (
        "import sys, warnings;"
        "warnings.simplefilter('ignore');"
        "from tlc_plugin_sdk.shared import url_utils;"
        "url_utils.get_url_column_names(object());"
        "sys.exit('tlc imported' if 'tlc' in sys.modules else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
