# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The golden fixtures of the provider contract: one JSON file per wire type, emitted by ``FakeProvider``.

Every answer is what :class:`~tlc_plugin_sdk.infrastructure.testing.FakeProvider` answers through
its routes; every request is the body the SDK's own type writes. Each file is
``{"type", "route", "example"}``. The four ``capabilities.<provider>.json`` files are what an
AWS-like, an Azure-like, a RunPod-like and a machines-like provider declare: the shapes a Hub form
is built from.

``python tests/contract_fixtures.py --write`` rewrites ``tests/fixtures/contract/`` after a
deliberate change to the wire; ``tests/test_infra_fixtures.py`` fails when a type and its fixture
have drifted apart.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

from tlc_plugin_sdk.connections import (
    LAUNCHER_ZONE_HEADER,
    ConnectionBinding,
    ConnectionPermissions,
    ConnectionUse,
    IdentityToken,
    SecretToken,
    StorageRule,
    encode_credential,
)
from tlc_plugin_sdk.harness import PluginHarness
from tlc_plugin_sdk.infrastructure import (
    AccessMethod,
    AccountSchema,
    BundleRequest,
    CapabilitiesResponse,
    Catalog,
    CatalogFacet,
    CatalogRequest,
    CreateNodeRequest,
    CreateNodeResponse,
    CreateStorageRequest,
    DeleteObjectsRequest,
    DeploymentBootstrap,
    DeploymentCapabilities,
    DeploymentCheckRequest,
    DeploymentHostnames,
    DeploymentOptionsRequest,
    DeploymentPrepareRequest,
    DeploymentProvider,
    DeploymentRequest,
    DeploymentResponse,
    DeploymentStorage,
    DnsChange,
    DnsFacet,
    DnsRecord,
    DnsRecords,
    DnsZone,
    FormField,
    GrantObservation,
    GrantRequest,
    GrantUpdateRequest,
    HostIdentity,
    IdentityTokenSpec,
    InfrastructurePlugin,
    Locations,
    NodeAccessRequest,
    NodeCapabilities,
    NodeCheckRequest,
    NodeProvider,
    NodeStateResponse,
    PermissionOption,
    PermissionSchema,
    PreflightResponse,
    PresignRequest,
    StorageCapabilities,
    StorageFacet,
    StorageListing,
    StorageLocationSchema,
    StorageRoot,
    SweepItem,
    SweepRequest,
    TokenIdentity,
    TransferRequest,
    VerifyRequest,
    WorkspaceFacet,
    WorkspaceListing,
    WorkspacePlacement,
    WorkspaceRequest,
)
from tlc_plugin_sdk.infrastructure.testing import FakeProvider, connection_headers

FIXTURES = Path(__file__).parent / "fixtures" / "contract"

_ZONE = DnsZone(id="Z0FAKE", name="deploy.example.com")
_PERMISSIONS = ConnectionPermissions(
    deployments=True,
    nodes=True,
    storage=[StorageRule(location="fake://fake-data/projects", access="read_write")],
    options={"deployment_nodes": True},
)
_BINDING = ConnectionBinding(
    id="conn-1",
    provider="fake",
    kind="KEYLESS",
    account={"account_id": "000000000000"},
    access={"role_name": "3lc-fake", "node_identity": "3lc-fake-node", "setup_identity": "fake-host-principal"},
    defaults={"location": "fake-1"},
    permissions=_PERMISSIONS,
    grant_ref="fake:grant/conn-1",
)
_USE = ConnectionUse(resource_id="node-1", source_identity="ada@example.com")
_ROOTS = [StorageRoot(url="fake://fake-data/projects")]


