# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Deprecation warnings for the ``shared.*`` helpers that move to 3lc (or go) in SDK 0.6.

SDK 0.6 never imports 3lc, so every helper that works with 3LC data leaves the SDK: its
replacement is public API in 3lc 3.5. Until then the old code runs unchanged and warns, naming
the replacement. The warning is a :class:`DeprecationWarning` attributed to the plugin's call:
Python shows it in a plugin's tests and in ``__main__``, not in a running worker's log.

Python hides a ``DeprecationWarning`` in a running worker, so the first use of each deprecated name
in a process is also logged once, as a warning through this module's logger, with the same text
and the plugin's call site: migration shows in worker logs on real deployments.

A helper called by another SDK module (one shared helper built on another, the worker applying
overrides) does not warn: the plugin's own call already did, or the plugin made none.
"""

from __future__ import annotations

import functools
import inspect
import logging
import sys
import threading
import warnings
from collections.abc import Callable
from typing import ParamSpec, TypeVar

_P = ParamSpec("_P")
_R = TypeVar("_R")

_SDK = "tlc_plugin_sdk"

logger = logging.getLogger(__name__)

_logged: set[str] = set()
_logged_lock = threading.Lock()


def warn_deprecated(name: str, message: str, *, stacklevel: int = 2) -> None:
    """Warn that *name* is deprecated, and log it the first time this process uses it.

    Args:
        name: What is deprecated; the log line is written once per name per process.
        message: The warning's text, which names the replacement.
        stacklevel: As for :func:`warnings.warn`, counted from the caller of this function: ``2``
            attributes the warning (and the logged call site) to whoever called that caller.

    """
    frame = sys._getframe(stacklevel)
    with _logged_lock:
        first = name not in _logged
        _logged.add(name)
    if first:
        # Logged before warning: an "error" warnings filter turns the warning into an exception.
        logger.warning("%s (first use in this process: %s:%d)", message, frame.f_code.co_filename, frame.f_lineno)
    warnings.warn(message, DeprecationWarning, stacklevel=stacklevel + 1)


def moved_to_3lc(instead: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Mark a ``shared.*`` function that SDK 0.6 removes; *instead* says what replaces it.

    Write *instead* as a clause naming the replacement exactly, for example
    ``"use tlc.helpers.ImageHelper.find_image_column (3lc 3.5) instead"``.
    """

    def decorate(func: Callable[_P, _R]) -> Callable[_P, _R]:
        name = f"{func.__module__}.{func.__qualname__}"
        message = f"{name} is deprecated and is removed in SDK 0.6: {instead}."

        @functools.wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            caller = sys._getframe(1).f_globals.get("__name__", "")
            if caller != _SDK and not caller.startswith(_SDK + "."):
                warn_deprecated(name, message)
            return func(*args, **kwargs)

        doc = inspect.cleandoc(func.__doc__ or "")
        wrapper.__doc__ = f"{doc}\n\n.. deprecated:: 0.5.5\n   {message}\n"
        return wrapper

    return decorate
