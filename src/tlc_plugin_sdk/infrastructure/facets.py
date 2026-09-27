# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Facets: the optional surfaces an infrastructure plugin opts into by subclassing.

A facet is a mixin ``ABC``. Add it to the class bases next to
:class:`~tlc_plugin_sdk.infrastructure.InfrastructurePlugin` and the base mounts that facet's
routes and lists its id in ``capabilities.facets``::

    class MyProvider(InfrastructurePlugin, StorageFacet, CatalogFacet): ...

The abstract methods are the smallest set a provider must write; every other method has a
default that raises :class:`~tlc_plugin_sdk.infrastructure.NotSupported` (HTTP 501), so the
flags in :class:`~tlc_plugin_sdk.infrastructure.StorageCapabilities` stay the UI's truth. All
methods are plain ``def``: the routes run them in a worker thread, and
:func:`tlc_plugin_sdk.connections.current_credential` works inside every one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from tlc_plugin_sdk.infrastructure.errors import NotSupported
from tlc_plugin_sdk.infrastructure.types import (
    CpuCatalog,
    CreateStorageRequest,
    Datacenters,
    DeleteObjectsRequest,
    GpuCatalog,
    NodeStateResponse,
    ObjectListing,
    OwnerCredentialsDescriptor,
    PresignRequest,
    PresignResponse,
    StorageCapabilities,
    StorageDeleted,
    StorageItem,
    StorageListing,
    WorkspaceListing,
)

if TYPE_CHECKING:
    from tlc_plugin_sdk.shared.storage_bundle import BundleRegistry
    from tlc_plugin_sdk.shared.storage_transfer import TransferRegistry

__all__ = ["CatalogFacet", "LegacyOwnerCredentialsFacet", "StorageFacet", "WorkspaceFacet"]


def _unsupported(what: str) -> str:
    return f"This provider does not support {what}."


class StorageFacet(ABC):
    """Buckets, containers or volumes the provider creates and the Data page fills and browses.

    Abstract: :meth:`storage_capabilities` and :meth:`list_storage`. The transfer and bundle
    routes are SDK-owned: hand back a :class:`~tlc_plugin_sdk.shared.storage_transfer.TransferRegistry`
    / :class:`~tlc_plugin_sdk.shared.storage_bundle.BundleRegistry` for a URL and the SDK serves
    ``POST/GET/DELETE /infra/storage/transfer[/{id}]`` and ``…/bundle[/{id}]``. Cache the registry
    per storage (a volume, a bucket): status and cancel search every registry the facet handed out.
    """

    @abstractmethod
    def storage_capabilities(self) -> StorageCapabilities:
        """What this provider's storage can do; merged into ``capabilities.storage`` and heading every listing."""

    @abstractmethod
    def list_storage(self, *, fallback_url: str = "") -> StorageListing:
        """``GET /infra/storage?fallback_url=``: every storage the credentials can list.

        Args:
            fallback_url: The deployment's project root, for credentials that may not list
                storage: its bucket stands in for the listing.

        Returns:
            The listing (its ``capabilities`` is normally :meth:`storage_capabilities`).
        """

    def create_storage(self, request: CreateStorageRequest) -> StorageItem:
        """``POST /infra/storage`` (201): create one storage.

        Args:
            request: The name, region and size asked for.

        Returns:
            The created item.

        Raises:
            NotSupported: By default.
        """
        msg = _unsupported("creating storage")
        raise NotSupported(msg)

    def delete_storage(self, storage_id: str) -> StorageDeleted:
        """``DELETE /infra/storage/{id}``.

        Args:
            storage_id: The item's ``id``.

        Returns:
            Whether it was deleted.

        Raises:
            NotSupported: By default.
        """
        msg = _unsupported("deleting storage")
        raise NotSupported(msg)

    def presign(self, request: PresignRequest) -> PresignResponse:
        """``POST /infra/storage/presign``: URLs the browser uploads to or downloads from.

        Args:
            request: The folder URL, the files and the mode.

        Returns:
            The presigned URLs.

        Raises:
            NotSupported: By default.
        """
        msg = _unsupported("presigned URLs")
        raise NotSupported(msg)

    def list_objects(self, url: str, *, next_token: str = "") -> ObjectListing:
        """``GET /infra/storage/list?url=&next_token=``: one level of a folder.

        Args:
            url: The folder URL.
            next_token: Continues a truncated listing.

        Returns:
            The folder's prefixes and objects.

        Raises:
            NotSupported: By default.
        """
        msg = _unsupported("browsing storage")
        raise NotSupported(msg)

    def delete_objects(self, request: DeleteObjectsRequest) -> dict[str, Any]:
        """``POST /infra/storage/delete``: delete files and folders under a folder (counted first with ``dry_run``).

        Args:
            request: The folder URL and the paths under it.

        Returns:
            The provider's own counts (``deleted``, ``count``, ``bytes``, ``failures`` …); the
            shape is provider-specific and the Data page only counts, so it is not typed.

        Raises:
            NotSupported: By default.
        """
        msg = _unsupported("deleting objects")
        raise NotSupported(msg)

    def transfer_registry(self, url: str) -> TransferRegistry:
        """The transfer engine for the storage ``url`` is on (backs the transfer routes).

        Args:
            url: A folder URL on the storage.

        Returns:
            The registry (cached per storage by the author).

        Raises:
            NotSupported: By default.
        """
        msg = _unsupported("transfers")
        raise NotSupported(msg)

    def bundle_registry(self, url: str) -> BundleRegistry:
        """The folder-download engine for the storage ``url`` is on (backs the bundle routes).

        Args:
            url: A folder URL on the storage.

        Returns:
            The registry (cached per storage by the author).

        Raises:
            NotSupported: By default.
        """
        msg = _unsupported("folder downloads")
        raise NotSupported(msg)