class _Recorder:
    """Drives one ``FakeProvider`` and keeps one example per wire type."""

    def __init__(self, harness: PluginHarness) -> None:
        self.h = harness
        self.examples: dict[str, dict[str, Any]] = {}
        self.headers = connection_headers(_BINDING, use=_USE, token=_TOKEN)

    def keep(self, name: str, route: str, example: Any) -> None:
        self.examples[name] = {"type": name, "route": route, "example": example}

    def call(
        self, method: str, path: str, answer: str, *, body: Any = None, request: str = "", bare: bool = False
    ) -> dict[str, Any]:
        launcher = {LAUNCHER_ZONE_HEADER: _ZONE.id} if path.startswith("/infra/dns/") else None
        response = self.h.call(method, path, json_body=body, headers=launcher if bare else self.headers)
        data: dict[str, Any] = response.json()
        route = f"{method} {path.split('?', 1)[0]}"
        if request:
            self.keep(request, f"{route} (request)", body)
        if answer:
            self.keep(answer, f"{route} ({response.status_code})", data)
        return data


_TOKEN = IdentityToken(
    connection_id="conn-1",
    audience="api://fake-token-exchange",
    token="eyJhbGciOiJSUzI1NiJ9.fake.signature",
    expires_at="2026-01-01T00:10:00Z",
)


