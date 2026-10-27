# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Deprecation warnings for the ``shared.*`` helpers that move to 3lc (or go) in SDK 0.6.

SDK 0.6 never imports 3lc, so every helper that works with 3LC data leaves the SDK: its
replacement is public API in 3lc 3.5. Until then the old code runs unchanged and warns, naming
the replacement. The warning is a :class:`DeprecationWarning` attributed to the plugin's call:
Python shows it in a plugin's tests and in ``__main__``, not in a running worker's log.

A helper called by another SDK module (one shared helper built on another, the worker applying
overrides) does not warn: the plugin's own call already did, or the plugin made none.
"""

from __future__ import annotations

import functools
import inspect
import sys
import warnings
from collections.abc import Callable
from typing import ParamSpec, TypeVar

_P = ParamSpec("_P")
_R = TypeVar("_R")

_SDK = "tlc_plugin_sdk"


def moved_to_3lc(instead: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Mark a ``shared.*`` function that SDK 0.6 removes; *instead* says what replaces it.

    Write *instead* as a clause naming the replacement exactly, for example
    ``"use tlc.helpers.ImageHelper.find_image_column (3lc 3.5) instead"``.
    """

    def decorate(func: Callable[_P, _R]) -> Callable[_P, _R]:
        message = f"{func.__module__}.{func.__qualname__} is deprecated and is removed in SDK 0.6: {instead}."

        @functools.wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            caller = sys._getframe(1).f_globals.get("__name__", "")
            if caller != _SDK and not caller.startswith(_SDK + "."):
                warnings.warn(message, DeprecationWarning, stacklevel=2)
            return func(*args, **kwargs)

        doc = inspect.cleandoc(func.__doc__ or "")
        wrapper.__doc__ = f"{doc}\n\n.. deprecated:: 0.5.5\n   {message}\n"
        return wrapper

    return decorate
