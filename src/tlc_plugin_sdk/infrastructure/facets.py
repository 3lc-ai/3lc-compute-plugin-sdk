# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The groups of operations an infrastructure plugin opts into by subclassing.

Each group is a mixin ``ABC``. Add it to the class bases next to
:class:`~tlc_plugin_sdk.infrastructure.InfrastructurePlugin` and the base mounts its routes and
lists its operations in ``capabilities.operations``::

    class MyProvider(InfrastructurePlugin, NodeProvider, StorageFacet, CatalogFacet): ...

The abstract methods are what a provider of that group must write. Every other method is optional:
its route is mounted, and its operation listed, only when the class overrides it, so a host never
calls an operation the provider did not write. All methods are plain ``def``: the routes run them
in a worker thread, and :func:`tlc_plugin_sdk.connections.current_credential` works inside every
one except :class:`DnsFacet`'s, which act as the host itself.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from tlc_plugin_sdk.infrastructure.errors import NotSupported
from tlc_plugin_sdk.infrastructure.types import (
    Catalog,
    CatalogRequest,
    CreateNodeRequest,
    CreateNodeResponse,
    CreateStorageRequest,
    DeleteObjectsRequest,
    DeploymentCheck,
    DeploymentCheckRequest,
    DeploymentListing,
    DeploymentOptions,
    DeploymentPrepared,
    DeploymentPrepareRequest,
    DeploymentRequest,
    DeploymentResponse,
    DnsChange,
    DnsRecords,
    DnsZone,
    Locations,
    NodeCheckRequest,
    NodeStateResponse,
    ObjectListing,
    PreflightResponse,
    PresignRequest,
    PresignResponse,
    StorageCapabilities,
    StorageDeleted,
    StorageItem,
    StorageListing,
    SweepReport,
    SweepRequest,
)

if TYPE_CHECKING:
    from tlc_plugin_sdk.shared.storage_bundle import BundleRegistry
    from tlc_plugin_sdk.shared.storage_transfer import TransferRegistry

__all__ = ["CatalogFacet", "DeploymentProvider", "DnsFacet", "NodeProvider", "StorageFacet"]


def _unsupported(what: str) -> str:
    return f"This provider does not support {what}."


class NodeProvider(ABC):
    """The provider starts nodes: machines that run a node agent the host reaches at one address.

    Abstract: :meth:`create_node`, :meth:`node_state`, :meth:`delete_node` (operations
    ``node.create``, ``node.state``, ``node.delete``). Optional: :meth:`check_node`
    (``node.check``) and :meth:`node_diagnostics` (``node.diagnostics``). The class's capabilities
    declare ``nodes``.
    """

    @abstractmethod
    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse:
        """``POST /infra/nodes`` (201): create a node and start the agent on it.

        Idempotent on ``request.node_id``: a repeat, after a timeout or a host restart, returns
        the resource the first call created and creates nothing. The provider finds it by the
        ``3lc-id`` tag it put on every resource of the node. The provider attaches the node
        identity the request's Connection grants, read from the binding's ``access``, and says
        which in ``identity``; it never copies a credential onto the node.

        Args:
            request: The node asked for.

        Returns:
            The provider's id for the node and the agent's address.

        Raises:
            ProviderError: With the provider's own sentence; any other exception answers 502.
        """

    @abstractmethod
    def node_state(self, provider_id: str) -> NodeStateResponse:
        """``GET /infra/nodes/{provider_id}``: the state of a node.

        Args:
            provider_id: The id ``create_node`` answered.

        Returns:
            ``running`` if the agent is alive, ``pending`` while it starts, ``exited``,
            ``terminated`` or ``gone`` if it is not, ``unknown`` if the provider cannot tell.
        """

    @abstractmethod
    def delete_node(self, provider_id: str) -> NodeStateResponse:
        """``DELETE /infra/nodes/{provider_id}``: terminate a node and remove what was created for it.

        Idempotent: a repeat on a node that is gone answers ``terminated`` or ``gone``.

        Args:
            provider_id: The id ``create_node`` answered.

        Returns:
            The node's state after the call.
        """

    def check_node(self, request: NodeCheckRequest) -> PreflightResponse:
        """``POST /infra/nodes/check``: whether a node of this shape could start now (funds, stock, quota).

        Args:
            request: The node type, pricing, location and storage roots to check.

        Returns:
            The checks and whether they pass.

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = _unsupported("node checks")
        raise NotSupported(msg)

    def node_diagnostics(self, provider_id: str) -> NodeStateResponse:
        """``GET /infra/nodes/{provider_id}?diagnostics=true``: the state with startup diagnostics.

        Fill the ``bootstrap_*`` fields from the provider's console or boot log. A provider that
        does not override this answers the plain :meth:`node_state` for that request.

        Args:
            provider_id: The id ``create_node`` answered.

        Returns:
            The node's state with what its startup said.
        """
        return self.node_state(provider_id)


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
            fallback_url: The deployment's project root, for a credential that may not list
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

        The route calls ``start(url=<the request's url>, name=<the request's name or "">)``. A
        registry that normalises the URL (a subclass overriding ``start``) passes ``name`` on
        as it came: an empty one is resolved from the normalised URL by the base registry.

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
    """Live offerings for the pickers: node types, deployment sizes, locations with stock.

    Abstract: :meth:`catalog` (operation ``catalog``). Optional: :meth:`locations`
    (``catalog.locations``).
    """

    @abstractmethod
    def catalog(self, request: CatalogRequest) -> Catalog:
        """``POST /infra/catalog``: what can be started, with prices, for one purpose and location.

        Args:
            request: The purpose (GPU node, CPU node, deployment size) and where to price.

        Returns:
            At most 500 offers; a failed lookup is an empty list with ``error`` set, never an exception.
        """

    def locations(self, *, node_type: str = "") -> Locations:
        """``GET /infra/catalog/locations?node_type=``: where a node can go, with stock where known.

        Args:
            node_type: The type to check stock of, or ``""``.

        Returns:
            The places a ``location`` may name.

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = _unsupported("listing locations")
        raise NotSupported(msg)