def _fake_examples(root: Path) -> dict[str, dict[str, Any]]:
    fake = FakeProvider()
    fake.require_identity_token(_TOKEN.audience)
    with PluginHarness(fake, plugin_id="fake", config_root=root) as h:
        r = _Recorder(h)
        r.keep("ConnectionBinding", f"header {connection_headers(_BINDING).popitem()[0]}", _BINDING.to_dict())
        r.keep("ConnectionUse", "header x-3lc-connection-use", _USE.to_dict())
        r.keep("ConnectionPermissions", "ConnectionBinding.permissions", _PERMISSIONS.to_dict())
        r.keep("StorageRule", "ConnectionPermissions.storage[]", _PERMISSIONS.storage[0].to_dict())
        token = SecretToken(provider="fake", secret=json.dumps({"api_key": "fake-key"}), connection_id="conn-2")
        r.keep("SecretToken", "header x-tlc-bound-credential", json.loads(encode_credential(token)))

        r.call("GET", "/infra/capabilities", "CapabilitiesResponse", bare=True)
        grant = GrantRequest(
            connection_id="conn-1",
            kind="KEYLESS",
            account=dict(_BINDING.account),
            access={"role_name": "3lc-fake"},
            permissions=_PERMISSIONS,
            default_storage_root="fake://fake-data/projects",
            identity=TokenIdentity(
                issuer="https://issuer.3lc.invalid", subject="conn-1", audience="api://fake-token-exchange"
            ),
        )
        r.call("POST", "/infra/connection/grant", "Grant", body=grant.to_dict(), request="GrantRequest", bare=True)
        verified = r.call(
            "POST",
            "/infra/connection/verify",
            "ConnectionVerification",
            body=VerifyRequest(storage_roots=_ROOTS).to_dict(),
            request="VerifyRequest",
        )
        r.keep("GrantObservation", "ConnectionVerification.observation", verified["observation"])
        update = GrantUpdateRequest(
            installed=GrantObservation.from_dict(verified["observation"]),
            pending=ConnectionPermissions(nodes=True),
            revision=1,
            pending_revision=2,
        )
        r.call(
            "POST", "/infra/connection/grant/update", "GrantUpdate", body=update.to_dict(), request="GrantUpdateRequest"
        )
        access = NodeAccessRequest(storage_roots=_ROOTS, storage_access={"rules": []}, expected_revision="0")
        r.call(
            "POST",
            "/infra/connection/node-access",
            "ConnectionNodeAccess",
            body=access.to_dict(),
            request="NodeAccessRequest",
        )
        r.call("GET", "/infra/storage/candidates", "StorageCandidates")

        check = NodeCheckRequest(node_type="fake-gpu", pricing="spot", location="fake-1", storage_roots=_ROOTS)
        r.call("POST", "/infra/nodes/check", "PreflightResponse", body=check.to_dict(), request="NodeCheckRequest")
        node = CreateNodeRequest(
            node_id="node-1",
            node_type="fake-gpu",
            token="node-token",
            location="fake-1",
            env={"TLC_EXAMPLE": "1"},
            ports=[8888],
            owner="ada@example.com",
            host_id="svc-host-1",
            pricing="spot",
            compute_spec="3lc-compute==1.3.0",
            storage_roots=_ROOTS,
        )
        r.call("POST", "/infra/nodes", "CreateNodeResponse", body=node.to_dict(), request="CreateNodeRequest")
        r.call("GET", "/infra/nodes/fake-node-1?diagnostics=true", "NodeStateResponse")
        r.call("DELETE", "/infra/nodes/fake-node-1", "")
        r.call("POST", "/infra/nodes", "ErrorAnswer", body={**node.to_dict(), "node_type": "no-such-type"})

        r.call(
            "POST",
            "/infra/catalog",
            "Catalog",
            body=CatalogRequest(purpose="gpu_node", location="fake-1").to_dict(),
            request="CatalogRequest",
        )
        r.call("GET", "/infra/catalog/locations?node_type=fake-gpu", "Locations")

        selected = DeploymentOptionsRequest(selected={"location": "fake-1"})
        r.call(
            "POST",
            "/infra/deployments/options",
            "DeploymentOptions",
            body=selected.to_dict(),
            request="DeploymentOptionsRequest",
        )
        placement = {"location": "fake-1", "network": "net-fake-1-a"}
        storage = DeploymentStorage(
            root="fake://fake-data/projects", scan=["fake://fake-data/train"], create_root=False
        )
        checked = r.call(
            "POST",
            "/infra/deployments/check",
            "DeploymentCheck",
            body=DeploymentCheckRequest(
                label="ada", size="fake-small", placement=placement, storage=storage, zone=_ZONE
            ).to_dict(),
            request="DeploymentCheckRequest",
        )
        key = checked["ingress_key"]
        namespace = f"i-{key}.{_ZONE.name}"
        prepare = DeploymentPrepareRequest(
            deployment_id="dep-1", placement=placement, namespace=namespace, size="fake-small", ingress_key=key
        )
        prepared = r.call(
            "POST",
            "/infra/deployments/prepare",
            "DeploymentPrepared",
            body=prepare.to_dict(),
            request="DeploymentPrepareRequest",
        )
        change = DnsChange(zone=_ZONE, create=[DnsRecord.from_dict(x) for x in prepared["dns_records"]])
        r.call("POST", "/infra/dns/changes", "", body=change.to_dict(), request="DnsChange", bare=True)
        r.call(
            "GET",
            f"/infra/dns/records?zone_id={_ZONE.id}&zone_name={_ZONE.name}&under={namespace}",
            "DnsRecords",
            bare=True,
        )
        deployment = DeploymentRequest(
            deployment_id="dep-1",
            name="Ada's deployment",
            bootstrap=DeploymentBootstrap(
                record_id="rec-1",
                config_service_url="https://config.example.com",
                token="bootstrap-token",
                launcher_service_id="svc-launcher-1",
            ),
            hostnames=DeploymentHostnames(
                hostname="",
                object_service=f"os-ada.{namespace}",
                compute_service=f"compute-ada.{namespace}",
                namespace=namespace,
            ),
            ingress_key=key,
            size="fake-small",
            placement=placement,
            storage=storage,
            compute_spec="3lc-compute==1.3.0",
            env={"TLC_EXAMPLE": "1"},
            owner="ada@example.com",
            public=True,
            seed_provider_configs={"fake": {"default_location": "fake-east-1"}},
        )
        r.call(
            "POST", "/infra/deployments", "DeploymentResponse", body=deployment.to_dict(), request="DeploymentRequest"
        )
        r.call("GET", "/infra/deployments", "DeploymentListing")
        sweep = SweepRequest(
            keep=[SweepItem(deployment_id="dep-1", provider_id="fake-dep-dep-1", name="Apollo")],
            gone=[SweepItem(deployment_id="dep-0", name="Gemini")],
            locations=["fake-1"],
            zone=_ZONE,
        )
        fake.leftovers.append("fake:address/203.0.113.250")
        r.call("POST", "/infra/deployments/sweep", "SweepReport", body=sweep.to_dict(), request="SweepRequest")

        r.call("GET", "/infra/storage", "StorageListing")
        created = CreateStorageRequest(name="fake-new", region="fake-1", owner="ada@example.com")
        r.call("POST", "/infra/storage", "StorageItem", body=created.to_dict(), request="CreateStorageRequest")
        r.call("DELETE", "/infra/storage/fake-new", "StorageDeleted")
        presign = PresignRequest(url="fake://fake-data/train", files=[{"path": "c.jpg", "size": 3}])
        r.call("POST", "/infra/storage/presign", "PresignResponse", body=presign.to_dict(), request="PresignRequest")
        r.call("GET", "/infra/storage/list?url=fake://fake-data", "ObjectListing")
        doomed = DeleteObjectsRequest(url="fake://fake-data", paths=["readme.txt"], dry_run=True)
        r.call("POST", "/infra/storage/delete", "", body=doomed.to_dict(), request="DeleteObjectsRequest")
        transfer = TransferRequest(src_url="fake://fake-data/train", dst_url="fake://fake-data/copy", items=["a.jpg"])
        r.keep("TransferRequest", "POST /infra/storage/transfer (request)", transfer.to_dict())
        r.keep(
            "BundleRequest",
            "POST /infra/storage/bundle (request)",
            BundleRequest(url="fake://fake-data/train").to_dict(),
        )

        # ── transitional ──
        workspace = CreateNodeRequest(
            node_id="ws-1",
            node_type="fake-cpu",
            token="node-token",
            owner="ada@example.com",
            flavor="workspace",
            workspace=WorkspaceRequest(name="ada", workspace_id="rec-1", region="fake-1"),
        )
        r.keep("WorkspaceRequest", "CreateNodeRequest.workspace", workspace.workspace.to_dict())
        r.keep("CreateNodeRequest.workspace", "POST /infra/nodes (request, flavor workspace)", workspace.to_dict())
        r.call("POST", "/infra/nodes", "CreateNodeResponse.workspace", body=workspace.to_dict())
        r.call("GET", "/infra/workspaces", "WorkspaceListing")
        r.keep(
            "WorkspacePlacement",
            "GET /infra/workspace-placement (200)",
            WorkspacePlacement(
                regions=[{"id": "fake-1", "name": "Fake region 1"}], defaults={"region": "fake-1"}
            ).to_dict(),
        )
        # ── end transitional ──
    caps = CapabilitiesResponse.from_dict(r.examples["CapabilitiesResponse"]["example"])
    nested: dict[str, tuple[str, Any]] = {
        "AccountSchema": ("CapabilitiesResponse.account", caps.account),
        "AccessMethod": ("CapabilitiesResponse.access_methods[]", caps.access_methods[0]),
        "IdentityTokenSpec": ("AccessMethod.identity_token", caps.access_methods[0].identity_token),
        "TokenIdentity": ("GrantRequest.identity", grant.identity),
        "FormField": ("AccountSchema.fields[]", caps.account.fields[0] if caps.account else None),
        "Choice": ("FormField.choices[]", caps.account.defaults[0].choices[0] if caps.account else None),
        "HostIdentity": ("CapabilitiesResponse.host", caps.host),
        "PermissionSchema": ("CapabilitiesResponse.permissions", caps.permissions),
        "PermissionOption": ("PermissionSchema.options[]", caps.permissions.options[0]),
        "StorageLocationSchema": ("PermissionSchema.storage", caps.permissions.storage),
        "NodeCapabilities": ("CapabilitiesResponse.nodes", caps.nodes),
        "DeploymentCapabilities": ("CapabilitiesResponse.deployments", caps.deployments),
        "StorageCapabilities": ("CapabilitiesResponse.storage", caps.storage),
    }
    for name, (route, value) in nested.items():
        assert value is not None, name
        r.keep(name, route, value.to_dict())
    for parent, key, name in (
        ("Grant", "files", "GrantFile"),
        ("ConnectionVerification", "checks", "PreflightCheck"),
        ("StorageCandidates", "items", "StorageCandidate"),
        ("Catalog", "offers", "Offer"),
        ("Locations", "locations", "Location"),
        ("DeploymentOptions", "placement", "PlacementField"),
        ("DeploymentOptions", "costs", "CostNote"),
        ("DeploymentResponse", "addresses", "Address"),
        ("DeploymentListing", "deployments", "DeploymentInstance"),
        ("DnsRecords", "records", "DnsRecord"),
        ("StorageListing", "storage", "StorageItem"),
        ("StorageListing", "regions", "Region"),
        ("WorkspaceListing", "workspaces", "WorkspaceInstance"),
    ):
        r.keep(name, f"{parent}.{key}[]", r.examples[parent]["example"][key][0])
    listed = r.examples["StorageItem"]["example"]
    r.keep("StorageAccess", "StorageItem.access", listed["access"])
    r.keep("StorageGrant", "StorageAccess.grants[]", listed["access"]["grants"][0])
    options = r.examples["DeploymentOptions"]["example"]
    r.keep("PlacementChoice", "PlacementField.choices[]", options["placement"][0]["choices"][0])
    for parent, key, name in (
        ("DeploymentCheckRequest", "storage", "DeploymentStorage"),
        ("DeploymentCheckRequest", "zone", "DnsZone"),
        ("DeploymentRequest", "bootstrap", "DeploymentBootstrap"),
        ("DeploymentRequest", "hostnames", "DeploymentHostnames"),
        ("VerifyRequest", "storage_roots", "StorageRoot"),
        ("SweepRequest", "keep", "SweepItem"),
    ):
        value = r.examples[parent]["example"][key]
        r.keep(name, f"{parent}.{key}", value[0] if isinstance(value, list) else value)
    return r.examples


