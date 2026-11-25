# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The Litestar route handlers for an infrastructure plugin, built from its typed methods.

Imported only from :meth:`~tlc_plugin_sdk.infrastructure.InfrastructurePlugin.get_route_handlers`
so that ``import tlc_plugin_sdk.infrastructure`` never loads litestar. :func:`build_infra_handlers`
mounts ``GET /infra/capabilities``, then the routes of every operation the plugin's class provides
(:func:`~tlc_plugin_sdk.infrastructure.plugin.provided_operations`) and no other, then ``/settings``
when the plugin has a settings layer.

Every handler runs the plugin's method through :func:`answer`, which maps what it raises to an
HTTP status and the body ``{"detail", "code", "remedy", "retry_after_s"}``, scrubbed of the
plugin's secrets, of the request's credential and of the request's own secrets. Never an opaque
500: the host writes the sentence on the node record and branches on the code.

Whether a route takes a Connection is part of the contract. A route that needs one answers 400
without it; the routes that read this host's own identity are refused a Connection by
:func:`tlc_plugin_sdk.connections.connection_middleware` before a handler runs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from litestar import MediaType, Request, Response
from litestar import delete as http_delete
from litestar import get as http_get
from litestar import post as http_post
from litestar.exceptions import HTTPException
from litestar.types import ExceptionHandlersMap

from tlc_plugin_sdk.connections import (
    CONNECTION_HEADER,
    LAUNCHER_ZONE_HEADER,
    CredentialUnavailable,
    current_connection,
    current_credential,
    current_identity_token,
)
from tlc_plugin_sdk.infrastructure.errors import (
    InvalidRequest,
    NotFound,
    ProviderError,
    code_for_status,
    error_body,
    scrub,
)
from tlc_plugin_sdk.infrastructure.facets import CatalogFacet, DeploymentProvider, DnsFacet, NodeProvider, StorageFacet
from tlc_plugin_sdk.infrastructure.plugin import (
    InfrastructurePlugin,
    capabilities_problems,
    provided_operations,
    settle_readiness,
)
from tlc_plugin_sdk.infrastructure.transitional import WorkspaceFacet
from tlc_plugin_sdk.infrastructure.types import (
    BundleRequest,
    CatalogRequest,
    CreateNodeRequest,
    CreateStorageRequest,
    DeleteObjectsRequest,
    DeploymentCheckRequest,
    DeploymentOptionsRequest,
    DeploymentPrepareRequest,
    DeploymentRequest,
    DnsChange,
    DnsZone,
    GrantRequest,
    GrantUpdateRequest,
    NodeAccessRequest,
    NodeCheckRequest,
    PresignRequest,
    StorageRoot,
    SweepRequest,
    TransferRequest,
    VerifyRequest,
)
from tlc_plugin_sdk.shared.settings import SettingsUnreadable

__all__ = [
    "OPERATION_HANDLERS",
    "ProviderHTTPError",
    "answer",
    "build_infra_handlers",
    "describe_unexpected",
    "error_handlers",
    "scrub",
    "settings_handlers",
    "storage_handlers",
    "workspace_handlers",
]

T = TypeVar("T")

#: A transfer or bundle id as the SDK registries mint them (12 hex characters; the range tolerates other minters).
_JOB_ID = re.compile(r"^[0-9a-f]{6,32}$")
_DETAIL_MAX = 500
_CATALOG_PURPOSES = ("gpu_node", "cpu_node", "deployment")
_DNS_TYPES = ("A", "AAAA", "CNAME", "TXT")
_DIAGNOSTICS = ("", "baseline", "detailed")
_KINDS = ("AMBIENT", "KEYLESS", "SECRET")
_MAX_DNS_CHANGE = 100


# ── The error answer ───────────────────────────────────────────────────────────


class ProviderHTTPError(HTTPException):
    """The HTTP answer :func:`answer` raises: a status and the contract's error body.

    A route mounted with :func:`error_handlers` answers the body as it is. On any other route
    litestar renders ``detail`` at the top and the remaining keys under ``extra``.

    Attributes:
        body: ``{"detail", "code", "remedy", "retry_after_s"}``.
    """

    def __init__(
        self, status_code: int, detail: str, code: str, remedy: str = "", retry_after_s: float | None = None
    ) -> None:
        self.body = error_body(detail, code, remedy, retry_after_s)
        extra = {"code": code, "remedy": remedy, "retry_after_s": retry_after_s}
        super().__init__(status_code=status_code, detail=detail, extra=extra)


