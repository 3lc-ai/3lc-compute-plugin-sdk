# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""A failed job reports what failed, not how many errors a task group held."""

from __future__ import annotations

from tlc_plugin_sdk.worker import describe_failure


def test_an_exception_is_its_type_and_message() -> None:
    assert describe_failure(ValueError("bad value")) == "ValueError: bad value"


def test_an_exception_group_reports_its_leaves() -> None:
    inner = ExceptionGroup("inner", [ConnectionError("connection refused")])
    group = ExceptionGroup("unhandled errors in a TaskGroup", [inner, TimeoutError("read timed out")])
    assert describe_failure(group) == "ConnectionError: connection refused; TimeoutError: read timed out"
    assert describe_failure(ExceptionGroup("empty", [RuntimeError("")])) == "RuntimeError: "
