# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The typed wire between a host and an infrastructure plugin: contract generation 2.

Every message the host sends a provider, and every answer it reads, is a dataclass here (the
Connection binding and its permissions are in :mod:`tlc_plugin_sdk.connections`). The rules every
one of them follows:

- ``from_dict(data)`` reads only known keys; unknown keys are dropped; ``None`` or a missing key
  is the field default; scalars are coerced and lists filtered to their item type, so an older or
  newer peer never raises here.
- ``to_dict()`` emits every field. A key whose presence carries meaning (a quote the provider
  does not have, a part of capabilities the provider does not offer) is omitted when it is
  ``None``, and a few answers omit a key that is empty where their docstring says so.
- A provider value a person must see comes with its display text from the provider. Nothing above
  the plugin parses a provider identifier.
- There is no untyped ``extra`` and no free-form display map. A value only the plugin's own page
  needs is served by the plugin's own routes.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Self

from tlc_plugin_sdk.connections import ConnectionKind, ConnectionPermissions, StorageAccessMode, StorageRule
from tlc_plugin_sdk.infrastructure._wire import (
    Wire,
    _any_dict,
    _bool,
    _dict_list,
    _int,
    _int_list,
    _mapping,
    _opt_bool,
    _opt_float,
    _opt_int,
    _str,
    _str_dict,
    _str_list,
    _stripped,
    refuse_unknown,
)
from tlc_plugin_sdk.infrastructure.errors import error_body
from tlc_plugin_sdk.infrastructure.transitional import (
    WorkspaceInstance,
    WorkspaceListing,
    WorkspacePlacement,
    WorkspaceRequest,
)

__all__ = [
    "CONTRACT_GENERATION",
    "FIELD_KEY_PATTERN",
    "OPERATIONS",
    "PROVIDER_PATTERN",
    "AccessMethod",
    "AccountSchema",
    "Address",
    "BundleRequest",
    "CapabilitiesResponse",
    "Catalog",
    "CatalogPurpose",
    "CatalogRequest",
    "Choice",
    "ConnectionKind",
    "ConnectionNodeAccess",
    "ConnectionPermissions",
    "ConnectionVerification",
    "CostNote",
    "CreateNodeRequest",
    "CreateNodeResponse",
    "CreateStorageRequest",
    "DeleteObjectsRequest",
    "DeploymentBootstrap",
    "DeploymentCapabilities",
    "DeploymentCheck",
    "DeploymentCheckRequest",
    "DeploymentHostnames",
    "DeploymentInstance",
    "DeploymentListing",
    "DeploymentOptions",
    "DeploymentOptionsRequest",
    "DeploymentPrepareRequest",
    "DeploymentPrepared",
    "DeploymentRequest",
    "DeploymentResponse",
    "DeploymentStorage",
    "DnsChange",
    "DnsRecord",
    "DnsRecords",
    "DnsZone",
    "ErrorAnswer",
    "FieldKind",
    "FormField",
    "Grant",
    "GrantFile",
    "GrantObservation",
    "GrantRequest",
    "GrantUpdate",
    "GrantUpdateRequest",
    "GrantUpdateStatus",
    "HostIdentity",
    "Location",
    "Locations",
    "NodeAccessRequest",
    "NodeCapabilities",
    "NodeCheckRequest",
    "NodeKind",
    "NodeProviderState",
    "NodeStateResponse",
    "NodeStorageAccess",
    "ObjectListing",
    "Offer",
    "PermissionOption",
    "PermissionSchema",
    "PlacementChoice",
    "PlacementField",
    "PreflightCheck",
    "PreflightResponse",
    "PresignRequest",
    "PresignResponse",
    "Pricing",
    "Purpose",
    "Region",
    "SettingsField",
    "StorageAccess",
    "StorageAccessMode",
    "StorageCandidate",
    "StorageCandidates",
    "StorageCapabilities",
    "StorageDeleted",
    "StorageGrant",
    "StorageItem",
    "StorageListing",
    "StorageLocationSchema",
    "StorageRoot",
    "StorageRule",
    "SweepItem",
    "SweepReport",
    "SweepRequest",
    "TransferRequest",
    "VerifyRequest",
    "WorkspaceInstance",
    "WorkspaceListing",
    "WorkspacePlacement",
    "WorkspaceRequest",
    "is_node_state",
]

#: The generation of this contract. A host registers an infrastructure plugin only when its
#: capabilities say this number.
CONTRACT_GENERATION = 2

#: Every operation id ``capabilities.operations`` can list, in the order it lists them. The SDK
#: lists an operation exactly when it mounted the operation's route.
OPERATIONS: tuple[str, ...] = (
    "connection.grant",
    "connection.grant_update",
    "connection.verify",
    "connection.node_access",
    "storage.candidates",
    "node.create",
    "node.state",
    "node.delete",
    "node.check",
    "node.diagnostics",
    "catalog",
    "catalog.locations",
    "deployment.options",
    "deployment.check",
    "deployment.prepare",
    "deployment.create",
    "deployment.state",
    "deployment.delete",
    "deployment.retain",
    "deployment.list",
    "deployment.sweep",
    "storage",
    "dns",
)

#: What a provider slug matches.
PROVIDER_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"
#: What a form field's key matches.
FIELD_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"

FieldKind = Literal["text", "secret", "select"]
Purpose = Literal["deployments", "nodes", "storage"]
NodeKind = Literal["gpu", "cpu"]
Pricing = Literal["on_demand", "spot"]
#: How a node reaches storage: through the identity attached to it, with what the machine already
#: has, or not at all.
NodeStorageAccess = Literal["identity", "ambient", "none"]
CatalogPurpose = Literal["gpu_node", "cpu_node", "deployment"]
GrantUpdateStatus = Literal["pending", "ready", "applied", "no_changes", "failed"]


# ── Errors ─────────────────────────────────────────────────────────────────────


@dataclass
class ErrorAnswer(Wire):
    """The body of every non-2xx answer of an infrastructure route.

    Attributes:
        detail: The sentence a person reads.
        code: What a host branches on (:data:`~tlc_plugin_sdk.infrastructure.errors.ERROR_CODES`).
        remedy: What a person can do about it, or ``""``.
        retry_after_s: Seconds after which a repeat may succeed, or ``None``.
    """

    detail: str
    code: str = "provider_error"
    remedy: str = ""
    retry_after_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """All four keys, ``retry_after_s`` as ``null`` when unset."""
        return error_body(self.detail, self.code, self.remedy, self.retry_after_s)


# ── Settings ───────────────────────────────────────────────────────────────────


@dataclass
class SettingsField:
    """One field of a plugin's settings, as the settings layer describes it to the plugin's own page.

    On the wire to a host, a prompt is a :class:`FormField` (:meth:`FormField.from_settings`).

    Attributes:
        key: The settings key.
        label: What the prompt says.
        required: Whether a node cannot be created without it.
        secret: Whether the value is a secret (masked input, never echoed).
        help: Where the value is found, in a sentence.
        href: A link to where the value is found.
        placeholder: An example value.
    """

    key: str
    label: str
    required: bool = True
    secret: bool = False
    help: str = ""
    href: str = ""
    placeholder: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> SettingsField:
        """Parse one field description (anything else is an empty field)."""
        d = _mapping(data)
        return cls(
            key=_str(d.get("key")),
            label=_str(d.get("label")),
            required=_bool(d.get("required"), True),
            secret=_bool(d.get("secret")),
            help=_str(d.get("help")),
            href=_str(d.get("href")),
            placeholder=_str(d.get("placeholder")),
        )

    def to_dict(self) -> dict[str, Any]:
        """All seven keys, always."""
        return {
            "key": self.key,
            "label": self.label,
            "required": self.required,
            "secret": self.secret,
            "help": self.help,
            "href": self.href,
            "placeholder": self.placeholder,
        }


# ── Capabilities ───────────────────────────────────────────────────────────────


@dataclass
class Choice(Wire):
    """One option of a ``select`` field.

    Attributes:
        id: The value stored.
        label: What a person reads; ``""`` shows the id.
    """

    id: str
    label: str = ""