# ── What four kinds of provider declare ────────────────────────────────────────


class _Declared(InfrastructurePlugin):
    """A provider that only declares: its class says which operations exist, ``caps`` what it answers."""

    caps: CapabilitiesResponse

    def get_ui_fragment(self) -> str:
        return ""

    def capabilities(self) -> CapabilitiesResponse:
        return self.caps

    # The methods below exist so that their operations are listed; none is ever called.

    def create_node(self, request: Any) -> CreateNodeResponse:
        raise NotImplementedError

    def node_state(self, provider_id: str) -> NodeStateResponse:
        raise NotImplementedError

    def delete_node(self, provider_id: str) -> NodeStateResponse:
        raise NotImplementedError

    def check_node(self, request: Any) -> PreflightResponse:
        raise NotImplementedError

    def catalog(self, request: Any) -> Catalog:
        raise NotImplementedError

    def locations(self, *, node_type: str = "") -> Locations:
        raise NotImplementedError

    def list_storage(self, *, fallback_url: str = "") -> StorageListing:
        raise NotImplementedError

    def list_workspaces(self, *, owner: str = "") -> WorkspaceListing:
        raise NotImplementedError

    def create_deployment(self, request: Any) -> DeploymentResponse:
        raise NotImplementedError

    def deployment_state(self, provider_id: str, *, diagnostics: str = "") -> NodeStateResponse:
        raise NotImplementedError

    def delete_deployment(self, provider_id: str) -> NodeStateResponse:
        raise NotImplementedError

    def list_records(self, zone: Any, *, under: str = "", cursor: str = "") -> DnsRecords:
        raise NotImplementedError

    def change_records(self, change: Any) -> None:
        raise NotImplementedError