def _render(_: Request[Any, Any, Any], exc: Exception) -> Response[dict[str, Any]]:
    """Any HTTP error of an infrastructure route as the contract's error body."""
    if isinstance(exc, ProviderHTTPError):
        return Response(content=exc.body, status_code=exc.status_code, media_type=MediaType.JSON)
    status = exc.status_code if isinstance(exc, HTTPException) else 500
    detail = str(exc.detail) if isinstance(exc, HTTPException) else type(exc).__name__
    return Response(
        content=error_body(detail[:_DETAIL_MAX], code_for_status(status)),
        status_code=status,
        media_type=MediaType.JSON,
    )


_ERRORS: ExceptionHandlersMap = {HTTPException: _render}


def error_handlers() -> ExceptionHandlersMap:
    """The ``exception_handlers`` that make a route answer the contract's error body.

    Every route the SDK mounts has them. Pass them to a plugin-private route that runs its work
    through :func:`answer`, so its errors read the same::

        @get("/infra/my-check", sync_to_thread=True, exception_handlers=error_handlers())

    Returns:
        The map to pass to a litestar route decorator.
    """
    return dict(_ERRORS)


def _credential_secrets() -> list[str]:
    """What the request's own credential and identity token must never see echoed."""
    token = current_identity_token()
    hidden = [token.token] if token is not None else []
    credential = current_credential()
    values = getattr(credential, "secret_values", None)
    if not callable(values):
        return hidden
    try:
        return [*hidden, *(str(v) for v in values() if v)]
    except Exception:
        return hidden


def _secrets(plugin: InfrastructurePlugin, extra: Iterable[str]) -> list[str]:
    return [*plugin.secret_values(), *_credential_secrets(), *extra]


