# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The Litestar route handlers for an infrastructure plugin, built from its typed methods.

Imported only from :meth:`~tlc_plugin_sdk.infrastructure.InfrastructurePlugin.get_route_handlers`
so that ``import tlc_plugin_sdk.infrastructure`` never loads litestar. :func:`build_infra_handlers`
mounts the core routes, then one group per facet the plugin's class implements, then
``/settings`` when the plugin has a settings layer.

Every handler runs the plugin's method through :func:`answer`, which maps what it raises to an
HTTP status and a ``{"detail": "<sentence>"}`` body scrubbed of the plugin's secrets — never an
opaque 500: the host writes the sentence on the node record.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from typing import Any, TypeVar

from litestar import delete as http_delete
from litestar import get as http_get
from litestar import post as http_post
from litestar.exceptions import HTTPException

from tlc_plugin_sdk.connections import CONNECTION_HEADER, CredentialUnavailable, current_connection
from tlc_plugin_sdk.infrastructure.errors import InvalidRequest, NotFound, ProviderError, scrub
from tlc_plugin_sdk.infrastructure.facets import (
    CatalogFacet,
    LegacyOwnerCredentialsFacet,
    StorageFacet,
    WorkspaceFacet,
)
from tlc_plugin_sdk.infrastructure.legacy import (
    request_credentials,
    secret_credential_values,
    strip_request_credentials,
)
from tlc_plugin_sdk.infrastructure.plugin import InfrastructurePlugin
from tlc_plugin_sdk.infrastructure.types import (
    BundleRequest,
    ConnectionCheckRequest,
    CreateNodeRequest,
    CreateStorageRequest,
    DeleteObjectsRequest,
    PresignRequest,
    TransferRequest,
)
from tlc_plugin_sdk.shared.settings import SettingsUnreadable

__all__ = [
    "answer",
    "build_infra_handlers",
    "catalog_handlers",
    "core_handlers",
    "describe_unexpected",
    "legacy_handlers",
    "scrub",
    "settings_handlers",
    "storage_handlers",
    "workspace_handlers",
]

T = TypeVar("T")

#: A transfer or bundle id as the SDK registries mint them (12 hex characters; the range tolerates other minters).
_JOB_ID = re.compile(r"^[0-9a-f]{6,32}$")
_DETAIL_MAX = 500
_NO_LEGACY_CREDENTIALS = "This provider takes no request credentials; act through a Connection instead."


# ── The exception → HTTP mapper ────────────────────────────────────────────────


def _detail(plugin: InfrastructurePlugin, exc: BaseException, extra: Iterable[str]) -> str:
    text = str(exc).strip() or type(exc).__name__
    return scrub(text, [*plugin.secret_values(), *extra])[:_DETAIL_MAX]


def describe_unexpected(plugin: InfrastructurePlugin, exc: Exception, secrets: Iterable[str] = ()) -> str:
    """The sentence for an exception the provider did not word: its ``describe_error``, always scrubbed.

    The exception's own text is scrubbed first (it is the fallback); the hook's answer is coerced
    to ``str`` and scrubbed of the plugin's secrets and ``secrets`` whatever it returned; a hook
    that raises or answers nothing falls back to the scrubbed text. Truncation comes last.

    Args:
        plugin: The plugin whose hook words the error.
        exc: The exception.
        secrets: Request-scoped values to scrub as well.

    Returns:
        At most 500 characters.
    """
    values = [*plugin.secret_values(), *secrets]
    raw = scrub(str(exc).strip() or type(exc).__name__, values)
    try:
        text = scrub(str(plugin.describe_error(exc) or "").strip(), values)
    except Exception:
        text = ""
    return (text or raw)[:_DETAIL_MAX]


class _ScrubbedDescriber:
    """A registry's own ``describe_error``, its answer coerced to ``str`` and scrubbed of the plugin's secrets."""

    def __init__(self, plugin: InfrastructurePlugin, own: Callable[[Exception], Any]) -> None:
        self.plugin = plugin
        self.own = own

    def __call__(self, exc: Exception) -> str:
        values = self.plugin.secret_values()
        raw = scrub(str(exc).strip() or type(exc).__name__, values)
        try:
            text = scrub(str(self.own(exc) or "").strip(), values)
        except Exception:
            text = ""
        return text or raw


