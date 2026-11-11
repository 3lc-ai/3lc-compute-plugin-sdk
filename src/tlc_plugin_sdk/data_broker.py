# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Read-only storage for isolated Python, using the selected node's normal 3LC adapters.

The sandbox gets data and the SDK's identity-only spawn handoff, never a cloud
credential. The trusted worker on that same node enforces every read; data never
traverses the deployment control channel. Local paths are limited to configured
project roots and data aliases, with private/config files excluded. Opt-in cloud
access uses the native adapter and the node's storage grants. Responses and the
per-turn cache are bounded.
"""

from __future__ import annotations

import base64
import json
import os
import posixpath
import re
import stat
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

CHUNK_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TURN_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 10000
_PRIVATE_PARTS = {".ssh", ".config", ".aws", ".azure", ".gnupg", ".kube", ".git", ".netrc", ".3lc-compute", "Keychains"}
_CONFIG_SUFFIXES = (".yaml", ".yml", ".env", ".pem", ".key", ".sqlite", ".sqlite3", ".db")


def _canonical(url: str) -> tuple[str, str, str]:
    # urlsplit discards some control characters. Reject them before parsing so
    # the authorization check and the storage adapter see the same object key.
    if any(ord(c) < 32 or ord(c) == 127 for c in unquote(url)):
        message = "Invalid data URL"
        raise ValueError(message)
    parts = urlsplit(url)
    azure_container = (
        parts.scheme in {"abfs", "abfss"}
        and parts.username
        and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", parts.username)
        and re.fullmatch(r"[a-z0-9]+\.(?:dfs|blob)\.core\.windows\.net", parts.hostname or "")
    )
    if (
        (parts.username and not azure_container)
        or parts.password
        or parts.port
        or parts.query
        or parts.fragment
        or "\\" in url
    ):
        msg = "Invalid data URL"
        raise ValueError(msg)
    if parts.scheme in ("", "file"):
        if parts.netloc and parts.netloc != "localhost":
            msg = "Remote file authorities are not supported"
            raise ValueError(msg)
        return "file", "", str(Path(unquote(parts.path)).resolve())
    if parts.scheme not in {"s3", "gs", "az", "azure", "abfs", "abfss"}:
        msg = "Data reads require a configured storage root"
        raise ValueError(msg)
    if (
        not parts.netloc
        or any(c.isspace() for c in parts.netloc)
        or any(c.isspace() and c != " " for c in parts.path)
        or url[:1].isspace()
        or any(segment in {".", ".."} for segment in unquote(parts.path).split("/"))
        or re.search(r"%(?:2f|5c)", parts.path, re.IGNORECASE)
    ):
        msg = "Ambiguous cloud data URL"
        raise ValueError(msg)
    return parts.scheme, parts.netloc, posixpath.normpath(parts.path)


def _safe_root(value: str, protected_roots: tuple[Path, ...] = ()) -> bool:
    try:
        scheme, _, path = _canonical(value)
        if scheme == "file":
            root = Path(path)
            protected = (
                Path.home(),
                Path.home() / ".3lc-compute",
                Path.home() / "Library/Application Support/3LC",
                *protected_roots,
            )
            if any(p == root or root in p.parents for p in protected):
                return False
        return not any(part in _PRIVATE_PARTS or part.startswith(".env") for part in Path(path).parts)
    except (ValueError, OSError):
        return False


def _trusted_aliases() -> dict[str, str]:
    from tlc.helpers.url_aliases import get_registered_url_aliases
    from tlcconfig import ConfigStore
    from tlcconfig.default_locations import get_default_root_dir

    trusted = {
        "USER_CONFIG_FILE",
        "ENVIRONMENT",
        "COMMAND_LINE",
        "API",
        "CONFIG_SERVICE_DEFAULT",
        "CONFIG_SERVICE_ADVISORY",
        "CONFIG_SERVICE_AXIOM",
    }
    provenance = ConfigStore.instance().get_provenance("aliases")
    # Legacy deployment-wide aliases are a local configuration file, never a
    # remote dataset's alias file. Preserve that existing configuration path.
    legacy = (Path(get_default_root_dir()) / "projects/default_aliases.3lc.yaml").resolve()
    aliases = {}
    for name, value in get_registered_url_aliases().items():
        source = provenance.get(name) if isinstance(provenance, dict) else None
        if source is None:
            continue
        local_config = False
        if source.source.name == "DATA_CONFIG_FILE" and source.source_info:
            try:
                scheme, _, path = _canonical(source.source_info)
                local_config = scheme == "file" and Path(path) == legacy
            except (ValueError, OSError):
                pass
        if (source.source.name in trusted or local_config) and _safe_root(str(value)):
            aliases[name] = str(value)
    return aliases


def configured_roots() -> list[str]:
    """Data locations of this compute node, resolved by its normal 3LC configuration.

    Central configuration and the node's own aliases/identity are already installed
    by the worker. No caller-supplied URL can add a location to this allow-list.
    """
    import tlc
    from tlc._core.objects.mutable_objects.configuration import Configuration

    roots = [str(tlc.config.project_root_url or "")]
    for entry in Configuration.instance().scan_urls:
        if not isinstance(entry, dict) or entry.get("layout") != "project":
            continue
        if not entry.get("url"):
            continue
        try:
            roots.append(str(tlc.Url(str(entry["url"]))))
        except ValueError:
            continue  # A configured but unavailable URL adapter grants no access.
    return list(dict.fromkeys(root for root in roots if root))


def _entry(entry: Any) -> dict[str, Any]:
    return {"name": str(entry.name), "path": str(entry.path), "is_dir": bool(entry.is_dir()), "size": int(entry.size)}


class DataBroker:
    """One turn's authorized data locations and bounded immutable byte cache."""

    def __init__(
        self,
        roots: list[str],
        *,
        aliases: dict[str, str] | None = None,
        protected_roots: tuple[Path, ...] = (),
        allow_node_cloud_storage: bool = False,
    ) -> None:
        self.allow_node_cloud_storage = allow_node_cloud_storage
        self.protected_roots = tuple(p.resolve() for p in protected_roots)
        self.aliases = _trusted_aliases() if aliases is None else dict(aliases)
        self.roots = list(
            dict.fromkeys(v for v in [*roots, *self.aliases.values()] if _safe_root(v, self.protected_roots))
        )
        self.cache: OrderedDict[str, bytes] = OrderedDict()
        self.transferred = 0
        self._lock = threading.Lock()

    def bootstrap(self) -> dict[str, Any]:
        from _tlc_auth_state import ensure_key_activated
        from _tlcnative._core import _emit_inherited_token

        ensure_key_activated()
        identity = _emit_inherited_token()
        if not identity:
            msg = "The deployment has no active 3LC SDK identity"
            raise PermissionError(msg)
        # Fail closed if the native spawn contract ever gains credential fields.
        claims = json.loads(base64.b64decode(identity))
        if set(claims) != {"tenant_id", "user_id", "expires_at", "auth_mode", "authorization_context"}:
            msg = "Unsupported SDK identity handoff"
            raise PermissionError(msg)
        return {"identity": identity, "aliases": self.aliases, "roots": self.roots}

    def checked(self, value: str) -> str:
        from tlc import Url

        if len(value) > 4096:
            msg = "Data URL is too long"
            raise ValueError(msg)
        value = str(Url(value).expand_aliases(allow_unexpanded=False).to_absolute())
        wanted = _canonical(value)
        path = Path(wanted[2])
        if (
            any(p in _PRIVATE_PARTS or p.startswith((".env", ".3lc-stack")) for p in path.parts)
            or path.name.lower().endswith(_CONFIG_SUFFIXES)
            or (wanted[0] == "file" and any(path.is_relative_to(root) for root in self.protected_roots))
        ):
            msg = "Private files are not data sources"
            raise PermissionError(msg)
        if self.allow_node_cloud_storage and wanted[0] in {"s3", "gs", "abfs", "abfss"}:
            # Explicit native cloud schemes only; adapter IAM remains authoritative.
            # Local files retain the configured-root boundary below.
            return value
        for root in self.roots:
            base = _canonical(root)
            if wanted[:2] == base[:2] and (wanted[2] == base[2] or wanted[2].startswith(base[2].rstrip("/") + "/")):
                # Use the canonical local path after validation, not an alias/symlink again.
                return wanted[2] if wanted[0] == "file" else value
        msg = "Data URL is outside this compute node's configured data locations"
        raise PermissionError(msg)

    @staticmethod
    def _local_bytes(path: str) -> bytes:
        parts = Path(path).parts
        directory = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
                msg = "Data file is not a bounded regular file"
                raise ValueError(msg)
            return stream.read(MAX_FILE_BYTES + 1)

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        # A job normally has one outstanding host request. Serialize here as well
        # so cache and transfer admission remain bounded for other callers.
        with self._lock:
            return self._request(payload)

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        from tlc import Url
        from tlcurl.url_adapters._registry import UrlAdapterRegistry

        operation = payload.get("operation")
        if operation not in {"read", "stat", "exists", "is_dir", "list"}:
            msg = "Python storage access is read-only"
            raise PermissionError(msg)
        try:
            value = self.checked(str(payload.get("url") or ""))
        except PermissionError:
            if operation in {"exists", "is_dir"}:
                return {"value": False}
            raise
        url = Url(value)
        try:
            if operation == "exists":
                return {"value": url.exists()}
            if operation == "is_dir":
                return {"value": UrlAdapterRegistry.is_dir(url)}
            if operation == "stat":
                return {"value": _entry(UrlAdapterRegistry.stat(url))}
            if operation == "list":
                entries: list[dict[str, Any]] = []
                for entry in UrlAdapterRegistry.list_dir(url):
                    if len(entries) >= MAX_ENTRIES:
                        msg = "Data directory exceeds the listing limit"
                        raise ValueError(msg)
                    try:
                        self.checked(str(entry.path))
                    except PermissionError:
                        continue
                    entries.append(_entry(entry))
                return {"value": entries}
            offset = payload.get("offset", 0)
            if not isinstance(offset, int) or offset < 0 or offset > MAX_FILE_BYTES:
                msg = "Invalid data offset"
                raise ValueError(msg)
            if self.transferred >= MAX_TURN_BYTES:
                msg = "This job exceeded its 512 MiB sandbox read limit"
                raise ValueError(msg)
            content = self.cache.get(value)
            if content is None:
                size = UrlAdapterRegistry.get_file_size(url)
                if size < 0 or size > MAX_FILE_BYTES:
                    msg = "Data file exceeds the 64 MiB read limit"
                    raise ValueError(msg)
                if _canonical(value)[0] == "file":
                    content = self._local_bytes(value)
                else:
                    reader = getattr(url, "read_bytes_bounded", None)
                    if not callable(reader):
                        msg = "Update this node's 3LC library: secure cloud reads require bounded native I/O"
                        raise RuntimeError(msg)
                    content = reader(MAX_FILE_BYTES)
                if len(content) > MAX_FILE_BYTES:
                    msg = "Data file exceeds the 64 MiB read limit"
                    raise ValueError(msg)
                self.cache[value] = content
                while sum(map(len, self.cache.values())) > MAX_FILE_BYTES:
                    self.cache.popitem(last=False)
            chunk = content[offset : offset + CHUNK_BYTES]
            if self.transferred + len(chunk) > MAX_TURN_BYTES:
                msg = "This job exceeded its 512 MiB sandbox read limit"
                raise ValueError(msg)
            self.transferred += len(chunk)
            self.cache.move_to_end(value)
            return {"data": base64.b64encode(chunk).decode(), "size": len(content), "offset": offset}
        except FileNotFoundError:
            return {"missing": True}
