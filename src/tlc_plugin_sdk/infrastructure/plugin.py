# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The infrastructure-plugin base: the typed provider contract for remote nodes.

An infrastructure plugin (``kind = "infrastructure"`` in its manifest) owns exactly the provider
API: capabilities, create node, node state, delete node, and an optional preflight. The host's
``InfraManager`` calls these through the worker proxy; everything else (node registry, lifecycle
state machine, heartbeats, idle teardown) lives in the host. A created node is reached by its
agent URL alone: the host talks to the node agent, and the agent proxies host traffic to the
node's loopback-only workers.

Subclass :class:`InfrastructurePlugin`, implement the four abstract methods, and the base mounts
the ``/infra/*`` routes for the core. Add a facet base class
(:class:`~tlc_plugin_sdk.infrastructure.StorageFacet`,
:class:`~tlc_plugin_sdk.infrastructure.CatalogFacet`,
:class:`~tlc_plugin_sdk.infrastructure.WorkspaceFacet`,
:class:`~tlc_plugin_sdk.infrastructure.LegacyOwnerCredentialsFacet`) and its routes are mounted
and its id listed in ``capabilities.facets``. Set ``settings`` to a
:class:`~tlc_plugin_sdk.shared.settings.PluginSettings` and ``GET/POST /settings`` are mounted.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Any

from tlc_plugin_sdk.contract import HubPlugin
from tlc_plugin_sdk.infrastructure import facets as _facets
from tlc_plugin_sdk.infrastructure.errors import scrub
from tlc_plugin_sdk.infrastructure.types import (
    FACET_CATALOG,
    FACET_LEGACY_OWNER_CREDENTIALS,
    FACET_STORAGE,
    FACET_WORKSPACES,
    CapabilitiesResponse,
    ConnectionCheckResponse,
    CreateNodeRequest,
    CreateNodeResponse,
    NodeStateResponse,
    PreflightResponse,
)

if TYPE_CHECKING:
    from tlc_plugin_sdk.shared.settings import PluginSettings

__all__ = ["InfrastructurePlugin"]

#: Facet id → the mixin that implements it, in wire order.
_FACET_CLASSES: tuple[tuple[str, type[Any]], ...] = (
    (FACET_STORAGE, _facets.StorageFacet),
    (FACET_CATALOG, _facets.CatalogFacet),
    (FACET_WORKSPACES, _facets.WorkspaceFacet),
    (FACET_LEGACY_OWNER_CREDENTIALS, _facets.LegacyOwnerCredentialsFacet),
)


class InfrastructurePlugin(HubPlugin):
    """Base class for infrastructure-provider plugins (``kind = "infrastructure"``).

    A provider implements the four abstract methods below; the default
    :meth:`get_route_handlers` mounts Litestar handlers for the core ``/infra/*`` routes, for
    every facet in the class's bases, and for ``/settings`` when :attr:`settings` is set.

    To add plugin-private routes (an auth flow, a machine check), override
    ``get_route_handlers`` and append to the default list::

        def get_route_handlers(self):
            return [*super().get_route_handlers(), my_private_route]

    Attributes:
        settings: The plugin's settings layer, or ``None`` for a plugin that keeps its own
            ``/settings`` routes (the base then mounts none).
    """

    settings: PluginSettings[Any] | None = None

    # ── core, mandatory ──

    @abstractmethod
    def capabilities(self) -> CapabilitiesResponse:
        """Report what this provider offers.

        Called via ``GET /infra/capabilities``. The ``node_types`` list populates the host's
        create-node dialog. The SDK fills ``facets`` and ``storage`` from the class's facets
        before answering; an author-set value of either is overwritten.
        """

    @abstractmethod
    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
        """Create a node (start the agent process on the provider's infrastructure).

        Called via ``POST /infra/nodes``. The host supplies the ``node_id`` and ``token`` for
        the agent; the provider starts the agent and returns the ``provider_id`` the host uses
        for subsequent status/delete calls. A workspace (``request.flavor == "workspace"``)
        answers ``services`` instead of ``agent_url``.

        Raises:
            ProviderError: With the provider's own sentence; any other exception answers 502.
        """

    @abstractmethod
    def node_state(self, provider_id: str) -> NodeStateResponse:
        """Query the state of a previously created node.

        Called via ``GET /infra/nodes/{provider_id}``.

        Returns:
            ``running`` if the agent is alive, ``pending`` while it starts, ``exited``,
            ``terminated`` or ``gone`` if it is not, ``unknown`` if the provider cannot tell.
        """

    @abstractmethod
    def delete_node(self, provider_id: str) -> NodeStateResponse:
        """Terminate a node and clean up provider resources.

        Called via ``DELETE /infra/nodes/{provider_id}``. Must be idempotent — a repeated
        delete on a stopped node returns ``terminated`` or ``gone``.
        """

    # ── core, optional ──

    def preflight(self, node_type: str = "", datacenter: str = "") -> PreflightResponse:
        """Optional pre-start checks (funds, stock, quota, reachability).

        Called via ``GET /infra/preflight``. The default returns an unconditional pass;
        override to run provider-specific checks.
        """
        return PreflightResponse(ok=True, summary="no preflight checks")

    def connection_check(self) -> ConnectionCheckResponse:
        """Who the request's Connection acts as (``GET /infra/connection/check``).

        Runs only with a Connection on the request, after the SDK resolved it: the default
        reports just that. Override to ask the provider who the resolved credential is (the AWS
        plugin: ``sts:GetCallerIdentity``). A failure raises like any handler (a refused role is
        already a 424 before this runs).
        """
        return ConnectionCheckResponse(checked=["resolve"])

    def node_diagnostics(self, provider_id: str) -> NodeStateResponse:
        """The node's state with startup diagnostics (``GET /infra/nodes/{id}?diagnostics=true``).

        The default is :meth:`node_state`; override to fill the ``bootstrap_*`` fields from the
        provider's console or boot log.
        """
        return self.node_state(provider_id)

    def secret_values(self) -> list[str]:
        """Every value an error message must never echo.

        The route layer scrubs these from every ``detail`` it answers. The default is the
        settings layer's secrets when :attr:`settings` is set, else nothing.

        Returns:
            The values (empty ones excluded).
        """
        if self.settings is None:
            return []
        try:
            return self.settings.secret_values(self.settings.load())
        except Exception:
            return []

    def describe_error(self, exc: Exception) -> str:
        """The sentence a person reads for an exception the provider did not word (the 502 detail).

        Every route the SDK mounts — the core, each facet's calls, the transfer and bundle
        registries' plan, start and status, and the errors a registry's background job records —
        answers an unexpected exception with this. The default is the exception's own text
        scrubbed of :meth:`secret_values`; override to strip what a cloud SDK's message carries
        (ARNs, request ids, endpoints). The route layer scrubs the result again, so an override
        need not — and must not truncate or re-encode the text (quote, escape, base64): the SDK
        scrubs whole secret values first and truncates after, and a cut or re-encoded secret is
        no longer recognised.

        Args:
            exc: The exception.

        Returns:
            One or two sentences, untruncated; ``""`` falls back to the default.
        """
        return scrub(str(exc).strip() or type(exc).__name__, self.secret_values())

    def settings_view(self, settings: Any) -> dict[str, Any]:
        """What ``GET /settings`` and ``POST /settings`` answer: the redacted record.

        Override to add computed keys (an effective placement, a derived URL, per-row problems).

        Args:
            settings: The record, as :attr:`settings` loaded or saved it.

        Returns:
            A JSON-serializable dict with no secret value in it.
        """
        if self.settings is None:
            return {}
        return self.settings.redacted(settings)

    # ── facets ──

    def implemented_facets(self) -> list[str]:
        """The facet ids this class implements, in wire order (what ``capabilities.facets`` lists)."""
        return [facet_id for facet_id, cls in _FACET_CLASSES if isinstance(self, cls)]

    # ── routes ──

    def get_route_handlers(self) -> list[Any]:
        """Litestar handlers for the core ``/infra/*`` routes, every implemented facet, and ``/settings``.

        Override and extend (``[*super().get_route_handlers(), ...]``) to add plugin-private routes.
        """
        # Litestar lives behind this import: ``import tlc_plugin_sdk`` stays light.
        from tlc_plugin_sdk.infrastructure.routes import build_infra_handlers

        return build_infra_handlers(self)