def answer(plugin: InfrastructurePlugin, fn: Callable[[], T], *, secrets: Iterable[str] = ()) -> T:
    """Run ``fn`` and turn what it raises into the HTTP answer a host can read.

    In order: a litestar ``HTTPException`` passes as raised; a
    :class:`~tlc_plugin_sdk.infrastructure.ProviderError` answers its ``status``;
    :class:`~tlc_plugin_sdk.shared.settings.SettingsUnreadable` 409; ``ValueError`` (the transfer
    engine's ``TransferError`` included) and ``TypeError`` 400;
    :class:`~tlc_plugin_sdk.connections.CredentialUnavailable` 424; ``NotImplementedError`` 501;
    anything else 502 with the sentence the plugin's
    :meth:`~tlc_plugin_sdk.infrastructure.InfrastructurePlugin.describe_error` makes of it. Every
    other detail is the exception's own sentence. Each is scrubbed of the plugin's
    :meth:`~tlc_plugin_sdk.infrastructure.InfrastructurePlugin.secret_values` and ``secrets``.

    Args:
        plugin: The plugin whose secrets are scrubbed.
        fn: The call.
        secrets: Request-scoped values to scrub as well (a token, request credentials).

    Returns:
        What ``fn`` returned.

    Raises:
        HTTPException: The mapped answer.
    """
    try:
        return fn()
    except HTTPException as exc:
        # A plugin's own litestar answer keeps its status; its detail is scrubbed like every other.
        detail = scrub(str(exc.detail), [*plugin.secret_values(), *secrets])[:_DETAIL_MAX]
        raise HTTPException(status_code=exc.status_code, detail=detail, headers=exc.headers) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=exc.status, detail=_detail(plugin, exc, secrets)) from exc
    except SettingsUnreadable as exc:
        raise HTTPException(status_code=409, detail=_detail(plugin, exc, secrets)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=_detail(plugin, exc, secrets)) from exc
    except CredentialUnavailable as exc:
        raise HTTPException(status_code=424, detail=_detail(plugin, exc, secrets)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=_detail(plugin, exc, secrets)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=describe_unexpected(plugin, exc, secrets)) from exc


def _job_id(value: str, what: str) -> str:
    text = str(value or "").strip()
    if not _JOB_ID.match(text):
        msg = f"'{text[:40]}' is not a {what} id. Use the id the {what} answered."
        raise InvalidRequest(msg)
    return text


