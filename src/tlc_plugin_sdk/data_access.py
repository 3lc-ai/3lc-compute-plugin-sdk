# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Use the real 3LC SDK inside a credential-free, network-isolated Python process.

The caller supplies a bounded synchronous transport to its authorized host.
Only storage reads cross that transport. Workspace files keep their ordinary
local adapter, so Pandas/3LC transformations and generated figures stay local.
This module does not import the data plane until explicitly installed.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

Transport = Callable[[dict[str, Any]], dict[str, Any]]
MAX_FILE_BYTES = 64 * 1024 * 1024


def install(transport: Transport, bootstrap: dict[str, Any], workspace: str) -> None:
    """Install read adapters after importing the SDK with its identity-only handoff."""
    import os

    root = Path(workspace).resolve()
    os.environ["TLC_AUTH_INHERITED_HANDLE"] = str(bootstrap["identity"])
    os.environ["TLC_PROJECT_ROOT_URL"] = str(root / "projects")
    os.environ["TLC_SCAN_URLS"] = "[]"
    os.environ["TLC_DEFAULT_ROOT_DIR"] = str(root / ".3lc")
    # An isolated notebook has no telemetry egress or API-key refresh authority.
    os.environ["TLC_SENTRY_ENVIRONMENT"] = "local"
    import tlc
    from _tlc_auth_state import _import_inherited_handle
    from tlcurl.url_adapters._base import UrlAdapter, UrlAdapterDirEntry
    from tlcurl.url_adapters._registry import UrlAdapterRegistry

    # A reused notebook refreshes the inherited claims without gaining a key.
    _import_inherited_handle(str(bootstrap["identity"]))
    os.environ.pop("TLC_AUTH_INHERITED_HANDLE", None)
    for name, value in bootstrap.get("aliases", {}).items():
        tlc.url.register_url_alias(name, value, force=True)

    class ReadAdapter(UrlAdapter):
        def schemes(self) -> list[str]:
            return ["file", "s3", "gs", "abfs", "abfss"]

        def _local(self, url: Any) -> bool:
            return str(url.scheme) in {"file", "Scheme.FILE"} and Path(
                str(url).removeprefix("file://")
            ).resolve().is_relative_to(root)

        def _ask(self, operation: str, url: Any, **extra: Any) -> dict[str, Any]:
            answer = transport({"operation": operation, "url": str(url), **extra})
            if answer.get("missing"):
                raise FileNotFoundError(str(url))
            if answer.get("error"):
                raise PermissionError(str(answer["error"]))
            return answer

        def read_binary_content_from_url(self, url: Any) -> bytes:
            if self._local(url):
                return Path(str(url).removeprefix("file://")).read_bytes()
            result = bytearray()
            while True:
                answer = self._ask("read", url, offset=len(result))
                size = answer.get("size")
                if not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES or answer.get("offset") != len(result):
                    msg = "Invalid storage response"
                    raise ValueError(msg)
                chunk = base64.b64decode(answer["data"], validate=True)
                if len(result) + len(chunk) > size or (not chunk and len(result) < size):
                    msg = "Invalid storage chunk"
                    raise ValueError(msg)
                result.extend(chunk)
                if len(result) == size:
                    return bytes(result)

        def exists(self, url: Any) -> bool:
            if self._local(url):
                return Path(str(url).removeprefix("file://")).exists()
            answer = self._ask("exists", url)
            return bool(answer.get("value"))

        def is_dir(self, url: Any) -> bool:
            if self._local(url):
                return Path(str(url).removeprefix("file://")).is_dir()
            return bool(self._ask("is_dir", url).get("value"))

        def stat(self, url: Any) -> Any:
            if self._local(url):
                path = Path(str(url).removeprefix("file://"))
                return UrlAdapterDirEntry(path.name, str(path), is_dir=path.is_dir(), size=path.stat().st_size)
            return UrlAdapterDirEntry(**self._ask("stat", url)["value"])

        def get_file_size(self, url: Any) -> int:
            return int(self.stat(url).size)

        def list_dir(self, url: Any) -> Iterator[Any]:
            if self._local(url):
                for path in Path(str(url).removeprefix("file://")).iterdir():
                    yield self.stat(tlc.Url(str(path)))
            else:
                for value in self._ask("list", url)["value"]:
                    yield UrlAdapterDirEntry(**value)

        def write_binary_content_to_url(self, url: Any, content: bytes) -> Any:
            if self._local(url):
                Path(str(url).removeprefix("file://")).write_bytes(content)
                return url
            msg = "Publish data through an approved plugin operation"
            raise PermissionError(msg)

        def make_dirs(self, url: Any, exist_ok: bool = False) -> None:
            if self._local(url):
                Path(str(url).removeprefix("file://")).mkdir(parents=True, exist_ok=exist_ok)
                return
            msg = "Publish data through an approved plugin operation"
            raise PermissionError(msg)

    UrlAdapterRegistry.register_url_adapter(ReadAdapter(), force=True)