class DeploymentProvider(ABC):
    """The provider hosts deployments: one machine running an Object Service and a Compute Service.

    A deployment is created only by a launcher. Abstract: :meth:`create_deployment`,
    :meth:`deployment_state`, :meth:`delete_deployment` (operations ``deployment.create``,
    ``deployment.state``, ``deployment.delete``). Optional: :meth:`deployment_options`,
    :meth:`check_deployment`, :meth:`prepare_deployment`, :meth:`retain_deployment`,
    :meth:`list_deployments`, :meth:`sweep_deployments`. The class's capabilities declare
    ``deployments``. The provider derives no hostname and writes no DNS record: the launcher
    allocates the names and writes its zone through a :class:`DnsFacet`.
    """

    def deployment_options(self, selected: dict[str, str]) -> DeploymentOptions:
        """``POST /infra/deployments/options``: the placement fields, with the choices valid so far.

        Args:
            selected: The placement chosen so far, by field key.

        Returns:
            The fields in the order they are chosen, costs, warnings and blockers.

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = _unsupported("deployment placement discovery")
        raise NotSupported(msg)

    def check_deployment(self, request: DeploymentCheckRequest) -> DeploymentCheck:
        """``POST /infra/deployments/check``: provider-side refusals before a launch, and the ingress key.

        Answers what only the provider can know: a label too long for its ingress, a placement
        that cannot host a deployment, quota. Checks the storage asked for against the
        Connection's ``permissions.storage``.

        Args:
            request: The label, size, placement, storage and the launcher's zone.

        Returns:
            Whether the launch can go ahead, and the ``ingress_key`` when deployments share an ingress.

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = _unsupported("deployment checks")
        raise NotSupported(msg)

    def prepare_deployment(self, request: DeploymentPrepareRequest) -> DeploymentPrepared:
        """``POST /infra/deployments/prepare``: create what deployments share, one step per call.

        Called repeatedly until ``ready`` or ``failed``. Idempotent. Answers the DNS records that
        must exist for it to finish, every one under ``request.namespace``; the launcher writes
        them and calls again after ``retry_after_s``.

        Args:
            request: The deployment, its placement, namespace and ingress key.

        Returns:
            Where the shared resources stand.

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = _unsupported("preparing shared deployment resources")
        raise NotSupported(msg)

    @abstractmethod
    def create_deployment(self, request: DeploymentRequest) -> DeploymentResponse:
        """``POST /infra/deployments`` (201): create the machine and return when it exists.

        Idempotent on ``request.deployment_id``: a repeat returns the existing machine. With
        ``request.hostnames`` unset the provider names the deployment by its own address and
        reports the names in ``endpoints``. ``failure`` set means resources exist and the launch
        did not complete.

        Args:
            request: The deployment asked for.

        Returns:
            The provider's id, the addresses the launcher points names at, and the endpoints.
        """

    @abstractmethod
    def deployment_state(self, provider_id: str, *, diagnostics: str = "") -> NodeStateResponse:
        """``GET /infra/deployments/{provider_id}?diagnostics=``: the state of a deployment.

        Args:
            provider_id: The id ``create_deployment`` answered.
            diagnostics: ``""`` for the state alone, ``"baseline"`` to follow a boot, or
                ``"detailed"`` for the separately granted collector.

        Returns:
            The state, with the ``bootstrap_*`` fields and ``diagnostics`` as asked.
        """

    @abstractmethod
    def delete_deployment(self, provider_id: str) -> NodeStateResponse:
        """``DELETE /infra/deployments/{provider_id}``: remove the deployment and what was created for it.

        Idempotent; a refused step raises and the launcher retries. Also deletes a deployment no
        registry tracks, found through :meth:`list_deployments`.

        Args:
            provider_id: The id ``create_deployment`` or ``list_deployments`` answered.

        Returns:
            The deployment's state after the call.
        """

    def retain_deployment(self, provider_id: str) -> NodeStateResponse:
        """``POST /infra/deployments/{provider_id}/retain``: stop routing traffic to a failed machine, keep the machine.

        Args:
            provider_id: The id ``create_deployment`` answered.

        Returns:
            The deployment's state after the call.

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = _unsupported("retaining failed deployments")
        raise NotSupported(msg)

    def list_deployments(self, *, owner: str = "") -> DeploymentListing:
        """``GET /infra/deployments?owner=``: every deployment the account runs, tracked by the launcher or not.

        Args:
            owner: Scope to one owner (a tag), or ``""`` for all.

        Returns:
            At most 500 deployments.

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = _unsupported("listing deployments")
        raise NotSupported(msg)

    def sweep_deployments(self, request: SweepRequest) -> SweepReport:
        """``POST /infra/deployments/sweep``: release what a launch or teardown that never finished left behind.

        Releases what no ``keep`` item owns once it is older than ``grace_s``. Never touches DNS.

        Args:
            request: What is alive, what is gone, and where to look.

        Returns:
            What was released, what live deployments hold, and what could not be done.

        Raises:
            NotSupported: By default (the route is then not mounted).
        """
        msg = _unsupported("sweeping deployments")
        raise NotSupported(msg)


class DnsFacet(ABC):
    """The plugin writes a launcher's DNS zone (operation ``dns``).

    Both methods act with the launcher's own identity and never a Connection: the routes refuse a
    request that carries one. They act only on the zone the host names at call time, in the
    host-owned ``x-3lc-launcher-zone`` header: the routes refuse a request without it or for
    another zone before a method here runs. The DNS plugin and a deployment's provider are independent.
    """

    @abstractmethod
    def list_records(self, zone: DnsZone, *, under: str = "", cursor: str = "") -> DnsRecords:
        """``GET /infra/dns/records?zone_id=&zone_name=&under=&cursor=``: one page of a zone's records.

        Args:
            zone: The zone.
            under: Only names equal to or under this one; ``""`` lists the zone.
            cursor: ``next_cursor`` of the previous page; ``""`` starts.

        Returns:
            At most 300 records, and the cursor of the next page.
        """

    @abstractmethod
    def change_records(self, change: DnsChange) -> None:
        """``POST /infra/dns/changes``: one atomic change of at most 100 records.

        A create of a name and type that exists with the same values succeeds. With other values
        it raises and nothing in the change is applied, so two launches racing for a name cannot
        both win. A delete removes a record only when it matches exactly as listed; a missing one
        is success.

        Args:
            change: The records to create and to delete.

        Raises:
            Conflict: When a record to create exists with other values.
        """
