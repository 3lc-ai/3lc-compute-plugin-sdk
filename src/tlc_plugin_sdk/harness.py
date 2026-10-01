# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Run a plugin's routes in-process, without a compute service.

The worker (:mod:`tlc_plugin_sdk.worker`) serves a plugin's Litestar app on a socket for the host
to proxy to. The harness builds the **same** app (:func:`~tlc_plugin_sdk.asgi_app.build_plugin_app`)
and calls it in-process: no host, no supervisor, no socket, no sign-in. One request is one ASGI
call, which is the whole interaction for an infrastructure plugin's request/response routes.

Uses:

- **Headless tests and smoke checks.** Drive a plugin's routes from CI or a terminal against
  recorded or real provider responses::

      with PluginHarness.from_manifest("src/tlc_plugin_aws") as h:
          print(h.get("/infra/capabilities").json())

  or ``python -m tlc_plugin_sdk.harness src/tlc_plugin_aws GET /infra/capabilities``.
- **Running a plugin somewhere a worker cannot run**, with the caller supplying everything a host
  would: the request body (including any credentials the plugin reads from it) and, through
  ``config_root``, the settings the plugin loads.

The harness adds nothing to a request: whatever a host would stamp (identity, credentials) the
caller passes explicitly. Job routes (``/jobs/*``) are not mounted; a harnessed plugin serves its
own routes plus ``/health``, ``/ui`` and ``/compute``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from litestar.testing import TestClient

    from tlc_plugin_sdk.contract import HubPlugin

__all__ = [
    "CredentialRequirement",
    "HarnessResponse",
    "Manifest",
    "PluginHarness",
    "forward_for",
    "parse_credential_routes",
    "parse_credentials",
    "read_manifest",
]

_SERVICE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SERVICE_MAX_LENGTH = 100


@dataclass(frozen=True)
class CredentialRequirement:
    """One service a plugin may be given a SECRET Connection's value for (``[runtime] credentials``).

    Attributes:
        service: The service slug (``huggingface``, ``kaggle``, ``wandb``, …): what a SECRET
            Connection's ``provider`` names.
        required: The plugin cannot do its work without one.
    """

    service: str
    required: bool = False


def parse_credentials(raw: object) -> tuple[CredentialRequirement, ...]:
    """Validate a manifest's ``[runtime] credentials``: a list of ``{service, required?}`` tables.

    Args:
        raw: The manifest value (``None`` when absent).

    Returns:
        The requirements, in declaration order.

    Raises:
        ValueError: When the value is not a list of such tables, a ``service`` is not a lower-case
            slug (``^[a-z0-9][a-z0-9._-]*$``, at most 100 characters), ``required`` is not a
            boolean, or a service is listed twice.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        msg = "[runtime] credentials is a list of { service, required } tables"
        raise ValueError(msg)
    requirements: list[CredentialRequirement] = []
    for entry in raw:
        if not isinstance(entry, dict):
            msg = f"[runtime] credentials entries are tables, got {entry!r}"
            raise ValueError(msg)
        service, required = entry.get("service"), entry.get("required", False)
        if not isinstance(service, str) or len(service) > _SERVICE_MAX_LENGTH or not _SERVICE_PATTERN.match(service):
            msg = f"[runtime] credentials: service {service!r} is not a lower-case slug"
            raise ValueError(msg)
        if not isinstance(required, bool):
            msg = f"[runtime] credentials: required for {service!r} is true or false"
            raise ValueError(msg)
        if any(r.service == service for r in requirements):
            msg = f"[runtime] credentials lists {service!r} twice"
            raise ValueError(msg)
        requirements.append(CredentialRequirement(service=service, required=required))
    return tuple(requirements)


def parse_credential_routes(raw: object) -> tuple[str, ...]:
    """Validate and normalize a manifest's ``[runtime] credential_routes`` (as ``node_routes``).

    ``"preview"``, ``"/preview/"`` and ``"/preview"`` all mean ``/preview``; ``"/"`` alone means
    every custom route. Duplicates collapse.

    Args:
        raw: The manifest value (``None`` when absent).

    Returns:
        The normalized prefixes, in declaration order.

    Raises:
        ValueError: When the value is not a list of non-blank strings.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        msg = '[runtime] credential_routes is a list of route prefixes ("/preview")'
        raise ValueError(msg)
    routes: list[str] = []
    for item in raw:
        text = item.strip().strip("/")
        prefix = "/" + text if text else "/"
        if prefix not in routes:
            routes.append(prefix)
    return tuple(routes)


@dataclass(frozen=True)
class Manifest:
    """The fields of a plugin manifest the harness needs.

    Attributes:
        id: The plugin id (``id``).
        entrypoint: ``module:ClassName`` (``[runtime] entrypoint``).
        kind: ``compute``, ``infrastructure`` or ``service`` (``kind``; ``compute`` when absent).
        source_dir: The directory the manifest was read from. Its parent is what makes the
            plugin's package importable when the plugin is not installed.
        credentials: The services the plugin may be given a token for (``[runtime] credentials``).
        credential_routes: The custom routes that receive it (``[runtime] credential_routes``).
    """

    id: str
    entrypoint: str
    kind: str
    source_dir: Path
    credentials: tuple[CredentialRequirement, ...] = field(default=())
    credential_routes: tuple[str, ...] = field(default=())


def _toml_load(path: Path) -> dict[str, Any]:
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - Python 3.10 only
        try:
            import tomli as tomllib
        except ModuleNotFoundError as exc:
            msg = "Reading a plugin manifest on Python 3.10 needs the 'tomli' package"
            raise RuntimeError(msg) from exc
    with path.open("rb") as f:
        return tomllib.load(f)


def read_manifest(plugin_dir: str | Path) -> Manifest:
    """Read a plugin's manifest from ``plugin.toml`` or ``pyproject.toml`` in ``plugin_dir``.

    Accepts the two layouts a host accepts: a standalone ``plugin.toml`` with top-level keys, or a
    ``[tool.tlc-compute]`` table (in either file). ``plugin.toml`` wins when both exist.

    Args:
        plugin_dir: The directory holding the manifest (e.g. ``src/tlc_plugin_aws``).

    Returns:
        The manifest's id, entrypoint, kind and credential declarations.

    Raises:
        FileNotFoundError: When neither file holds a manifest.
        ValueError: When the manifest lacks ``id`` or ``[runtime] entrypoint``, or declares
            ``credentials`` / ``credential_routes`` of the wrong shape, or ``credential_routes``
            without ``credentials``.
    """
    directory = Path(plugin_dir).resolve()
    for filename in ("plugin.toml", "pyproject.toml"):
        path = directory / filename
        if not path.is_file():
            continue
        data = _toml_load(path)
        tool = data.get("tool")
        table = tool.get("tlc-compute") if isinstance(tool, dict) else None
        if not isinstance(table, dict):
            table = data if "id" in data else None
        if table is None:
            continue
        runtime = table.get("runtime")
        entrypoint = runtime.get("entrypoint", "") if isinstance(runtime, dict) else ""
        plugin_id = str(table.get("id", "") or "")
        if not plugin_id or not entrypoint:
            msg = f"{path}: a manifest needs 'id' and '[runtime] entrypoint'"
            raise ValueError(msg)
        runtime_table = runtime if isinstance(runtime, dict) else {}
        try:
            credentials = parse_credentials(runtime_table.get("credentials"))
            credential_routes = parse_credential_routes(runtime_table.get("credential_routes"))
        except ValueError as exc:
            msg = f"{path}: {exc}"
            raise ValueError(msg) from exc
        if credential_routes and not credentials:
            msg = f"{path}: [runtime] credential_routes needs [runtime] credentials to say which service"
            raise ValueError(msg)
        return Manifest(
            id=plugin_id,
            entrypoint=str(entrypoint),
            kind=str(table.get("kind", "") or "compute"),
            source_dir=directory,
            credentials=credentials,
            credential_routes=credential_routes,
        )
    msg = f"No plugin manifest (plugin.toml or [tool.tlc-compute] in pyproject.toml) in {directory}"
    raise FileNotFoundError(msg)


def _load_entrypoint(entrypoint: str) -> HubPlugin:
    module_name, _, cls_name = entrypoint.partition(":")
    if not module_name or not cls_name:
        msg = f"An entrypoint is 'module:ClassName', got {entrypoint!r}"
        raise ValueError(msg)
    import importlib

    module = importlib.import_module(module_name)
    plugin: HubPlugin = getattr(module, cls_name)()
    return plugin


@dataclass(frozen=True)
class HarnessResponse:
    """One response from a harnessed plugin.

    Attributes:
        status_code: The HTTP status.
        headers: Response headers, lower-cased names.
        content: The raw body.
    """

    status_code: int
    headers: dict[str, str]
    content: bytes

    @property
    def text(self) -> str:
        """The body decoded as UTF-8 (undecodable bytes replaced)."""
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """The body parsed as JSON.

        Returns:
            The decoded JSON value.
        """
        return json.loads(self.content)


class PluginHarness:
    """A plugin's Litestar app, called in-process.

    Use as a context manager: entering runs the app's lifespan (and the plugin's
    ``initialise_runtime`` hook, when ``initialise`` is true), leaving shuts both down and
    restores any redirected settings root.

    Args:
        plugin: The plugin instance.
        plugin_id: The id a host would hydrate onto the instance (the manifest ``id``).
        config_root: When set, :class:`~tlc_plugin_sdk.shared.config_store.PluginConfigStore`
            reads and writes settings under this directory instead of ``~/.3lc-plugin-configs``
            for the harness's lifetime — so a run can use a prepared, secret-free settings file
            and never touch the real one. The redirect is process-wide: harnesses with different
            roots must not be open at the same time.
        initialise: Run the plugin's ``initialise_runtime`` hook on entry, as a worker does.
    """

    def __init__(
        self,
        plugin: HubPlugin,
        *,
        plugin_id: str,
        config_root: str | Path | None = None,
        initialise: bool = True,
    ) -> None:
        plugin.id = plugin_id
        self.plugin = plugin
        self.plugin_id = plugin_id
        self._config_root = Path(config_root) if config_root is not None else None
        self._initialise = initialise
        # The override in force before ``__enter__`` swapped it (``None`` is "follow the home
        # directory"), and whether a swap is outstanding.
        self._previous_root: Path | None = None
        self._swapped_root = False
        self._client: TestClient[Any] | None = None

    @classmethod
    def from_manifest(
        cls,
        plugin_dir: str | Path,
        *,
        config_root: str | Path | None = None,
        initialise: bool = True,
    ) -> PluginHarness:
        """Load the plugin a manifest names and wrap it.

        The manifest directory's parent is put on ``sys.path`` when the entrypoint's module is not
        importable already, so a source checkout (``src/<package>/plugin.toml``) works without an
        install. The plugin's own dependencies must be importable in this interpreter.

        Args:
            plugin_dir: The directory holding ``plugin.toml`` / ``pyproject.toml``.
            config_root: See :class:`PluginHarness`.
            initialise: See :class:`PluginHarness`.

        Returns:
            A harness for the plugin, not yet entered.
        """
        manifest = read_manifest(plugin_dir)
        import importlib.util

        module_name = manifest.entrypoint.partition(":")[0]
        if importlib.util.find_spec(module_name.split(".")[0]) is None:
            sys.path.insert(0, str(manifest.source_dir.parent))
        plugin = _load_entrypoint(manifest.entrypoint)
        return cls(plugin, plugin_id=manifest.id, config_root=config_root, initialise=initialise)

    def __enter__(self) -> PluginHarness:
        from litestar.testing import TestClient

        from tlc_plugin_sdk.asgi_app import build_plugin_app
        from tlc_plugin_sdk.shared import config_store

        if self._config_root is not None:
            self._config_root.mkdir(parents=True, exist_ok=True)
            self._previous_root = config_store.CONFIG_ROOT
            config_store.CONFIG_ROOT = self._config_root
            self._swapped_root = True
        if self._initialise:
            from tlc_plugin_sdk.worker import _initialise_runtime

            _initialise_runtime(self.plugin, self.plugin_id)
        client: TestClient[Any] = TestClient(app=build_plugin_app(self.plugin))
        client.__enter__()
        self._client = client
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        client, self._client = self._client, None
        try:
            if client is not None:
                client.__exit__(exc_type, exc, tb)
        finally:
            if self._swapped_root:
                from tlc_plugin_sdk.shared import config_store

                config_store.CONFIG_ROOT = self._previous_root
                self._previous_root = None
                self._swapped_root = False

    def call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> HarnessResponse:
        """Send one request to the plugin's app.

        Args:
            method: The HTTP method.
            path: The plugin-relative path, e.g. ``/infra/capabilities`` (a query string is kept).
            json_body: A JSON-serializable body, or ``None`` for none.
            params: Query parameters.
            headers: Request headers, sent as given (the harness adds none).
            content: A raw body, sent as given (instead of ``json_body``).

        Returns:
            The plugin's response.

        Raises:
            RuntimeError: When the harness has not been entered.
        """
        if self._client is None:
            msg = "Enter the harness (`with PluginHarness(...) as h:`) before calling it"
            raise RuntimeError(msg)
        response = self._client.request(
            method.upper(),
            "/" + path.lstrip("/"),
            json=json_body,
            content=content,
            params=params,
            headers=headers,
        )
        return HarnessResponse(
            status_code=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            content=response.content,
        )

    def get(self, path: str, **kwargs: Any) -> HarnessResponse:
        """``call("GET", path, ...)``.

        Args:
            path: The plugin-relative path.
            **kwargs: As :meth:`call`.

        Returns:
            The plugin's response.
        """
        return self.call("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> HarnessResponse:
        """``call("POST", path, ...)``.

        Args:
            path: The plugin-relative path.
            **kwargs: As :meth:`call`.

        Returns:
            The plugin's response.
        """
        return self.call("POST", path, **kwargs)


Forward = Callable[..., Awaitable[tuple[int, dict[str, str], bytes]]]


def forward_for(harness: PluginHarness) -> Forward:
    """A host-shaped ``forward`` that calls the harnessed plugin instead of a worker.

    A host test plugs the result in where the host's worker transport goes: it is
    ``async def forward(spec, method, path, *, headers=None, content=None, timeout=None)`` and
    returns ``(status, headers, body)`` — the harness is called in a thread, ``spec`` is ignored
    (the harness holds one plugin), and ``path`` keeps any query string.

    Args:
        harness: An entered harness.

    Returns:
        The forwarder.
    """

    async def forward(
        spec: Any,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        del spec, timeout
        import asyncio

        response = await asyncio.to_thread(
            harness.call, method, path, headers=dict(headers) if headers else None, content=content
        )
        return response.status_code, response.headers, response.content

    return forward


def main(argv: list[str] | None = None) -> int:
    """``python -m tlc_plugin_sdk.harness <plugin_dir> <METHOD> <path> [--json BODY] [--header N=V]``.

    Prints the status line to stderr and the body to stdout (pretty-printed when it is JSON).

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        0 for a 2xx response, 1 otherwise.
    """
    parser = argparse.ArgumentParser(prog="python -m tlc_plugin_sdk.harness", description=__doc__.splitlines()[0])
    parser.add_argument("plugin_dir", help="Directory holding plugin.toml or pyproject.toml")
    parser.add_argument("method", help="HTTP method")
    parser.add_argument("path", help="Plugin-relative path, e.g. /infra/capabilities")
    parser.add_argument("--json", dest="body", default=None, help="JSON request body")
    parser.add_argument(
        "--header", action="append", default=[], metavar="NAME=VALUE", help="Request header (repeatable)"
    )
    parser.add_argument("--config-root", default=None, help="Settings root instead of ~/.3lc-plugin-configs")
    parser.add_argument("--no-initialise", action="store_true", help="Skip the plugin's initialise_runtime hook")
    args = parser.parse_args(argv)

    import logging

    # The in-process client logs every request at INFO; the status line below says the same.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    body = json.loads(args.body) if args.body is not None else None
    headers = dict(item.split("=", 1) for item in args.header)
    harness = PluginHarness.from_manifest(
        args.plugin_dir, config_root=args.config_root, initialise=not args.no_initialise
    )
    with harness as h:
        response = h.call(args.method, args.path, json_body=body, headers=headers or None)
    print(f"{response.status_code} {args.method.upper()} {args.path}", file=sys.stderr)
    try:
        print(json.dumps(response.json(), indent=2))
    except ValueError:
        print(response.text)
    return 0 if 200 <= response.status_code < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
