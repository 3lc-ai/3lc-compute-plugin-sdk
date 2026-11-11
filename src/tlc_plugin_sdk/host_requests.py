# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Bounded request/reply channel over a job's existing authenticated control transport."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

MAX_BYTES = 8 * 1024 * 1024
MAX_PENDING = 16


class HostRequests:
    def __init__(self, emit: Callable[[dict[str, Any]], None], cancelled: threading.Event) -> None:
        self.emit, self.cancelled = emit, cancelled
        self._lock = threading.Lock()
        self._capacity = threading.BoundedSemaphore(MAX_PENDING)
        self._pending: dict[str, tuple[dict[str, Any], threading.Event, dict[str, Any]]] = {}

    def call(self, operation: str, payload: dict[str, Any], timeout: float = 120) -> Any:
        if not 0 < timeout <= 3600 or len(json.dumps(payload).encode()) > MAX_BYTES:
            msg = "Invalid host request size or deadline"
            raise ValueError(msg)
        request_id = uuid.uuid4().hex
        request = {
            "event": "host_request",
            "request_id": request_id,
            "operation": operation,
            "payload": payload,
            "timeout": timeout,
        }
        ready = threading.Event()
        result: dict[str, Any] = {}
        deadline = time.monotonic() + timeout
        while not self._capacity.acquire(timeout=min(0.1, timeout)):
            if self.cancelled.is_set():
                msg = "Job cancelled during host request admission"
                raise RuntimeError(msg)
            if time.monotonic() >= deadline:
                msg = "The job's host request channel is busy"
                raise TimeoutError(msg)
        with self._lock:
            self._pending[request_id] = (request, ready, result)
        try:
            self.emit(request)
            while not ready.wait(0.1):
                if self.cancelled.is_set():
                    msg = "Job cancelled during host request"
                    raise RuntimeError(msg)
                if time.monotonic() >= deadline:
                    msg = "The deployment did not answer; the operation's outcome is unknown"
                    raise TimeoutError(msg)
            if "error" in result:
                raise RuntimeError(str(result["error"]))
            return result.get("result")
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
            self._capacity.release()

    def answer(self, request_id: str, data: dict[str, Any]) -> bool:
        if len(json.dumps(data).encode()) > MAX_BYTES:
            msg = "Host response too large"
            raise ValueError(msg)
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending[1].is_set():
                return False
            pending[2].update(data)
            pending[1].set()
        return True

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(p[0]) for p in self._pending.values() if not p[1].is_set()]
