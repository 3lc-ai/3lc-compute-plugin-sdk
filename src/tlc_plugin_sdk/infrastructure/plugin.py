# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The infrastructure-plugin base: the typed provider contract.

An infrastructure plugin (``kind = "infrastructure"`` in its manifest) owns exactly the provider's
API. The host calls it through the worker proxy; everything else (the node registry, the
lifecycle state machine, heartbeats, idle teardown, DNS names, grants) lives in the host.

Subclass :class:`InfrastructurePlugin`, implement :meth:`~InfrastructurePlugin.capabilities`, and
add the groups the provider offers as mixins
(:class:`~tlc_plugin_sdk.infrastructure.NodeProvider`,
:class:`~tlc_plugin_sdk.infrastructure.DeploymentProvider`,
:class:`~tlc_plugin_sdk.infrastructure.StorageFacet`,
:class:`~tlc_plugin_sdk.infrastructure.CatalogFacet`,
:class:`~tlc_plugin_sdk.infrastructure.DnsFacet`). The base mounts one ``/infra/*`` route per
operation the class provides and lists exactly those in ``capabilities.operations``: a mixin's
abstract methods always, an optional method when the class overrides it. Set ``settings`` to a
:class:`~tlc_plugin_sdk.shared.settings.PluginSettings` and ``GET/POST /settings`` are mounted.
"""

from __future__ import annotations

import re
from abc import abstractmethod
from typing import TYPE_CHECKING, Any

from tlc_plugin_sdk.contract import HubPlugin
from tlc_plugin_sdk.infrastructure.errors import NotSupported, scrub
from tlc_plugin_sdk.infrastructure.facets import CatalogFacet, DeploymentProvider, DnsFacet, NodeProvider, StorageFacet
from tlc_plugin_sdk.infrastructure.transitional import (
    OPERATION_WORKSPACE_CREATE,
    OPERATION_WORKSPACE_NAME_CHECK,
    OPERATION_WORKSPACE_SWEEP,
    WorkspaceFacet,
)
from tlc_plugin_sdk.infrastructure.types import (
    CONTRACT_GENERATION,
    FIELD_KEY_PATTERN,
    OPERATIONS,
    PROVIDER_PATTERN,
    CapabilitiesResponse,
    ConnectionNodeAccess,
    ConnectionVerification,
    FormField,
    Grant,
    GrantRequest,
    GrantUpdate,
    GrantUpdateRequest,
    NodeAccessRequest,
    StorageCandidates,
    VerifyRequest,
)

if TYPE_CHECKING:
    from tlc_plugin_sdk.shared.settings import PluginSettings

__all__ = ["InfrastructurePlugin", "capabilities_problems", "provided_operations"]

_PROVIDER = re.compile(PROVIDER_PATTERN)
_FIELD_KEY = re.compile(FIELD_KEY_PATTERN)
_MAX_FIELD_LENGTH = 2048
_MAX_RULES = 20


class InfrastructurePlugin(HubPlugin):
    """Base class for infrastructure-provider plugins (``kind = "infrastructure"``).

    A provider implements :meth:`capabilities` and adds the mixins of the groups it offers. The
    methods below the capabilities are the *connect an account* group: each is optional, and
    overriding one mounts its route and lists its operation. A plugin may be any combination of
    groups, including none besides capabilities.

    To add plugin-private routes (an auth flow, a machine check, the data behind the plugin's own
    page), override ``get_route_handlers`` and append to the default list::

        def get_route_handlers(self):
            return [*super().get_route_handlers(), my_private_route]

    Attributes:
        settings: The plugin's settings layer, or ``None`` for a plugin that keeps its own
            ``/settings`` routes (the base then mounts none).
    """

    settings: PluginSettings[Any] | None = None

    # ── capabilities ──

    @abstractmethod
    def capabilities(self) -> CapabilitiesResponse:
        """``GET /infra/capabilities``: what this provider offers and how an account is connected to it.

        Never sent a Connection: anything it says about this host (``host``, whether a method is
        ``available``) is read from the machine's own credential chain. The SDK fills ``storage``
        and ``operations`` before answering, overwriting an author-set value of either, and
        answers 502 for a declaration :func:`capabilities_problems` finds fault with.
        """

    def readiness(self) -> dict[str, Any]:
        """``{"ready", "missing_fields"}`` for :meth:`capabilities`, from the settings layer.

        Returns:
            Keyword arguments for :class:`~tlc_plugin_sdk.infrastructure.CapabilitiesResponse`:
            the settings still needed as prompts, and ``ready`` when no required one is missing.
            A plugin without a settings layer is ready.
        """
        if self.settings is None:
            return {"ready": True, "missing_fields": []}
        missing = self.settings.missing_fields(self.settings.load())
        return {
            "ready": not any(f.required for f in missing),
            "missing_fields": [FormField.from_settings(f) for f in missing],
        }

    # ── connect an account (each optional) ──

    def describe_grant(self, request: GrantRequest) -> Grant:
        """``POST /infra/connection/grant``: what a person runs in their own account (``connection.grant``).

        Called with no Connection: it changes nothing and needs nothing of the account. It may
        read this host's own identity to name what the grant trusts. ``action="update"`` answers
        a link or files that change the existing grant in place, never a create link. The answer
        carries the request's permissions in canonical form and the access fields to store on
        the Connection.

        Args:
            request: The Connection the grant is for and what it should allow.

        Returns:
            The grant, for the person to apply. 3LC never applies it.

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = "This provider has no grant to describe."
        raise NotSupported(msg)

    def prepare_grant_update(self, request: GrantUpdateRequest) -> GrantUpdate:
        """``POST /infra/connection/grant/update``: prepare a reviewed change (``connection.grant_update``).

        For a provider whose cloud has a reviewed change (a change set). Runs under the
        Connection and prepares, never executes. A provider without one does not implement this:
        its update is ``describe_grant(action="update")``.

        Args:
            request: The installed grant and the permissions asked for.

        Returns:
            Where the change stands.

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = "This provider prepares no grant updates."
        raise NotSupported(msg)

    def verify_connection(self, request: VerifyRequest) -> ConnectionVerification:
        """``POST /infra/connection/verify``: who the request's Connection is (``connection.verify``).

        Runs under the Connection, after the SDK resolved it. Ask the provider's API who the
        credential is and which account it is in; read back the installed grant when there is
        one. A step that fails raises; a sample that could not be confirmed is a ``warn`` check; a
        refused operation is an ``error`` check.

        Args:
            request: The storage roots to sample.

        Returns:
            The identity, the account as the cloud says it, and what was observed.

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = "This provider verifies no Connections."
        raise NotSupported(msg)

    def connection_node_access(self, request: NodeAccessRequest, *, apply: bool = False) -> ConnectionNodeAccess:
        """``GET``/``POST /infra/connection/node-access``: inspect or set up node access (``connection.node_access``).

        How a machine on its own sign-in sets up a scoped node identity directly, when it has
        the rights to. The host authorizes Connection administration before applying a setup and
        supplies its own storage roots. Inspection must not change anything in the account.

        Args:
            request: The host's storage roots, and for a setup the reviewed rules.
            apply: ``False`` inspects (``GET``); ``True`` applies the displayed setup (``POST``).

        Returns:
            The setup and whether it is in place.

        Raises:
            NotSupported: By default (the routes are then not mounted).
        """
        msg = "This provider sets up no node access."
        raise NotSupported(msg)

    def storage_candidates(self) -> StorageCandidates:
        """``GET /infra/storage/candidates``: the locations a rule editor can offer (``storage.candidates``).

        Runs under the Connection with a discovery-only grant and returns names, never contents.

        Returns:
            At most 2000 candidates, with ``truncated`` set when the account holds more.

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = "This provider lists no storage candidates."
        raise NotSupported(msg)

    # ── errors and settings ──

    def secret_values(self) -> list[str]:
        """Every value an error message must never echo.

        The route layer scrubs these from every ``detail`` and ``remedy`` it answers, with the
        request's credential and the request's own secrets. The default is the settings layer's
        secrets when :attr:`settings` is set, else nothing.

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

        Every route the SDK mounts, and the errors a transfer or bundle registry's background job
        records, answers an unexpected exception with this. The default is the exception's own
        text scrubbed of :meth:`secret_values`; override to strip what a cloud SDK's message
        carries (resource names, request ids, endpoints). The route layer scrubs the result again,
        so an override need not, and must not truncate or re-encode the text (quote, escape,
        base64): the SDK scrubs whole secret values first and truncates after, and a cut or
        re-encoded secret is no longer recognised.

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

    # ── operations and routes ──

    def operations(self) -> list[str]:
        """The ids of the operations this class provides (what ``capabilities.operations`` lists)."""
        return provided_operations(self)

    def get_route_handlers(self) -> list[Any]:
        """Litestar handlers for capabilities, every operation the class provides, and ``/settings``.

        Override and extend (``[*super().get_route_handlers(), ...]``) to add plugin-private routes.
        """
        # Litestar lives behind this import: ``import tlc_plugin_sdk`` stays light.
        from tlc_plugin_sdk.infrastructure.routes import build_infra_handlers

        return build_infra_handlers(self)


def _overrides(plugin: object, base: type[Any], method: str) -> bool:
    """Whether ``plugin``'s class provides ``method`` itself rather than inheriting ``base``'s default."""
    return getattr(type(plugin), method, None) is not getattr(base, method)


#: ``(operation, the group it belongs to, the optional method that provides it or "" for always)``.
_PROVIDERS: tuple[tuple[str, type[Any], str], ...] = (
    ("connection.grant", InfrastructurePlugin, "describe_grant"),
    ("connection.grant_update", InfrastructurePlugin, "prepare_grant_update"),
    ("connection.verify", InfrastructurePlugin, "verify_connection"),
    ("connection.node_access", InfrastructurePlugin, "connection_node_access"),
    ("storage.candidates", InfrastructurePlugin, "storage_candidates"),
    ("node.create", NodeProvider, ""),
    ("node.state", NodeProvider, ""),
    ("node.delete", NodeProvider, ""),
    ("node.check", NodeProvider, "check_node"),
    ("node.diagnostics", NodeProvider, "node_diagnostics"),
    ("catalog", CatalogFacet, ""),
    ("catalog.locations", CatalogFacet, "locations"),
    ("deployment.options", DeploymentProvider, "deployment_options"),
    ("deployment.check", DeploymentProvider, "check_deployment"),
    ("deployment.prepare", DeploymentProvider, "prepare_deployment"),
    ("deployment.create", DeploymentProvider, ""),
    ("deployment.state", DeploymentProvider, ""),
    ("deployment.delete", DeploymentProvider, ""),
    ("deployment.retain", DeploymentProvider, "retain_deployment"),
    ("deployment.list", DeploymentProvider, "list_deployments"),
    ("deployment.sweep", DeploymentProvider, "sweep_deployments"),
    ("storage", StorageFacet, ""),
    ("dns", DnsFacet, ""),
)


def provided_operations(plugin: InfrastructurePlugin) -> list[str]:
    """The operations ``plugin``'s class provides, in the order of :data:`OPERATIONS`.

    An operation is provided when the class has its group's mixin and, for an optional one,
    overrides the method. The route layer mounts exactly these.

    Args:
        plugin: The plugin.

    Returns:
        The operation ids, the transitional ones last.
    """
    provided = {
        operation
        for operation, group, method in _PROVIDERS
        if isinstance(plugin, group) and (not method or _overrides(plugin, group, method))
    }
    listed = [operation for operation in OPERATIONS if operation in provided]
    # ── transitional ──
    if isinstance(plugin, WorkspaceFacet):
        if isinstance(plugin, NodeProvider):
            listed.append(OPERATION_WORKSPACE_CREATE)
        private = set(plugin.private_operations)
        listed += [op for op in (OPERATION_WORKSPACE_NAME_CHECK, OPERATION_WORKSPACE_SWEEP) if op in private]
    # ── end transitional ──
    return listed


def _field_problems(where: str, fields: list[FormField]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for f in fields:
        if not _FIELD_KEY.match(f.key):
            problems.append(f"{where}: field key {f.key!r} does not match {FIELD_KEY_PATTERN}")
        if f.key in seen:
            problems.append(f"{where}: field key {f.key!r} is listed twice")
        seen.add(f.key)
        if f.secret != (f.kind == "secret"):
            problems.append(f"{where}: field {f.key!r} must have secret true exactly when its kind is 'secret'")
        if not 1 <= f.max_length <= _MAX_FIELD_LENGTH:
            problems.append(f"{where}: field {f.key!r} has max_length outside 1..{_MAX_FIELD_LENGTH}")
        if f.choices and f.kind != "select":
            problems.append(f"{where}: field {f.key!r} lists choices and is not a select")
    return problems


def capabilities_problems(plugin: InfrastructurePlugin, caps: CapabilitiesResponse) -> list[str]:
    """What is wrong with a capabilities declaration, one sentence each; ``[]`` when it is valid.

    The rules: ``provider`` matches :data:`~tlc_plugin_sdk.infrastructure.types.PROVIDER_PATTERN`;
    ``contract`` is this generation; field keys are well formed and unique per list, and across
    the account's fields, its defaults and each method's fields; a field is ``secret`` exactly when
    its kind is; there is at most one access method per kind and at most one ``preferred``; every
    permission option's key is unique; a storage schema allows 1 to 20 rules; ``nodes`` is set
    exactly when the class is a ``NodeProvider`` and ``deployments`` exactly when it is a
    ``DeploymentProvider``.

    Args:
        plugin: The plugin whose class the declaration must agree with.
        caps: The declaration.

    Returns:
        The problems.
    """
    problems: list[str] = []
    if not _PROVIDER.match(caps.provider):
        problems.append(f"provider {caps.provider!r} does not match {PROVIDER_PATTERN}")
    if caps.contract != CONTRACT_GENERATION:
        problems.append(f"contract is {caps.contract!r}, not {CONTRACT_GENERATION}")
    problems += _field_problems("missing_fields", caps.missing_fields)
    account_fields: list[FormField] = []
    if caps.account is not None:
        problems += _field_problems("account.fields", caps.account.fields)
        problems += _field_problems("account.defaults", caps.account.defaults)
        account_fields = [*caps.account.fields, *caps.account.defaults]
        shared = {f.key for f in caps.account.fields} & {f.key for f in caps.account.defaults}
        problems += [f"account: field key {key!r} is both a field and a default" for key in sorted(shared)]
    kinds: set[str] = set()
    for method in caps.access_methods:
        where = f"access_methods[{method.kind}]"
        if method.kind in kinds:
            problems.append(f"{where}: more than one access method of this kind")
        kinds.add(method.kind)
        problems += _field_problems(f"{where}.fields", method.fields)
        clash = {f.key for f in method.fields} & {f.key for f in account_fields}
        problems += [f"{where}: field key {key!r} is also an account field" for key in sorted(clash)]
    if sum(1 for method in caps.access_methods if method.preferred) > 1:
        problems.append("access_methods: more than one is preferred")
    option_keys = [option.key for option in caps.permissions.options]
    problems += [
        f"permissions.options: key {key!r} is listed twice"
        for key in sorted(set(option_keys))
        if option_keys.count(key) > 1
    ]
    storage = caps.permissions.storage
    if storage is not None and not 1 <= storage.max_rules <= _MAX_RULES:
        problems.append(f"permissions.storage: max_rules is outside 1..{_MAX_RULES}")
    if (caps.nodes is not None) != isinstance(plugin, NodeProvider):
        problems.append("nodes must be set exactly when the class is a NodeProvider")
    if (caps.deployments is not None) != isinstance(plugin, DeploymentProvider):
        problems.append("deployments must be set exactly when the class is a DeploymentProvider")
    return problems