@dataclass
class FormField(Wire):
    """One value a person is asked for. The Hub renders it; nothing above the plugin reads it.

    Attributes:
        key: The key the value is stored under (:data:`FIELD_KEY_PATTERN`), unique within its list.
        label: What the prompt says.
        kind: ``text``, ``secret`` or ``select``.
        required: Whether the form cannot be submitted without it.
        secret: Masked input, never echoed, stored only in a sealed value. True exactly when
            ``kind == "secret"``.
        help: Where the value is found, in a sentence.
        example: An example value, shown as the placeholder.
        pattern: A regular expression the whole value must match; ``""`` accepts anything.
        max_length: The longest value accepted, 1 to 2048.
        choices: The options of a ``select``.
        default: The value a new form starts with.
    """

    key: str
    label: str
    kind: FieldKind = "text"
    required: bool = True
    secret: bool = False
    help: str = ""
    example: str = ""
    pattern: str = ""
    max_length: int = 256
    choices: list[Choice] = field(default_factory=list)
    default: str = ""

    @classmethod
    def from_settings(cls, described: SettingsField) -> FormField:
        """The prompt for a settings field the plugin still needs (an entry of ``missing_fields``).

        Args:
            described: The field, as the settings layer describes it.

        Returns:
            The form field; a secret one has ``kind == "secret"``.
        """
        return cls(
            key=described.key,
            label=described.label,
            kind="secret" if described.secret else "text",
            required=described.required,
            secret=described.secret,
            help=described.help,
            example=described.placeholder,
        )


@dataclass
class AccountSchema(Wire):
    """How this provider names an account.

    Attributes:
        noun: What an account is called (``"AWS account"``).
        fields: The identity of the account: compared, key for key, with what the cloud says
            when the Connection is verified. Empty when the secret is the account.
        defaults: What is not identity: a default location.
        help: A sentence shown with the form.
    """

    noun: str
    fields: list[FormField] = field(default_factory=list)
    defaults: list[FormField] = field(default_factory=list)
    help: str = ""


@dataclass
class AccessMethod(Wire):
    """One way a Compute Service obtains authority in an account.

    Attributes:
        kind: The Connection kind this method creates.
        label: What a person reads (``"Uses access you grant to 3LC"``).
        summary: One sentence shown under the label.
        purposes: What a Connection of this method may be for.
        available: Whether this host can use the method at all.
        unavailable_reason: The sentence when it cannot.
        preferred: The method a new Connection gets; at most one method says so.
        fields: The method's ``access`` fields a person enters.
        grants: Whether ``describe_grant`` applies to this method.
    """

    kind: ConnectionKind
    label: str
    summary: str = ""
    purposes: list[Purpose] = field(default_factory=lambda: ["deployments", "nodes", "storage"])
    available: bool = True
    unavailable_reason: str = ""
    preferred: bool = False
    fields: list[FormField] = field(default_factory=list)
    grants: bool = False


@dataclass
class HostIdentity(Wire):
    """Who this host is to the provider, read from the machine's own credential chain only.

    Attributes:
        display: What a person recognises.
        matches: The access fields a Connection must carry, with these values, to act for this
            host. ``{}`` scopes nothing.
        deployment_of: On a deployment, the access fields of the Connection it was launched
            through. A Connection carrying all of them belongs to this host whatever ``matches`` says.
    """

    display: str = ""
    matches: dict[str, str] = field(default_factory=dict)
    deployment_of: dict[str, str] = field(default_factory=dict)


@dataclass
class PermissionOption(Wire):
    """A provider-declared switch of a Connection's permissions.

    Attributes:
        key: The key in ``ConnectionPermissions.options``, unique among the options.
        label: What a person reads.
        purpose: Which of the three purposes it belongs under.
        help: A sentence shown with it.
        default: Its value on a new Connection.
        operations: The Config Service operations it enables.
    """

    key: str
    label: str
    purpose: Purpose
    help: str = ""
    default: bool = False
    operations: list[str] = field(default_factory=list)


@dataclass
class StorageLocationSchema(Wire):
    """How a storage rule's location is written for this provider.

    Attributes:
        scheme: The URL scheme (``"s3"``, ``"abfss"``).
        example: An example location (``"s3://my-bucket/projects"``).
        pattern: A regular expression one rule's location must match, wildcards included.
        access_modes: The modes this cloud can express.
        help: A sentence shown with the editor.
        max_rules: How many rules a Connection may carry, 1 to 20.
    """

    scheme: str
    example: str
    pattern: str
    access_modes: list[StorageAccessMode] = field(default_factory=lambda: ["read", "read_write"])
    help: str = ""
    max_rules: int = 20


@dataclass
class PermissionSchema(Wire):
    """What a Connection's permissions can say for this provider.

    Attributes:
        options: The provider's own switches.
        storage: How storage rules are written, or ``None`` when this provider has none.
    """

    options: list[PermissionOption] = field(default_factory=list)
    storage: StorageLocationSchema | None = None


@dataclass
class NodeCapabilities(Wire):
    """What nodes this provider starts.

    Attributes:
        kinds: ``gpu``, ``cpu`` or both.
        types: The configured node types, offered by the create-node dialog.
        type_label: What a node type is called (``"Machine"``).
        pricing: How a node may be paid for.
        storage_access: How a node of this provider can reach storage.
        type_connections: ``{node_type: connection_id}`` for a provider whose node types each
            have a credential of their own (a machine and its ssh key). The host creates and
            checks a node of that type on the named Connection when the request names none,
            under a grant for the person asking.
    """

    kinds: list[NodeKind] = field(default_factory=lambda: ["gpu"])
    types: list[str] = field(default_factory=list)
    type_label: str = ""
    pricing: list[Pricing] = field(default_factory=lambda: ["on_demand"])
    storage_access: list[Literal["identity", "ambient"]] = field(default_factory=list)
    type_connections: dict[str, str] = field(default_factory=dict)


@dataclass
class DeploymentCapabilities(Wire):
    """What deployments this provider hosts.

    Attributes:
        certificate: Who obtains a deployment's certificate in a launcher's zone: the ``machine``
            itself once its names resolve to it, or the ``provider`` (a managed certificate).
        shared_ingress: ``check_deployment`` answers an ``ingress_key``.
        sslip: The provider can name a deployment by its own address.
    """

    certificate: Literal["machine", "provider"]
    shared_ingress: bool = False
    sslip: bool = False