def _split_legacy(
    data: dict[str, Any], *, legacy: bool
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """The typed body and the transient legacy part; 400 for a malformed part or a provider without the facet."""
    try:
        body, credentials, provider_configs = strip_request_credentials(data)
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if (credentials is not None or provider_configs) and not legacy:
        raise HTTPException(status_code=400, detail=_NO_LEGACY_CREDENTIALS)
    return body, credentials, provider_configs


class _CredentialSecrets:
    """The secret-bearing values of request credentials, worked out only when an error is scrubbed.

    Iterable any number of times; ``credential_descriptor()`` is read on the first iteration only,
    so a call that succeeds never loads it.
    """

    def __init__(self, plugin: InfrastructurePlugin, credentials: dict[str, Any] | None, *also: str) -> None:
        self._plugin = plugin
        self._credentials = credentials
        self._also = [a for a in also if a]
        self._values: list[str] | None = None

    def __iter__(self) -> Iterator[str]:
        if self._values is None:
            descriptor = None
            if self._credentials and isinstance(self._plugin, LegacyOwnerCredentialsFacet):
                try:
                    descriptor = self._plugin.credential_descriptor()
                except Exception:
                    descriptor = None
            self._values = [*self._also, *secret_credential_values(self._credentials, descriptor)]
        return iter(self._values)


def _secret_strings(plugin: InfrastructurePlugin, credentials: dict[str, Any] | None) -> _CredentialSecrets:
    """The secret-bearing values of request credentials (a region or a role ARN stays readable), lazily."""
    return _CredentialSecrets(plugin, credentials)


# ── Core ───────────────────────────────────────────────────────────────────────


def core_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    """The core routes: capabilities, preflight, create, state (with diagnostics), delete, connection check.

    Args:
        plugin: The plugin.

    Returns:
        The handlers.
    """
    legacy = isinstance(plugin, LegacyOwnerCredentialsFacet)

    @http_get("/infra/capabilities", sync_to_thread=True)
    def _capabilities() -> dict[str, Any]:
        def run() -> dict[str, Any]:
            caps = plugin.capabilities()
            caps.facets = plugin.implemented_facets()
            if isinstance(plugin, StorageFacet):
                caps.storage = plugin.storage_capabilities()
            d = caps.to_dict()
            if isinstance(plugin, LegacyOwnerCredentialsFacet):
                d.update(plugin.credential_descriptor().to_dict())
            return d

        return answer(plugin, run)

    @http_get("/infra/preflight", sync_to_thread=True)
    def _preflight(node_type: str = "", gpu_type: str = "", datacenter: str = "") -> dict[str, Any]:
        return answer(
            plugin, lambda: plugin.preflight(node_type=node_type or gpu_type, datacenter=datacenter).to_dict()
        )

    @http_post("/infra/nodes", status_code=201, sync_to_thread=True)
    def _create_node(data: dict[str, Any]) -> dict[str, Any]:
        body, credentials, provider_configs = _split_legacy(data, legacy=legacy)
        req = CreateNodeRequest.from_dict(body)
        if not req.node_id or not req.token:
            raise HTTPException(status_code=400, detail="The create call needs both a node_id and a token")

        def run() -> dict[str, Any]:
            with request_credentials(credentials, provider_configs, owner=req.owner):
                return plugin.create_node(req).to_dict()

        return answer(plugin, run, secrets=_CredentialSecrets(plugin, credentials, req.token))

    @http_get("/infra/nodes/{provider_id:str}", sync_to_thread=True)
    def _node_state(provider_id: str, diagnostics: bool = False) -> dict[str, Any]:
        if diagnostics:
            return answer(plugin, lambda: plugin.node_diagnostics(provider_id).to_dict())
        return answer(plugin, lambda: plugin.node_state(provider_id).to_dict())

    @http_delete("/infra/nodes/{provider_id:str}", status_code=200, sync_to_thread=True)
    def _delete_node(provider_id: str) -> dict[str, Any]:
        return answer(plugin, lambda: plugin.delete_node(provider_id).to_dict())

    @http_get("/infra/connection/check", sync_to_thread=True)
    def _connection_check(project_root_url: str | None = None) -> dict[str, Any]:
        if current_connection() is None:
            raise HTTPException(status_code=400, detail=f"Send the Connection to check in {CONNECTION_HEADER}")
        return answer(
            plugin, lambda: plugin.connection_check_with_context(ConnectionCheckRequest(project_root_url)).to_dict()
        )

    return [_capabilities, _preflight, _create_node, _node_state, _delete_node, _connection_check]


# ── Storage facet ──────────────────────────────────────────────────────────────


def storage_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    """The twelve storage routes; transfers and bundles are served over the plugin's registries.

    Args:
        plugin: A plugin whose class implements :class:`~tlc_plugin_sdk.infrastructure.StorageFacet`.

    Returns:
        The handlers.

    Raises:
        TypeError: When the plugin has no storage facet.
    """
    if not isinstance(plugin, StorageFacet):
        msg = "storage_handlers needs a StorageFacet plugin"
        raise TypeError(msg)
    facet: StorageFacet = plugin
    legacy = isinstance(plugin, LegacyOwnerCredentialsFacet)
    transfer_registries: list[Any] = []
    bundle_registries: list[Any] = []

    def job_error(exc: Exception) -> str:
        # What a registry's background job records: a worded error keeps its sentence, like on a route.
        if isinstance(exc, (ProviderError, ValueError)):
            return _detail(plugin, exc, ())
        return describe_unexpected(plugin, exc)

    def remember(registries: list[Any], registry: Any) -> None:
        own = getattr(registry, "describe_error", False)
        if own is None:
            registry.describe_error = job_error
        elif callable(own) and own is not job_error and not isinstance(own, _ScrubbedDescriber):
            # A describer the provider built the registry with keeps its words, scrubbed like the routes'.
            registry.describe_error = _ScrubbedDescriber(plugin, own)
        if not any(r is registry for r in registries):
            registries.append(registry)

    @http_get("/infra/storage", sync_to_thread=True)
    def _list_storage(fallback_url: str = "") -> dict[str, Any]:
        return answer(plugin, lambda: facet.list_storage(fallback_url=fallback_url).to_dict())

    @http_post("/infra/storage", status_code=201, sync_to_thread=True)
    def _create_storage(data: dict[str, Any]) -> dict[str, Any]:
        body, credentials, provider_configs = _split_legacy(data, legacy=legacy)
        req = CreateStorageRequest.from_dict(body)

        def run() -> dict[str, Any]:
            if not req.name:
                msg = "The storage needs a name"
                raise InvalidRequest(msg)
            with request_credentials(credentials, provider_configs, owner=req.owner):
                return facet.create_storage(req).to_dict()

        return answer(plugin, run, secrets=_secret_strings(plugin, credentials))

    @http_delete("/infra/storage/{storage_id:str}", status_code=200, sync_to_thread=True)
    def _delete_storage(storage_id: str) -> dict[str, Any]:
        return answer(plugin, lambda: facet.delete_storage(storage_id).to_dict())

    @http_post("/infra/storage/presign", sync_to_thread=True)
    def _presign(data: dict[str, Any]) -> dict[str, Any]:
        req = PresignRequest.from_dict(data)

        def run() -> dict[str, Any]:
            if req.mode not in ("upload", "download"):
                msg = f'\'{req.mode[:40]}\' is not a presign mode; use "upload" or "download"'
                raise InvalidRequest(msg)
            if not req.url or not req.files:
                msg = f"Give the folder url and the files to {req.mode}"
                raise InvalidRequest(msg)
            return facet.presign(req).to_dict()

        return answer(plugin, run)

    @http_get("/infra/storage/list", sync_to_thread=True)
    def _list_objects(url: str = "", next_token: str = "") -> dict[str, Any]:
        def run() -> dict[str, Any]:
            if not url:
                msg = "Give the folder url to list"
                raise InvalidRequest(msg)
            return facet.list_objects(url, next_token=next_token).to_dict()

        return answer(plugin, run)

    @http_post("/infra/storage/delete", sync_to_thread=True)
    def _delete_objects(data: dict[str, Any]) -> dict[str, Any]:
        req = DeleteObjectsRequest.from_dict(data)

        def run() -> dict[str, Any]:
            if not req.url or not req.paths:
                msg = "Give the folder url and the paths to delete"
                raise InvalidRequest(msg)
            return facet.delete_objects(req)

        return answer(plugin, run)

    @http_post("/infra/storage/transfer", sync_to_thread=True)
    def _transfer(data: dict[str, Any]) -> dict[str, Any]:
        req = TransferRequest.from_dict(data)

        def run() -> dict[str, Any]:
            if not req.src_url or not req.dst_url or not req.items:
                msg = "Give src_url, dst_url and the items to transfer"
                raise InvalidRequest(msg)
            registry = facet.transfer_registry(req.src_url)
            if facet.transfer_registry(req.dst_url) is not registry:
                msg = "Copies stay within one storage. To move data between two, use a node that reaches both."
                raise InvalidRequest(msg)
            remember(transfer_registries, registry)
            if req.dry_run:
                _, summary = registry.plan(req.src_url, req.dst_url, req.items, rename_to=req.rename_to)
                return dict(summary)
            return dict(registry.start(req.src_url, req.dst_url, req.items, mode=req.mode, rename_to=req.rename_to))

        return answer(plugin, run)

    @http_get("/infra/storage/transfer/{transfer_id:str}", sync_to_thread=True)
    def _transfer_status(transfer_id: str) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            tid = _job_id(transfer_id, "transfer")
            for registry in transfer_registries:
                status = registry.status(tid)
                if status is not None:
                    return dict(status)
            msg = f"No transfer {tid}: it finished hours ago or was started elsewhere."
            raise NotFound(msg)

        return answer(plugin, run)

    @http_delete("/infra/storage/transfer/{transfer_id:str}", status_code=200, sync_to_thread=True)
    def _cancel_transfer(transfer_id: str) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            tid = _job_id(transfer_id, "transfer")
            return {"cancelled": any(registry.cancel(tid) for registry in transfer_registries)}

        return answer(plugin, run)

    @http_post("/infra/storage/bundle", sync_to_thread=True)
    def _bundle(data: dict[str, Any]) -> dict[str, Any]:
        req = BundleRequest.from_dict(data)

        def run() -> dict[str, Any]:
            if not req.url:
                msg = "Give the folder url to download"
                raise InvalidRequest(msg)
            registry = facet.bundle_registry(req.url)
            remember(bundle_registries, registry)
            # No name: the registry names the archive after the URL it bundles, once it has normalised it.
            return dict(registry.start(url=req.url, name=req.name))

        return answer(plugin, run)

    @http_get("/infra/storage/bundle/{bundle_id:str}", sync_to_thread=True)
    def _bundle_status(bundle_id: str) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            bid = _job_id(bundle_id, "bundle")
            for registry in bundle_registries:
                status = registry.status(bid)
                if status is not None:
                    return dict(status)
            msg = (
                f"Bundle '{bid}' is not known to this plugin: it finished more than 6 hours ago, or the plugin "
                "restarted. Start the folder download again."
            )
            raise NotFound(msg)

        return answer(plugin, run)

    @http_delete("/infra/storage/bundle/{bundle_id:str}", status_code=200, sync_to_thread=True)
    def _cancel_bundle(bundle_id: str) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            bid = _job_id(bundle_id, "bundle")
            return {"cancelled": any(registry.cancel(bid) for registry in bundle_registries)}

        return answer(plugin, run)

    return [
        _list_storage,
        _create_storage,
        _delete_storage,
        _presign,
        _list_objects,
        _delete_objects,
        _transfer,
        _transfer_status,
        _cancel_transfer,
        _bundle,
        _bundle_status,
        _cancel_bundle,
    ]


# ── Catalog facet ──────────────────────────────────────────────────────────────


def catalog_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    """The three catalog routes.

    Args:
        plugin: A plugin whose class implements :class:`~tlc_plugin_sdk.infrastructure.CatalogFacet`.

    Returns:
        The handlers.

    Raises:
        TypeError: When the plugin has no catalog facet.
    """
    if not isinstance(plugin, CatalogFacet):
        msg = "catalog_handlers needs a CatalogFacet plugin"
        raise TypeError(msg)
    facet: CatalogFacet = plugin

    @http_get("/infra/gpu-catalog", sync_to_thread=True)
    def _gpu_catalog(node_type: str = "", gpu_type: str = "", region: str = "") -> dict[str, Any]:
        return answer(plugin, lambda: facet.gpu_catalog(node_type=node_type or gpu_type, region=region).to_dict())

    @http_get("/infra/cpu-catalog", sync_to_thread=True)
    def _cpu_catalog(region: str = "") -> dict[str, Any]:
        return answer(plugin, lambda: facet.cpu_catalog(region=region).to_dict())

    @http_get("/infra/datacenters", sync_to_thread=True)
    def _datacenters(node_type: str = "", gpu_type: str = "") -> dict[str, Any]:
        return answer(plugin, lambda: facet.datacenters(node_type=node_type or gpu_type).to_dict())

    return [_gpu_catalog, _cpu_catalog, _datacenters]


# ── Workspace facet ────────────────────────────────────────────────────────────


def workspace_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    """The workspaces listing route.

    Args:
        plugin: A plugin whose class implements :class:`~tlc_plugin_sdk.infrastructure.WorkspaceFacet`.

    Returns:
        The handlers.

    Raises:
        TypeError: When the plugin has no workspace facet.
    """
    if not isinstance(plugin, WorkspaceFacet):
        msg = "workspace_handlers needs a WorkspaceFacet plugin"
        raise TypeError(msg)
    facet: WorkspaceFacet = plugin

    @http_get("/infra/workspaces", sync_to_thread=True)
    def _list_workspaces(owner: str = "") -> dict[str, Any]:
        return answer(plugin, lambda: facet.list_workspaces(owner=owner).to_dict())

    return [_list_workspaces]


# ── Legacy owner-credentials facet ─────────────────────────────────────────────


def legacy_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    """The legacy routes: terminate-with-credentials and discover always; role-setup and login only when overridden.

    Args:
        plugin: A plugin whose class implements
            :class:`~tlc_plugin_sdk.infrastructure.LegacyOwnerCredentialsFacet`.

    Returns:
        The handlers.

    Raises:
        TypeError: When the plugin has no legacy facet.
    """
    if not isinstance(plugin, LegacyOwnerCredentialsFacet):
        msg = "legacy_handlers needs a LegacyOwnerCredentialsFacet plugin"
        raise TypeError(msg)
    facet: LegacyOwnerCredentialsFacet = plugin
    cls = type(plugin)

    def credentials_of(data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        _, credentials, _ = _split_legacy(data, legacy=True)
        return credentials or {}, str(data.get("owner", "") or "")

    @http_post("/infra/nodes/{provider_id:str}/terminate", status_code=200, sync_to_thread=True)
    def _terminate_with_credentials(provider_id: str, data: dict[str, Any]) -> dict[str, Any]:
        credentials, owner = credentials_of(data)

        def run() -> dict[str, Any]:
            if not credentials:
                msg = "The request carries no credentials. Send the credentials of the account that owns this node."
                raise InvalidRequest(msg)
            return facet.terminate_with_credentials(provider_id, credentials=credentials, owner=owner).to_dict()

        return answer(plugin, run, secrets=_secret_strings(plugin, credentials))

    @http_post("/infra/storage/discover", sync_to_thread=True)
    def _discover_storage(data: dict[str, Any]) -> dict[str, Any]:
        credentials, owner = credentials_of(data)

        def run() -> dict[str, Any]:
            if not credentials:
                msg = "The request carries no credentials. Send the credentials of the account to list."
                raise InvalidRequest(msg)
            return facet.discover_storage(credentials=credentials, owner=owner).to_dict()

        return answer(plugin, run, secrets=_secret_strings(plugin, credentials))

    handlers: list[Any] = [_terminate_with_credentials, _discover_storage]

    if cls.role_setup is not LegacyOwnerCredentialsFacet.role_setup:

        @http_get("/infra/role-setup", sync_to_thread=True)
        def _role_setup(owner: str = "", bucket_url: str = "") -> dict[str, Any]:
            return answer(plugin, lambda: facet.role_setup(owner=owner, bucket_url=bucket_url))

        handlers.append(_role_setup)

    if cls.login_start is not LegacyOwnerCredentialsFacet.login_start:

        @http_post("/infra/login", sync_to_thread=True)
        def _login_start(data: dict[str, Any]) -> dict[str, Any]:
            body = dict(data or {})
            owner = str(body.get("owner", "") or "")
            return answer(plugin, lambda: facet.login_start(body, owner=owner))

        handlers.append(_login_start)

    if cls.login_poll is not LegacyOwnerCredentialsFacet.login_poll:

        @http_get("/infra/login/{login_id:str}", sync_to_thread=True)
        def _login_poll(login_id: str, owner: str = "") -> dict[str, Any]:
            return answer(plugin, lambda: facet.login_poll(login_id, owner=owner))

        handlers.append(_login_poll)

    if cls.login_credentials is not LegacyOwnerCredentialsFacet.login_credentials:

        @http_post("/infra/login/{login_id:str}/credentials", sync_to_thread=True)
        def _login_credentials(login_id: str, data: dict[str, Any]) -> dict[str, Any]:
            body = dict(data or {})
            owner = str(body.get("owner", "") or "")
            return answer(plugin, lambda: facet.login_credentials(login_id, body, owner=owner))

        handlers.append(_login_credentials)

    return handlers


# ── Settings ───────────────────────────────────────────────────────────────────


def settings_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    """``GET /settings`` and ``POST /settings`` over the plugin's settings layer.

    Args:
        plugin: A plugin with ``settings`` set.

    Returns:
        The handlers.

    Raises:
        TypeError: When the plugin has no settings layer.
    """
    settings = plugin.settings
    if settings is None:
        msg = "settings_handlers needs a plugin with settings"
        raise TypeError(msg)

    @http_get("/settings", sync_to_thread=True)
    def _get_settings() -> dict[str, Any]:
        return answer(plugin, lambda: plugin.settings_view(settings.load()))

    @http_post("/settings", sync_to_thread=True)
    def _update_settings(data: dict[str, Any] | None = None) -> dict[str, Any]:
        # A corrupt file is a 409 before any merge: ``save`` loads first.
        return answer(plugin, lambda: plugin.settings_view(settings.save(data or {})))

    return [_get_settings, _update_settings]


# ── Assembly ───────────────────────────────────────────────────────────────────


def build_infra_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    """Every handler the base mounts for ``plugin``: core, each implemented facet, settings.

    Args:
        plugin: The plugin.

    Returns:
        The handlers, in mount order.
    """
    handlers = core_handlers(plugin)
    if isinstance(plugin, StorageFacet):
        handlers += storage_handlers(plugin)
    if isinstance(plugin, CatalogFacet):
        handlers += catalog_handlers(plugin)
    if isinstance(plugin, WorkspaceFacet):
        handlers += workspace_handlers(plugin)
    if isinstance(plugin, LegacyOwnerCredentialsFacet):
        handlers += legacy_handlers(plugin)
    if plugin.settings is not None:
        handlers += settings_handlers(plugin)
    return handlers