class CatalogFacet(ABC):
    """Live offerings for the pickers: GPU types, workspace sizes, datacenters with stock."""

    @abstractmethod
    def gpu_catalog(self, *, node_type: str = "", region: str = "") -> GpuCatalog:
        """``GET /infra/gpu-catalog?node_type=&region=`` (``gpu_type`` accepted as the alias).

        Args:
            node_type: A type to focus on, or ``""``.
            region: A region to price in, or ``""`` for the configured one.

        Returns:
            The rows; a failed lookup is an empty list with ``error`` set, never an exception.
        """

    def cpu_catalog(self, *, region: str = "") -> CpuCatalog:
        """``GET /infra/cpu-catalog?region=``: workspace sizes with prices (empty by default).

        Args:
            region: A region to price in, or ``""``.

        Returns:
            The rows.
        """
        return CpuCatalog()

    def datacenters(self, *, node_type: str = "") -> Datacenters:
        """``GET /infra/datacenters?node_type=`` (alias ``gpu_type``): sites with stock (empty by default).

        Args:
            node_type: The type to check stock of, or ``""`` for the configured default.

        Returns:
            The sites.
        """
        return Datacenters()


class WorkspaceFacet(ABC):
    """The provider creates ``flavor == "workspace"`` nodes and can list the ones it runs.

    A workspace is created through the core ``create_node`` (the request's ``flavor`` and
    ``workspace`` say so) and answered with ``services`` and ``managed_by`` in
    :class:`~tlc_plugin_sdk.infrastructure.CreateNodeResponse`. The author still lists
    ``"workspace"`` in ``capabilities().flavors`` — the host gates on ``flavors``; the
    conformance kit asserts the two agree.
    """

    @abstractmethod
    def list_workspaces(self, *, owner: str = "") -> WorkspaceListing:
        """``GET /infra/workspaces?owner=``: every workspace instance the account runs.

        Args:
            owner: Scope to one owner (a tag), or ``""`` for all.

        Returns:
            The instances, tracked by the host or not.
        """


class LegacyOwnerCredentialsFacet(ABC):
    """LEGACY — request-carried owner credentials: a visitor's resources in their own account.

    Superseded by Connections (:mod:`tlc_plugin_sdk.connections`); kept so the demo and hosted
    flows — a visitor's workspace in their own account, sign-in, role grant, "check my key" —
    keep working. Deliberately dict-typed: these steps are provider-shaped and may change without
    notice.

    Abstract: :meth:`credential_descriptor`, :meth:`terminate_with_credentials`,
    :meth:`discover_storage`. The ``role_setup`` and ``login_*`` routes are mounted only when
    the provider overrides the method, so a provider never serves a 501 route it did not write.
    Inside ``create_node`` and ``create_storage`` the request's credentials are read from
    :func:`tlc_plugin_sdk.infrastructure.legacy.current_request_credentials`.
    """

    @abstractmethod
    def credential_descriptor(self) -> OwnerCredentialsDescriptor:
        """What a request's ``credentials`` must carry, and how a person supplies them (merged into capabilities)."""

    @abstractmethod
    def terminate_with_credentials(
        self, provider_id: str, *, credentials: dict[str, Any], owner: str = ""
    ) -> NodeStateResponse:
        """``POST /infra/nodes/{id}/terminate`` ``{credentials, owner}``: terminate a node in another account.

        Args:
            provider_id: The node's provider id.
            credentials: The keys (or role) that created it.
            owner: The owner the host acts for.

        Returns:
            The node's state after the call.
        """

    @abstractmethod
    def discover_storage(self, *, credentials: dict[str, Any], owner: str = "") -> StorageListing:
        """``POST /infra/storage/discover`` ``{credentials, owner}``: the storage another account can list.

        Args:
            credentials: The account's keys.
            owner: The owner the host acts for.

        Returns:
            The listing.
        """

    def role_setup(self, *, owner: str = "", bucket_url: str = "") -> dict[str, Any]:
        """``GET /infra/role-setup?owner=&bucket_url=``: what a person needs to grant a cross-account role.

        Args:
            owner: The owner the role is bound to.
            bucket_url: The bucket the role must reach.

        Returns:
            Provider-shaped (``{host_account_id, external_id, role_name, quick_create_url, …}``).

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = _unsupported("role setup")
        raise NotSupported(msg)

    def login_start(self, body: dict[str, Any], *, owner: str = "") -> dict[str, Any]:
        """``POST /infra/login``: start a device sign-in.

        Args:
            body: Provider-shaped (a start URL, a region …).
            owner: The owner signing in.

        Returns:
            Provider-shaped (a code and a link).

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = _unsupported("sign-in")
        raise NotSupported(msg)

    def login_poll(self, login_id: str, *, owner: str = "") -> dict[str, Any]:
        """``GET /infra/login/{id}?owner=``: the sign-in's state.

        Args:
            login_id: The id ``login_start`` answered.
            owner: The owner signing in.

        Returns:
            Provider-shaped (``{state: pending|authorized|expired|error, accounts?}``).

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = _unsupported("sign-in")
        raise NotSupported(msg)

    def login_credentials(self, login_id: str, body: dict[str, Any], *, owner: str = "") -> dict[str, Any]:
        """``POST /infra/login/{id}/credentials``: temporary credentials for a signed-in account.

        Args:
            login_id: The id ``login_start`` answered.
            body: Provider-shaped (``{account_id, role_name}``).
            owner: The owner signing in.

        Returns:
            Provider-shaped temporary credentials.

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = _unsupported("sign-in")
        raise NotSupported(msg)