def _detail(plugin: InfrastructurePlugin, exc: BaseException, extra: Iterable[str]) -> str:
    text = str(exc).strip() or type(exc).__name__
    return scrub(text, _secrets(plugin, extra))[:_DETAIL_MAX]


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
    values = _secrets(plugin, secrets)
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

    In order: a litestar ``HTTPException`` keeps its status, with the code of that status; a
    :class:`~tlc_plugin_sdk.infrastructure.ProviderError` answers its ``status``, ``code``,
    ``remedy`` and ``retry_after_s``; :class:`~tlc_plugin_sdk.shared.settings.SettingsUnreadable`
    409 ``not_configured``; ``ValueError`` (the transfer engine's ``TransferError`` included) and
    ``TypeError`` 400 ``invalid_request``;
    :class:`~tlc_plugin_sdk.connections.CredentialUnavailable` 424 ``credential_unavailable``;
    ``NotImplementedError`` 501 ``not_supported``; anything else 502 ``provider_error`` with the
    sentence the plugin's
    :meth:`~tlc_plugin_sdk.infrastructure.InfrastructurePlugin.describe_error` makes of it. Every
    other detail is the exception's own sentence. Detail and remedy are scrubbed of the plugin's
    :meth:`~tlc_plugin_sdk.infrastructure.InfrastructurePlugin.secret_values`, of the request's
    credential and of ``secrets``.

    Args:
        plugin: The plugin whose secrets are scrubbed.
        fn: The call.
        secrets: Request-scoped values to scrub as well (a node token, a bootstrap token).

    Returns:
        What ``fn`` returned.

    Raises:
        ProviderHTTPError: The mapped answer.
    """
    try:
        return fn()
    except ProviderHTTPError:
        raise
    except HTTPException as exc:
        # A plugin's own litestar answer keeps its status; its detail is scrubbed like every other.
        detail = scrub(str(exc.detail), _secrets(plugin, secrets))[:_DETAIL_MAX]
        raise ProviderHTTPError(exc.status_code, detail, code_for_status(exc.status_code)) from exc
    except ProviderError as exc:
        remedy = scrub(str(exc.remedy or ""), _secrets(plugin, secrets))[:_DETAIL_MAX]
        raise ProviderHTTPError(exc.status, _detail(plugin, exc, secrets), exc.code, remedy, exc.retry_after_s) from exc
    except SettingsUnreadable as exc:
        raise ProviderHTTPError(409, _detail(plugin, exc, secrets), "not_configured") from exc
    except (ValueError, TypeError) as exc:
        raise ProviderHTTPError(400, _detail(plugin, exc, secrets), "invalid_request") from exc
    except CredentialUnavailable as exc:
        remedy = scrub(str(exc.remedy or ""), _secrets(plugin, secrets))[:_DETAIL_MAX]
        raise ProviderHTTPError(424, _detail(plugin, exc, secrets), exc.code, remedy) from exc
    except NotImplementedError as exc:
        raise ProviderHTTPError(501, _detail(plugin, exc, secrets), "not_supported") from exc
    except Exception as exc:
        raise ProviderHTTPError(502, describe_unexpected(plugin, exc, secrets), "provider_error") from exc


def _job_id(value: str, what: str) -> str:
    text = str(value or "").strip()
    if not _JOB_ID.match(text):
        msg = f"'{text[:40]}' is not a {what} id. Use the id the {what} answered."
        raise InvalidRequest(msg)
    return text


def _require_connection() -> None:
    """400 unless the host sent the Connection this call acts on."""
    if current_connection() is None:
        detail = f"This call acts on an account: send its Connection in {CONNECTION_HEADER}"
        raise ProviderHTTPError(400, detail, "invalid_request")


def _refuse_connection() -> None:
    """400 when the host sent a Connection to a call that is the plugin's own business."""
    if current_connection() is not None:
        raise ProviderHTTPError(400, "This route is never sent a Connection", "invalid_request")


# ── Capabilities ───────────────────────────────────────────────────────────────


def _capabilities_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    @http_get("/infra/capabilities", sync_to_thread=True, exception_handlers=_ERRORS)
    def _capabilities() -> dict[str, Any]:
        def run() -> dict[str, Any]:
            caps = plugin.capabilities()
            settle_readiness(caps)
            caps.operations = provided_operations(plugin)
            caps.storage = plugin.storage_capabilities() if isinstance(plugin, StorageFacet) else None
            problems = capabilities_problems(plugin, caps)
            if problems:
                msg = "This plugin's capabilities break the provider contract: " + "; ".join(problems)
                raise ProviderError(msg, remedy="Update the plugin.")
            return caps.to_dict()

        return answer(plugin, run)

    return [_capabilities]


# ── Connect an account ─────────────────────────────────────────────────────────


def _grant_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    @http_post("/infra/connection/grant", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS)
    def _describe_grant(data: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            if str(data.get("kind") or "").upper() not in _KINDS:
                msg = f"A grant is described for a Connection kind: one of {', '.join(_KINDS)}"
                raise InvalidRequest(msg)
            request = GrantRequest.from_dict({**data, "kind": str(data.get("kind")).upper()})
            if not request.connection_id:
                msg = "A grant is described for a Connection: send its connection_id"
                raise InvalidRequest(msg)
            return plugin.describe_grant(request).to_dict()

        return answer(plugin, run)

    return [_describe_grant]


def _grant_update_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    @http_post("/infra/connection/grant/update", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS)
    def _prepare_grant_update(data: dict[str, Any]) -> dict[str, Any]:
        _require_connection()
        request = GrantUpdateRequest.from_dict(data)
        return answer(plugin, lambda: plugin.prepare_grant_update(request).to_dict())

    return [_prepare_grant_update]


def _verify_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    @http_post("/infra/connection/verify", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS)
    def _verify_connection(data: dict[str, Any] | None = None) -> dict[str, Any]:
        _require_connection()
        request = VerifyRequest.from_dict(data)
        return answer(plugin, lambda: plugin.verify_connection(request).to_dict())

    return [_verify_connection]


def _query_roots(raw: str) -> list[StorageRoot]:
    """The ``storage_roots`` query of a ``GET``: a JSON list of storage roots; ``""`` is none."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not isinstance(value, list):
        raise ProviderHTTPError(400, "storage_roots is a JSON list of storage roots", "invalid_request")
    return NodeAccessRequest.from_dict({"storage_roots": value}).storage_roots


def _node_access_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    @http_get("/infra/connection/node-access", sync_to_thread=True, exception_handlers=_ERRORS)
    def _node_access(storage_roots: str = "") -> dict[str, Any]:
        _require_connection()
        request = NodeAccessRequest(storage_roots=_query_roots(storage_roots))
        return answer(plugin, lambda: plugin.connection_node_access(request).to_dict())

    @http_post("/infra/connection/node-access", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS)
    def _setup_node_access(data: dict[str, Any]) -> dict[str, Any]:
        _require_connection()
        if not isinstance(data.get("storage_roots"), list):
            raise ProviderHTTPError(400, "The host's storage roots are required", "invalid_request")
        storage = data.get("storage_access")
        if storage is not None and not isinstance(storage, dict):
            raise ProviderHTTPError(400, "storage_access must be an object", "invalid_request")
        request = NodeAccessRequest.from_dict(data)
        return answer(plugin, lambda: plugin.connection_node_access(request, apply=True).to_dict())

    return [_node_access, _setup_node_access]


def _candidates_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    @http_get("/infra/storage/candidates", sync_to_thread=True, exception_handlers=_ERRORS)
    def _storage_candidates() -> dict[str, Any]:
        _require_connection()
        return answer(plugin, lambda: plugin.storage_candidates().to_dict())

    return [_storage_candidates]


# ── Nodes ──────────────────────────────────────────────────────────────────────


def _node_create_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    provider = _node_provider(plugin)

    @http_post("/infra/nodes", status_code=201, sync_to_thread=True, exception_handlers=_ERRORS)
    def _create_node(data: dict[str, Any]) -> dict[str, Any]:
        req = answer(plugin, lambda: CreateNodeRequest.from_dict(data))
        if not req.node_id or not req.token:
            raise ProviderHTTPError(400, "The create call needs both a node_id and a token", "invalid_request")
        # ── transitional ──
        if req.flavor == "workspace" and not isinstance(plugin, WorkspaceFacet):
            raise ProviderHTTPError(400, "This provider starts no deployment from a node request", "invalid_request")
        # ── end transitional ──
        # The node token and a deployment's bootstrap token are the request's own secrets: a
        # provider's error that quotes its request must not put either in the host's ledger.
        return answer(
            plugin, lambda: provider.create_node(req).to_dict(), secrets=[req.token, req.workspace.bootstrap_token]
        )

    return [_create_node]


def _node_provider(plugin: InfrastructurePlugin) -> NodeProvider:
    if not isinstance(plugin, NodeProvider):
        msg = "node routes need a NodeProvider plugin"
        raise TypeError(msg)
    return plugin


def _node_state_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    provider = _node_provider(plugin)

    @http_get("/infra/nodes/{provider_id:str}", sync_to_thread=True, exception_handlers=_ERRORS)
    def _node_state(provider_id: str, diagnostics: bool = False) -> dict[str, Any]:
        if diagnostics:
            return answer(plugin, lambda: provider.node_diagnostics(provider_id).to_dict())
        return answer(plugin, lambda: provider.node_state(provider_id).to_dict())

    return [_node_state]


def _node_delete_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    provider = _node_provider(plugin)

    @http_delete("/infra/nodes/{provider_id:str}", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS)
    def _delete_node(provider_id: str) -> dict[str, Any]:
        return answer(plugin, lambda: provider.delete_node(provider_id).to_dict())

    return [_delete_node]


def _node_check_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    provider = _node_provider(plugin)

    @http_post("/infra/nodes/check", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS)
    def _check_node(data: dict[str, Any] | None = None) -> dict[str, Any]:
        request = NodeCheckRequest.from_dict(data)
        return answer(plugin, lambda: provider.check_node(request).to_dict())

    return [_check_node]


def _no_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    """An operation served by another operation's route (``node.diagnostics`` by the node-state route)."""
    return []


# ── Catalog ────────────────────────────────────────────────────────────────────


def _catalog_facet(plugin: InfrastructurePlugin) -> CatalogFacet:
    if not isinstance(plugin, CatalogFacet):
        msg = "catalog routes need a CatalogFacet plugin"
        raise TypeError(msg)
    return plugin


def _catalog_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    facet = _catalog_facet(plugin)

    @http_post("/infra/catalog", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS)
    def _catalog(data: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            if data.get("purpose") not in _CATALOG_PURPOSES:
                msg = f"A catalog is asked for a purpose: one of {', '.join(_CATALOG_PURPOSES)}"
                raise InvalidRequest(msg)
            return facet.catalog(CatalogRequest.from_dict(data)).to_dict()

        return answer(plugin, run)

    return [_catalog]


def _locations_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    facet = _catalog_facet(plugin)

    @http_get("/infra/catalog/locations", sync_to_thread=True, exception_handlers=_ERRORS)
    def _locations(node_type: str = "") -> dict[str, Any]:
        return answer(plugin, lambda: facet.locations(node_type=node_type).to_dict())

    return [_locations]


# ── Deployments ────────────────────────────────────────────────────────────────


def _deployment_provider(plugin: InfrastructurePlugin) -> DeploymentProvider:
    if not isinstance(plugin, DeploymentProvider):
        msg = "deployment routes need a DeploymentProvider plugin"
        raise TypeError(msg)
    return plugin


def _deployment_options_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    provider = _deployment_provider(plugin)

    @http_post("/infra/deployments/options", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS)
    def _deployment_options(data: dict[str, Any] | None = None) -> dict[str, Any]:
        _require_connection()
        selected = DeploymentOptionsRequest.from_dict(data).selected
        return answer(plugin, lambda: provider.deployment_options(selected).to_dict())

    return [_deployment_options]


def _deployment_check_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    provider = _deployment_provider(plugin)

    @http_post("/infra/deployments/check", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS)
    def _check_deployment(data: dict[str, Any]) -> dict[str, Any]:
        _require_connection()
        request = DeploymentCheckRequest.from_dict(data)
        if not request.label:
            raise ProviderHTTPError(400, "A deployment is checked by its label", "invalid_request")
        return answer(plugin, lambda: provider.check_deployment(request).to_dict())

    return [_check_deployment]


def _deployment_prepare_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    provider = _deployment_provider(plugin)

    @http_post("/infra/deployments/prepare", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS)
    def _prepare_deployment(data: dict[str, Any]) -> dict[str, Any]:
        _require_connection()
        request = DeploymentPrepareRequest.from_dict(data)
        if not request.deployment_id:
            raise ProviderHTTPError(400, "The prepare call needs a deployment_id", "invalid_request")
        return answer(plugin, lambda: provider.prepare_deployment(request).to_dict())

    return [_prepare_deployment]


def _deployment_create_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    provider = _deployment_provider(plugin)

    @http_post("/infra/deployments", status_code=201, sync_to_thread=True, exception_handlers=_ERRORS)
    def _create_deployment(data: dict[str, Any]) -> dict[str, Any]:
        _require_connection()
        request = answer(plugin, lambda: DeploymentRequest.from_dict(data))
        if not request.deployment_id:
            raise ProviderHTTPError(400, "The create call needs a deployment_id", "invalid_request")
        if request.bootstrap.token and not request.bootstrap.record_id:
            detail = "A bootstrap token needs the deployment record it redeems for"
            raise ProviderHTTPError(400, detail, "invalid_request")
        return answer(plugin, lambda: provider.create_deployment(request).to_dict(), secrets=[request.bootstrap.token])

    return [_create_deployment]


def _deployment_state_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    provider = _deployment_provider(plugin)

    @http_get("/infra/deployments/{provider_id:str}", sync_to_thread=True, exception_handlers=_ERRORS)
    def _deployment_state(provider_id: str, diagnostics: str = "") -> dict[str, Any]:
        _require_connection()
        if diagnostics not in _DIAGNOSTICS:
            raise ProviderHTTPError(400, 'diagnostics is "", "baseline" or "detailed"', "invalid_request")
        return answer(plugin, lambda: provider.deployment_state(provider_id, diagnostics=diagnostics).to_dict())

    return [_deployment_state]


def _deployment_delete_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    provider = _deployment_provider(plugin)

    @http_delete(
        "/infra/deployments/{provider_id:str}", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS
    )
    def _delete_deployment(provider_id: str) -> dict[str, Any]:
        return answer(plugin, lambda: provider.delete_deployment(provider_id).to_dict())

    return [_delete_deployment]


def _deployment_retain_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    provider = _deployment_provider(plugin)

    @http_post(
        "/infra/deployments/{provider_id:str}/retain", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS
    )
    def _retain_deployment(provider_id: str) -> dict[str, Any]:
        _require_connection()
        return answer(plugin, lambda: provider.retain_deployment(provider_id).to_dict())

    return [_retain_deployment]


def _deployment_list_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    provider = _deployment_provider(plugin)

    @http_get("/infra/deployments", sync_to_thread=True, exception_handlers=_ERRORS)
    def _list_deployments(owner: str = "") -> dict[str, Any]:
        return answer(plugin, lambda: provider.list_deployments(owner=owner).to_dict())

    return [_list_deployments]


def _deployment_sweep_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    provider = _deployment_provider(plugin)

    @http_post("/infra/deployments/sweep", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS)
    def _sweep_deployments(data: dict[str, Any] | None = None) -> dict[str, Any]:
        request = SweepRequest.from_dict(data)
        return answer(plugin, lambda: provider.sweep_deployments(request).to_dict())

    return [_sweep_deployments]


# ── DNS ────────────────────────────────────────────────────────────────────────


def _require_launcher_zone(request: Request[Any, Any, Any], zone_id: str) -> None:
    """Refuse a zone other than the one the host says it is configured with.

    The DNS routes act with the host's own identity, and a worker cannot tell the host's manager
    from any other caller. The host names its zone in :data:`LAUNCHER_ZONE_HEADER`, which it strips
    from every request it forwards for someone else: a call without it, or for another zone, is
    not the launcher's.
    """
    named = request.headers.get(LAUNCHER_ZONE_HEADER, "").strip()
    if not named or named != zone_id.strip():
        msg = (
            f"The zone {zone_id[:64]} is not the one this launcher is configured with: the DNS routes act "
            f"only on the zone the host names in {LAUNCHER_ZONE_HEADER}"
        )
        raise InvalidRequest(msg)


def _dns_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    if not isinstance(plugin, DnsFacet):
        msg = "DNS routes need a DnsFacet plugin"
        raise TypeError(msg)
    facet: DnsFacet = plugin

    @http_get("/infra/dns/records", sync_to_thread=True, exception_handlers=_ERRORS)
    def _list_records(
        request: Request[Any, Any, Any], zone_id: str = "", zone_name: str = "", under: str = "", cursor: str = ""
    ) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            if not zone_id:
                msg = "Name the zone to list: zone_id and zone_name"
                raise InvalidRequest(msg)
            _require_launcher_zone(request, zone_id)
            zone = DnsZone(id=zone_id, name=zone_name)
            return facet.list_records(zone, under=under, cursor=cursor).to_dict()

        return answer(plugin, run)

    @http_post("/infra/dns/changes", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS)
    def _change_records(request: Request[Any, Any, Any], data: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            raw = [r for key in ("create", "delete") for r in (data.get(key) or []) if isinstance(r, dict)]
            if any(r.get("type") not in _DNS_TYPES for r in raw):
                msg = f"A record's type is one of {', '.join(_DNS_TYPES)}"
                raise InvalidRequest(msg)
            change = DnsChange.from_dict(data)
            if not change.zone.id:
                msg = "Name the zone to change"
                raise InvalidRequest(msg)
            _require_launcher_zone(request, change.zone.id)
            if len(change.create) + len(change.delete) > _MAX_DNS_CHANGE:
                msg = f"One change holds at most {_MAX_DNS_CHANGE} records"
                raise InvalidRequest(msg)
            facet.change_records(change)
            return {}

        return answer(plugin, run)

    return [_list_records, _change_records]


# ── Storage ────────────────────────────────────────────────────────────────────


def storage_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    """The twelve storage routes, each needing a Connection; transfers and bundles run over the plugin's registries.

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

    @http_get("/infra/storage", sync_to_thread=True, exception_handlers=_ERRORS)
    def _list_storage(fallback_url: str = "") -> dict[str, Any]:
        _require_connection()
        return answer(plugin, lambda: facet.list_storage(fallback_url=fallback_url).to_dict())

    @http_post("/infra/storage", status_code=201, sync_to_thread=True, exception_handlers=_ERRORS)
    def _create_storage(data: dict[str, Any]) -> dict[str, Any]:
        _require_connection()
        req = CreateStorageRequest.from_dict(data)

        def run() -> dict[str, Any]:
            if not req.name:
                msg = "The storage needs a name"
                raise InvalidRequest(msg)
            return facet.create_storage(req).to_dict()

        return answer(plugin, run)

    @http_delete("/infra/storage/{storage_id:str}", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS)
    def _delete_storage(storage_id: str) -> dict[str, Any]:
        _require_connection()
        return answer(plugin, lambda: facet.delete_storage(storage_id).to_dict())

    @http_post("/infra/storage/presign", sync_to_thread=True, exception_handlers=_ERRORS)
    def _presign(data: dict[str, Any]) -> dict[str, Any]:
        _require_connection()
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

    @http_get("/infra/storage/list", sync_to_thread=True, exception_handlers=_ERRORS)
    def _list_objects(url: str = "", next_token: str = "") -> dict[str, Any]:
        _require_connection()

        def run() -> dict[str, Any]:
            if not url:
                msg = "Give the folder url to list"
                raise InvalidRequest(msg)
            return facet.list_objects(url, next_token=next_token).to_dict()

        return answer(plugin, run)

    @http_post("/infra/storage/delete", sync_to_thread=True, exception_handlers=_ERRORS)
    def _delete_objects(data: dict[str, Any]) -> dict[str, Any]:
        _require_connection()
        req = DeleteObjectsRequest.from_dict(data)

        def run() -> dict[str, Any]:
            if not req.url or not req.paths:
                msg = "Give the folder url and the paths to delete"
                raise InvalidRequest(msg)
            return facet.delete_objects(req)

        return answer(plugin, run)

    @http_post("/infra/storage/transfer", sync_to_thread=True, exception_handlers=_ERRORS)
    def _transfer(data: dict[str, Any]) -> dict[str, Any]:
        _require_connection()
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

    @http_get("/infra/storage/transfer/{transfer_id:str}", sync_to_thread=True, exception_handlers=_ERRORS)
    def _transfer_status(transfer_id: str) -> dict[str, Any]:
        _require_connection()

        def run() -> dict[str, Any]:
            tid = _job_id(transfer_id, "transfer")
            for registry in transfer_registries:
                status = registry.status(tid)
                if status is not None:
                    return dict(status)
            msg = f"No transfer {tid}: it finished hours ago or was started elsewhere."
            raise NotFound(msg)

        return answer(plugin, run)

    @http_delete(
        "/infra/storage/transfer/{transfer_id:str}", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS
    )
    def _cancel_transfer(transfer_id: str) -> dict[str, Any]:
        _require_connection()

        def run() -> dict[str, Any]:
            tid = _job_id(transfer_id, "transfer")
            return {"cancelled": any(registry.cancel(tid) for registry in transfer_registries)}

        return answer(plugin, run)

    @http_post("/infra/storage/bundle", sync_to_thread=True, exception_handlers=_ERRORS)
    def _bundle(data: dict[str, Any]) -> dict[str, Any]:
        _require_connection()
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

    @http_get("/infra/storage/bundle/{bundle_id:str}", sync_to_thread=True, exception_handlers=_ERRORS)
    def _bundle_status(bundle_id: str) -> dict[str, Any]:
        _require_connection()

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

    @http_delete(
        "/infra/storage/bundle/{bundle_id:str}", status_code=200, sync_to_thread=True, exception_handlers=_ERRORS
    )
    def _cancel_bundle(bundle_id: str) -> dict[str, Any]:
        _require_connection()

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


# ── transitional: the workspace routes ─────────────────────────────────────────


def workspace_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    """The four workspace routes of the transitional deployment surface.

    Args:
        plugin: A plugin whose class implements
            :class:`~tlc_plugin_sdk.infrastructure.transitional.WorkspaceFacet`.

    Returns:
        The handlers.

    Raises:
        TypeError: When the plugin has no workspace facet.
    """
    if not isinstance(plugin, WorkspaceFacet):
        msg = "workspace_handlers needs a WorkspaceFacet plugin"
        raise TypeError(msg)
    facet: WorkspaceFacet = plugin

    @http_get("/infra/workspaces", sync_to_thread=True, exception_handlers=_ERRORS)
    def _list_workspaces(owner: str = "") -> dict[str, Any]:
        return answer(plugin, lambda: facet.list_workspaces(owner=owner).to_dict())

    @http_get("/infra/workspace-placement", sync_to_thread=True, exception_handlers=_ERRORS)
    def _workspace_placement(region: str = "", vpc_id: str = "") -> dict[str, Any]:
        return answer(plugin, lambda: facet.workspace_placement(region=region, vpc_id=vpc_id).to_dict())

    @http_get("/infra/workspace-diagnostics/{provider_id:str}", sync_to_thread=True, exception_handlers=_ERRORS)
    def _workspace_diagnostics(provider_id: str) -> dict[str, Any]:
        return answer(plugin, lambda: facet.workspace_diagnostics(provider_id).to_dict())

    @http_post(
        "/infra/workspace-retain/{provider_id:str}", sync_to_thread=True, status_code=200, exception_handlers=_ERRORS
    )
    def _workspace_retain(provider_id: str) -> dict[str, Any]:
        return answer(plugin, lambda: facet.workspace_retain_failed(provider_id).to_dict())

    return [_list_workspaces, _workspace_placement, _workspace_diagnostics, _workspace_retain]


# ── end transitional ───────────────────────────────────────────────────────────

# ── Settings ───────────────────────────────────────────────────────────────────


def settings_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    """``GET /settings`` and ``POST /settings`` over the plugin's settings layer. Never sent a Connection.

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

    @http_get("/settings", sync_to_thread=True, exception_handlers=_ERRORS)
    def _get_settings() -> dict[str, Any]:
        _refuse_connection()
        return answer(plugin, lambda: plugin.settings_view(settings.load()))

    @http_post("/settings", sync_to_thread=True, exception_handlers=_ERRORS)
    def _update_settings(data: dict[str, Any] | None = None) -> dict[str, Any]:
        _refuse_connection()
        # A corrupt file is a 409 before any merge: ``save`` loads first.
        return answer(plugin, lambda: plugin.settings_view(settings.save(data or {})))

    return [_get_settings, _update_settings]


# ── Assembly ───────────────────────────────────────────────────────────────────

#: Operation id → what mounts its routes. Every id of ``OPERATIONS`` has an entry.
OPERATION_HANDLERS: dict[str, Callable[[InfrastructurePlugin], list[Any]]] = {
    "connection.grant": _grant_handlers,
    "connection.grant_update": _grant_update_handlers,
    "connection.verify": _verify_handlers,
    "connection.node_access": _node_access_handlers,
    "storage.candidates": _candidates_handlers,
    "node.create": _node_create_handlers,
    "node.state": _node_state_handlers,
    "node.delete": _node_delete_handlers,
    "node.check": _node_check_handlers,
    "node.diagnostics": _no_handlers,
    "catalog": _catalog_handlers,
    "catalog.locations": _locations_handlers,
    "deployment.options": _deployment_options_handlers,
    "deployment.check": _deployment_check_handlers,
    "deployment.prepare": _deployment_prepare_handlers,
    "deployment.create": _deployment_create_handlers,
    "deployment.state": _deployment_state_handlers,
    "deployment.delete": _deployment_delete_handlers,
    "deployment.retain": _deployment_retain_handlers,
    "deployment.list": _deployment_list_handlers,
    "deployment.sweep": _deployment_sweep_handlers,
    "storage": storage_handlers,
    "dns": _dns_handlers,
}


def build_infra_handlers(plugin: InfrastructurePlugin) -> list[Any]:
    """Every handler the base mounts for ``plugin``: capabilities, each provided operation, settings.

    Args:
        plugin: The plugin.

    Returns:
        The handlers, in mount order.
    """
    handlers = _capabilities_handlers(plugin)
    for operation in provided_operations(plugin):
        build = OPERATION_HANDLERS.get(operation)
        if build is not None:
            handlers += build(plugin)
    # ── transitional ──
    if isinstance(plugin, WorkspaceFacet):
        handlers += workspace_handlers(plugin)
    # ── end transitional ──
    if plugin.settings is not None:
        handlers += settings_handlers(plugin)
    return handlers
