# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Infrastructure-plugin contract — the typed provider surface for remote nodes.

The public import path for everything a provider author needs:

- :class:`InfrastructurePlugin` — the base (:mod:`~tlc_plugin_sdk.infrastructure.plugin`).
- The facets — :class:`StorageFacet`, :class:`CatalogFacet`, :class:`WorkspaceFacet`,
  :class:`LegacyOwnerCredentialsFacet` (:mod:`~tlc_plugin_sdk.infrastructure.facets`).
- The wire dataclasses (:mod:`~tlc_plugin_sdk.infrastructure.types`).
- The error family (:mod:`~tlc_plugin_sdk.infrastructure.errors`).
- The settings layer — :class:`PluginSettings`, :func:`secret`, :func:`option`
  (:mod:`tlc_plugin_sdk.shared.settings`).

Importing this package pulls in neither litestar nor ``tlc``: the route layer
(:mod:`~tlc_plugin_sdk.infrastructure.routes`) loads behind
:meth:`InfrastructurePlugin.get_route_handlers`, and the conformance kit
(:mod:`~tlc_plugin_sdk.infrastructure.testing`) is imported on its own.
"""

from __future__ import annotations

from tlc_plugin_sdk.infrastructure.errors import (
    Conflict,
    InvalidRequest,
    NotConfigured,
    NotFound,
    NotSupported,
    ProviderError,
)
from tlc_plugin_sdk.infrastructure.facets import (
    CatalogFacet,
    LegacyOwnerCredentialsFacet,
    StorageFacet,
    WorkspaceFacet,
)
from tlc_plugin_sdk.infrastructure.plugin import InfrastructurePlugin
from tlc_plugin_sdk.infrastructure.types import (
    FACET_CATALOG,
    FACET_LEGACY_OWNER_CREDENTIALS,
    FACET_STORAGE,
    FACET_WORKSPACES,
    FACETS,
    BundleRequest,
    CapabilitiesResponse,
    ConnectionCheckResponse,
    CpuCatalog,
    CreateNodeRequest,
    CreateNodeResponse,
    CreateStorageRequest,
    Datacenters,
    DeleteObjectsRequest,
    GpuCatalog,
    LoginDescriptor,
    NodeProviderState,
    NodeStateResponse,
    ObjectListing,
    OwnerCredentialsDescriptor,
    PreflightCheck,
    PreflightResponse,
    PresignRequest,
    PresignResponse,
    ProjectStorage,
    Region,
    RoleDescriptor,
    SettingsField,
    StorageCapabilities,
    StorageDeleted,
    StorageItem,
    StorageListing,
    TransferRequest,
    WorkspaceInstance,
    WorkspaceListing,
    WorkspaceRequest,
    is_node_state,
    preflight_query,
)
from tlc_plugin_sdk.shared.settings import PluginSettings, SettingsUnreadable, option, secret

__all__ = [
    "FACETS",
    "FACET_CATALOG",
    "FACET_LEGACY_OWNER_CREDENTIALS",
    "FACET_STORAGE",
    "FACET_WORKSPACES",
    "BundleRequest",
    "CapabilitiesResponse",
    "CatalogFacet",
    "Conflict",
    "ConnectionCheckResponse",
    "CpuCatalog",
    "CreateNodeRequest",
    "CreateNodeResponse",
    "CreateStorageRequest",
    "Datacenters",
    "DeleteObjectsRequest",
    "GpuCatalog",
    "InfrastructurePlugin",
    "InvalidRequest",
    "LegacyOwnerCredentialsFacet",
    "LoginDescriptor",
    "NodeProviderState",
    "NodeStateResponse",
    "NotConfigured",
    "NotFound",
    "NotSupported",
    "ObjectListing",
    "OwnerCredentialsDescriptor",
    "PluginSettings",
    "PreflightCheck",
    "PreflightResponse",
    "PresignRequest",
    "PresignResponse",
    "ProjectStorage",
    "ProviderError",
    "Region",
    "RoleDescriptor",
    "SettingsField",
    "SettingsUnreadable",
    "StorageCapabilities",
    "StorageDeleted",
    "StorageFacet",
    "StorageItem",
    "StorageListing",
    "TransferRequest",
    "WorkspaceFacet",
    "WorkspaceInstance",
    "WorkspaceListing",
    "WorkspaceRequest",
    "is_node_state",
    "option",
    "preflight_query",
    "secret",
]