def _connect(cls: type[Any], *names: str) -> None:
    """Mark the optional connect methods ``names`` as written by ``cls``."""
    for name in names:

        def written(self: Any, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        setattr(cls, name, written)


_AWS_STORAGE = StorageCapabilities(kind="bucket", label="S3 bucket")
_AWS_ACCOUNT = AccountSchema(
    noun="AWS account",
    fields=[
        FormField(
            key="account_id",
            label="AWS account ID",
            help="The 12-digit ID of the account 3LC is let into.",
            example="123456789012",
            pattern=r"^\d{12}$",
            max_length=12,
        )
    ],
    defaults=[FormField(key="location", label="Default region", required=False, example="eu-west-1", max_length=32)],
)
_AWS_METHODS = [
    AccessMethod(
        kind="KEYLESS",
        label="Uses access you grant to 3LC",
        summary="You create a role in your account that trusts this Compute Service. No key is stored.",
        preferred=True,
        grants=True,
        fields=[
            FormField(
                key="role_name",
                label="Role name",
                help="The name of the role the grant creates.",
                example="3lc-connection",
                pattern=r"^[\w+=,.@-]{1,53}$",
                max_length=53,
                default="3lc-connection",
            )
        ],
    ),
    AccessMethod(
        kind="AMBIENT",
        label="Uses this machine's own sign-in",
        summary="The Compute Service acts as the identity its machine already has.",
        purposes=["nodes", "storage"],
    ),
]
_AWS_PERMISSIONS = PermissionSchema(
    options=[
        PermissionOption(
            key="allow_shared_ingress",
            label="Create shared HTTPS access for deployments",
            purpose="deployments",
            help="Allows a shared load balancer and certificate.",
            default=True,
        ),
        PermissionOption(
            key="allow_diagnostics",
            label="Allow detailed startup diagnostics",
            purpose="deployments",
            help="Allows a fixed command to read startup logs on opted-in deployments.",
        ),
        PermissionOption(
            key="deployments_start_nodes",
            label="Deployments start their own GPU nodes",
            purpose="deployments",
            default=True,
            operations=["node.create", "node.describe", "node.terminate"],
        ),
        PermissionOption(
            key="node_storage", label="Give GPU nodes the Connection's storage access", purpose="nodes", default=True
        ),
        PermissionOption(
            key="discover_buckets",
            label="List the names of the account's buckets",
            purpose="storage",
            help="Names only, never contents.",
            operations=["storage.discover"],
        ),
        PermissionOption(
            key="allow_bucket_creation",
            label="Create buckets in the account",
            purpose="storage",
            help="Creating a bucket grants no access to its objects.",
            operations=["storage.create"],
        ),
    ],
    storage=StorageLocationSchema(
        scheme="s3",
        example="s3://my-bucket/projects",
        pattern=r"^s3://[a-z0-9.*-]{1,63}(/.*)?$",
        access_modes=["read", "read_write_no_delete", "read_write"],
        help="A bucket, or a folder in one. * matches any run of characters.",
    ),
)
_AWS_NODES = NodeCapabilities(
    kinds=["gpu", "cpu"],
    types=["g5.xlarge", "g6.2xlarge"],
    type_label="Instance type",
    pricing=["on_demand", "spot"],
    storage_access=["identity"],
)
_AWS_HOST = HostIdentity(
    display="arn:aws:iam::111122223333:role/3lc-launcher",
    matches={"setup_identity": "arn:aws:iam::111122223333:role/3lc-launcher"},
)


def _aws(deployments: DeploymentCapabilities | None) -> CapabilitiesResponse:
    return CapabilitiesResponse(
        provider="aws",
        display_name="AWS",
        ready=True,
        requires_connection=True,
        account=_AWS_ACCOUNT,
        access_methods=_AWS_METHODS,
        host=_AWS_HOST,
        permissions=_AWS_PERMISSIONS,
        nodes=_AWS_NODES,
        deployments=deployments,
    )


class _StorageDeclared(_Declared, StorageFacet):
    flags: StorageCapabilities

    def storage_capabilities(self) -> StorageCapabilities:
        return self.flags


class _AwsNow(_StorageDeclared, NodeProvider, CatalogFacet, WorkspaceFacet):
    """AWS while a deployment is still a workspace node: the transitional operations are listed."""

    private_operations = ("workspace.name_check", "workspace.sweep")
    flags = _AWS_STORAGE
    caps = _aws(None)


class _AwsDeployments(_StorageDeclared, NodeProvider, DeploymentProvider, CatalogFacet, DnsFacet):
    """AWS with the typed deployment group and the launcher's DNS zone."""

    flags = _AWS_STORAGE
    caps = _aws(DeploymentCapabilities(certificate="provider", shared_ingress=True, sslip=True))


class _Azure(_StorageDeclared, NodeProvider, DeploymentProvider, CatalogFacet):
    flags = StorageCapabilities(kind="container", label="Azure storage container", bundle=False)
    caps = CapabilitiesResponse(
        provider="azure",
        display_name="Azure",
        ready=True,
        requires_connection=True,
        account=AccountSchema(
            noun="Azure subscription and resource group",
            fields=[
                FormField(
                    key="tenant_id", label="Tenant ID", example="00000000-0000-0000-0000-000000000000", max_length=36
                ),
                FormField(
                    key="subscription_id",
                    label="Subscription ID",
                    example="11111111-1111-1111-1111-111111111111",
                    max_length=36,
                ),
                FormField(key="resource_group", label="Resource group", example="3lc", max_length=90),
            ],
            defaults=[FormField(key="location", label="Default location", required=False, example="westeurope")],
            help="3LC creates everything in this one resource group.",
        ),
        access_methods=[
            AccessMethod(
                kind="AMBIENT",
                label="Uses this machine's own sign-in",
                summary="An az login session on this machine, or the managed identity of the VM it runs on.",
                purposes=["deployments", "nodes", "storage"],
            ),
            AccessMethod(
                kind="KEYLESS",
                label="Uses access you grant to 3LC",
                summary=(
                    "You deploy a template that creates managed identities trusting tokens 3LC signs for this "
                    "Connection. No key is stored."
                ),
                purposes=["deployments", "nodes", "storage"],
                grants=True,
                preferred=True,
                fields=[
                    FormField(
                        key="name_prefix",
                        label="Name prefix",
                        help="What the template's identities, network and storage are named after.",
                        example="tlc",
                        default="tlc",
                        required=False,
                        pattern=r"^[a-z][a-z0-9-]{1,19}$",
                        max_length=20,
                    )
                ],
                identity_token=IdentityTokenSpec(audience="api://AzureADTokenExchange"),
                paste_back=[
                    FormField(key="location", label="Location", example="eastus", max_length=64),
                    FormField(key="control_client_id", label="Control identity client ID", max_length=36),
                    FormField(key="deployment_identity_id", label="Deployment identity", max_length=512),
                    FormField(key="node_identity_id", label="Node identity", max_length=512),
                    FormField(key="node_identity_client_id", label="Node identity client ID", max_length=36),
                    FormField(key="storage_account", label="Storage account", example="tlcabc123", max_length=24),
                    FormField(key="container", label="Container", example="tlc", max_length=63),
                    FormField(key="subnet_id", label="Subnet", max_length=512),
                    FormField(key="issuer", label="Issuer", example="https://issuer.3lc.ai", max_length=600),
                    FormField(key="subject", label="Subject", example="connection:<id>", max_length=600),
                ],
            ),
        ],
        host=HostIdentity(display="ada@example.com (az login)"),
        permissions=PermissionSchema(
            storage=StorageLocationSchema(
                scheme="abfss",
                example="abfss://projects@myaccount.dfs.core.windows.net/3lc",
                pattern=r"^abfss://[a-z0-9*-]{1,63}@[a-z0-9*]{1,24}\.dfs\.core\.windows\.net(/.*)?$",
                access_modes=["read", "read_write"],
                help="A container, or a folder in one.",
            )
        ),
        nodes=NodeCapabilities(
            kinds=["gpu", "cpu"],
            types=["Standard_NC4as_T4_v3", "Standard_NC24ads_A10_v5"],
            type_label="VM size",
            pricing=["on_demand", "spot"],
            storage_access=["identity"],
        ),
        deployments=DeploymentCapabilities(certificate="machine", shared_ingress=False, sslip=True),
    )


_connect(
    _AwsNow,
    "node_diagnostics",
    "describe_grant",
    "prepare_grant_update",
    "verify_connection",
    "connection_node_access",
    "storage_candidates",
)
_connect(
    _AwsDeployments,
    "node_diagnostics",
    "describe_grant",
    "prepare_grant_update",
    "verify_connection",
    "connection_node_access",
    "storage_candidates",
    "deployment_options",
    "check_deployment",
    "prepare_deployment",
    "retain_deployment",
    "list_deployments",
    "sweep_deployments",
)
_connect(
    _Azure,
    "node_diagnostics",
    "describe_grant",
    "verify_connection",
    "connection_node_access",
    "storage_candidates",
    "deployment_options",
    "check_deployment",
    "list_deployments",
    "sweep_deployments",
)


class _RunPod(_StorageDeclared, NodeProvider, CatalogFacet):
    flags = StorageCapabilities(kind="volume", label="RunPod network volume", bundle=False, rename=False)
    caps = CapabilitiesResponse(
        provider="runpod",
        display_name="RunPod",
        ready=True,
        requires_connection=True,
        account=AccountSchema(noun="RunPod account", help="The API key is the account."),
        access_methods=[
            AccessMethod(
                kind="SECRET",
                label="Uses an API key you store",
                summary="3LC seals the key and releases it for each use.",
                purposes=["nodes", "storage"],
                preferred=True,
                fields=[
                    FormField(
                        key="api_key",
                        label="API key",
                        kind="secret",
                        secret=True,
                        help="RunPod console, Settings, API keys.",
                        example="rpa_…",
                    ),
                    FormField(
                        key="volume_access_key",
                        label="Network volume access key",
                        kind="secret",
                        secret=True,
                        required=False,
                        help="Only for browsing and filling network volumes.",
                    ),
                    FormField(
                        key="volume_secret_key",
                        label="Network volume secret key",
                        kind="secret",
                        secret=True,
                        required=False,
                    ),
                ],
            )
        ],
        nodes=NodeCapabilities(
            kinds=["gpu"],
            types=["NVIDIA GeForce RTX 4090", "NVIDIA RTX A6000"],
            type_label="GPU",
            pricing=["on_demand", "spot"],
        ),
    )


_connect(_RunPod, "verify_connection")


class _Machines(_Declared, NodeProvider):
    caps = CapabilitiesResponse(
        provider="machines",
        display_name="Machines",
        ready=True,
        nodes=NodeCapabilities(
            kinds=["gpu", "cpu"],
            types=["lab-gpu-1", "lab-gpu-2"],
            type_label="Machine",
            storage_access=["ambient"],
            type_connections={"lab-gpu-2": "0b9d0c53-5d0f-4a51-9b0e-0c5a2f1d7e11"},
        ),
    )


_DECLARED: tuple[tuple[str, type[_Declared]], ...] = (
    ("aws", _AwsNow),
    ("aws-deployments", _AwsDeployments),
    ("azure", _Azure),
    ("runpod", _RunPod),
    ("machines", _Machines),
)


def _declared_examples(root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, cls in _DECLARED:
        plugin = cls()
        with PluginHarness(plugin, plugin_id=plugin.caps.provider, config_root=root) as h:
            response = h.get("/infra/capabilities")
            assert response.status_code == 200, response.text
            out[f"capabilities.{name}"] = {
                "type": "CapabilitiesResponse",
                "route": "GET /infra/capabilities (200)",
                "example": response.json(),
            }
    return out


def build_examples() -> dict[str, dict[str, Any]]:
    """Every fixture, by file stem: one per wire type, and the capabilities of the declared providers."""
    with tempfile.TemporaryDirectory(prefix="tlc-contract-fixtures-") as scratch:
        return {**_fake_examples(Path(scratch)), **_declared_examples(Path(scratch))}


def render(example: dict[str, Any]) -> str:
    """One fixture file's text."""
    return json.dumps(example, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    """``--write`` rewrites the fixtures; without it, lists the files that differ and exits 1."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    examples = build_examples()
    stale = [stem for stem, example in examples.items() if _read(stem) != render(example)]
    extra = sorted(p.stem for p in FIXTURES.glob("*.json") if p.stem not in examples)
    if "--write" in argv:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for stem, example in examples.items():
            (FIXTURES / f"{stem}.json").write_text(render(example), encoding="utf-8")
        for stem in extra:
            (FIXTURES / f"{stem}.json").unlink()
        print(f"wrote {len(examples)} fixtures, removed {len(extra)}")
        return 0
    for stem in [*stale, *extra]:
        print(f"differs: {stem}.json")
    return 1 if stale or extra else 0


def _read(stem: str) -> str:
    path = FIXTURES / f"{stem}.json"
    return path.read_text(encoding="utf-8") if path.exists() else ""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