@dataclass
class StorageCapabilities:
    """What the storage a provider offers can do: the ``storage`` object of capabilities, and the header of a listing.

    Attributes:
        kind: ``"bucket"`` or ``"volume"``.
        label: What the Hub calls one (``"S3 bucket"``, ``"RunPod network volume"``).
        creatable: ``POST /infra/storage`` works.
        upload: ``POST /infra/storage/presign`` (mode ``upload``) works.
        browse: ``GET /infra/storage/list`` works.
        download: ``POST /infra/storage/presign`` (mode ``download``) works.
        bundle: ``POST /infra/storage/bundle`` works.
        delete: ``POST /infra/storage/delete`` works.
        transfer: ``POST /infra/storage/transfer`` works.
        rename: A transfer of one item with ``rename_to`` works.
        default_id: The storage a node gets when the request names none (``""`` for none).
        upload_hint: Why the browser cannot fill this storage right now, in a sentence.
    """

    kind: str
    label: str
    creatable: bool = True
    upload: bool = True
    browse: bool = True
    download: bool = True
    bundle: bool = True
    delete: bool = True
    transfer: bool = True
    rename: bool = True
    default_id: str = ""
    upload_hint: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> StorageCapabilities:
        """Parse the ``storage`` object of capabilities or the header of a listing."""
        d = _mapping(data)
        return cls(
            kind=_str(d.get("kind")),
            label=_str(d.get("label")),
            creatable=_bool(d.get("creatable"), True),
            upload=_bool(d.get("upload"), True),
            browse=_bool(d.get("browse"), True),
            download=_bool(d.get("download"), True),
            bundle=_bool(d.get("bundle"), True),
            delete=_bool(d.get("delete"), True),
            transfer=_bool(d.get("transfer"), True),
            rename=_bool(d.get("rename"), True),
            default_id=_str(d.get("default_id")),
            upload_hint=_str(d.get("upload_hint")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Every flag; ``default_id`` and ``upload_hint`` only when non-empty."""
        d: dict[str, Any] = {
            "kind": self.kind,
            "label": self.label,
            "creatable": self.creatable,
            "upload": self.upload,
            "browse": self.browse,
            "download": self.download,
            "bundle": self.bundle,
            "delete": self.delete,
            "transfer": self.transfer,
            "rename": self.rename,
        }
        if self.default_id:
            d["default_id"] = self.default_id
        if self.upload_hint:
            d["upload_hint"] = self.upload_hint
        return d


@dataclass
class CapabilitiesResponse(Wire):
    """What a provider answers for ``GET /infra/capabilities``.

    The SDK fills ``storage`` (from the storage facet) and ``operations`` (from the routes it
    mounted) before answering, overwriting an author-set value of either, and refuses to answer a
    declaration that breaks the contract's rules.

    Attributes:
        provider: The Connection provider slug (:data:`PROVIDER_PATTERN`); equals the plugin id.
        display_name: What a person calls the provider (``"AWS"``).
        contract: The contract generation; a peer that says none reads as ``0``.
        ready: Whether the plugin's own settings are complete.
        not_ready_reason: The sentence when they are not.
        missing_fields: The plugin settings still needed, as prompts.
        requires_connection: A call to this provider acts on an account the request must name.
        account: How an account is named, or ``None`` when this provider takes no Connection.
        access_methods: The ways into an account, at most one per kind.
        host: Who this host is to the provider, or ``None``.
        permissions: What a Connection's permissions can say.
        nodes: Set exactly when the class is a ``NodeProvider``.
        deployments: Set exactly when the class is a ``DeploymentProvider``.
        storage: What the provider's storage can do, when it has a storage facet.
        operations: The ids of the operations whose routes are mounted.
    """

    provider: str
    display_name: str
    contract: int = CONTRACT_GENERATION
    ready: bool = False
    not_ready_reason: str = ""
    missing_fields: list[FormField] = field(default_factory=list)
    requires_connection: bool = False
    account: AccountSchema | None = None
    access_methods: list[AccessMethod] = field(default_factory=list)
    host: HostIdentity | None = None
    permissions: PermissionSchema = field(default_factory=PermissionSchema)
    nodes: NodeCapabilities | None = None
    deployments: DeploymentCapabilities | None = None
    storage: StorageCapabilities | None = None
    operations: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        """Read a provider's answer; one without ``contract`` reads as generation ``0``."""
        caps = super().from_dict(data)
        caps.contract = _int(_mapping(data).get("contract"), 0)
        return caps


# ── Connect an account ─────────────────────────────────────────────────────────


@dataclass
class GrantRequest(Wire):
    """What ``describe_grant`` is asked: the grant for one Connection, to create or to update.

    Attributes:
        connection_id: The Connection.
        kind: Its access method.
        account: Its account fields.
        access: Its access fields as stored, server-generated ones included.
        permissions: What the grant should allow.
        action: ``create``, or ``update`` to change the installed grant in place.
        installed_ref: ``GrantObservation.resource_ref`` of the installed grant, for an update.
        default_storage_root: The host's project root, when the caller may be offered it.
    """

    connection_id: str
    kind: ConnectionKind
    account: dict[str, str]
    access: dict[str, str]
    permissions: ConnectionPermissions
    action: Literal["create", "update"] = "create"
    installed_ref: str = ""
    default_storage_root: str = ""


@dataclass
class GrantFile(Wire):
    """A file a person uses to create the grant by hand.

    Attributes:
        name: The file name (``"3lc-connection.json"``).
        content: Its text.
        media_type: Its media type.
    """

    name: str
    content: str
    media_type: str = "application/json"


@dataclass
class Grant(Wire):
    """What a person runs in their own account, and what it creates. Never applied by 3LC.

    Attributes:
        summary: What the grant is, in a sentence.
        action: ``create`` or ``update``; an update never answers a create link.
        link: An https link that opens the provider's console with everything filled in.
        link_label: What the link says (``"Open AWS CloudFormation"``).
        steps: What to do, in order, waiting included.
        creates: Per purpose, plain sentences for what is created.
        trusts: What the grant trusts, for display.
        files: For creating it by hand.
        command: A ready-to-paste command using ``files``.
        permissions: The request's permissions in canonical form: what is stored and later
            compared with an observation.
        access: The access fields to store on the Connection before the person follows the link.
        version: The grant template's version.
        console_url: Where the installed grant can be inspected.
        notes: Everything else: revoking, verifying.
    """

    summary: str
    action: Literal["create", "update"] = "create"
    link: str = ""
    link_label: str = ""
    steps: list[str] = field(default_factory=list)
    creates: dict[str, list[str]] = field(default_factory=dict)
    trusts: str = ""
    files: list[GrantFile] = field(default_factory=list)
    command: str = ""
    permissions: ConnectionPermissions = field(default_factory=ConnectionPermissions)
    access: dict[str, str] = field(default_factory=dict)
    version: str = ""
    console_url: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class GrantObservation(Wire):
    """The installed grant as read back from the account. Evidence, not a copy of what was asked.

    Attributes:
        permissions: What the installed grant allows.
        account: The account the grant was found in.
        resource_ref: The provider's opaque reference to it (a stack ARN, an ARM deployment id).
        status: ``complete``, ``in_progress`` or ``failed``; an unreadable one reads as ``failed``.
        version: The grant template's version.
    """

    permissions: ConnectionPermissions
    account: dict[str, str]
    resource_ref: str
    status: Literal["complete", "in_progress", "failed"]
    version: str = ""
    _unreadable: ClassVar[dict[str, str]] = {"status": "failed"}


@dataclass
class GrantUpdateRequest(Wire):
    """What ``prepare_grant_update`` is asked: the change from what is installed to what is pending.

    Attributes:
        installed: The grant as last observed.
        pending: The permissions the person asked for.
        revision: The revision of the installed permissions.
        pending_revision: The revision of the pending ones.
    """

    installed: GrantObservation
    pending: ConnectionPermissions
    revision: int
    pending_revision: int


@dataclass
class GrantUpdate(Wire):
    """Where a reviewed change to the grant stands. Prepared by 3LC, applied by the customer.

    Attributes:
        status: ``pending``, ``ready``, ``applied``, ``no_changes`` or ``failed``; an unreadable
            one reads as ``failed``.
        detail: The provider's own sentence for the state.
        review_url: Where the customer reviews and applies the change; ``ready`` only.
        revision: The revision of the installed permissions.
        pending_revision: The revision the change brings.
    """

    status: GrantUpdateStatus
    detail: str = ""
    review_url: str = ""
    revision: int = 0
    pending_revision: int = 0
    _unreadable: ClassVar[dict[str, str]] = {"status": "failed"}


@dataclass
class StorageRoot(Wire):
    """A storage location the host points a provider at: a hint for what to reach, never a boundary.

    Attributes:
        url: The location.
        access: ``read`` or ``read_write``.
        role: ``project_root``, a ``scan`` folder or a ``data`` location.
    """

    _refuses_unknown: ClassVar[bool] = True

    url: str
    access: Literal["read", "read_write"] = "read_write"
    role: Literal["project_root", "scan", "data"] = "project_root"


@dataclass
class VerifyRequest(Wire):
    """What ``verify_connection`` is asked.

    Attributes:
        storage_roots: Sample targets; ``[]`` samples none. Probe targets, not an authorization.
    """

    storage_roots: list[StorageRoot] = field(default_factory=list)


@dataclass
class ConnectionVerification(Wire):
    """What verifying the request's Connection found.

    Reaching the handler already means the binding resolved; a provider adds what it asked its
    own API. A step that fails raises; a sample that could not be confirmed is a ``warn`` check;
    a refused operation is an ``error`` check, which the host records as a failed verification.

    Attributes:
        identity: Who the Connection acts as, in the provider's words.
        account: The account the provider's API says the credential is in, as account fields.
        account_label: What a person calls that account.
        checked: The verification steps that succeeded, in order.
        checks: Diagnostic samples.
        observation: The installed grant as read back, or ``None`` when there is none to read.
        facts: Labelled rows the Hub shows under the check.
    """

    identity: str
    account: dict[str, str]
    account_label: str = ""
    checked: list[str] = field(default_factory=lambda: ["resolve"])
    checks: list[PreflightCheck] = field(default_factory=list)
    observation: GrantObservation | None = None
    facts: dict[str, str] = field(default_factory=dict)


@dataclass
class StorageCandidate(Wire):
    """A storage location the rule editor can offer.

    Attributes:
        location: A rule location.
        label: What a person reads; ``""`` shows the location.
    """

    location: str
    label: str = ""


@dataclass
class StorageCandidates(Wire):
    """The answer of ``GET /infra/storage/candidates``: names, never contents.

    Attributes:
        items: At most 2000.
        truncated: Whether the account holds more.
    """

    items: list[StorageCandidate] = field(default_factory=list)
    truncated: bool = False


@dataclass
class NodeAccessRequest(Wire):
    """What ``connection_node_access`` is asked.

    Attributes:
        storage_roots: The host's storage roots when the request is made. A provider must not
            substitute its own configuration for them.
        storage_access: Setup only: the rules an administrator reviewed, for the setup to apply
            exactly. ``None`` applies the active setup unchanged.
        expected_revision: Setup only: the ``revision`` the review was based on. A provider
            applying ``storage_access`` raises ``Conflict`` when its active setup has moved on.
    """

    storage_roots: list[StorageRoot] = field(default_factory=list)
    storage_access: dict[str, Any] | None = None
    expected_revision: str = ""


# ── Nodes ──────────────────────────────────────────────────────────────────────


def _idle_ttl(value: Any) -> float:
    """An idle TTL in seconds: ``0`` and negatives kept (never auto-off).

    Only a finite number is read: a missing or unreadable value, ``nan`` and ``inf`` (``"inf"``
    included) are the 1800 s default — never mapped to "never".
    """
    parsed = _opt_float(value)
    return parsed if parsed is not None and math.isfinite(parsed) else 1800.0


def _node_kind(value: Any) -> NodeKind:
    """A request's node kind; unset is ``gpu``, anything else unknown is refused."""
    refuse_unknown("kind", _stripped(value).lower(), ("gpu", "cpu"))
    return "cpu" if _stripped(value).lower() == "cpu" else "gpu"


def _pricing(value: Any) -> Literal["", "on_demand", "spot"]:
    """A request's pricing; unset is ``""`` (the provider's choice), anything else unknown is refused."""
    refuse_unknown("pricing", _stripped(value).lower(), ("", "on_demand", "spot"))
    text = _stripped(value).lower()
    if text == "on_demand":
        return "on_demand"
    if text == "spot":
        return "spot"
    return ""


def _storage_access(value: Any) -> NodeStorageAccess:
    text = _stripped(value).lower()
    if text == "identity":
        return "identity"
    if text == "ambient":
        return "ambient"
    return "none"


def _roots(value: Any) -> list[StorageRoot]:
    return [StorageRoot.from_dict(v) for v in value if isinstance(v, Mapping)] if isinstance(value, list) else []


@dataclass
class CreateNodeRequest:
    """What the host sends when it asks a provider to create a node.

    ``create_node`` is idempotent on ``node_id``: a repeat returns the resource the first call
    created. Every resource a provider creates for the node carries the tags ``3lc`` = ``node``,
    ``3lc-id`` = ``node_id``, ``3lc-owner`` = ``owner`` and ``3lc-host`` = ``host_id``.

    Attributes:
        node_id: The host's id for the node.
        node_type: One of ``capabilities.nodes.types``.
        token: The node agent's token; scrubbed from error answers.
        kind: ``gpu`` or ``cpu``.
        location: Where the node goes, an id from ``CatalogFacet.locations``; ``""`` is the
            Connection's default, else the plugin's.
        env: Environment for the node agent. A credential variable in it is dropped by the provider.
        agent_port: Where the node agent listens.
        ports: The browser-facing app ports the provider exposes besides ``agent_port``. Workers
            are loopback-only and never listed.
        idle_ttl_s: How long the node may sit idle before its agent turns it off; ``0`` or less
            means never. Only a finite number is read: anything else is the 1800 s default.
        owner: Who the node is for.
        host_id: The creating host's service id.
        pricing: ``on_demand``, ``spot``, or ``""`` to leave the choice to the provider.
        compute_spec: The pip requirement for the node agent's own distribution, as one
            requirement (``3lc-compute==1.3.0``), or ``""``.
        storage_roots: What the node's identity should at least reach. A hint, never a boundary.
        storage_id: Provider storage to attach (a network volume), or ``""``.
        flavor: Transitional. ``"workspace"`` asks for a deployment; ``""`` for a node.
        workspace: Transitional. The deployment asked for, when ``flavor == "workspace"``.
    """

    node_id: str
    node_type: str
    token: str
    kind: NodeKind = "gpu"
    location: str = ""
    env: dict[str, str] = field(default_factory=dict)
    agent_port: int = 8800
    ports: list[int] = field(default_factory=list)
    idle_ttl_s: float = 1800.0
    owner: str = ""
    host_id: str = ""
    pricing: Literal["", "on_demand", "spot"] = ""
    compute_spec: str = ""
    storage_roots: list[StorageRoot] = field(default_factory=list)
    storage_id: str = ""
    # ── transitional ──
    flavor: str = ""
    workspace: WorkspaceRequest = field(default_factory=WorkspaceRequest)
    # ── end transitional ──

    @classmethod
    def from_dict(cls, data: Any) -> CreateNodeRequest:
        """Parse from the JSON body the host sends; keys it does not know are ignored.

        Raises:
            InvalidRequest: ``kind``, ``pricing`` or a storage root holds a value the contract does not define.
        """
        d = _mapping(data)
        return cls(
            node_id=_stripped(d.get("node_id")),
            node_type=_str(d.get("node_type")),
            token=_str(d.get("token")),
            kind=_node_kind(d.get("kind")),
            location=_stripped(d.get("location")),
            env=_str_dict(d.get("env")),
            agent_port=_int(d.get("agent_port"), 8800) or 8800,
            ports=_int_list(d.get("ports")),
            idle_ttl_s=_idle_ttl(d.get("idle_ttl_s")),
            owner=_str(d.get("owner")),
            host_id=_str(d.get("host_id")),
            pricing=_pricing(d.get("pricing")),
            compute_spec=_stripped(d.get("compute_spec")),
            storage_roots=_roots(d.get("storage_roots")),
            storage_id=_str(d.get("storage_id")),
            # ── transitional ──
            flavor="workspace" if _stripped(d.get("flavor")) == "workspace" else "",
            workspace=WorkspaceRequest.from_dict(d.get("workspace")),
            # ── end transitional ──
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the body the host sends."""
        d: dict[str, Any] = {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "token": self.token,
            "kind": self.kind,
            "location": self.location,
            "env": dict(self.env),
            "agent_port": self.agent_port,
            "ports": list(self.ports),
            "idle_ttl_s": self.idle_ttl_s,
            "owner": self.owner,
            "host_id": self.host_id,
            "pricing": self.pricing,
            "compute_spec": self.compute_spec,
            "storage_roots": [root.to_dict() for root in self.storage_roots],
            "storage_id": self.storage_id,
        }
        # ── transitional ──
        if self.flavor == "workspace":
            d["flavor"] = self.flavor
            d["workspace"] = self.workspace.to_dict()
        # ── end transitional ──
        return d


@dataclass
class CreateNodeResponse:
    """What a provider returns after creating a node.

    Attributes:
        provider_id: The provider's id for the node, used for every later call.
        agent_url: The one address the host needs: it talks to the node agent there and reaches
            the node's workers through the agent's proxy.
        token: The agent token in force, when the provider changed it.
        pricing: How the node is paid for.
        detail: A sentence about the create.
        hourly_rate: The provider's hourly quote in USD, or ``None`` for no quote.
        location: Where the node lives, in the provider's words.
        identity: The node identity attached, for display.
        storage_access: How the node reaches the request's storage roots.
        services: Transitional. A deployment's ``object_service_url`` and ``compute_service_url``.
        placement: Transitional. Where a deployment actually went.
        ingress: Transitional. A deployment's ingress record.
    """

    provider_id: str
    agent_url: str = ""
    token: str = ""
    pricing: str = ""
    detail: str = ""
    hourly_rate: float | None = None
    location: str = ""
    identity: str = ""
    storage_access: NodeStorageAccess = "none"
    # ── transitional ──
    services: dict[str, str] = field(default_factory=dict)
    placement: dict[str, str] = field(default_factory=dict)
    ingress: dict[str, Any] = field(default_factory=dict)
    # ── end transitional ──

    @classmethod
    def from_dict(cls, data: Any) -> CreateNodeResponse:
        """Parse a provider's answer (for the host)."""
        d = _mapping(data)
        return cls(
            provider_id=_str(d.get("provider_id")),
            agent_url=_str(d.get("agent_url")),
            token=_str(d.get("token")),
            pricing=_str(d.get("pricing")),
            detail=_str(d.get("detail")),
            hourly_rate=_opt_float(d.get("hourly_rate")),
            location=_str(d.get("location")),
            identity=_str(d.get("identity")),
            storage_access=_storage_access(d.get("storage_access")),
            # ── transitional ──
            services={k: v for k, v in _mapping(d.get("services")).items() if isinstance(v, str)},
            placement=_str_dict(d.get("placement")),
            ingress=_any_dict(d.get("ingress")),
            # ── end transitional ──
        )

    def to_dict(self) -> dict[str, Any]:
        """``provider_id`` and ``storage_access`` always; every other key only when set."""
        d: dict[str, Any] = {"provider_id": self.provider_id, "storage_access": self.storage_access}
        if self.agent_url:
            d["agent_url"] = self.agent_url
        if self.token:
            d["token"] = self.token
        if self.pricing:
            d["pricing"] = self.pricing
        if self.detail:
            d["detail"] = self.detail
        if self.hourly_rate is not None:
            d["hourly_rate"] = float(self.hourly_rate)
        if self.location:
            d["location"] = self.location
        if self.identity:
            d["identity"] = self.identity
        # ── transitional ──
        if self.services:
            d["services"] = dict(self.services)
        if self.placement:
            d["placement"] = dict(self.placement)
        if self.ingress:
            d["ingress"] = dict(self.ingress)
        # ── end transitional ──
        return d


#: The states a provider can report for a node it manages. ``pending`` is a node the provider is
#: still starting; ``exited`` one that stopped on its own (the host reads it during boot).
NodeProviderState = Literal["pending", "running", "exited", "terminated", "gone", "unknown"]
_NODE_STATES: frozenset[str] = frozenset(("pending", "running", "exited", "terminated", "gone", "unknown"))


def _node_state(value: Any) -> NodeProviderState:
    text = _str(value).strip().lower()
    if text == "pending":
        return "pending"
    if text == "running":
        return "running"
    if text == "exited":
        return "exited"
    if text == "terminated":
        return "terminated"
    if text == "gone":
        return "gone"
    return "unknown"


@dataclass
class NodeStateResponse:
    """What a provider returns for a node-state query.

    The ``bootstrap_*`` trio is diagnostics a provider adds on ``GET /infra/nodes/{id}?diagnostics=true``
    (:meth:`~tlc_plugin_sdk.infrastructure.InfrastructurePlugin.node_diagnostics`): the startup
    milestones seen so far, the current one in a sentence, and whether startup failed —
    ``None`` when the provider cannot tell (a console it could not read).
    """

    state: NodeProviderState
    detail: str = ""
    bootstrap_history: list[str] = field(default_factory=list)
    bootstrap_detail: str = ""
    bootstrap_failed: bool | None = None
    bootstrap_stage: str = ""
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NodeStateResponse:
        """Parse a provider's answer; an unknown state reads as ``unknown``."""
        return cls(
            state=_node_state(data.get("state")),
            detail=_str(data.get("detail")),
            bootstrap_history=_str_list(data.get("bootstrap_history")),
            bootstrap_detail=_str(data.get("bootstrap_detail")),
            bootstrap_failed=_opt_bool(data.get("bootstrap_failed")),
            bootstrap_stage=_str(data.get("bootstrap_stage")),
            diagnostics=_dict_list(data.get("diagnostics")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON body the host expects; diagnostics keys only when set."""
        d: dict[str, Any] = {"state": self.state}
        if self.detail:
            d["detail"] = self.detail
        if self.bootstrap_history:
            d["bootstrap_history"] = list(self.bootstrap_history)
        if self.bootstrap_detail:
            d["bootstrap_detail"] = self.bootstrap_detail
        if self.bootstrap_stage:
            d["bootstrap_stage"] = self.bootstrap_stage
        if self.diagnostics:
            d["diagnostics"] = [dict(item) for item in self.diagnostics]
        if self.bootstrap_failed is not None:
            d["bootstrap_failed"] = self.bootstrap_failed
        return d


@dataclass
class PreflightCheck:
    """One check in a preflight response."""

    name: str
    ok: bool
    level: str = "info"
    detail: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> PreflightCheck:
        """Parse one check."""
        d = _mapping(data)
        return cls(
            name=_str(d.get("name")),
            ok=_bool(d.get("ok")),
            level=_str(d.get("level")) or "info",
            detail=_str(d.get("detail")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON."""
        return {"name": self.name, "ok": self.ok, "level": self.level, "detail": self.detail}


@dataclass
class PreflightResponse:
    """The result of a preflight check on a provider."""

    ok: bool
    checks: list[PreflightCheck] = field(default_factory=list)
    summary: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PreflightResponse:
        """Parse a provider's answer."""
        checks = data.get("checks")
        return cls(
            ok=_bool(data.get("ok")),
            checks=[PreflightCheck.from_dict(c) for c in checks] if isinstance(checks, (list, tuple)) else [],
            summary=_str(data.get("summary")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON body the host passes through."""
        return {"ok": self.ok, "checks": [c.to_dict() for c in self.checks], "summary": self.summary}


@dataclass
class ConnectionNodeAccess:
    """Provider-owned node-access setup, rendered generically by the Hub.

    ``ready`` confirms configuration only, not live storage access. ``action_label`` is empty
    when no setup action is available. GET inspects; POST explicitly applies the displayed setup.

    A provider whose node access an administrator may widen beyond the project location answers
    ``storage_access`` (the active rules, in the Connection ``storage_access`` shape) and its
    ``revision``. ``None`` means the provider offers no such choice; the Hub then shows no editor.
    """

    supported: bool = False
    ready: bool = False
    title: str = "Node storage access"
    detail: str = ""
    action_label: str = ""
    checks: list[PreflightCheck] = field(default_factory=list)
    storage_access: dict[str, Any] | None = None
    revision: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConnectionNodeAccess:
        """Parse a provider's answer."""
        checks = data.get("checks")
        return cls(
            supported=_bool(data.get("supported")),
            ready=_bool(data.get("ready")),
            title=_str(data.get("title")) or "Node storage access",
            detail=_str(data.get("detail")),
            action_label=_str(data.get("action_label")),
            checks=[PreflightCheck.from_dict(c) for c in checks] if isinstance(checks, list) else [],
            storage_access=dict(data["storage_access"]) if isinstance(data.get("storage_access"), Mapping) else None,
            revision=_str(data.get("revision")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the generic setup result; ``storage_access`` and ``revision`` only when offered."""
        out: dict[str, Any] = {
            "supported": self.supported,
            "ready": self.ready,
            "title": self.title,
            "detail": self.detail,
            "action_label": self.action_label,
            "checks": [c.to_dict() for c in self.checks],
        }
        if self.storage_access is not None:
            out["storage_access"] = self.storage_access
            out["revision"] = self.revision
        return out


@dataclass
class NodeCheckRequest(Wire):
    """What ``check_node`` is asked: whether a node of this shape could start now.

    Attributes:
        node_type: The node type to check, or ``""`` for the provider's default.
        pricing: ``on_demand``, ``spot`` or ``""``.
        location: A location to check stock in, or ``""``.
        storage_roots: The host's storage roots, for a check of what the node would reach.
    """

    node_type: str = ""
    pricing: str = ""
    location: str = ""
    storage_roots: list[StorageRoot] = field(default_factory=list)


# ── Catalog ────────────────────────────────────────────────────────────────────


@dataclass
class Offer(Wire):
    """One node type or deployment size on offer.

    Attributes:
        id: The node type or deployment size to send back.
        label: What a person reads; ``""`` shows the id.
        description: A sentence about it.
        vcpus: Virtual CPUs.
        memory_gb: Memory.
        gpu_name: The GPU model, or ``""``.
        gpu_count: How many.
        gpu_memory_gb: Memory per GPU.
        hourly: The on-demand price, in ``currency``.
        hourly_spot: The spot price, in ``currency``.
        currency: The ISO 4217 code of both prices.
        available: Whether it can be started now; ``None`` when the provider cannot tell.
        unavailable_reason: Why not (quota, not offered here).
        capacity: The provider's words for stock, ``""`` when unknown.
        recommended: Whether the provider suggests it.
    """

    id: str
    label: str = ""
    description: str = ""
    vcpus: int | None = None
    memory_gb: float | None = None
    gpu_name: str = ""
    gpu_count: int = 0
    gpu_memory_gb: float | None = None
    hourly: float | None = None
    hourly_spot: float | None = None
    currency: str = "USD"
    available: bool | None = None
    unavailable_reason: str = ""
    capacity: str = ""
    recommended: bool = False


@dataclass
class CatalogRequest(Wire):
    """What ``catalog`` is asked.

    Attributes:
        purpose: ``gpu_node``, ``cpu_node`` or ``deployment``.
        location: Where to price; ``""`` prices in the Connection's default location.
    """

    purpose: CatalogPurpose
    location: str = ""


@dataclass
class Catalog(Wire):
    """The answer of ``POST /infra/catalog``.

    Attributes:
        offers: At most 500.
        location: Where the offers were priced.
        error: Why the list is short: a failed lookup is an empty list with the reason.
    """

    offers: list[Offer] = field(default_factory=list)
    location: str = ""
    error: str = ""


@dataclass
class Location(Wire):
    """A place a node or a deployment can go.

    Attributes:
        id: The value sent back as ``location``.
        label: What a person reads; ``""`` shows the id.
        available: Whether the node type asked about can start there; ``None`` when the
            provider cannot tell.
        capacity: The provider's words for stock there, ``""`` when unknown.
    """

    id: str
    label: str = ""
    available: bool | None = None
    capacity: str = ""


@dataclass
class Locations(Wire):
    """The answer of ``GET /infra/catalog/locations``.

    Attributes:
        locations: The places, in the order to offer them.
        node_type: The node type stock was checked for, or ``""``.
        default: The location a request that names none goes to, or ``""`` when the provider
            chooses per request.
        error: Why the list is short.
    """

    locations: list[Location] = field(default_factory=list)
    node_type: str = ""
    default: str = ""
    error: str = ""


# ── Deployments ────────────────────────────────────────────────────────────────


@dataclass
class PlacementChoice(Wire):
    """One value of a placement field.

    Attributes:
        id: The value sent back.
        label: What a person reads; ``""`` shows the id.
    """

    id: str
    label: str = ""


@dataclass
class PlacementField(Wire):
    """One thing a person chooses about where a deployment goes.

    Attributes:
        key: The key sent back in ``placement``.
        label: What a person reads (``"Region"``, ``"Virtual network"``).
        choices: The values valid for what is selected so far.
        default: The value a new form starts with.
        required: Whether a launch needs it.
    """

    key: str
    label: str
    choices: list[PlacementChoice] = field(default_factory=list)
    default: str = ""
    required: bool = True


@dataclass
class CostNote(Wire):
    """One line of what a deployment costs.

    Attributes:
        label: What is paid for.
        description: A sentence about it.
        hourly: The hourly price, or ``None`` when unknown.
        currency: The price's currency.
        reused: Whether an existing shared resource would be used; ``None`` when not applicable.
    """

    label: str
    description: str = ""
    hourly: float | None = None
    currency: str = "USD"
    reused: bool | None = None


@dataclass
class DeploymentOptionsRequest(Wire):
    """What ``deployment_options`` is asked.

    Attributes:
        selected: The placement chosen so far, by field key.
    """

    selected: dict[str, str] = field(default_factory=dict)


@dataclass
class DeploymentOptions(Wire):
    """Where a deployment can go under the request's Connection, given what is selected so far.

    Attributes:
        placement: The fields, in the order they are chosen.
        costs: What a deployment there costs.
        warnings: Sentences a person should read before launching.
        blockers: Sentences for why a launch there cannot work.
    """

    placement: list[PlacementField] = field(default_factory=list)
    costs: list[CostNote] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


@dataclass
class DeploymentStorage(Wire):
    """Where a deployment keeps and finds its data.

    Attributes:
        root: The project root; ``""`` keeps projects on the machine's disk.
        scan: Read-only folders to index.
        data: Read-only dataset locations.
        create_root: Create the root's container when it does not exist.
    """

    root: str = ""
    scan: list[str] = field(default_factory=list)
    data: list[str] = field(default_factory=list)
    create_root: bool = False


@dataclass
class DnsZone(Wire):
    """A launcher's DNS zone.

    Attributes:
        id: The zone's id, opaque to the host and to every provider but the DNS plugin.
        name: The zone's name (``"deploy.example.com"``).
    """

    id: str
    name: str


@dataclass
class DeploymentHostnames(Wire):
    """The names a launcher allocated for a deployment in its zone. The provider derives no name.

    Attributes:
        hostname: The deployment's own name; ``""`` in the shared shape.
        object_service: The Object Service's fully qualified name.
        compute_service: The Compute Service's fully qualified name.
        namespace: The shared shape's parent name; ``""`` otherwise.
    """

    hostname: str
    object_service: str
    compute_service: str
    namespace: str = ""


@dataclass
class DeploymentBootstrap(Wire):
    """How the machine becomes its deployment record at the Config Service.

    Attributes:
        record_id: The deployment record the machine becomes.
        config_service_url: Where it redeems its token.
        token: The single-use bootstrap token; scrubbed from error answers and never in a repr.
        launcher_service_id: The launcher's service id.
    """

    record_id: str
    config_service_url: str
    token: str = field(default="", repr=False)
    launcher_service_id: str = ""


@dataclass
class DeploymentCheckRequest(Wire):
    """What ``check_deployment`` is asked before a launch.

    Attributes:
        label: The DNS label the launcher derived from the deployment's name.
        size: The size asked for, or ``""`` for the default.
        placement: The placement chosen, by field key.
        storage: Where the deployment would keep and find its data.
        zone: The launcher's zone, or ``None`` when it names deployments by address.
    """

    label: str
    size: str = ""
    placement: dict[str, str] = field(default_factory=dict)
    storage: DeploymentStorage = field(default_factory=DeploymentStorage)
    zone: DnsZone | None = None


@dataclass
class DeploymentCheck(Wire):
    """Whether a deployment of this shape can be launched.

    Attributes:
        ok: Whether it can.
        reason: The sentence when it cannot.
        ingress_key: Opaque to the launcher: the same key means the same shared ingress, ``""``
            means the deployment gets its own address.
        checks: What was checked.
    """

    ok: bool
    reason: str = ""
    ingress_key: str = ""
    checks: list[PreflightCheck] = field(default_factory=list)


@dataclass
class DeploymentPrepareRequest(Wire):
    """What ``prepare_deployment`` is asked, repeatedly, until it is ready. Carries no token.

    Attributes:
        deployment_id: The launcher's durable id.
        placement: The placement chosen.
        namespace: The parent name every answered DNS record must be under.
        size: The size asked for.
        ingress_key: As ``check_deployment`` answered it.
    """

    deployment_id: str
    placement: dict[str, str] = field(default_factory=dict)
    namespace: str = ""
    size: str = ""
    ingress_key: str = ""


@dataclass
class DnsRecord(Wire):
    """One DNS record set.

    Attributes:
        name: The fully qualified name, lower case, no trailing dot.
        type: ``A``, ``AAAA``, ``CNAME`` or ``TXT``; an unreadable one reads as ``TXT``.
        values: The record's values.
        ttl: Its time to live in seconds.
    """

    name: str
    type: Literal["A", "AAAA", "CNAME", "TXT"]
    values: list[str]
    ttl: int = 60
    _unreadable: ClassVar[dict[str, str]] = {"type": "TXT"}


@dataclass
class DeploymentPrepared(Wire):
    """Where what deployments share stands.

    Attributes:
        state: ``ready``, ``pending`` or ``failed``; an unreadable one reads as ``failed``.
        detail: The provider's sentence for the state.
        dns_records: The records that must exist for ``ready`` to be reached; the launcher
            writes them.
        retry_after_s: When to ask again.
    """

    state: Literal["ready", "pending", "failed"]
    detail: str = ""
    dns_records: list[DnsRecord] = field(default_factory=list)
    retry_after_s: float = 5.0
    _unreadable: ClassVar[dict[str, str]] = {"state": "failed"}


@dataclass
class DeploymentRequest(Wire):
    """What ``create_deployment`` is asked. Idempotent on ``deployment_id``.

    Every resource the provider creates carries the tags ``3lc`` = ``deployment`` and ``3lc-id`` =
    ``deployment_id``.

    Attributes:
        deployment_id: The launcher's durable id.
        name: The name as the person typed it, for display and tags.
        bootstrap: How the machine becomes its record.
        hostnames: The names the launcher allocated, or ``None`` to name the deployment by its
            address.
        ingress_key: As ``check_deployment`` answered it.
        size: An ``Offer.id`` of ``catalog(purpose="deployment")``; ``""`` is the default.
        placement: The placement chosen.
        storage: Where the deployment keeps and finds its data.
        compute_spec: The pip requirement for the Compute Service to install.
        env: Environment for the deployment's services.
        plugins: Install sources; ``[]`` installs the default roster.
        owner: Who the deployment is for.
        mode: ``full`` or ``demo``.
        detailed_diagnostics: Whether the detailed diagnostics collector is granted.
    """

    _refuses_unknown: ClassVar[bool] = True

    deployment_id: str
    name: str
    bootstrap: DeploymentBootstrap
    hostnames: DeploymentHostnames | None = None
    ingress_key: str = ""
    size: str = ""
    placement: dict[str, str] = field(default_factory=dict)
    storage: DeploymentStorage = field(default_factory=DeploymentStorage)
    compute_spec: str = ""
    env: dict[str, str] = field(default_factory=dict)
    plugins: list[str] = field(default_factory=list)
    owner: str = ""
    mode: Literal["full", "demo"] = "full"
    detailed_diagnostics: bool = False


@dataclass
class Address(Wire):
    """Where a deployment answers.

    Attributes:
        type: ``ipv4``, ``ipv6`` or ``hostname``; an unreadable one reads as ``hostname``.
        value: The address.
    """

    type: Literal["ipv4", "ipv6", "hostname"]
    value: str
    _unreadable: ClassVar[dict[str, str]] = {"type": "hostname"}


@dataclass
class DeploymentResponse(Wire):
    """What a provider returns once a deployment's machine exists.

    Attributes:
        provider_id: The provider's id for the deployment.
        addresses: What the launcher points the hostnames at.
        endpoints: ``object_service_url`` and ``compute_service_url``, https.
        location: Where it lives, in the provider's words.
        placement: Where it actually went.
        hourly_rate: The provider's hourly quote in USD, or ``None``.
        resource_ref: The provider's opaque reference to what it created.
        failure: Set when resources exist but the launch failed; the launcher records it, then
            deletes.
    """

    provider_id: str
    addresses: list[Address]
    endpoints: dict[str, str]
    location: str = ""
    placement: dict[str, str] = field(default_factory=dict)
    hourly_rate: float | None = None
    resource_ref: str = ""
    failure: str = ""


@dataclass
class DeploymentInstance(Wire):
    """One deployment the account runs, tracked by the launcher or not.

    Attributes:
        provider_id: The provider's id, what ``delete_deployment`` takes.
        deployment_id: The launcher's id from the ``3lc-id`` tag, or ``""`` when it carries none.
        name: The name it was given.
        owner: Who it is for.
        size: Its size, an ``Offer.id``.
        state: Its state.
        location: Where it lives.
        addresses: Where it answers.
    """

    provider_id: str
    deployment_id: str = ""
    name: str = ""
    owner: str = ""
    size: str = ""
    state: NodeProviderState = "unknown"
    location: str = ""
    addresses: list[Address] = field(default_factory=list)


@dataclass
class DeploymentListing(Wire):
    """The answer of ``GET /infra/deployments``.

    Attributes:
        deployments: At most 500.
    """

    deployments: list[DeploymentInstance] = field(default_factory=list)


@dataclass
class SweepItem(Wire):
    """A deployment the launcher knows.

    Attributes:
        deployment_id: The launcher's id.
        provider_id: The provider's id, when the launcher learned it.
    """

    deployment_id: str
    provider_id: str = ""


@dataclass
class SweepRequest(Wire):
    """What ``sweep_deployments`` is asked: release what no live deployment owns.

    Attributes:
        keep: Alive or mid-launch: never touched.
        gone: Dead: what they left may be removed.
        locations: Where to look; the provider adds the Connection's default location.
        grace_s: How old an unowned resource must be before it is released.
    """

    keep: list[SweepItem] = field(default_factory=list)
    gone: list[SweepItem] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    grace_s: float = 600.0


@dataclass
class SweepReport(Wire):
    """What a sweep did. It never touches DNS.

    Attributes:
        complete: Whether every location was swept.
        released: What was removed, for the log.
        held_addresses: The addresses live deployments hold, so the launcher can tell which
            names in its zone point at nothing.
        pending: Candidates still inside the grace period.
        problems: What could not be done.
    """

    complete: bool
    released: list[str] = field(default_factory=list)
    held_addresses: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


# ── DNS ────────────────────────────────────────────────────────────────────────


@dataclass
class DnsChange(Wire):
    """One atomic change of at most 100 records in a launcher's zone.

    Attributes:
        zone: The zone.
        create: Records to create. One that exists with the same values succeeds; with other
            values the whole change is refused with ``Conflict``.
        delete: Records to delete, each only when it matches exactly; a missing one is success.
    """

    zone: DnsZone
    create: list[DnsRecord] = field(default_factory=list)
    delete: list[DnsRecord] = field(default_factory=list)


@dataclass
class DnsRecords(Wire):
    """One page of a zone's records.

    Attributes:
        records: At most 300.
        next_cursor: Continues the listing; ``""`` on the last page.
    """

    records: list[DnsRecord] = field(default_factory=list)
    next_cursor: str = ""


# ── Storage ────────────────────────────────────────────────────────────────────


@dataclass
class StorageGrant(Wire):
    """What the request's Connection may do under one location of a storage.

    Attributes:
        url: The location the grant covers: it and everything under it.
        read: Objects may be read.
        write: Objects may be written.
        delete: Objects may be deleted.
    """

    url: str
    read: bool = False
    write: bool = False
    delete: bool = False


@dataclass
class StorageAccess(Wire):
    """What the request's Connection may do in one storage, as the Data page offers it.

    The provider projects its own policies into these, so the Hub never reads a cloud policy.

    Attributes:
        browse: Names may be listed.
        delete_location: The storage itself may be deleted.
        bundle: A bundle may be built from it.
        grants: The locations where objects may be read, written or deleted. A location no grant
            covers allows nothing beyond ``browse``.
    """

    browse: bool = False
    delete_location: bool = False
    bundle: bool = False
    grants: list[StorageGrant] = field(default_factory=list)


@dataclass
class StorageItem:
    """One bucket, container or volume as the Data page lists it.

    ``url`` is what the Data page browses and uploads to (``s3://…``, ``abfs://…``,
    ``volume://<id>``); ``default`` is whether a node gets this storage when the request names
    none (``None`` when the provider has no such notion). ``access`` is what the request's
    Connection may do in it, or ``None`` when the provider does not say: the provider's storage
    capabilities then say what works, and the storage routes still authorize each call.
    """

    id: str
    name: str = ""
    url: str = ""
    kind: str = ""
    region: str = ""
    size_gb: int | None = None
    default: bool | None = None
    managed_by: str = ""
    access: StorageAccess | None = None

    @classmethod
    def from_dict(cls, data: Any) -> StorageItem:
        """Parse one listed item."""
        d = _mapping(data)
        return cls(
            id=_str(d.get("id")),
            name=_str(d.get("name")),
            url=_str(d.get("url")),
            kind=_str(d.get("kind")),
            region=_str(d.get("region")),
            size_gb=_opt_int(d.get("size_gb")),
            default=_opt_bool(d.get("default")),
            managed_by=_str(d.get("managed_by")),
            access=StorageAccess.from_dict(d["access"]) if isinstance(d.get("access"), Mapping) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """``id``, ``name``, ``url``, ``region``, ``size_gb`` always; the rest when set."""
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "region": self.region,
            "size_gb": self.size_gb,
        }
        if self.kind:
            d["kind"] = self.kind
        if self.default is not None:
            d["default"] = self.default
        if self.managed_by:
            d["managed_by"] = self.managed_by
        if self.access is not None:
            d["access"] = self.access.to_dict()
        return d


@dataclass
class Region:
    """A place storage can be created in."""

    id: str
    name: str = ""
    location: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> Region:
        """Parse one region."""
        d = _mapping(data)
        return cls(id=_str(d.get("id")), name=_str(d.get("name")), location=_str(d.get("location")))

    def to_dict(self) -> dict[str, Any]:
        """``id`` and ``name`` always; ``location`` when set."""
        d: dict[str, Any] = {"id": self.id, "name": self.name}
        if self.location:
            d["location"] = self.location
        return d


@dataclass
class StorageListing:
    """The answer of ``GET /infra/storage``: the header flags plus the items.

    ``to_dict`` flattens: the :class:`StorageCapabilities` keys at the top level, then
    ``storage`` and ``regions``, then ``account`` and ``region`` when set, the shape the host
    splits into per-provider meta and items.
    """

    capabilities: StorageCapabilities
    storage: list[StorageItem] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    account: str = ""
    region: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StorageListing:
        """Parse a provider's flattened answer."""
        items = data.get("storage")
        regions = data.get("regions")
        return cls(
            capabilities=StorageCapabilities.from_dict(data),
            storage=[StorageItem.from_dict(i) for i in items] if isinstance(items, (list, tuple)) else [],
            regions=[Region.from_dict(r) for r in regions] if isinstance(regions, (list, tuple)) else [],
            account=_str(data.get("account")),
            region=_str(data.get("region")),
        )

    def to_dict(self) -> dict[str, Any]:
        """The flattened answer."""
        d: dict[str, Any] = self.capabilities.to_dict()
        d["storage"] = [i.to_dict() for i in self.storage]
        d["regions"] = [r.to_dict() for r in self.regions]
        if self.account:
            d["account"] = self.account
        if self.region:
            d["region"] = self.region
        return d


@dataclass
class CreateStorageRequest:
    """The body of ``POST /infra/storage``: ``{name, region?, size_gb?, make_default?, owner?}``.

    ``owner`` is the caller the host acts for (an identity, not a credential).
    """

    name: str
    region: str = ""
    size_gb: int | None = None
    make_default: bool = False
    owner: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CreateStorageRequest:
        """Parse the body."""
        return cls(
            name=_stripped(data.get("name")),
            region=_stripped(data.get("region")),
            size_gb=_opt_int(data.get("size_gb")),
            make_default=_bool(data.get("make_default")),
            owner=_str(data.get("owner")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the body."""
        d: dict[str, Any] = {
            "name": self.name,
            "region": self.region,
            "make_default": self.make_default,
            "owner": self.owner,
        }
        if self.size_gb is not None:
            d["size_gb"] = self.size_gb
        return d


@dataclass
class StorageDeleted:
    """The answer of ``DELETE /infra/storage/{id}``."""

    deleted: bool
    id: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StorageDeleted:
        """Parse the answer."""
        return cls(deleted=_bool(data.get("deleted")), id=_str(data.get("id")))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the answer."""
        return {"deleted": self.deleted, "id": self.id}


@dataclass
class PresignRequest:
    """The body of ``POST /infra/storage/presign``: ``{url, files: [{path, size?, content_type?}], mode?}``."""

    url: str
    files: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "upload"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PresignRequest:
        """Parse the body."""
        return cls(
            url=_str(data.get("url")), files=_dict_list(data.get("files")), mode=_str(data.get("mode")) or "upload"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the body."""
        return {"url": self.url, "files": [dict(f) for f in self.files], "mode": self.mode}


@dataclass
class PresignResponse:
    """Presigned URLs for the browser.

    The item dicts are browser-facing and provider-shaped; only the envelope is typed. ``cors`` is
    whether the storage accepts the browser's cross-origin upload: ``False`` when the provider
    could not arrange it, ``None`` (the key left out) when it has nothing to say.
    """

    expires_s: int = 0
    uploads: list[dict[str, Any]] = field(default_factory=list)
    downloads: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    region: str = ""
    cors: bool | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PresignResponse:
        """Parse the answer."""
        return cls(
            expires_s=_int(data.get("expires_s"), 0),
            uploads=_dict_list(data.get("uploads")),
            downloads=_dict_list(data.get("downloads")),
            refused=_dict_list(data.get("refused")),
            region=_str(data.get("region")),
            cors=_opt_bool(data.get("cors")),
        )

    def to_dict(self) -> dict[str, Any]:
        """``expires_s``, ``uploads``, ``downloads`` and ``refused`` always; ``region`` and ``cors`` when set."""
        d: dict[str, Any] = {
            "expires_s": self.expires_s,
            "uploads": [dict(u) for u in self.uploads],
            "downloads": [dict(u) for u in self.downloads],
            "refused": [dict(r) for r in self.refused],
        }
        if self.region:
            d["region"] = self.region
        if self.cors is not None:
            d["cors"] = self.cors
        return d


@dataclass
class ObjectListing:
    """One level of a storage prefix: the answer of ``GET /infra/storage/list``.

    ``prefixes`` are ``{name, url}``; ``objects`` are ``{key, size, name, url, …}``. ``next_token``
    continues a ``truncated`` listing (emitted only when there is one).
    """

    url: str
    prefixes: list[dict[str, Any]] = field(default_factory=list)
    objects: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    next_token: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ObjectListing:
        """Parse the answer."""
        return cls(
            url=_str(data.get("url")),
            prefixes=_dict_list(data.get("prefixes")),
            objects=_dict_list(data.get("objects")),
            truncated=_bool(data.get("truncated")),
            next_token=_str(data.get("next_token")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the answer."""
        d: dict[str, Any] = {
            "url": self.url,
            "prefixes": [dict(p) for p in self.prefixes],
            "objects": [dict(o) for o in self.objects],
            "truncated": self.truncated,
        }
        if self.next_token:
            d["next_token"] = self.next_token
        return d


@dataclass
class DeleteObjectsRequest:
    """The body of ``POST /infra/storage/delete``: paths relative to ``url``; a trailing ``/`` names a folder."""

    url: str
    paths: list[str] = field(default_factory=list)
    dry_run: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeleteObjectsRequest:
        """Parse the body."""
        return cls(url=_str(data.get("url")), paths=_str_list(data.get("paths")), dry_run=_bool(data.get("dry_run")))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the body."""
        return {"url": self.url, "paths": list(self.paths), "dry_run": self.dry_run}


@dataclass
class TransferRequest:
    """The body of ``POST /infra/storage/transfer``: copy or move ``items`` from ``src_url`` under ``dst_url``."""

    src_url: str
    dst_url: str
    items: list[str] = field(default_factory=list)
    mode: str = "copy"
    rename_to: str = ""
    dry_run: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TransferRequest:
        """Parse the body."""
        return cls(
            src_url=_str(data.get("src_url")),
            dst_url=_str(data.get("dst_url")),
            items=_str_list(data.get("items")),
            mode=_str(data.get("mode")) or "copy",
            rename_to=_str(data.get("rename_to")),
            dry_run=_bool(data.get("dry_run")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the body."""
        return {
            "src_url": self.src_url,
            "dst_url": self.dst_url,
            "items": list(self.items),
            "mode": self.mode,
            "rename_to": self.rename_to,
            "dry_run": self.dry_run,
        }


@dataclass
class BundleRequest:
    """The body of ``POST /infra/storage/bundle``: the folder to zip and the archive's name."""

    url: str
    name: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BundleRequest:
        """Parse the body."""
        return cls(url=_str(data.get("url")), name=_stripped(data.get("name")))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the body."""
        return {"url": self.url, "name": self.name}


def is_node_state(value: Any) -> bool:
    """Whether ``value`` is one of :data:`NodeProviderState` (for tests and the conformance kit)."""
    return isinstance(value, str) and value in _NODE_STATES
