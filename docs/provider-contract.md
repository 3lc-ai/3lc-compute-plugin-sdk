# Infrastructure provider contract

**Status: the SDK part is built; the rest is a proposal.** This specifies contract generation 2
of the infrastructure plugin surface (`tlc_plugin_sdk.infrastructure` and
`tlc_plugin_sdk.connections`), to ship as SDK 0.8. It is written so that the SDK, the Compute
Service (the host), the Hub, the Config Service and each provider plugin can be built against it
independently. The SDK implements sections 2.1 to 2.8 and 3.1 to 3.3 (package A of section 6.2),
with the reference provider and the golden fixtures of section 6.4; no host, Hub, Config Service
or provider plugin implements it yet.

The contract serves three goals a person has on any cloud: connect to cloud storage, start a GPU
node, start a managed deployment. A fourth group, connect an account, is what the other three
stand on. Everything above the plugin (host, Hub, Config Service) handles provider values as data
it stores and shows and never interprets.

A provider author starts from the plugin guide, [Which methods to write](plugin-guide.md#which-methods-to-write):
the methods for nodes only, for nodes and connect, and for deployments, with the test to run.

Section 2 is the minimal 0.8: what the first SDK package builds. Section 2.9 specifies what is
added later, each piece with the package that needs it.

## How to read the citations

Every type and route is justified by the call site that needs it, cited as `repo:path:line`.
The repos are abbreviated `sdk` (3lc-compute-plugin-sdk, `main`), `compute` (3lc-compute,
`develop`), `hub` (3lc-hub-frontend, `develop`), `cs` (tlc-config-service, `develop`), `aws`,
`azure`, `runpod`, `machines` (3lc-compute-plugin-*, `main`). Paths under `sdk` are relative to
`src/tlc_plugin_sdk/`, under `compute` to `src/tlc_compute/`, under `cs` to
`src/tlc_config_service/`, under `aws` to `src/tlc_plugin_aws/`, under `hub` to `frontend/`,
unless they start with `docs/` or `tests/`, or name a top-level file.

Types are written as the SDK writes its wire types: plain dataclasses with a tolerant
`from_dict` and a `to_dict` (`sdk:infrastructure/types.py:5-19`). The SDK has no pydantic
dependency (`sdk:pyproject.toml:48-56`) and this contract adds none; the Config Service models in
section 4 are pydantic, as that service's are (`cs:api/hub/connections.py:115`).

---

## 1. Vocabulary

**Account.** The unit of a cloud that a customer owns and 3LC is let into. It has no common
form: it is identified by fields the provider declares, and those fields are opaque to every
service above the plugin. Every account field is identity: it is compared, key for key, with
what the cloud says when the Connection is verified. A Connection names exactly one account.

**Access method.** How a Compute Service obtains authority in an account. There are three, and
the method is a fact about a Connection, shown to the person, not a first choice they make:

- *Own sign-in*: the machine the Compute Service runs on is already signed in to the cloud, and
  acts as itself.
- *Granted*: the customer creates something in their account (a grant) that trusts 3LC, and 3LC
  exchanges proof of its own identity for short-lived credentials. No secret is stored anywhere.
- *Secret*: the customer hands over a stored value (an API key), which the Config Service seals
  and releases per use.

On the wire the three are the Connection kinds `AMBIENT`, `KEYLESS` and `SECRET`
(`cs:db/models/connection.py:33-36`). The words above are what a person reads; the kinds are
what services exchange.

**Connection.** An account, the access method into it, and what 3LC may do there (start
deployments, start nodes, reach these storage locations). Held by the Config Service as
non-secret data plus, for the secret method, one sealed value. Used only through short-lived
grants (`cs:api/hub/plugin_grants.py:11-37`).

**Node identity.** The cloud identity a node's own processes run as, which decides what storage
the node reaches. It is created by the grant, attached by the provider when the node is created,
and never a credential copied onto the node.

**Deployment.** One machine in the customer's account running an Object Service and a Compute
Service, reachable under a public hostname with a valid certificate. It acts day to day as its
own deployment identity and never assumes anything
(`compute:docs/internal/launcher-and-deployments.md:48-51`).

**Launcher.** A Compute Service that creates deployments in other organizations' accounts. It
owns each deployment's public name for the deployment's whole life
(`compute:docs/internal/launcher-and-deployments.md:43-44`). Where names come from is a
launcher setting with three values: a DNS zone the launcher owns, `sslip.io`, or none.

| Term | AWS | Azure |
|---|---|---|
| Account | Account id (12 digits) | Tenant id, subscription id, resource group |
| Default location (not identity) | Region | Location |
| Own sign-in | boto3's default chain: an instance profile, a profile on the machine | `az login` on a laptop, or a managed identity on an Azure VM |
| Granted | A role in the account that trusts the launcher's IAM principal, assumed with an external id | A user-assigned managed identity whose federated credential trusts a 3LC OIDC issuer and one subject per Connection; a signed token is exchanged at the tenant for an ARM token |
| Secret | Not offered | Not offered |
| Grant | A CloudFormation stack (control, deployment and node roles) | A template deployment into the resource group (control, deployment and node identities, role assignments) |
| Node identity | Instance profile `<role>-node` | User-assigned identity attached to the VM |
| Deployment | EC2 instance behind a shared load balancer with an ACM certificate | VM with a public IP, certificate obtained on the machine |
| Storage location | `s3://bucket/prefix` | `abfss://container@account.dfs.core.windows.net/path` |

RunPod reads: account = nothing to name (the key is the account), access = secret only, no node
identity, no deployments. Machines reads: no account, no cloud access method, nodes only.

---

## 2. The typed contract

### 2.0 Wire rules

These hold for every type below and are the SDK's existing rules (`sdk:infrastructure/types.py:5-19`):

- `from_dict` reads known keys only, drops unknown ones, coerces scalars and never raises on a
  peer's shape. A required field a peer left out reads as the empty value of its type, and an
  unreadable status reads as the safe one (`failed`). `to_dict` emits every field, and omits a
  key whose presence carries meaning when it is unset: a field that is `None`, and the optional
  keys of the answers that already did so (`CreateNodeResponse`, `NodeStateResponse`, the
  storage types).
- One exception to "never raises": a request a provider acts on refuses an enum value it does
  not know. In `CreateNodeRequest` (`kind`, `pricing`), `DeploymentRequest` (`mode`) and every
  `StorageRoot` (`access`, `role`), a value that is set and not one of the field's values is an
  `InvalidRequest` (400) whose remedy names the allowed values, so nothing launches on a default
  nobody chose. An unset or empty value is the field's default, and case is not significant for
  `kind` and `pricing`. An answer stays tolerant: an unknown value in a response reads as the
  field's default or its safe value.
- New optional fields and new operations may be added in a later minor without a change from a
  provider that does not use them. Nothing in a request is ever required of a provider that did
  not declare the operation it belongs to.
- A provider value a person must see comes with its display text from the provider. The Hub
  never parses a provider identifier.
- There is no untyped `extra` and no free-form display map. See section 3.3.

An error answer is `{"detail": str, "code": str, "remedy": str, "retry_after_s": float | null}`.
`detail` is the sentence a person reads, as today (`sdk:infrastructure/routes.py:121-162`);
the other keys are specified in 2.4.

### 2.1 Capabilities declaration

`GET /infra/capabilities`, never sent a Connection (2.3, rule 1).

```python
CONTRACT_GENERATION = 2

FieldKind = Literal["text", "secret", "select"]
ConnectionKind = Literal["AMBIENT", "KEYLESS", "SECRET"]
Purpose = Literal["deployments", "nodes", "storage"]
StorageAccessMode = Literal["read", "read_write_no_delete", "read_write"]


@dataclass
class Choice:
    id: str
    label: str = ""                    # "" shows the id


@dataclass
class FormField:
    """One value a person is asked for. The Hub renders it; nothing above the plugin reads it."""

    key: str                           # ^[a-z][a-z0-9_]{0,39}$, unique within its list
    label: str
    kind: FieldKind = "text"
    required: bool = True
    secret: bool = False               # masked input, never echoed, stored only in a sealed value
    help: str = ""                     # where the value is found, in a sentence
    example: str = ""                  # shown as the placeholder
    pattern: str = ""                  # regex the whole value must match; "" accepts anything
    max_length: int = 256              # 1..2048
    choices: list[Choice] = field(default_factory=list)   # kind == "select" only
    default: str = ""


@dataclass
class AccountSchema:
    noun: str                          # "AWS account", "Azure subscription and resource group"
    fields: list[FormField] = field(default_factory=list)     # identity; [] when the secret is the account
    defaults: list[FormField] = field(default_factory=list)   # not identity: a default location
    help: str = ""


@dataclass
class AccessMethod:
    kind: ConnectionKind
    label: str                         # "Uses access you grant to 3LC"
    summary: str = ""                  # one sentence shown under the label
    purposes: list[Purpose] = field(default_factory=lambda: ["deployments", "nodes", "storage"])
    available: bool = True             # whether this host can use the method at all
    unavailable_reason: str = ""       # the sentence when it cannot
    preferred: bool = False            # at most one True: the method a new Connection gets
    fields: list[FormField] = field(default_factory=list)   # the method's `access` fields a person enters
    grants: bool = False               # `describe_grant` applies to this method


@dataclass
class HostIdentity:
    """Who this host is to the provider. Ambient-only; see 2.3 rule 1."""

    display: str = ""                  # what a person recognises
    matches: dict[str, str] = field(default_factory=dict)
    # Access fields a Connection must carry to act for this host. {} scopes nothing.
    deployment_of: dict[str, str] = field(default_factory=dict)
    # On a deployment: access fields of the Connection it was launched through. A Connection
    # carrying all of them belongs to this host whatever `matches` says.


@dataclass
class PermissionOption:
    key: str                           # a key of ConnectionPermissions.options
    label: str
    purpose: Purpose
    help: str = ""
    default: bool = False
    operations: list[str] = field(default_factory=list)   # Config Service operations it enables


@dataclass
class StorageLocationSchema:
    scheme: str                        # "s3", "abfss"
    example: str                       # "s3://my-bucket/projects"
    pattern: str                       # regex one rule's location must match, wildcards included
    access_modes: list[StorageAccessMode] = field(default_factory=lambda: ["read", "read_write"])
    help: str = ""
    max_rules: int = 20                # 1..20


@dataclass
class PermissionSchema:
    options: list[PermissionOption] = field(default_factory=list)
    storage: StorageLocationSchema | None = None   # None: storage rules are not a thing here


@dataclass
class NodeCapabilities:
    kinds: list[Literal["gpu", "cpu"]] = field(default_factory=lambda: ["gpu"])
    types: list[str] = field(default_factory=list)            # configured node types
    type_label: str = ""                                      # "Machine"
    pricing: list[Literal["on_demand", "spot"]] = field(default_factory=lambda: ["on_demand"])
    storage_access: list[Literal["identity", "ambient"]] = field(default_factory=list)
    type_connections: dict[str, str] = field(default_factory=dict)   # node type -> Connection id


@dataclass
class DeploymentCapabilities:
    certificate: Literal["machine", "provider"]   # in a launcher's zone
    shared_ingress: bool = False       # `check` answers an `ingress_key`
    sslip: bool = False                # can name a deployment by its own address


@dataclass
class CapabilitiesResponse:
    provider: str                      # the Connection provider slug; equals the plugin id
    display_name: str                  # "AWS"
    contract: int = CONTRACT_GENERATION
    ready: bool = False
    not_ready_reason: str = ""
    missing_fields: list[FormField] = field(default_factory=list)   # plugin settings still needed
    requires_connection: bool = False
    account: AccountSchema | None = None          # None: this provider takes no Connection
    access_methods: list[AccessMethod] = field(default_factory=list)
    host: HostIdentity | None = None
    permissions: PermissionSchema = field(default_factory=PermissionSchema)
    nodes: NodeCapabilities | None = None          # None: the provider starts no nodes
    deployments: DeploymentCapabilities | None = None
    storage: StorageCapabilities | None = None     # filled by the SDK from StorageFacet, unchanged
    operations: list[str] = field(default_factory=list)   # filled by the SDK; see section 3.2
```

Validation, enforced by the SDK before answering and asserted by the conformance kit:
`provider` matches `^[a-z0-9][a-z0-9._-]*$`; `contract` is 2; a field key matches its pattern
and is unique in its list, across `account.fields` and `account.defaults`, and between each
method's `fields` and the account's (two methods may use the same key, since a Connection has
one method); `secret` is true exactly when `kind == "secret"`; `max_length` is 1 to 2048 and
`max_rules` 1 to 20; at most one access method per kind and at most one `preferred`; every
`PermissionOption.key` is unique; `nodes` is set exactly when the class is a `NodeProvider`, and
`deployments` exactly when it is a `DeploymentProvider`. A declaration that breaks a rule is
answered 502 with the rules it breaks (`capabilities_problems`), never served. The conformance
kit also asserts that `provider` equals the plugin id.

`CapabilitiesResponse.from_dict` reads an answer without `contract` as generation 0, so a host
can tell a provider of the previous surface from one of this.

`missing_fields` come from the plugin's settings layer: `InfrastructurePlugin.readiness()`
answers `{ready, missing_fields}` with each missing setting as a `FormField`
(`FormField.from_settings`). A provider with `requires_connection` takes its account from the
Connection every call names, so before answering the SDK settles the declaration
(`settle_readiness`): a missing setting whose key is one of `account.fields` or
`account.defaults` stays in `missing_fields` with `required` false, for a plugin's own settings
page and a Connection that leaves the account empty, and no longer counts against `ready`.
`ready` is then true unless another required setting is missing or `not_ready_reason` is set.
Without `requires_connection` every required missing setting keeps the provider not ready. The
settings layer keeps describing its own fields as
`SettingsField` (`sdk:shared/settings.py:86-98`, `:472-505`), which is what a plugin's own
settings page reads and is never sent to a host.

Why each part exists:

- `account` and `access_methods[].fields` replace the role-ARN form. Today the Hub composes an
  ARN from `role_arn_template`, `account_id_label`, `account_id_pattern` and `role_name`
  (`aws:plugin.py:333-340`, `hub:static/js/connection-create.js:1386-1390`, `:2178-2191`) and the
  Config Service validates the ARN's shape (`cs:api/hub/connections.py:117`).
- `access_methods` replaces `connection_kinds` (`aws:plugin.py:237`,
  `hub:static/js/connection-create.js:1375-1377`), `connection_onboarding_unavailable`
  (`aws:plugin.py:354-356`) and the Hub's two hard-coded access cards
  (`hub:static/js/connection-create.js:359-365`). `purposes` replaces the Hub's own rule that an
  own-sign-in Connection cannot be for deployments (`hub:static/js/connection-create.js:2265`).
- `host` replaces `host_identity`, `host_principal` and `host_connection`
  (`aws:plugin.py:359-371`), which the host reads to decide which Connections act for another
  deployment (`compute:infra/manager.py:2892-2909`, `compute:connections.py:213-236`).
- `permissions` replaces `permission_switches`, `location_schema`, `storage_access_modes` and the
  default `configuration` (`aws:plugin.py:243-332`, `hub:static/js/connection-create.js:1219`,
  `:1731`, `:1822`). `access_modes` is declared because not every cloud can express every mode:
  Azure's built-in data roles have no write-without-delete.
- `nodes.type_connections` is the typed place for a provider whose node types each have a
  credential of their own (a machine and its ssh key).

**Which host a Connection acts for.** The host compares the Connection's `access` fields with
`host.matches`: a Connection that carries every key with the same value acts for this host, as
does one that carries every key of `host.deployment_of`; any other is counted as acting
elsewhere and is not offered. This is today's rule (`compute:connections.py:229-236`) with the
key named by the provider rather than by the host. The AWS plugin answers
`matches = {"setup_identity": <its principal>}`, the field a Connection records when it is set
up (`cs:api/hub/connections.py:94-105`, `aws:provider.py:323-342`). A provider with no host
principal (Azure) answers `{}`: any Compute Service the Config Service grants the Connection to
may use it, and the control is the Config Service's own, a Compute Service of the Connection's
organization or a launcher the Connection consents to in `authorized_services`
(`cs:api/hub/plugin_grants.py:43-49`).

**The method a person gets.** The host offers a method when the provider says `available` and
the host's own rules allow it. What the host does today: a demo Compute lends no identity of its
own, so it neither offers nor accepts `AMBIENT` (`compute:connections.py:179-199`; the check is
`is_demo()` only), and a host with a Config Service does not offer its built-in own-identity
Connection for new work (`compute:connections.py:159-165`). The contract requires no more of the
host; whether a method can carry deployments is the provider's `purposes`. Among what remains, a
new Connection gets the `preferred` method, else the first listed. When more than one remains
the Hub shows the derived method as a fact with the others behind "use another way"; when one
remains nothing is asked.

### 2.2 Connect an account

```python
@dataclass
class StorageRule:
    location: str                      # a URL or URL pattern in the provider's grammar; 1..256 chars
    access: StorageAccessMode = "read"


@dataclass
class ConnectionPermissions:
    """What 3LC may do in the account: the three ticks, plus provider-declared options."""

    deployments: bool = False
    nodes: bool = False
    storage: list[StorageRule] = field(default_factory=list)   # at most 20
    options: dict[str, bool] = field(default_factory=dict)     # keys from PermissionSchema.options


@dataclass
class GrantRequest:
    connection_id: str
    kind: ConnectionKind
    account: dict[str, str]
    access: dict[str, str]             # as stored, server-generated fields included
    permissions: ConnectionPermissions
    action: Literal["create", "update"] = "create"
    installed_ref: str = ""            # GrantObservation.resource_ref of the installed grant
    default_storage_root: str = ""     # the host's project root, when the caller may be offered it


@dataclass
class GrantFile:
    name: str                          # "3lc-connection.json"
    content: str
    media_type: str = "application/json"


@dataclass
class Grant:
    """What a person runs in their own account, and what it creates. Never applied by 3LC."""

    summary: str
    action: Literal["create", "update"] = "create"
    link: str = ""                     # https; opens the provider's console with everything filled in
    link_label: str = ""               # "Open AWS CloudFormation"
    steps: list[str] = field(default_factory=list)            # what to do, in order, waiting included
    creates: dict[str, list[str]] = field(default_factory=dict)   # Purpose -> plain sentences
    trusts: str = ""                   # what the grant trusts, for display
    files: list[GrantFile] = field(default_factory=list)      # for creating it by hand
    command: str = ""                  # a ready-to-paste command using `files`
    permissions: ConnectionPermissions = field(default_factory=ConnectionPermissions)   # canonical form
    access: dict[str, str] = field(default_factory=dict)      # access fields to store on the Connection
    version: str = ""                  # the grant template's version
    console_url: str = ""              # where the installed grant can be inspected
    notes: list[str] = field(default_factory=list)            # everything else: revoking, verifying


GrantUpdateStatus = Literal["pending", "ready", "applied", "no_changes", "failed"]


@dataclass
class GrantObservation:
    """The installed grant as read back from the account. Evidence, not a copy of what was asked."""

    permissions: ConnectionPermissions
    account: dict[str, str]            # the account the grant was found in
    resource_ref: str                  # opaque: a stack ARN, an ARM deployment id
    status: Literal["complete", "in_progress", "failed"]
    version: str = ""


@dataclass
class GrantUpdateRequest:
    installed: GrantObservation
    pending: ConnectionPermissions
    revision: int
    pending_revision: int


@dataclass
class GrantUpdate:
    status: GrantUpdateStatus
    detail: str = ""                   # the provider's own sentence for the state
    review_url: str = ""               # where the customer reviews and applies the change; `ready` only
    revision: int = 0
    pending_revision: int = 0


@dataclass
class StorageRoot:
    url: str
    access: Literal["read", "read_write"] = "read_write"
    role: Literal["project_root", "scan", "data"] = "project_root"


@dataclass
class VerifyRequest:
    storage_roots: list[StorageRoot] = field(default_factory=list)   # sample targets; [] samples none


@dataclass
class ConnectionVerification:
    identity: str                      # who the Connection acts as, in the provider's words
    account: dict[str, str]            # the account the provider's API says the credential is in
    account_label: str = ""
    checked: list[str] = field(default_factory=lambda: ["resolve"])
    checks: list[PreflightCheck] = field(default_factory=list)
    observation: GrantObservation | None = None
    facts: dict[str, str] = field(default_factory=dict)   # labelled rows the Hub shows under the check


@dataclass
class StorageCandidate:
    location: str                      # a rule location the editor can offer
    label: str = ""


@dataclass
class StorageCandidates:
    items: list[StorageCandidate] = field(default_factory=list)   # at most 2000
    truncated: bool = False


@dataclass
class NodeAccessRequest:
    storage_roots: list[StorageRoot] = field(default_factory=list)
    storage_access: dict[str, Any] | None = None   # setup only: the reviewed rules; None keeps the active ones
    expected_revision: str = ""        # setup only: the revision the review was based on
```

Methods on `InfrastructurePlugin`. Each is optional; overriding one lists its operation.

```python
def describe_grant(self, request: GrantRequest) -> Grant: ...
def prepare_grant_update(self, request: GrantUpdateRequest) -> GrantUpdate: ...
def verify_connection(self, request: VerifyRequest) -> ConnectionVerification: ...
def connection_node_access(self, request: NodeAccessRequest, *, apply: bool = False) -> ConnectionNodeAccess: ...
def storage_candidates(self) -> StorageCandidates: ...
```

- **`describe_grant`** is called with no Connection: it changes nothing and needs nothing of the
  account. It may read this host's own identity (2.3 rule 1) to name what the grant trusts. It
  replaces `POST /infra/connection/onboarding` and its 25-key dict (`aws:plugin.py:932-1013`,
  `aws:provider.py:6005-6067`). `action="update"` answers a link or files that change the
  existing grant in place and never a create link for an update
  (`sdk:docs/plugin-guide.md:764-771`). `permissions` is returned in canonical form: the
  provider normalises each rule's location, and what it returns is what is stored and later
  compared with an observation. `access` is what the Hub stores on the Connection before the
  person follows the link (on AWS: `role_arn`, `node_instance_profile`).
- **`prepare_grant_update`** is for a provider whose cloud has a reviewed change (a change set).
  It runs under the Connection and prepares, never executes (`aws:connection_updates.py:42-174`).
  The five states are the ones any provider can produce; a provider says more in `detail`
  (the AWS plugin's eight states collapse as `waiting`, `preparing`, `applying` into `pending`,
  and `permission_required` into `failed`). A provider without a reviewed change does not
  implement it; its update is `describe_grant(action="update")`.
- **`verify_connection`** runs under the Connection, after the SDK resolved it. A step that fails
  raises; a sample that could not be confirmed is a `warn` check; a refused operation is an
  `error` check, which the host records as a failed verification
  (`compute:infra/manager.py:2697-2705`). `account` is what the cloud says, so the Config Service
  can compare it with what was stored without knowing the provider's grammar (section 4).
  `observation` replaces `metadata.configuration_observation` (`compute:infra/manager.py:2694-2696`,
  `cs:identity/connection_configuration.py:97-102`). `facts` replaces the rest of `metadata`,
  which the host returns to the Hub with the check (`compute:infra/manager.py:2689-2691`, `:2752`);
  the AWS-only `stack` answer (`aws:plugin.py:131-151`) becomes check rows and `facts`.
- **`connection_node_access`** is unchanged in shape (`sdk:infrastructure/types.py:648-719`): it
  is how a machine on its own sign-in sets up a scoped node identity directly, when it has the
  rights to. `NodeAccessRequest` is today's `ConnectionCheckRequest` renamed, with
  `project_root_url` replaced by `storage_roots`. The inspecting `GET` has no body: it carries
  the roots as the query `storage_roots`, a JSON list of `StorageRoot` (absent means none).
- **`storage_candidates`** runs under the Connection with a discovery-only grant and returns
  names, never contents (`aws:provider.py:2485-2508`).

### 2.3 The credential seam

What a request carries, all host-owned headers a host strips from callers
(`compute:connections.py:65-74`, `:361-372`):

| Header | Carries | When |
|---|---|---|
| `x-3lc-connection` | `ConnectionBinding` as JSON | Every call that acts on an account |
| `x-3lc-connection-use` | `ConnectionUse` as JSON | Optional; the resource and the person the call is for |
| `x-tlc-bound-credential` | `SecretToken` as JSON | `SECRET` |

```python
@dataclass(frozen=True)
class ConnectionBinding:
    id: str
    provider: str
    kind: ConnectionKind
    account: dict[str, str] = field(default_factory=dict)
    access: dict[str, str] = field(default_factory=dict)       # non-secret method fields
    defaults: dict[str, str] = field(default_factory=dict)     # AccountSchema.defaults
    permissions: ConnectionPermissions | None = None           # installed; None before a first verification
    grant_ref: str = ""                                        # GrantObservation.resource_ref, when installed


class ProviderCredential(Protocol):
    """What a resolver returns. The class is the provider plugin's own; the SDK names no vendor."""

    @property
    def expires_at(self) -> str: ...   # ISO-8601, or "" when it does not expire; read-only, so a frozen dataclass conforms

    def secret_values(self) -> list[str]: ...   # scrubbed from every error answer


@dataclass(frozen=True)
class ResolveRequest:
    binding: ConnectionBinding
    use: ConnectionUse | None


Resolver = Callable[[ResolveRequest], Ambient | ProviderCredential]   # Ambient: rule 7

def register_resolver(provider: str, resolver: Resolver) -> None: ...   # KEYLESS only
def current_connection() -> ConnectionBinding | None: ...
def current_use() -> ConnectionUse | None: ...
def current_credential(service: str | None = None) -> Ambient | SecretToken | ProviderCredential | None: ...
def credential_as(cls: type[T]) -> T: ...   # the request's credential as `cls`, else CredentialUnavailable
```

`CredentialUnavailable` takes an optional `remedy`. What a resolver raises is answered before
any handler runs: `CredentialUnavailable` as 424, a typed error of 2.4 (`RateLimited`,
`PermissionDenied`) with its own status and code, and anything else as a 424 that names the
Connection and the exception's class but not its text.

The binding names no vendor concept. What a provider needs beyond the account is in `access`,
under keys the provider declares and reads: the AWS plugin's are `role_arn`, `external_id`,
`session_duration_s`, `node_instance_profile` and `setup_identity`, which are today's metadata
keys under the same names (`cs:api/hub/connections.py:105`, `:117-123`,
`aws:provider.py:371-379`). Two of them are not entered by a person: `external_id` is generated
by the Config Service for every `KEYLESS` Connection and can never be set by a caller
(`cs:api/hub/connections.py:465-466`, `:691`); a provider that needs no nonce ignores it.
`setup_identity` is written by the Hub from `host.matches` when the Connection is created
(`hub:static/js/connection-create.js:1744`).

How each kind resolves, in the worker, before the handler runs (`sdk:connections.py:545-610`):

- `AMBIENT` resolves in the SDK to `Ambient(provider, account, defaults)` and cannot be replaced
  (`sdk:connections.py:373-375`). The plugin turns it into its cloud library's default chain.
  A plugin that sees it must not fall back to anything saved in its settings. The host's
  built-in own-identity Connection carries an empty `account` (`compute:connections.py:159-166`);
  the plugin then takes the account from its own settings and asks for them in `missing_fields`.
- `KEYLESS` calls the plugin's resolver with a `ResolveRequest`, in a worker thread. The AWS
  resolver returns its own `AwsSession` holding STS session keys (`aws:provider.py:345-482`).
- `SECRET` is not resolved: the value arrives beside the binding and is bound as a
  `SecretToken`. The binding and the value must name the same Connection and provider, else 424.
  A multi-field secret (an API key and a storage key pair) is a JSON object whose keys are the
  method's `secret` fields; `SecretToken.fields()` returns it as a `dict[str, str]` and raises
  `CredentialUnavailable` for any other shape, as service tokens already do
  (`sdk:connections.py:118-124`, `:436-474`).

`AwsSession` leaves the SDK (`sdk:connections.py:282-302`); `ResolvedCredential` no longer lists
a vendor type (`sdk:connections.py:344`).

**Size.** The worker serves with uvicorn's defaults (`sdk:worker.py:876`), and the worker venvs
install uvicorn without `httptools`, so requests are parsed by h11, whose limit on a request's
whole head, the request line and all headers together, is 16 KiB
(`h11/_connection.py`, `DEFAULT_MAX_INCOMPLETE_EVENT_SIZE = 16 * 1024`; uvicorn passes it
through unless `h11_max_incomplete_event_size` is set). A request over it is answered 400 by the
server before any SDK code runs, with no sentence. So:

- The serialized `x-3lc-connection` value is at most 8 KiB. The host measures it before sending
  and refuses a larger one itself, with status 409, `code = "invalid_request"` and a sentence
  naming the Connection and saying its storage rules must be shortened. The SDK's middleware
  refuses the same way (400) so a host that did not check is still told why.
- That is why a storage rule is at most 256 characters and a Connection has at most 20: twenty
  rules of that length are under 6 KiB. Today both the Config Service and the AWS plugin allow
  50 (`cs:identity/connection_configuration.py:80`, `aws:storage_access.py:17`).
- The `x-tlc-bound-credential` value, the JSON object that holds the secret, is at most 4 KiB.
  The SDK refuses a larger one with 400.
- The SDK worker sets `h11_max_incomplete_event_size` to 64 KiB explicitly, so the limit does
  not depend on which parser a venv happens to hold. The caps above hold either way.

**Caching and refresh rules.**

1. **Host identity is ambient-only.** `HostIdentity`, and anything else a grant will trust, is
   read from the machine's own credential chain and never from a request's keys, its Connection
   or a resolved credential. A cache of it is keyed by the ambient credential itself, never by
   region or by request (`aws:provider.py:4972-4975`, `:4992-5028`). The SDK enforces the
   precondition: `GET /infra/capabilities`, `POST /infra/connection/grant` and the DNS routes
   answer 400 when any of the three Connection headers is present, so those handlers can never
   observe one. The refusal is the Connection middleware's, made by path before anything is
   resolved. `/settings` is not an infrastructure-only path, so there the refusal is the
   handler's, when a binding is present.
2. A resolved credential belongs to one request. The SDK holds it in a context variable for the
   handler and resets it after (`sdk:connections.py:600-606`); it never writes one to
   `os.environ`, a file or the plugin's settings.
3. A plugin may keep an exchanged credential until 60 seconds before its `expires_at`, in
   memory, keyed by a digest of the whole binding plus `ConnectionUse.source_identity`. A changed
   binding is a different key. It must drop the entry on any authentication failure.
4. A secret value is never cached by a plugin.
5. Clients built from a credential are cached per credential digest, never per region alone
   (`aws:provider.py:598-664`). Anything cached per account (quotas, offerings, prices that
   differ per account) is keyed by the account the credential belongs to, and skipped when that
   cannot be named (`aws:provider.py:671-680`).
6. Background work a request starts (a transfer, a bundle) keeps that request's credential and
   cannot outlive it. When the credential expires the work stops and records
   `code = "credential_expired"` with what was done so far; it is never continued on the host's
   own identity. Starting it again resumes, since a copy skips what is already there.
7. A deployment acting on the Connection it was launched through acts directly, as itself: a
   resolver returns `Ambient` for it and assumes nothing (`aws:provider.py:400-404`). A binding
   that carries `host.matches` keys with other values is refused before the cloud is asked
   (`aws:provider.py:323-342`).

### 2.4 Start a node

A provider that starts nodes adds the `NodeProvider` mixin.

```python
@dataclass
class CreateNodeRequest:
    node_id: str
    node_type: str
    token: str                         # the node agent's token; scrubbed from error answers
    kind: Literal["gpu", "cpu"] = "gpu"
    location: str = ""                 # a Locations id; "" = the Connection's default, else the plugin's
    env: dict[str, str] = field(default_factory=dict)
    agent_port: int = 8800
    ports: list[int] = field(default_factory=list)
    idle_ttl_s: float = 1800.0
    owner: str = ""
    host_id: str = ""                  # the creating host's service id, for the `3lc-host` tag
    pricing: Literal["", "on_demand", "spot"] = ""
    compute_spec: str = ""
    storage_roots: list[StorageRoot] = field(default_factory=list)
    storage_id: str = ""               # provider storage to attach (a network volume)


@dataclass
class CreateNodeResponse:
    provider_id: str
    agent_url: str = ""
    token: str = ""
    pricing: str = ""
    detail: str = ""
    hourly_rate: float | None = None
    location: str = ""                 # where the node lives, in the provider's words
    identity: str = ""                 # the node identity attached, for display
    storage_access: Literal["identity", "ambient", "none"] = "none"


@dataclass
class NodeStateResponse:               # unchanged
    state: NodeProviderState           # pending, running, exited, terminated, gone, unknown
    detail: str = ""
    bootstrap_history: list[str] = field(default_factory=list)
    bootstrap_detail: str = ""
    bootstrap_failed: bool | None = None
    bootstrap_stage: str = ""
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class NodeCheckRequest:
    node_type: str = ""
    pricing: str = ""
    location: str = ""
    storage_roots: list[StorageRoot] = field(default_factory=list)


class NodeProvider(ABC):
    @abstractmethod
    def create_node(self, request: CreateNodeRequest) -> CreateNodeResponse: ...
    @abstractmethod
    def node_state(self, provider_id: str) -> NodeStateResponse: ...
    @abstractmethod
    def delete_node(self, provider_id: str) -> NodeStateResponse: ...      # idempotent
    def check_node(self, request: NodeCheckRequest) -> PreflightResponse: ...
    def node_diagnostics(self, provider_id: str) -> NodeStateResponse: ...
```

`node.diagnostics` has no route of its own: it is the `diagnostics=true` query of the
`node.state` route. A provider that does not list it answers the plain state for that query.

- **`kind`** replaces `flavor` (`sdk:infrastructure/types.py:407`). A node is `gpu` or `cpu`; a
  deployment is not a node flavor and has its own group (2.5).
- **Location.** A node request names where it goes with one string, `location`, chosen from
  `CatalogFacet.locations()`. No node-options operation exists: a node has one placement
  dimension a person chooses, and everything finer (subnet, security group, image) is the
  plugin's own settings, as now (`aws:plugin.py:412-424`). Today a node goes where the plugin's
  saved region says and the request cannot name another (`aws:plugin.py:424`); a datacenter can
  be named only in a preflight (`sdk:infrastructure/routes.py:245-253`).
- **Idempotency.** `create_node` is idempotent on `node_id`: a repeat, after a timeout or a
  host restart, returns the resource the first call created and creates nothing. The provider
  finds it by tag. Every resource a provider creates for a node carries four tags (or labels,
  or the nearest thing the cloud has): `3lc` = `node`, `3lc-id` = `node_id`, `3lc-owner` =
  `owner`, `3lc-host` = `host_id`. A deployment's carry `3lc` = `deployment` and `3lc-id` =
  `deployment_id`. These are what a sweep and a restart-time reap search by; the AWS plugin tags
  `3lc=node`, an owner and a launcher id already (`aws:provider.py:1396-1410`, `:2188`,
  `aws:ingress.py:43`), and the host has no way today to find a node whose create call it did
  not see answered (`compute:infra/manager.py:1208-1213`).
- **Identity attach.** The request carries no identity. The provider attaches the node identity
  the Connection's grant created, which it reads from the binding's `access`
  (`aws:provider.py:1033-1070`), and says which in `identity`. It never copies a credential onto
  the node: credential variables in `env` are dropped (`aws:provider.py:1012-1031`).
- **The node's 3LC credential.** The host, not the provider, gives a worker node its credential
  for 3LC services, as `TLC_NODE_CREDENTIAL` in the create request's `env`. Under a Config Service Connection it comes with that Connection's `node.create`
  grant. With no Connection, or under the built-in Connection that acts as the host's own
  identity, it comes with an identity-only `node.create` grant whenever the host has a Config
  Service. A host without a Config Service gives the node none, and node preflight then carries
  a `node_credential` check at `warning`. The host revokes the credential when the node is deleted.
- **Worker identity selection.** A machine with more than one identity attached must tell its
  workers which to use. The provider sets that in the machine's environment at bootstrap, under
  names the node agent's allow-list passes to workers. No response field carries it. The
  allow-list is the host's (`compute:node_agent/__init__.py:170-183`); `AZURE_CLIENT_ID` and
  `AZURE_MSI_RESOURCE_ID` are added to it by the neutral data-movement work (branch
  `feature/gudbrand/node-data-movement-neutral`, `node_agent/__init__.py:189-190`). A provider
  that needs another name asks for it there. Which machine image, GPU driver and boot script a
  node gets is plugin-private and not part of this contract.
- **Storage roots** replace `ProjectStorage` (`sdk:infrastructure/types.py:239-264`,
  `compute:infra/manager.py:940-949`). They say what the node's identity should at least reach,
  and are a hint, never a boundary the provider restricts by. `storage_access` says how the node
  reaches them: through its attached `identity`, with what the machine already has (`ambient`),
  or not at all.
- **`check_node`** replaces `preflight` and `preflight_with_context`
  (`sdk:infrastructure/plugin.py:124-136`, `sdk:infrastructure/routes.py:244-253`), with the
  host's storage roots in the body rather than `project_root_url` in a query.

**Typed errors.** A provider raises one of these; the route layer answers its status with
`{detail, code, remedy, retry_after_s}`, scrubbed of `secret_values()` and of the request's own
secrets (`sdk:infrastructure/routes.py:121-162`, `:266-270`). The host branches on `code`, never
on the words of a sentence: today it searches provider sentences for `AccessDenied`
(`compute:infra/manager.py:672-688`).

| Class | Status | `code` | The host | The remedy sentence says |
|---|---|---|---|---|
| `InvalidRequest` | 400 | `invalid_request` | reports | What to change in the request |
| `PermissionDenied` (new) | 403 | `permission_denied` | reports; permanent until granted | Which permission is missing and where it is granted |
| `NotFound` | 404 | `not_found` | reports | Nothing; the detail names what is gone |
| `NotConfigured` | 409 | `not_configured` | reports | Which setting or setup step is missing |
| `Conflict` | 409 | `conflict` | reports | What is in the way |
| `CredentialUnavailable` | 424 | `credential_unavailable` | reports | Why the Connection could not be used from this host |
| `CredentialExpired` (new) | 424 | `credential_expired` | asks for a fresh grant and repeats the call once | Nothing |
| `RateLimited` (new) | 429 | `rate_limited` | repeats after `retry_after_s`, at most three times | Nothing |
| `NotSupported` | 501 | `not_supported` | reports | Nothing |
| `ProviderError` | 502 | `provider_error` | reports | Whatever the provider can say; may be empty |

`ProviderError` gains `code: str` on the class, and `remedy: str = ""` and
`retry_after_s: float | None = None` as keyword arguments. Any other exception still answers 502
through `describe_error` (`sdk:infrastructure/plugin.py:190-208`) with `code = "provider_error"`.
A plugin's own HTTP error and a body the framework could not parse keep their status and get the
code of that status. A plugin-private route answers the same body when it is mounted with
`routes.error_handlers()` and runs its work through `routes.answer`. `CredentialExpired` is
for a credential that was valid when the call started and stopped being so during it; a repeat
is safe because every create is idempotent.

**Catalog.** One typed operation replaces `gpu-catalog`, `cpu-catalog` and their untyped rows
(`sdk:infrastructure/types.py:1306-1355`, `aws:provider.py:1640-1653`):

```python
@dataclass
class Offer:
    id: str                            # the node type or deployment size to send back
    label: str = ""
    description: str = ""
    vcpus: int | None = None
    memory_gb: float | None = None
    gpu_name: str = ""
    gpu_count: int = 0
    gpu_memory_gb: float | None = None
    hourly: float | None = None        # on demand, in `currency`
    hourly_spot: float | None = None
    currency: str = "USD"              # ISO 4217 code of both prices
    available: bool | None = None      # None: the provider cannot tell
    unavailable_reason: str = ""       # quota, not offered here
    capacity: str = ""                 # the provider's words for stock, "" when unknown
    recommended: bool = False


@dataclass
class CatalogRequest:
    purpose: Literal["gpu_node", "cpu_node", "deployment"]
    location: str = ""                 # "" prices in the Connection's default location


@dataclass
class Catalog:
    offers: list[Offer] = field(default_factory=list)   # at most 500
    location: str = ""
    error: str = ""                    # a failed lookup is an empty list with the reason


@dataclass
class Location:
    id: str                            # the value sent back as `location`
    label: str = ""
    available: bool | None = None      # for the node type asked about; None: the provider cannot tell
    capacity: str = ""                 # the provider's words for stock, "" when unknown


@dataclass
class Locations:
    locations: list[Location] = field(default_factory=list)
    node_type: str = ""
    default: str = ""                  # where a request that names none goes; "" when chosen per request
    error: str = ""


class CatalogFacet(ABC):
    @abstractmethod
    def catalog(self, request: CatalogRequest) -> Catalog: ...
    def locations(self, *, node_type: str = "") -> Locations: ...
```

`Locations` takes the place of `Datacenters` (`sdk:infrastructure/types.py:1358-1398`) with
typed rows. A provider's saved placement setting and what it resolves to, which `Datacenters`
carried, are the plugin's own settings and are served by its own routes (decision 7).

### 2.5 Start a deployment

A provider that hosts deployments adds the `DeploymentProvider` mixin. A deployment is created
only by a launcher, under a `workspace.create` grant (`compute:docs/internal/deployment-lifecycle.md:3-6`).

```python
@dataclass
class PlacementChoice:
    id: str
    label: str = ""


@dataclass
class PlacementField:
    key: str                           # the key sent back in `placement`
    label: str                         # "Region", "Virtual network"
    choices: list[PlacementChoice] = field(default_factory=list)   # valid for what is selected so far
    default: str = ""
    required: bool = True


@dataclass
class CostNote:
    label: str
    description: str = ""
    hourly: float | None = None
    currency: str = "USD"
    reused: bool | None = None         # an existing shared resource would be used


@dataclass
class DeploymentOptionsRequest:
    selected: dict[str, str] = field(default_factory=dict)   # the placement chosen so far, by field key


@dataclass
class DeploymentOptions:
    placement: list[PlacementField] = field(default_factory=list)   # in the order they are chosen
    costs: list[CostNote] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


@dataclass
class DeploymentStorage:
    root: str = ""                     # the project root; "" keeps projects on the machine's disk
    scan: list[str] = field(default_factory=list)   # read-only folders to index
    data: list[str] = field(default_factory=list)   # read-only dataset locations
    create_root: bool = False          # create the root's container when it does not exist


@dataclass
class DnsZone:
    id: str                            # opaque to the host and to every provider but the DNS plugin
    name: str                          # "deploy.example.com"


@dataclass
class DeploymentHostnames:
    """Allocated by the launcher in its zone. The provider derives no name."""

    hostname: str                      # the deployment's own name; "" in the shared shape
    object_service: str                # FQDN
    compute_service: str               # FQDN
    namespace: str = ""                # the shared shape's parent name; "" otherwise


@dataclass
class DeploymentBootstrap:
    record_id: str                     # the Config Service's deployment record the machine becomes;
                                       # "" when the launcher has no Config Service
    config_service_url: str
    token: str = field(default="", repr=False)   # single use; scrubbed from error answers; "" with record_id
    launcher_service_id: str = ""


@dataclass
class DeploymentCheckRequest:
    label: str                         # the DNS label the launcher derived from the name
    size: str = ""
    placement: dict[str, str] = field(default_factory=dict)
    storage: DeploymentStorage = field(default_factory=DeploymentStorage)
    zone: DnsZone | None = None        # the launcher's zone; None when it names by address


@dataclass
class DeploymentCheck:
    ok: bool
    reason: str = ""
    ingress_key: str = ""              # opaque; same key = same shared ingress; "" = its own address
    checks: list[PreflightCheck] = field(default_factory=list)


@dataclass
class DeploymentPrepareRequest:
    deployment_id: str
    placement: dict[str, str] = field(default_factory=dict)
    namespace: str = ""
    size: str = ""
    ingress_key: str = ""              # as `check` answered it


@dataclass
class DnsRecord:
    name: str                          # FQDN, lower case, no trailing dot
    type: Literal["A", "AAAA", "CNAME", "TXT"]
    values: list[str]
    ttl: int = 60


@dataclass
class DeploymentPrepared:
    state: Literal["ready", "pending", "failed"]
    detail: str = ""
    dns_records: list[DnsRecord] = field(default_factory=list)   # must exist for `ready` to be reached
    retry_after_s: float = 5.0


@dataclass
class DeploymentRequest:
    deployment_id: str                 # the launcher's durable id; tagged on every resource
    name: str                          # as the person typed it, for display and tags
    bootstrap: DeploymentBootstrap
    hostnames: DeploymentHostnames | None = None   # None: name the deployment by its address
    ingress_key: str = ""
    size: str = ""                     # an Offer.id of catalog(purpose="deployment"); "" = default
    placement: dict[str, str] = field(default_factory=dict)
    storage: DeploymentStorage = field(default_factory=DeploymentStorage)
    compute_spec: str = ""
    env: dict[str, str] = field(default_factory=dict)
    plugins: list[str] = field(default_factory=list)   # install sources; [] installs the default roster
    owner: str = ""
    mode: Literal["full", "demo"] = "full"
    detailed_diagnostics: bool = False
    public: bool = False               # the deployment admits visitors
    seed_provider_configs: dict[str, dict[str, str]] = field(default_factory=dict)
                                       # provider settings the deployment starts with, by plugin id


@dataclass
class Address:
    type: Literal["ipv4", "ipv6", "hostname"]
    value: str


@dataclass
class DeploymentResponse:
    provider_id: str
    addresses: list[Address]           # what the launcher points the hostnames at
    endpoints: dict[str, str]          # object_service_url, compute_service_url; https
    location: str = ""
    placement: dict[str, str] = field(default_factory=dict)   # where it actually went
    hourly_rate: float | None = None
    resource_ref: str = ""             # opaque: the provider's reference to what it created
    failure: str = ""                  # resources exist but the launch failed; the launcher records, then deletes


@dataclass
class SweepItem:
    deployment_id: str
    provider_id: str = ""


@dataclass
class SweepRequest:
    keep: list[SweepItem] = field(default_factory=list)   # alive or mid-launch: never touched
    gone: list[SweepItem] = field(default_factory=list)   # dead: what they left may be removed
    locations: list[str] = field(default_factory=list)
    grace_s: float = 600.0


@dataclass
class SweepReport:
    complete: bool
    released: list[str] = field(default_factory=list)          # what was removed, for the log
    held_addresses: list[str] = field(default_factory=list)    # addresses live deployments hold here
    pending: list[str] = field(default_factory=list)           # candidates still inside the grace period
    problems: list[str] = field(default_factory=list)


@dataclass
class DeploymentInstance:
    provider_id: str                   # what `delete_deployment` takes
    deployment_id: str = ""            # the `3lc-id` tag; "" when the resource carries none
    name: str = ""
    owner: str = ""
    size: str = ""
    state: NodeProviderState = "unknown"
    location: str = ""
    addresses: list[Address] = field(default_factory=list)


@dataclass
class DeploymentListing:
    deployments: list[DeploymentInstance] = field(default_factory=list)   # at most 500


class DeploymentProvider(ABC):
    def deployment_options(self, selected: dict[str, str]) -> DeploymentOptions: ...
    def check_deployment(self, request: DeploymentCheckRequest) -> DeploymentCheck: ...
    def prepare_deployment(self, request: DeploymentPrepareRequest) -> DeploymentPrepared: ...
    @abstractmethod
    def create_deployment(self, request: DeploymentRequest) -> DeploymentResponse: ...
    @abstractmethod
    def deployment_state(self, provider_id: str, *, diagnostics: str = "") -> NodeStateResponse: ...
    @abstractmethod
    def delete_deployment(self, provider_id: str) -> NodeStateResponse: ...   # idempotent
    def retain_deployment(self, provider_id: str) -> NodeStateResponse: ...
    def list_deployments(self, *, owner: str = "") -> DeploymentListing: ...   # at most 500
    def sweep_deployments(self, request: SweepRequest) -> SweepReport: ...
```

The launch, as the launcher runs it:

1. **Options and size.** `deployment_options(selected)` answers the placement fields with the
   choices valid for what is selected so far; `catalog(purpose="deployment")` answers sizes.
   These replace `WorkspacePlacement`'s fixed regions, networks, zones and subnets
   (`sdk:infrastructure/types.py:1576-1620`), the Hub's four hard-coded selects
   (`hub:static/js/deployment-launch.js:138`) and `workspace_sizes` (`aws:plugin.py:225`).
2. **Check.** The launcher derives the label from the name and refuses a reserved or malformed
   one itself (2.6). It calls `check_deployment`, which answers provider-side refusals (a label
   too long for its ingress, a placement that cannot host one, quota) and the `ingress_key`.
   This replaces the name check, which today cannot say whether a name is free because the final
   name depends on the account and network (`aws:ingress.py:1096-1113`).
3. **Allocate.** The launcher allocates the hostnames (2.6), checks they are free in its zone and
   writes the owner record that claims them (2.6), before prepare: a prepare that runs long cannot
   lose the name to another launch. A launch that ends without a deployment removes the claim.
4. **Prepare**, only when the provider lists `deployment.prepare`. Called repeatedly until
   `ready` or `failed`. It is idempotent, may create what deployments share (a load balancer, a
   certificate), and answers the DNS records that must exist for it to finish. The launcher
   writes those records and calls again after `retry_after_s`. Every record name must be under
   `namespace`; the launcher refuses any other. These records are shared and stay when a
   deployment goes. The request carries no bootstrap token: the token is sent once, on create.
   This step exists because a provider-managed certificate is validated through the launcher's
   zone while it is being created: today the provider writes that record itself and waits
   inside the create call for up to 15 minutes (`aws:ingress.py:593-655`, `:407-418`).
5. **Create.** One call that returns when the machine exists. A launcher without a Config Service
   sends `bootstrap.record_id` and `bootstrap.token` both empty; the provider then starts the
   machine with nothing to redeem, and the route refuses a token without a record. It is idempotent on
   `deployment_id`: a repeat returns the existing machine. It answers `addresses`; the launcher
   writes its records (2.6) and records them against the deployment. With
   `certificate == "machine"` the machine requests its certificates itself once its names
   resolve to it, so its startup must wait for that rather than fail on the first attempt.
   `failure` set means resources exist and the launch did not complete: the launcher saves its
   report, removes the records and deletes (`compute:infra/manager.py:3821-3823`).
6. **Readiness** is the launcher's: both `endpoints` answer
   (`compute:docs/internal/cloud-workspace.md:22-29`), while
   `deployment_state(diagnostics="baseline")` is followed for the reason a boot fails. In a
   zone, each endpoint's host must be one of the allocated hostnames; the launcher refuses others.

Afterwards:

- **Delete.** The launcher removes the deployment's records first, then calls
  `delete_deployment`, so a name never points at an address about to be freed
  (`aws:sweep.py:19-21`). Idempotent; a refused step raises and the launcher retries. The
  Connection is optional: without one the provider acts with the host's own identity. The same
  operation deletes a deployment no registry tracks, found through `list_deployments`, and the
  launcher deleting one it finds untracked has no Connection for it
  (`compute:infra/manager.py:3020-3030`, the call the host times as `delete_untracked_node`,
  `compute:infra/provider_client.py:74`).
- **Retain** removes whatever routes traffic to a failed machine and keeps the machine
  (`aws:plugin.py:457-468`); the launcher removes the records before calling it.
- **State** with `diagnostics="detailed"` is the separately granted collector
  (`sdk:infrastructure/facets.py:244-246`).
- **List** answers every deployment the account runs, tracked by the launcher or not
  (`sdk:infrastructure/facets.py:253-261`).
- **Sweep** covers what a launch or teardown that never finished left in the account. The
  provider releases what no `keep` item owns after `grace_s`, and reports `held_addresses` so the
  launcher can tell which names in its zone point at nothing (`aws:sweep.py:14-27`,
  `compute:infra/manager.py:3257-3410`). It never touches DNS. The launcher fills `locations`
  from `DeploymentResponse.location` of the deployments it tracks on that Connection, as it does
  today (`compute:infra/manager.py:690-693`, `:3357`); the provider adds the Connection's
  default location. A location no tracked deployment was ever in is not swept.

**How a deployment starts its own nodes.** A deployment's Compute Service asks the Config
Service for grants on the Connection it was launched through, like any Compute Service of that
organization; the option that lets it carries the node operations in
`PermissionOption.operations` (`aws:plugin.py:292-301`). The binding it receives is the
`KEYLESS` one. Its plugin's resolver recognises that the machine's own identity is that
Connection's deployment identity and returns `Ambient` (2.3 rule 7), so the node is created with
the deployment's attached identity and nothing is assumed. How a plugin recognises it is its
own: the AWS plugin reads its ambient identity, matches the role name the grant created and
confirms the template's tag on it (`aws:provider.py:226-277`), and learns the node identity to
attach from an environment variable the launch set on the machine (`aws:workspace.py:415-420`,
`aws:config.py:35`). It says so in `host.deployment_of`, and offers only `AMBIENT` for new
Connections (`aws:plugin.py:223`, `:237`).

### 2.6 What the host owns

**Where names come from.** A launcher setting, `deployment-hostname-source`, with three values:

| Value | Meaning | `DeploymentRequest.hostnames` |
|---|---|---|
| `zone` | The launcher owns a DNS zone and names every deployment in it | set |
| `sslip` | The deployment is named by its own address under `sslip.io`, with a certificate obtained on the machine | `None` |
| `none` | This host launches no deployments | (no request) |

The default is `zone` when the zone settings are both set and `sslip` otherwise, which is what
the host does now (`compute:infra/manager.py:952-968`). Under `sslip` the provider derives
`os.<ip>.sslip.io` and `compute.<ip>.sslip.io` from the address it was given
(`aws:hostnames.py:24-25`, `aws:bootstrap.py:625-636`) and reports them in `endpoints`; it must
declare `deployments.sslip`, and nothing of the DNS facet is used. Everything below is the
`zone` value.

**Hostname allocation.** The launcher turns a deployment's name into a DNS label and refuses
what cannot be one: lower case, runs of other characters become one hyphen, at most 63
characters, not a punycode label, not a reserved label. These rules and the reserved list move
from the AWS plugin to the host unchanged (`aws:hostnames.py:54-179`). The names are the two
shapes that exist today, unchanged, so a deployment that is running keeps its name:

| `ingress_key` | `hostname` | `object_service` | `compute_service` | `namespace` |
|---|---|---|---|---|
| `""` | `<label>.<zone>` | `os.<label>.<zone>` | `compute.<label>.<zone>` | `""` |
| set | `""` | `os-<label>.i-<key>.<zone>` | `compute-<label>.i-<key>.<zone>` | `i-<key>.<zone>` |

In the unshared shape the launcher writes three address records, the deployment's own name
first (`aws:hostnames.py:210-241`), and an owner record `_owner.<label>.<zone>`. In the shared
shape it writes two, plus an owner record `_owner-<label>.<namespace>` (`aws:ingress.py:667-696`).
The owner record is written at allocation, before prepare, and the address records at create.

The `ingress_key` is whatever opaque string the provider returns from `check`; the launcher
does not derive it and does not interpret it. The AWS plugin returns exactly the value it
computes today from the account, region, network, its own principal and the zone's id and name
(`aws:ingress.py:241-244`, `:558`), which is why `DeploymentCheckRequest` carries the zone: an
existing shared ingress, its certificate and the names under it stay as they are. The launcher
records which Connection account each namespace belongs to and refuses a key that would put a
second account in one.

**The DNS zone.** The launcher decides every record and removes exactly what it wrote. The
write itself goes through the plugin the launcher names in a setting, with the launcher's own
identity and never a Connection. That plugin implements the DNS facet:

```python
@dataclass
class DnsChange:
    zone: DnsZone
    create: list[DnsRecord] = field(default_factory=list)
    delete: list[DnsRecord] = field(default_factory=list)


@dataclass
class DnsRecords:
    records: list[DnsRecord] = field(default_factory=list)   # at most 300 per page
    next_cursor: str = ""              # "" on the last page


class DnsFacet(ABC):
    @abstractmethod
    def list_records(self, zone: DnsZone, *, under: str = "", cursor: str = "") -> DnsRecords: ...
    @abstractmethod
    def change_records(self, change: DnsChange) -> None: ...
```

`change_records` is one atomic change of at most 100 records. A create of a name and type that
already exists with the same values succeeds; with other values it raises `Conflict` and nothing
in the change is applied, so two launches racing for a name cannot both win. A delete removes a
record only when it matches exactly as listed, and a missing one is success. Only `A`, `AAAA`,
`CNAME` and `TXT` are ever written. These are the semantics the AWS plugin implements for itself
today (`aws:hostnames.py:305-336`, `aws:ingress.py:421-435`, `:819-857`). Both routes answer 400
when a Connection header is present: the zone is the launcher's, and the customer's account has
no say over it (`aws:provider.py:210-220`). `list_records` pages with a cursor because a zone
holds every deployment the launcher ever named.

The DNS plugin and the deployment provider are independent: an Azure deployment gets its name
in a Route 53 zone because the launcher's DNS plugin is the AWS one.

**The record ledger.** The launcher must know, after a restart, which records it wrote for
which deployment. A hosted Compute Service keeps no state of its own that survives one, so the
ledger cannot be a local file there.

| Where | For | Against |
|---|---|---|
| The Config Service's deployment record | Already written by the launcher at every step, already holds provider recovery data in a bounded map (`cs:api/hub/workspaces.py:111`, `cs:identity/launch_reports.py:102-106`); survives the launcher | A launcher without a Config Service has none; a write that fails leaves records unrecorded |
| Owner `TXT` records in the zone | Written in the same atomic change as the names, so a claim cannot exist without its owner; needs no other service; the shared shape does this today (`aws:ingress.py:685-693`) | The zone lists owners, not which deployment record they belong to, without a lookup |

**Recommended: both, each for what it is good at.** The owner `TXT` record is the claim and the
source of truth for whether a name is this launcher's: its value is
`3lc-owner:v1:<launcher id>:<deployment id>`, each part written as a truncated SHA-256 digest
(of the launcher id; of the launcher id with the deployment id), so the launcher recognises its
own claims and which deployment each is for while the zone shows neither id. It is written alone at allocation, so the claim
holds through a long prepare, and deleted last, with the names. The deployment record holds the
list of records written, so a delete does not need to search the zone. A launcher without a
Config Service keeps the list on its own node record (`compute:infra/manager.py:3800-3801`).

**The zone sweep reports, and removes only what it can prove gone.** A name whose owner record
is this launcher's and whose deployment the launcher does not track is a candidate, and the sweep
reports every candidate. It removes a candidate's records only when it can prove the deployment
gone. Without a Config Service its proof is its own launches: a deployment id it launched and
whose deletion or failed launch it recorded. The Config Service would add a list of that
launcher's non-deleted deployment ids, so that any id it does not name is gone, including one
launched before the launcher's own records began. An owner record with another launcher's id, or
a value it cannot read, is never touched.

**Launcher settings.**

| Setting | Environment | Meaning |
|---|---|---|
| `compute-service.infra.deployment-hostname-source` (new) | `TLC_COMPUTE_DEPLOYMENT_HOSTNAME_SOURCE` | `zone`, `sslip` or `none`; see above for the default |
| `compute-service.infra.deployment-dns-plugin` (new) | `TLC_COMPUTE_DEPLOYMENT_DNS_PLUGIN` | The id of the plugin whose DNS facet writes the zone |
| `compute-service.infra.deployment-dns-zone-id` | `TLC_COMPUTE_DEPLOYMENT_DNS_ZONE_ID` | The zone's id, in the DNS plugin's terms |
| `compute-service.infra.deployment-dns-zone-name` | `TLC_COMPUTE_DEPLOYMENT_DNS_ZONE_NAME` | The zone's name |
| `compute-service.infra.deployment-sweep-interval` | (unchanged) | How often the zone and the accounts are swept |
| `TLC_COMPUTE_LAUNCHER_KEY` | (unchanged) | The launcher's Config Service key |

The two zone settings exist (`compute:host_options.py:100-101`, `:272-291`); they stop being
written into a create request for a provider to act on (`compute:infra/manager.py:3753-3758`).

**Also the host's:** asking the Config Service for the grant before each call; the deployment
record and its reports; choosing where a job runs; the node agent; moving data to a node. The
node agent's storage probe and copy tools understand only `s3` on the default branch
(`compute:node_agent/__init__.py:292`, `:1739`); making them neutral is host work that this
contract only supplies inputs for (`storage_roots`, `storage_access`).

### 2.7 Reach storage

`StorageFacet` and its twelve routes are unchanged (`sdk:infrastructure/facets.py:54-189`,
`sdk:infrastructure/routes.py:362-499`): they are already provider-neutral, declare their own
optional parts in `StorageCapabilities` (`sdk:infrastructure/types.py:788-857`), and act through
a Connection like every other call: each of the twelve answers 400 without one. Two things
change around them. How a location is written
moves from `connection_defaults.location_schema` to `PermissionSchema.storage`. And the upper
bound on what a Connection may reach is `ConnectionPermissions.storage`, which the provider
checks a project root and data locations against before it creates anything
(`aws:workspace.py:259-290`).

### 2.8 Timeouts, deadlines and bounds

The host's timeout per call, replacing its table (`compute:infra/provider_client.py:63-90`):

| Operation | Seconds | Today |
|---|---|---|
| `capabilities` | 180 | 180 |
| `connection.grant`, `connection.grant_update`, `connection.verify`, `connection.node_access` | 60 | 60 |
| `storage.candidates` | 30 | 30 |
| `node.check` | 60 | 60 |
| `node.create` | 900 | 900 |
| `node.state` | 180 | 180 |
| `node.diagnostics` | 10 | 10 |
| `node.delete` | 60 | 60 |
| `catalog`, `catalog.locations` | 60 | 60 |
| `deployment.options`, `deployment.check` | 60 | 60, 30 |
| `deployment.prepare`, each call | 60 | (inside create) |
| `deployment.create` | 600 | 1500 with the shared-ingress wait inside (`compute:infra/manager.py:4425`) |
| `deployment.state` | 30 | 10 to 30 |
| `deployment.delete`, `deployment.retain` | 120 | 60 to 120 |
| `deployment.list` | 60 | 60 |
| `deployment.sweep` | 300 | 300 |
| `dns` list, change | 30 | (new) |

Deadlines the launcher keeps across calls: all of `prepare` 25 minutes, readiness after create
15 minutes, the whole launch 40 minutes. These are today's
(`compute:docs/internal/deployment-lifecycle.md:11-12`).

Bounds: `list_deployments` 500 items, `storage_candidates` 2000 with `truncated`, `catalog` 500
offers, all unpaged; DNS listing paged at 300; a DNS change 100 records; a binding 8 KiB; a
storage rule 256 characters, 20 per Connection; a secret value 4 KiB.

### 2.9 Additive, with the package that needs it

Specified here so the first package leaves room for them. Each lands as an additive change with
the package named; the identity token is built, the rest are not yet.

**With Azure granted access (package I): built.**

```python
@dataclass
class IdentityTokenSpec:
    audience: str                      # the `aud` the provider's token exchange expects

# AccessMethod gains:
#   identity_token: IdentityTokenSpec | None = None     # KEYLESS only
#   paste_back: list[FormField] = []                    # asked after the grant ran; only when `grants`

@dataclass
class TokenIdentity:
    issuer: str                        # https URL of the identity-token issuer
    subject: str                       # the one subject minted for this Connection
    audience: str

# GrantRequest gains:  identity: TokenIdentity | None = None

@dataclass(frozen=True, repr=False)
class IdentityToken:
    connection_id: str
    audience: str
    token: str                         # a signed JWT; never in a repr
    expires_at: str                    # ISO-8601

# ResolveRequest gains:  identity_token: IdentityToken | None = None
```

The token travels in a fourth host-owned header, `x-3lc-connection-token`, whose value is the
JSON object `{connection_id, audience, token, expires_at}` (`encode_identity_token`), at most
8 KiB. A plugin declares the need twice, in the method's `identity_token` and in
`register_resolver(provider, resolver, token_audience=...)`, which is what the SDK's middleware
reads; the conformance kit checks that the two agree. The testing helpers take it as
`connection_headers(..., token=)` and `acting_as(..., token=)`, and `FakeProvider.require_identity_token()`
turns the fake's `KEYLESS` method into one that needs it. A plugin does not ask for it: a worker has no channel back to its host, and none is added. The provider declares
the need; the host, which already asks the Config Service for a grant before every call
(`compute:config_service.py:716-724`), obtains a token for that Connection and audience and
sends it. Rules:

- The header joins the host's `HOST_OWNED_HEADERS` (`compute:connections.py:66-71`), so a copy
  a caller sent is stripped, and the token joins the values scrubbed from every error answer on
  both sides. It is never logged, by host or SDK, at any level.
- A token lives at most 10 minutes. The host may reuse one for the same Connection and audience
  until 60 seconds before it expires. A plugin never caches a token.
- A method that declares `identity_token` and a request without the header, or with a token for
  another Connection or audience, answers 424 from the SDK. The header on a capabilities or grant
  route, or without a Connection header, answers 400, as does a repeated or malformed one; no
  answer quotes the token.
- A `KEYLESS` method with `identity_token` is offered only when the host's Config Service can
  mint tokens.
- What a plugin may rely on: the token is a JWT signed by `GrantRequest.identity.issuer`, with
  `sub` equal to its `subject`, `aud` equal to the declared audience, and at least 60 seconds of
  life left when the request arrives.
- What the SDK's middleware enforces is `register_resolver(provider, resolver,
  token_audience=...)`: the header is required, and its audience checked, exactly for a provider
  registered with a `token_audience`. The header value is the JSON object
  `{"connection_id", "audience", "token", "expires_at"}`, at most 8 KiB encoded.
- The consequence for Azure: the access token the exchange yields lives up to 24 hours and cannot
  be revoked, so a leaked one is good until it expires; it is never logged or returned. It cannot
  be renewed inside the request, since renewing needs a new identity token. So no call outlives
  it. The exchange is retried, for up to about a minute, on the two Entra codes seen right after
  a grant or a key rotation while the federated credential or the issuer's keys propagate
  (`AADSTS700213` after a grant, `AADSTS7000272` after a rotation); any other refusal is final. A transfer that would stops at expiry and records `credential_expired`
  (2.3 rule 6); the Hub offers to continue, and the copy skips what is done.
- `paste_back` values are access fields the person reads off the finished grant (a client id).
  They need access fields to be writable after create (section 4).

**With spot interruption handling.** `NodeCapabilities.interruption_notice: bool` and
`NodeStateResponse.interruption: Interruption | None`, where `Interruption` is
`{deadline: str, reason: str}`. A provider reports a node being taken, ahead of time when the
cloud gives notice and after the fact (`aws:provider.py:110-116`).

**With storage leases.** The value `"lease"` in `NodeCapabilities.storage_access` and
`CreateNodeResponse.storage_access`. The lease itself is a host and node-agent mechanism.

**With quota-aware launch.** `QuotaExceeded` (409, `quota_exceeded`) and `CapacityUnavailable`
(503, `capacity_unavailable`). Until then both are a `ProviderError` whose `remedy` says which
quota or what else to try.

**With the Hub form that uses them.** `PlacementChoice.requires: dict[str, str]`, so the Hub can
narrow choices without a round trip; `StorageLocationSchema.patterns: list[Choice]`, the
wildcard forms an editor offers; `FormField.href` and the field kind `multiline`.

**With agent nodes and provisioning by an agent.** The node-create inputs `bootstrap_specs` and
`node_tls`, and a catalog for agent nodes. They are not absorbed here: each is an optional
request field or one more `CatalogRequest.purpose`, added when that work lands.

---

## 3. Route tables

### 3.1 Plugin routes

Every route is under the plugin's worker. "Connection" says whether the host sends the binding:
*never* answers 400 when one is present, *required* answers 400 when absent. "Grant" is the
Config Service operation whose binding the host uses (`compute:config_service.py:90-107`).

| Operation id | Method and path | Request | Response | Connection | Grant |
|---|---|---|---|---|---|
| (always) | `GET /infra/capabilities` | | `CapabilitiesResponse` | never | |
| `connection.grant` | `POST /infra/connection/grant` | `GrantRequest` | `Grant` | never | |
| `connection.grant_update` | `POST /infra/connection/grant/update` | `GrantUpdateRequest` | `GrantUpdate` | required | `connection.prepare` |
| `connection.verify` | `POST /infra/connection/verify` | `VerifyRequest` | `ConnectionVerification` | required | `connection.verify` |
| `connection.node_access` | `GET /infra/connection/node-access?storage_roots=`, `POST /infra/connection/node-access` | `NodeAccessRequest` (the `GET` carries only the roots, as JSON in the query) | `ConnectionNodeAccess` | required | `connection.verify` |
| `storage.candidates` | `GET /infra/storage/candidates` | | `StorageCandidates` | required | `storage.discover` |
| `node.create` | `POST /infra/nodes` | `CreateNodeRequest` | `CreateNodeResponse` (201) | optional | `node.create` |
| `node.state` | `GET /infra/nodes/{provider_id}` | | `NodeStateResponse` | optional | `node.describe` |
| `node.delete` | `DELETE /infra/nodes/{provider_id}` | | `NodeStateResponse` | optional | `node.terminate` |
| `node.check` | `POST /infra/nodes/check` | `NodeCheckRequest` | `PreflightResponse` | optional | `node.describe` |
| `node.diagnostics` | `GET /infra/nodes/{provider_id}?diagnostics=true` (the `node.state` route) | | `NodeStateResponse` | optional | `node.describe` |
| `catalog` | `POST /infra/catalog` | `CatalogRequest` | `Catalog` | optional | `node.describe`; `connection.verify` for `purpose == "deployment"` |
| `catalog.locations` | `GET /infra/catalog/locations?node_type=` | | `Locations` | optional | `node.describe` |
| `deployment.options` | `POST /infra/deployments/options` | `DeploymentOptionsRequest` (`{selected}`) | `DeploymentOptions` | required | `connection.verify` |
| `deployment.check` | `POST /infra/deployments/check` | `DeploymentCheckRequest` | `DeploymentCheck` | required | `connection.verify`; the launch's `workspace.create` inside a launch |
| `deployment.prepare` | `POST /infra/deployments/prepare` | `DeploymentPrepareRequest` | `DeploymentPrepared` | required | the launch's `workspace.create` |
| `deployment.create` | `POST /infra/deployments` | `DeploymentRequest` | `DeploymentResponse` (201) | required | `workspace.create` |
| `deployment.state` | `GET /infra/deployments/{provider_id}?diagnostics=` | | `NodeStateResponse` | required | `node.describe`; `workspace.diagnose` for `detailed` |
| `deployment.delete` | `DELETE /infra/deployments/{provider_id}` | | `NodeStateResponse` | optional | `workspace.teardown`; without a Connection the provider acts with the host's own identity |
| `deployment.retain` | `POST /infra/deployments/{provider_id}/retain` | | `NodeStateResponse` | required | `workspace.retain` |
| `deployment.list` | `GET /infra/deployments?owner=` | | `DeploymentListing` | optional | |
| `deployment.sweep` | `POST /infra/deployments/sweep` | `SweepRequest` | `SweepReport` | optional | `node.describe` |
| `storage` | the twelve `/infra/storage*` routes | unchanged | unchanged | required | `storage.*` |
| `dns` | `GET /infra/dns/records?zone_id=&zone_name=&under=&cursor=` | | `DnsRecords` | never | |
| `dns` | `POST /infra/dns/changes` | `DnsChange` | `{}` | never | |
| (settings) | `GET`, `POST /settings` | unchanged | unchanged | never | |

On the grant column, where it is not the obvious one:

- **Node checks and catalogs ride `node.describe`.** They only read, and a `node.create` grant
  would mint an identity for a node nobody has asked for yet; this is the host's
  `PREFLIGHT_OPERATION` (`compute:infra/manager.py:225-228`, `:3187`).
- **Deployment options, the interactive check and sizes ride `connection.verify`**, as
  placement and sizes do today (`compute:infra/manager.py:3129-3131`). They cannot ride `node.describe`: they are asked
  by a launcher, and the Config Service refuses a launcher every operation but
  `workspace.create`, `connection.verify`, `connection.prepare` and `storage.discover`
  (`cs:api/hub/plugin_grants.py:829-833`, `cs:db/models/connection.py:113`).
- **The check inside a launch rides the launch's own `workspace.create` grant**, as prepare
  does. A launch may run in the background, where no person's bearer is held to ask for a
  `connection.verify` grant; the name check a person runs while filling the form is the
  interactive one and rides `connection.verify`.
- **Prepare rides the launch's own `workspace.create` grant.** The launcher asks for that grant
  once, when the launch starts, and holds its bootstrap token in memory
  (`compute:infra/manager.py:3671-3672`, `:3736-3745`). `workspace.create` requires the
  deployment's `resource_id` and mints the single-use token
  (`cs:api/hub/plugin_grants.py:39-43`, `:555-564`, `:933-934`); a repeat for the same
  deployment while it is still `creating` and unredeemed re-arms the token rather than failing,
  which is what lets a prepare longer than a grant's five minutes go on. Prepare uses the
  grant's binding and never sees the token. Whether prepare should have an operation of its own
  is open decision 2.

### 3.2 Required and optional

- `GET /infra/capabilities` is the only route every infrastructure plugin serves.
- A `NodeProvider` must serve `node.create`, `node.state` and `node.delete`. A
  `DeploymentProvider` must serve `deployment.create`, `deployment.state` and
  `deployment.delete`. A `StorageFacet`, `CatalogFacet` or `DnsFacet` must serve its abstract
  methods. A plugin may be any combination, including none (a DNS-only plugin).
- Everything else is optional.

**How optionality is declared.** The SDK fills `capabilities.operations` with the id of every
operation whose route it mounted (`provided_operations`, in the order of `OPERATIONS`), and
mounts a route only when the plugin's class provides the method: a mixin's abstract methods, or
an optional method the class overrides. This is how the
sign-in routes are mounted today (`sdk:infrastructure/routes.py:643-677`) and it replaces both
`facets` (`sdk:infrastructure/plugin.py:227-229`) and the nine booleans the AWS plugin puts in
`extra` (`aws:plugin.py:226-236`), which the host reads by name
(`compute:infra/manager.py:1693`, `:1696`, `:2791`, `:2814`, `:2871`, `:2877`, `:3239`, `:3304`).
The host calls an operation only when it is listed; an unlisted one is never probed, and a
route that is not mounted answers 404. An operation with nothing to say (a provider with no
checks) is simply not implemented.

Each listed `StorageItem` may say what the request's Connection may do in it, so the Hub's Data
page offers only what works without reading a cloud policy
(`hub:templates/data/index.html:285-300`):

```python
@dataclass
class StorageGrant:
    url: str                    # covers this location and everything under it
    read: bool = False
    write: bool = False
    delete: bool = False


@dataclass
class StorageAccess:
    browse: bool = False        # names may be listed
    delete_location: bool = False   # the storage itself may be deleted
    bundle: bool = False        # a bundle may be built from it
    grants: list[StorageGrant] = field(default_factory=list)


# StorageItem gains:  access: StorageAccess | None = None
```

`access` is `None` when the provider does not project its policies (a Connection with no
installed storage rules, a provider without rules): the provider's `StorageCapabilities` then say
what works, and the storage routes still authorize every call. `to_dict` omits it when `None`.

### 3.3 The fate of `extra`

`CapabilitiesResponse.extra`, `StorageItem.extra`, `Region.extra`, `StorageListing.extra`,
`PresignResponse.extra` and `WorkspaceInstance.extra` are removed
(`sdk:infrastructure/types.py:897`, `:982`, `:1019`, `:1051`, `:1168`, `:1414`), and nothing
free-form replaces them on capabilities or on a deployment answer. `WorkspaceInstance` is part
of the transitional surface (6.2) and keeps its `extra` until that surface goes. A value the
host or the Hub needs is a typed field: the one such value the removed maps carried is whether a
storage accepts the browser's cross-origin upload, which the Hub's Data page reads from a
presign answer (`hub:templates/data/index.html:1291`), so it is `PresignResponse.cors:
bool | None`. What a Connection may do in each listed storage is the typed
`StorageItem.access` (2.7), not a provider's subclass. A value only the plugin's own page needs is served by the plugin's own
routes (`sdk:infrastructure/plugin.py:66-70`), which the host proxies and never calls. The one
labelled-rows map left is `ConnectionVerification.facts`, whose consumer is the Hub's
verification panel.

### 3.4 What the Hub calls on the Compute Service

The Hub never calls a plugin's `/infra` routes for these flows; it calls the Compute Service,
which authorizes, asks for the grant and calls the plugin. Paths stay as they are wherever the
meaning does; "Changes" says what does not.

| Hub calls | Where | Reaches | Changes |
|---|---|---|---|
| `GET /api/infra/capabilities` | `compute:routes/infra.py:544` | `capabilities` | The typed payload. A visitor is sent `account`, `access_methods`, `permissions`, `deployments`, `operations` and `host.matches`, replacing the allow-list by `extra` key (`compute:routes/infra.py:452-476`) |
| `GET /api/infra/connections` | `:725` | (Config Service) | None |
| `POST /api/infra/connections/onboarding` | `:847` | `connection.grant` | Body `{plugin_id, connection_id, action?, permissions?}`; the host reads `kind`, `account`, `access` and `installed_ref` from the Connection instead of taking `external_id` and `metadata` from the browser. Answer is `Grant` |
| `POST /api/infra/connections/prepare-update` | `:820` | `connection.grant_update` | Answer is `GrantUpdate` |
| `POST /api/infra/connections/{id}/verify` | `:743` | `connection.verify` | Answer gains `account`, `account_label`, `facts`; loses `metadata` |
| `GET`, `POST /api/infra/connections/node-access` | `:768`, `:785` | `connection.node_access` | None |
| `GET /api/infra/storage/{plugin}/candidates` | `:1312` | `storage.candidates` | Items are `{location, label}` |
| `POST /api/infra/preflight`, `GET /api/infra/nodes/preflight` | `:567`, `:879` | `node.check` | `location` replaces `datacenter`; `pricing` added |
| `POST /api/infra/nodes` | `:911` | `node.create` | `kind` replaces `flavor`; `location` added; a deployment is refused here |
| `GET /api/infra/nodes/catalog?plugin_id=&connection_id=&kind=gpu\|cpu&location=` | `:928` | `catalog(purpose="gpu_node"` or `"cpu_node")` | Answer is `Catalog` |
| `GET /api/infra/nodes/catalog/locations?plugin_id=&connection_id=&node_type=` | `:955` | `catalog.locations` | Answer is `Locations` |
| `GET`, `DELETE /api/infra/nodes/{id}` and the node file routes | `:960`-`:1173` | `node.state`, `node.delete`, the node agent | None |
| `GET /api/infra/{plugin}/placement` | `:510` | `deployment.options` | Becomes `POST` with `{connection_id, selected}`; answer is `DeploymentOptions` |
| `GET /api/infra/{plugin}/sizes` | `:527` | `catalog(purpose="deployment")` | `location` replaces `region`; answer is `Catalog` |
| `GET /api/workspaces/name-check` | `compute:routes/workspaces.py:322` | the launcher's label rules, zone lookup and `deployment.check` | Takes `connection_id` and the placement, so the answer's `hostname` is final |
| `POST /api/workspaces` | `compute:routes/workspaces.py:201` | check, allocate, `deployment.prepare`, `deployment.create` | `placement` object replaces `region`, `vpc_id`, `subnet_id`, `availability_zone` (`:242-245`) |
| `GET /api/workspaces`, `GET /api/workspaces/{id}`, `POST /api/workspaces/{id}/delete` | `compute:routes/workspaces.py:293`, `:347`, `:356` | the registry, `deployment.state`, `deployment.delete` | None |
| `GET /api/infra/workspaces`, `DELETE /api/infra/workspaces/{plugin}/{provider_id}` | `:1459`, `:1477` | `deployment.list`, `deployment.delete` | None |
| The `/api/infra/storage*` routes | `:1187`-`:1446` | `storage` | `POST /api/infra/storage/{plugin}` no longer takes `credentials` |
| `GET /api/infra/role-setup/{plugin}`, the three `/api/infra/login/{plugin}*` routes, `POST /api/infra/storage/{plugin}/discover` | `:1494`-`:1513`, `:1326` | (removed) | Removed with the owner-credentials facet |
| The plugin's own catalog page | proxied plugin route | `catalog` | `POST /infra/catalog` replaces `GET /infra/gpu-catalog` and `/infra/cpu-catalog` |

---

## 4. Config Service shape

**Proposal for the Config Service owner.** This section says what the contract needs of the
Config Service and one way to give it. The storage model and the migration are that service's
to design.

### 4.1 What the contract needs

1. A Connection for any provider slug, not only `aws` (`cs:api/hub/connections.py:86`, `:685-686`).
2. An account reference, access fields and defaults stored without the Config Service knowing
   their grammar (`cs:api/hub/connections.py:115-123`).
3. Permissions and their observation without bucket grammar or CloudFormation checks
   (`cs:identity/connection_configuration.py:28-102`, `:188-204`).
4. A deployment record that takes any provider's placement keys and resource reference
   (`cs:identity/launch_reports.py:65-70`, `cs:api/hub/workspaces.py:128`).

Later, with Azure granted access (2.9): access fields writable after create and before a first
verification; a subject per `KEYLESS` Connection; a signed identity token on request.

### 4.2 Proposed request and stored shape

```python
FieldMap = dict[str, str]     # at most 16 keys; key ^[a-z][a-z0-9_]{0,39}$; value 1..2048 chars, no control characters


class StorageRule(StrictModel):
    location: str = Field(min_length=1, max_length=256)   # no control characters, no credentials
    access: Literal["read", "read_write_no_delete", "read_write"]


class ConnectionPermissions(StrictModel):
    deployments: bool = False
    nodes: bool = False
    storage: list[StorageRule] = Field(default_factory=list, max_length=20)   # canonical order, deduplicated
    options: dict[str, bool] = Field(default_factory=dict, max_length=16)


class CreateConnection(BaseModel):
    provider: str                      # ^[a-z0-9][a-z0-9._-]*$, as today; no fixed list
    kind: Literal["AMBIENT", "KEYLESS", "SECRET"]
    account: FieldMap = {}
    access: FieldMap = {}
    defaults: FieldMap = {}
    # name, owner_type, owner_id, allowed_ops, allowed_plugins, authorized_services, secret_value: unchanged


class GrantObservation(StrictModel):
    permissions: ConnectionPermissions
    account: FieldMap
    resource_ref: str = Field(max_length=2048)
    status: Literal["complete", "in_progress", "failed"]
    version: str = Field(default="", max_length=64)


class VerificationReport(BaseModel):
    ok: bool
    grant_id: uuid.UUID | None = None
    identity: str | None = None
    account: FieldMap | None = None    # what the provider's API said
    checked: list[str] = []
    detail: str | None = None
    observation: GrantObservation | None = None
```

Validation the Config Service keeps, all of it provider-neutral:

- `external_id` is a reserved key of `access`: generated here for every `KEYLESS` Connection,
  never settable or changeable by a caller, shown only to a Connection administrator, exactly
  as now (`cs:api/hub/connections.py:465-466`, `:585-586`, `:691`).
- `account` and `access` are immutable from create, except the keys that are not binding today
  and stay changeable: `session_duration_s`, `node_instance_profile`, `setup_identity`
  (`cs:api/hub/connections.py:91`, `:779-782`). `defaults` may change. A rebind is a new
  Connection.
- A verification is recorded as `VERIFIED` only when the reported `account` equals the stored
  one, key for key. This replaces the ARN arithmetic of `identity_failure`
  (`cs:identity/connection_configuration.py:150-164`).
- An observation is recorded as installed only when its `account` equals the stored one and its
  `status` is `complete`; the revision and generation checks stay as they are
  (`cs:identity/connection_configuration.py:167-227`). Whether the reference belongs to the
  account is the provider's check, made before it reports.
- `setup_service_id` stays as it is (`cs:api/hub/connections.py:106-112`). Which host may use a
  `KEYLESS` Connection whose provider names no host principal is decided here and nowhere else:
  a Compute Service of the Connection's organization, or a launcher listed in
  `authorized_services` (`cs:api/hub/connections.py:145-149`, `cs:api/hub/plugin_grants.py:43-49`).
- `SECRET` rules are unchanged; a multi-field secret is one sealed JSON value.

What is no longer validated here: the shape of an account id, a role ARN, a bucket name, a
stack ARN, a region code. `PROVIDERS` and the three per-kind metadata models go
(`cs:api/hub/connections.py:86`, `:115-138`); `CloudConfiguration` and its bucket scopes become
`ConnectionPermissions` (`cs:identity/connection_configuration.py:28-94`).

The grant response's `connection` gains `account`, `access`, `defaults`, `permissions` and
`grant_ref` beside what it carries today (`cs:identity/connections.py:116-131`).

### 4.3 Deployment record

`placement` takes any `FieldMap` (`cs:identity/launch_reports.py:65-70`,
`compute:config_service.py:884`); `stack_id` is read as an opaque resource reference
(`cs:api/hub/workspaces.py:128`, `cs:db/models/workspace.py:119`); the bounded `ingress` map
holds the launcher's record ledger (2.6).

### 4.4 Existing Connections and older services

- **Stored rows.** Every stored cloud Connection is an AWS one whose `metadata` holds
  `role_arn`, `external_id` and optionally `region`, `session_duration_s`,
  `node_instance_profile`, `setup_identity` and `setup_service_id`
  (`cs:api/hub/connections.py:94-128`). A one-time back-fill gives each row:
  - `account = {account_id}`, read out of the ARN;
  - `access` = the same keys under the same names, `region` and `setup_service_id` excepted, so
    the AWS plugin reads `role_arn`, `external_id`, `session_duration_s`,
    `node_instance_profile` and `setup_identity` from `access` as it reads them from `metadata`
    now;
  - `defaults = {location: <region>}` where a region was stored;
  - `permissions` from an installed `storage_access` (a bucket scope becomes
    `s3://bucket/prefix`, a pattern `s3://prefix*`, an account scope `s3://*`). A row with more
    than 20 rules is left as it is and flagged for its owner; none is expected.
  `node_instance_profile` is copied only where the row has one. A row without it was set up
  from a grant that created no node role: it is not given a derived name, and the plugin goes on
  refusing a node or a deployment that needs the node role, as now (`cs:api/hub/connections.py:120-123`,
  `aws:workspace.py:139-142`).
  Nothing a row identifies changes, so its grants resolve to the same role.
- **Reading back unchanged.** For an `aws` row the API keeps answering `metadata` and
  `configuration_state` exactly as now, computed from the new columns, and keeps accepting the
  current create and patch bodies for `provider == "aws"`. A client that only knows the current
  shape sees no difference.
- **Older services.** No released Compute Service reads Connections: the last release is 1.1
  (`compute:CHANGELOG.md:151`), which has no infrastructure manager and no Connections client.
  The 1.2 line is the first that does; it reads `connection.metadata` and
  `connection.configuration` from a grant (`compute:config_service.py:609-632`). If 1.2 is
  released, the Config Service must keep emitting those two keys for `aws` Connections for as
  long as 1.2 is supported. A 1.2 Compute Service lists Connections by provider
  (`compute:connections.py:296-304`), so a Connection of another provider is invisible to it.

---

## 5. Mapping tables

### 5.1 Every current shape and its place

**SDK core and facets**

| Today | Where it is | Becomes |
|---|---|---|
| `InfrastructurePlugin.create_node` / `node_state` / `delete_node` | `sdk:infrastructure/plugin.py:90-120` | `NodeProvider` mixin, same three methods |
| `preflight`, `preflight_with_context` | `sdk:infrastructure/plugin.py:124-136` | `NodeProvider.check_node` |
| `connection_check`, `connection_check_with_context` | `sdk:infrastructure/plugin.py:138-155` | `verify_connection` |
| `connection_node_access` | `sdk:infrastructure/plugin.py:157-164` | unchanged; listed as `connection.node_access` |
| `node_diagnostics` | `sdk:infrastructure/plugin.py:166-172` | `NodeProvider.node_diagnostics` |
| `CapabilitiesResponse.node_types`, `pricing`, `node_type_label` | `sdk:infrastructure/types.py:884-890` | `nodes.types`, `nodes.pricing`, `nodes.type_label` |
| `flavors` | `sdk:infrastructure/types.py:885` | presence of `nodes` and `deployments` |
| `missing`, `missing_fields` | `sdk:infrastructure/types.py:887-888` | `missing_fields` (the key list is derived) |
| `region` | `sdk:infrastructure/types.py:891` | the binding's `defaults`; `Catalog.location` |
| `workspace_fields` | `sdk:infrastructure/types.py:892` | removed with the owner-credentials facet |
| `facets`, `lists_workspaces`, `facets_reported` | `sdk:infrastructure/types.py:894-900` | `operations` |
| `requires_connection` | `sdk:infrastructure/types.py:896` | unchanged |
| `extra` | `sdk:infrastructure/types.py:897` | typed fields |
| `SettingsField` | `sdk:infrastructure/types.py:185-232` | On the wire, `FormField` (`placeholder` is `example`; `href` returns later). The type stays as the settings layer's own description of a field |
| `ProjectStorage` | `sdk:infrastructure/types.py:239-264` | `storage_roots: list[StorageRoot]` |
| `CreateNodeRequest.flavor`, `.workspace` | `sdk:infrastructure/types.py:407-413` | `kind`; `DeploymentRequest` |
| `CreateNodeResponse.services`, `.ingress`, `.managed_by`, `.placement` | `sdk:infrastructure/types.py:478-485` | `DeploymentResponse.endpoints`; the launcher's ledger; removed; `DeploymentResponse.placement` |
| `ConnectionCheckRequest` | `sdk:infrastructure/types.py:700-719` | `VerifyRequest`, `NodeAccessRequest` |
| `ConnectionCheckResponse` | `sdk:infrastructure/types.py:722-766` | `ConnectionVerification` |
| `GpuCatalog`, `CpuCatalog` | `sdk:infrastructure/types.py:1306-1355` | `Catalog` with a `purpose` |
| `Datacenters` | `sdk:infrastructure/types.py:1358-1398` | `Locations` |
| `WorkspaceFacet` | `sdk:infrastructure/facets.py:230-261` | `DeploymentProvider` |
| `WorkspaceInstance`, `WorkspaceListing` | `sdk:infrastructure/types.py:1404-1459` | `DeploymentListing` |
| `WorkspacePlacement` | `sdk:infrastructure/types.py:1576-1620` | `DeploymentOptions` |
| `StorageFacet` and its types | `sdk:infrastructure/facets.py:54-189` | unchanged, minus `extra` |
| `LegacyOwnerCredentialsFacet` and its types | `sdk:infrastructure/facets.py:264-373`, `sdk:infrastructure/legacy.py` | removed (section 5.4) |
| `Ambient`, `SecretToken` | `sdk:connections.py:269-341` | kept; `Ambient` gains `account` and `defaults`, `SecretToken` gains `fields()` |
| `AwsSession` | `sdk:connections.py:282-302` | moves into the AWS plugin as its `ProviderCredential` |
| `ConnectionBinding.metadata`, `.configuration` | `sdk:connections.py:131-184` | `account`, `access`, `defaults`; `permissions` |
| `register_resolver(provider, kind, fn(binding))` | `sdk:connections.py:358-376` | `register_resolver(provider, fn(ResolveRequest))` |
| `resolve()`, `connection_middleware` | `sdk:connections.py:379-397`, `:545-610` | same roles; also refuse Connection headers on ambient-only routes and an oversized binding |
| `x-3lc-connection`, `x-3lc-connection-use`, `x-tlc-bound-credential` | `sdk:connections.py:94-111` | unchanged |
| `ProviderError` and subclasses | `sdk:infrastructure/errors.py:39-76` | gain `code`, `remedy`, `retry_after_s`; three new subclasses |

**`WorkspaceRequest` fields** (`sdk:infrastructure/types.py:290-309`)

| Field | Becomes |
|---|---|
| `project_root_url` | `DeploymentRequest.storage.root` |
| `project_scan_urls` | `storage.scan` |
| `data_buckets` | `storage.data` |
| `create_bucket` | `storage.create_root` |
| `mode` | `mode` |
| `name` | `name`, and the launcher's label |
| `bootstrap_plugins` | `plugins` |
| `workspace_id` | `bootstrap.record_id` |
| `config_service_url` | `bootstrap.config_service_url` |
| `bootstrap_token` | `bootstrap.token` |
| `launcher_service_id` | `bootstrap.launcher_service_id` |
| `region`, `vpc_id`, `subnet_id`, `availability_zone` | `placement[...]`, keys the provider's options declared |
| `dns_zone_id`, `dns_zone_name` | `DeploymentCheckRequest.zone` for the ingress key; on create the launcher sends `hostnames` |
| `detailed_diagnostics` | `detailed_diagnostics` |
| `public` | `public` |
| `seed_provider_configs` | `seed_provider_configs`, the settings themselves by plugin id rather than a flag |
| transient `credentials`, `provider_configs` | removed with the owner-credentials facet |

**The AWS plugin's private routes and `extra` keys**

| Today | Where it is | Becomes |
|---|---|---|
| `POST /infra/connection/onboarding` | `aws:plugin.py:1029-1031` | `connection.grant` |
| `POST /infra/connection/prepare-update` | `aws:plugin.py:1033-1037` | `connection.grant_update` |
| `GET /infra/storage-candidates` | `aws:plugin.py:1039-1045` | `storage.candidates` |
| `GET /infra/deployments/name-check` | `aws:plugin.py:1047-1053` | the launcher's own label and zone check, plus `deployment.check` |
| `POST /infra/deployments/sweep` | `aws:plugin.py:1055-1057` | `deployment.sweep` for the account; the launcher sweeps the zone |
| `connection_onboarding`, `connection_configuration`, `connection_update_preparation`, `connection_node_access`, `deployment_name_check`, `deployment_sweep`, `workspace_data_locations`, `workspace_scan_urls`, `workspace_storage_inherits_connection` | `aws:plugin.py:226-236` | `operations`; the three `workspace_*` flags are always true in generation 2 and disappear |
| `connection_kinds`, `connection_onboarding_unavailable` | `aws:plugin.py:237`, `:356` | `access_methods[].available`, `.unavailable_reason` |
| `connection_defaults.role_name`, `role_name_max_length`, `role_name_help` | `aws:plugin.py:239-240`, `:342` | a `role_name` field of the `KEYLESS` method |
| `role_arn_template`, `account_id_label`, `account_id_pattern`, `account_id_hint`, `account_help` | `aws:plugin.py:333-340` | `account.fields`; `describe_grant` answers `access.role_arn` |
| `node_instance_profile_template`, `deployment_instance_profile_template` | `aws:plugin.py:334-335` | `describe_grant` answers `access.node_instance_profile`; the deployment profile is derived in the plugin, as now |
| `location_schema`, `location_fields`, `storage_access_modes`, `storage_access_max_rules`, `storage_access_max_prefix_length` | `aws:plugin.py:241-320` | `permissions.storage` |
| `permission_switches` | `aws:plugin.py:270-312` | `permissions.options` |
| `configuration` (defaults) | `aws:plugin.py:321-332` | `PermissionOption.default` |
| `storage_candidates` | `aws:plugin.py:313` | `operations` lists `storage.candidates` |
| `template_version` | `aws:plugin.py:336` | `Grant.version`, `GrantObservation.version` |
| `summary`, `quick_create_help`, `cloud_name`, `setup_help` | `aws:plugin.py:341-345` | `AccessMethod.summary`, `Grant.steps`, `display_name` |
| `host_identity`, `host_principal` | `aws:plugin.py:359-365` | `host.display`, `host.matches` |
| `host_connection`, `host_is_deployment` | `aws:plugin.py:242`, `:366-371` | `host.deployment_of` |
| `not_ready_reason` | `aws:plugin.py:351` | `not_ready_reason` |
| `workspace_sizes` | `aws:plugin.py:225` | `catalog(purpose="deployment")` |
| `AwsConnectionCheckResponse.stack` | `aws:plugin.py:131-151` | check rows, `observation`, `facts` |
| `_ambient_route53` and every Route 53 call | `aws:provider.py:210-220`, `:3758-3844`, `aws:ingress.py:647`, `:693`, `:824`, `aws:sweep.py:439` | the AWS plugin's `DnsFacet`, called by the launcher |
| `hostnames.py` label rules and reserved labels | `aws:hostnames.py:54-179` | the host |
| `ingress.identity` (the namespace hash) | `aws:ingress.py:241-244`, `:558` | computed the same way, returned as `DeploymentCheck.ingress_key` |
| `_trust_statements`, `_connection_template` | `aws:provider.py:3226`, `:5530` | stay in the plugin, behind `describe_grant` |

**Host**

| Today | Where it is | Becomes |
|---|---|---|
| `caps.raw.get(...)` for nine flags | `compute:infra/manager.py:1693-3304` | `operations` |
| `host_scope` reading `host_principal`, `host_connection` | `compute:infra/manager.py:2892-2909` | `capabilities.host` |
| `acts_elsewhere` comparing `metadata` with `SCOPE_KEY` | `compute:connections.py:83-87`, `:213-236` | compares `access` with `host.matches` and `host.deployment_of`; names no key |
| `connection_onboarding(kind, external_id, metadata, stack_id, ...)` | `compute:infra/manager.py:2829-2890` | builds a `GrantRequest` from the Connection |
| `verify_connection` reading `metadata.configuration_observation` | `compute:infra/manager.py:2694-2696` | reads `observation`, reports `account` |
| `_is_permission_refusal` matching words | `compute:infra/manager.py:672-688` | `code == "permission_denied"` |
| `_deployment_dns_zone` injected into the request | `compute:infra/manager.py:952-968`, `:3753-3758` | the launcher's DNS module |
| `check_deployment_name` | `compute:infra/manager.py:3209-3251` | label rules and zone lookup in the host, then `deployment.check` |
| `sweep_deployment_names` | `compute:infra/manager.py:3257-3410` | zone sweep in the host, `deployment.sweep` per Connection |
| `delete_node` of an untracked instance, timed as `delete_untracked_node` | `compute:infra/manager.py:3020-3030`, `compute:infra/provider_client.py:74` | `deployment.delete` on a `provider_id` from `deployment.list` |
| `_WORKSPACE_KEYS` | `compute:infra/manager.py:250-269` | `DeploymentRequest` fields |
| `_provision` for a workspace | `compute:infra/manager.py:3716-3824` | check, allocate, prepare, create, write records |
| `WORKSPACE_PLACEMENT_KEYS` | `compute:config_service.py:884` | any placement keys |
| `report_workspace(stack_id=...)` | `compute:config_service.py:890-969` | sends `DeploymentResponse.resource_ref` |
| `deployment-dns-zone-id`, `-name` options | `compute:host_options.py:100-101` | kept; `deployment-hostname-source` and `deployment-dns-plugin` added |
| `ProviderClient` and `TIMEOUTS` | `compute:infra/provider_client.py:63-90` | section 2.8 |
| Visitor capability allow-list by `extra` key | `compute:routes/infra.py:452-476` | by typed field (3.4) |

**Config Service**

| Today | Where it is | Becomes |
|---|---|---|
| `PROVIDERS = ("aws",)` | `cs:api/hub/connections.py:86` | any slug |
| `KeylessMetadata.role_arn`, `session_duration_s` | `cs:api/hub/connections.py:117-119` | `access.role_arn`, `access.session_duration_s`, unvalidated here |
| `KeylessMetadata.node_instance_profile` | `cs:api/hub/connections.py:120-123` | `access.node_instance_profile`; copied by the back-fill only where present (4.4) |
| `region` on all three models | `cs:api/hub/connections.py:118`, `:128`, `:136` | `defaults.location` |
| `SecretMetadata.endpoint_url` | `cs:api/hub/connections.py:137-138` | `access.endpoint_url` |
| `setup_identity`, `setup_service_id` | `cs:api/hub/connections.py:94-112` | `access.setup_identity`; unchanged |
| `external_id` in metadata | `cs:api/hub/connections.py:465-478` | the reserved `access.external_id` |
| `BINDING_FIELDS` | `cs:api/hub/connections.py:91` | `account` and `access` immutable, three named keys excepted |
| `CloudConfiguration`, `StorageAccess`, the three scopes | `cs:identity/connection_configuration.py:28-94` | `ConnectionPermissions` |
| `ConfigurationObservation` | `cs:identity/connection_configuration.py:97-102` | `GrantObservation` |
| `identity_failure` | `cs:identity/connection_configuration.py:150-164` | `account` equality |
| `Placement` | `cs:identity/launch_reports.py:65-70` | any `FieldMap` |

**Hub**

| Today | Where it is | Becomes |
|---|---|---|
| Two hard-coded access cards | `hub:static/js/connection-create.js:359-365` | rendered from `access_methods` |
| Account id and role name form | `hub:static/js/connection-create.js:2178-2191` | rendered from `account.fields` and the method's `fields` |
| `composedRoleArn` | `hub:static/js/connection-create.js:1386-1390` | removed; `Grant.access` |
| Reading `connection_defaults` | `hub:static/js/connection-create.js:814-824` | reads the typed capabilities |
| Bucket-name editor | `hub:static/js/connection-create.js:197`, `:284`, `:1812` | location editor from `permissions.storage` |
| Onboarding panel reading 25 keys | `hub:static/js/connection-create.js:692-701`, `:1949-2091` | renders `Grant` |
| Four placement selects | `hub:static/js/deployment-launch.js:138` | rendered from `DeploymentOptions.placement` |
| `location(placement)` | `hub:static/js/deployment-launch.js:127-130` | `DeploymentResponse.location` |
| Pasted keys, sign-in, role grant, bucket discovery in the launch wizard | `hub:templates/settings/workspace.html:2224-2640` | removed (section 5.4) |

### 5.2 The Azure reading of every operation

| Operation | Azure |
|---|---|
| `capabilities.account` | `fields`: `tenant_id`, `subscription_id`, `resource_group`. `defaults`: `location` |
| `AMBIENT` method | Available when the Azure credential chain finds a sign-in (an `az login` session, or a managed identity). `purposes`: nodes, storage. The plugin opts its own client and the 3LC core into the CLI sign-in. With the host's built-in own-identity Connection the account comes from the plugin's settings. |
| `KEYLESS` method | `fields`: `name_prefix`. `identity_token.audience` `api://AzureADTokenExchange`. `paste_back`: the template's `connection` output less the account fields (`location`, `control_client_id`, `deployment_identity_id`, `node_identity_id`, `node_identity_client_id`, `storage_account`, `container`, `subnet_id`, `issuer`, `subject`). Offered by the host only when its Config Service mints tokens. |
| `host` | `matches = {}`: no host principal; the Config Service decides which host may use a Connection. `deployment_of` on a deployment VM |
| `describe_grant` | `files`: a per-Connection template (issuer and subject as parameter defaults) that creates the control, deployment and node identities, the federated credential, the network (subnet with its security group, the deployment's public IP), a storage account without shared-key access and its container, and the role assignments: Virtual Machine Contributor on the group (control, deployment), Managed Identity Operator on the identities each may attach, Storage Blob Data Contributor on the container; no Network Contributor. `command`: `az deployment group create ...`. A "Deploy to Azure" link only when the host serves the template at a URL (the plugin's `grant_template_url` setting). |
| `prepare_grant_update` | Not implemented. An update is `describe_grant(action="update")`: the same template redeployed. |
| `verify_connection` | Read the subscription and resource group, the role assignments and the identities; `account` from what ARM answers; `observation` from the template deployment's outputs |
| `connection_node_access` | On own sign-in with rights to assign roles: create the node identity and its storage role assignment directly |
| `storage_candidates` | Storage accounts and containers in the resource group, as `abfss://` locations |
| `permissions.storage.access_modes` | `read`, `read_write` |
| Resolver | Registered with `token_audience="api://AzureADTokenExchange"`. Exchange of the pushed identity token for an ARM token (a storage token on demand); returns the plugin's own credential class, an Azure `TokenCredential`. The exchanged token lives up to 24 hours and cannot be revoked; the exchange is retried only on `AADSTS700213` (right after a grant) and `AADSTS7000272` (right after a key rotation) |
| `create_node` | VM with the node identity attached at create, the agent installed by cloud-init, `AZURE_CLIENT_ID` set on the machine so workers pick that identity; `storage_access = "identity"`; tags as in 2.4 |
| `catalog`, `locations` | VM sizes with retail prices and per-family quota; `unavailable_reason` names a zero quota; locations where the size is offered |
| `deployment_options` | `location`, then `virtual_network`, then `subnet` |
| `check_deployment` | Quota and placement; `ingress_key = ""` |
| `prepare_deployment` | Not implemented |
| `create_deployment` | A public IP and a VM with the deployment identity; `addresses` is the public IPv4; `certificate = "machine"`; `sslip = true` |
| `delete_deployment`, `sweep_deployments` | Delete the VM, disk, NIC and public IP by the `3lc-id` tag; sweep releases untracked ones |
| `StorageFacet` | Containers under storage accounts in the resource group |
| `DnsFacet` | Not implemented; a launcher with an Azure DNS zone would need it |

Nothing in the table is an AWS idea under another name: there is no external id (the Config
Service still generates one, and the plugin ignores it), no host principal, no instance profile,
no stack ARN (the reference is opaque), no shared ingress, no change set, and no Route 53.

### 5.3 Machines and RunPod

**Machines.** `MachinesPlugin(InfrastructurePlugin, NodeProvider)`. `account` is `None`,
`access_methods` is empty and `requires_connection` is false: a machine is reached over ssh, not
through a cloud account (`machines:__init__.py:51-61`). It implements `node.create`,
`node.state`, `node.delete` and `node.check`, with `storage_access = "ambient"`. A machine whose
ssh key is held in a `SECRET` Connection names it in `nodes.type_connections`, and receives the
value in `x-tlc-bound-credential`. It implements no grant, no verification, no catalog, no
deployments, no storage and no DNS. Its "Test" route stays plugin-private.

**RunPod.** `RunPodPlugin(InfrastructurePlugin, NodeProvider, StorageFacet, CatalogFacet)`.
`account.fields` is empty: the key is the account. One access method, `SECRET`, whose fields are
`api_key` and the optional network-volume key pair, all `secret`; the stored value is a JSON
object of those keys. This fills the seam the plugin leaves open today
(`runpod:connection.py:54-63`). It implements `connection.verify` (who the key belongs to, as
`account_label`), the node operations, `catalog` and `catalog.locations` (stock by datacenter),
and storage over network volumes. Its capabilities list `nodes.storage_access` as empty, and a
create answers `storage_access = "none"`. It implements no grant, no node
identity and no deployments. A launcher is never granted a secret
(`cs:db/models/connection.py:105-113`), which is consistent: RunPod hosts no deployments.

### 5.4 The owner-credentials facet is removed

No flow launches a deployment with pasted keys or a signed-in session any more.

- The Hub's launch wizard proceeds only through the hosted launcher: without one it stops with
  "The configured launcher must be available" (`hub:templates/settings/workspace.html:2195-2205`).
- Its create posts `/api/workspaces` when a Config Service Connection is chosen and otherwise
  throws "Select a Connection authorized for the configured hosted launcher"
  (`hub:templates/settings/workspace.html:3621-3627`). The body that would carry `credentials`
  is built and never sent (`:3606`).
- `/api/workspaces` refuses a request without a `connection_id` and builds no `credentials`
  (`compute:routes/workspaces.py:231-249`).
- In Connection mode the wizard renders no key, sign-in or role fields
  (`hub:templates/settings/workspace.html:2466-2470`).

What remains is unreachable from the Hub: the wizard's key, sign-in, role-grant and discovery
code (`hub:templates/settings/workspace.html:2224-2640`), the host's forwarding routes
(`compute:routes/infra.py:1326`, `:1494-1513`), its acceptance of `credentials` on a direct
`POST /api/infra/nodes` (`compute:infra/manager.py:1872-1887`), and the SDK facet with the
plugins' implementations. The host's document still describes the key-carrying demo
(`compute:docs/internal/cloud-workspace.md:119-124`); it is out of date. All of it is deleted
with generation 2: the facet and `legacy.py` in package A, the host routes and the transient
credentials in B1, the wizard code in E1, the plugin methods in F1, G and H.

---

## 6. Build plan

### 6.1 Prerequisites: five unmerged branches

Generation 2 is built stacked on five pushed draft branches, not after their merge. Each
package branches from the matching one, merged with its repo's default branch.

| Repo | Branch | What the contract takes from it |
|---|---|---|
| 3lc-compute-plugin-sdk | `docs/infra-stored-credentials` | A `SECRET` binding handled by the Connection middleware beside its value; the 424 when binding and value disagree; `node_type_connections`, which becomes `nodes.type_connections` |
| 3lc-compute | `feature/gudbrand/infra-secret-connections` | The host sending a `SECRET` Connection's binding, use and value on node calls, asking for a grant each time, and masking the value in errors |
| 3lc-compute-plugin-machines | `feature/ssh-connection` | A machine reached with an ssh key held in a Connection: the one first-party user of `type_connections` and of a secret on infrastructure calls |
| 3lc-hub-frontend | `feature/gudbrand/ssh-connection-node-ops` | The Hub's credential picker for node operations |
| tlc-config-service | `feature/gudbrand/secret-release-allowed-plugins` | The release of a secret to an infrastructure plugin listed in `allowed_plugins` |

The parts of this document that depend on them: the `SECRET` row of 2.3, `SecretToken.fields()`,
`nodes.type_connections`, and the machines and RunPod readings in 5.3. A package that rebases
onto its default branch before its prerequisite merges loses them.

### 6.2 Milestone 1: Azure nodes on the new contract, AWS ported for nodes and connect

Critical path: A, then B1, then F1, G, H and E1 in parallel. The Config Service is not changed
in this milestone.

**What stays for deployments.** Deployments keep working through the existing path, untouched,
until milestone 2. That needs a transitional surface, kept in the SDK through milestone 1 and
removed in milestone 2 before 0.8.0 is cut. It is exactly:

- `CreateNodeRequest.flavor` and `.workspace`, `WorkspaceRequest`, and
  `CreateNodeResponse.services`, `.ingress`, `.placement`: a deployment is still created by
  `POST /infra/nodes` with `flavor == "workspace"`. `flavor` is `"workspace"` or empty: what a
  node is, is `kind`.
- `WorkspaceFacet` and its four routes, with `WorkspaceInstance` (its `extra` included),
  `WorkspaceListing` and `WorkspacePlacement`.
- The AWS plugin's two private routes, `GET /infra/deployments/name-check` and
  `POST /infra/deployments/sweep`, mounted by its own `get_route_handlers` as now.
- Three transitional operation ids so the host does not read `extra`: `workspace.create`
  (the provider takes `flavor == "workspace"`), `workspace.name_check`, `workspace.sweep`. The
  SDK lists `workspace.create` for a class that is both a `NodeProvider` and a `WorkspaceFacet`.
  It cannot see a plugin's private routes, so the plugin names the other two in the class
  attribute `WorkspaceFacet.private_operations` and the SDK lists those it names (decision 9).
- `ConnectionBinding.configuration`, the installed configuration in today's shape.

In the SDK the surface is one module, `infrastructure/transitional.py`, plus blocks delimited by
`# ── transitional ──` and `# ── end transitional ──` in the modules that must hold a piece of
it: the fields in `types.py` and `connections.py`, `workspace_handlers` and the workspace branch
of the node-create route in `routes.py`, the listing of the three ids in `plugin.py`, and the
workspace branch of `FakeProvider` and the `workspaces` check group in `testing.py`. Removing it
is deleting the module and those blocks. A plugin that is a `WorkspaceFacet` and not yet a
`DeploymentProvider` declares no `deployments` in its capabilities: the host and the Hub read
`workspace.create` from `operations` for as long as the surface exists.

**Which path the launcher takes.** The host's launcher takes the typed deployment path only when a
provider lists `deployment.create` and does not list `workspace.create`; a provider that lists
both is driven through the transitional surface. `FakeProvider()` lists both;
`FakeProvider(transitional=False)` presents only the generation 2 surface (no `workspace.*`
operations, no `WorkspaceFacet`) and is what a host tests the typed path against.

And one rule for the binding, because the Config Service is unchanged: in milestone 1 the host
builds the generation 2 binding from today's grant by copying `metadata` into `access` as
strings, key for key, leaving `account` and `defaults` empty and `permissions` unset, and
passing `configuration` through. The AWS plugin reads the same keys from `access` that it reads
from `metadata` now, so nothing in it changes when the Config Service starts sending `access`
itself; it derives the account from `role_arn` when `account` is empty.

| Package | Owns | Must not touch | Consumes | Done when |
|---|---|---|---|---|
| **A** SDK contract | `infrastructure/types.py`, `plugin.py`, `facets.py`, `routes.py`, `errors.py`, `testing.py`, `connections.py`, and the new `infrastructure/_wire.py` and `infrastructure/transitional.py`; removes `infrastructure/legacy.py`; `tests/fixtures/contract/`; the infrastructure sections of `docs/plugin-guide.md` | `worker.py` except the one `uvicorn.Config` line, `job_context.py`, `shared/`, `contract/` (the browser API) | this document | Every type in 2.1 to 2.8 exists with `from_dict`/`to_dict`; `FakeProvider` implements every operation including deployments and DNS; `check_provider` covers each; one JSON fixture per type is published; the transitional surface still passes its existing tests |
| **B1** Host: capabilities, connect, credential seam, nodes, typed errors | `infra/provider_client.py`; `connections.py`; in `infra/manager.py` the functions `capabilities`, `verify_connection`, `connection_node_access`, `prepare_connection_update`, `connection_onboarding`, `host_scope`, `preflight`, `create_node`, the node branch of `_provision`, `_is_permission_refusal`; the Connection and node routes and the visitor allow-list in `routes/infra.py`; removes the owner-credentials routes and transients | In `infra/manager.py`: the workspace branch of `_provision`, `_deployment_dns_zone`, `check_deployment_name`, `sweep_deployment_names`, `_sweep_through`, `_report_workspace`, `workspace_choices`. Also `routes/workspaces.py`, `host_options.py`, `config.py`, `config_service.py`, `tests/fake_config_service.py`, `node_agent/` | A's types and `FakeProvider` | The host's lockstep tests pass against `FakeProvider`; the four `caps.raw.get` reads on the deployment path use the transitional operation ids; a node is created, described and deleted through `FakeProvider` with each of the three kinds |
| **F1** AWS: capabilities, connect, nodes, storage | `plugin.py` (capabilities, connect, node and storage methods), the resolver and credential code in `provider.py`, `node_access.py`, `storage_access.py`, `connection_updates.py`, `validate.py`, `config.py` | `workspace.py`, `ingress.py`, `ingress_policy.py`, `hostnames.py`, `sweep.py`, the workspace renderer in `bootstrap.py`, except replacing reads of `binding.metadata` with `binding.access` and removing their reads of the removed `infrastructure.legacy` module (`workspace.py`, `ingress.py` and `provider.py` import it) | A | `check_provider` passes for every operation it lists; the plugin's existing tests pass; on the dev account: connect an account, verify, start a node and train from storage, and launch a deployment through the unchanged path |
| **G** Machines and RunPod | each plugin's capabilities and class bases; RunPod's `connection.py` | nothing shared | A | `check_provider` passes; a machines node starts with a key from a Connection |
| **H** Azure nodes | the whole plugin, rewritten: capabilities, own sign-in, nodes, catalog, storage | nothing shared | A | `check_provider` passes; in the sandbox, with the host's built-in own-identity Connection and no Config Service (`compute:connections.py:159-166`, `compute:infra/manager.py:3560-3561`): start a node with its identity attached, run a job on a table in Azure storage, delete it |
| **E1** Hub, for milestone 1 only | the defaults loader in `static/js/connection-create.js` (`:775-830`), reading typed capabilities into the object the form already uses; the node-create dialog in `static/js/plugin-loader.js` (`kind`, `location`, `nodes.types`); removal of the wizard's owner-credentials code | the Connection form itself, `connection-edit.js`, `connections-admin.js`, `deployment-launch.js` | A's capability fixtures | The current Connection form and node dialog work against a generation 2 Compute Service for `aws` and `azure`. The loader composes the AWS role ARN itself for this milestone, since the Config Service still requires it at create; that line is deleted in E2 |

### 6.3 Milestone 2: deployments, the Config Service, the Hub form, Azure grant and deployments

| Package | Owns | Must not touch | Consumes | Done when |
|---|---|---|---|---|
| **C** Launcher DNS, then **F2** AWS deployments, one owner, in that order | Host: a new `infra/dns.py` (label rules, allocation, ledger, zone sweep); in `infra/manager.py` the functions B1 left alone; `routes/workspaces.py`; `host_options.py`; `config.py`; the workspace reporting in `config_service.py`; `tests/fake_config_service.py`. AWS: `workspace.py`, `ingress.py`, `hostnames.py`, `sweep.py`, a new `dns.py` for the facet. SDK: removal of the transitional surface | B1's functions; the AWS files F1 owns | A's deployment and DNS types; `FakeProvider`'s in-memory zone | The launcher runs check, allocate, prepare, create against `FakeProvider`; the AWS acceptance run launches a deployment and reaches it under the same name shape as before; an existing shared ingress is reused without a new certificate; the conformance kit's DNS checks pass against a fake Route 53 |
| **D** Config Service | `api/hub/connections.py`, `identity/connection_configuration.py`, `identity/connections.py`, `api/hub/workspaces.py`, `identity/launch_reports.py`, `db/models/connection.py`, the migration | `api/hub/plugin_grants.py` except the binding it returns | section 4 | Its integration tests cover a non-`aws` Connection, the back-fill, and the unchanged `metadata` answer for an `aws` row |
| **E2** Hub form | `static/js/connection-create.js`, `connection-edit.js`, `connections-admin.js`, `connection-cards.js`, `deployment-launch.js`, the launch wizard in `templates/settings/workspace.html` | the Config Service client's transport | A's fixtures; D's API | The form, grant panel and wizard render from fixtures for `aws`, `azure`, `runpod` with no provider name in the code |
| **I** Azure grant | Azure: the template, `describe_grant`, verification, the resolver. Also the additive token pieces of 2.9 in the SDK, the host and the Config Service | everything else | A; D's access fields writable while pending | In the sandbox: one link creates the identities and role assignments; verification passes; a node starts through the granted method |
| **J** Azure deployments | Azure: options, check, create, state, delete, sweep | everything else | A; C | In the sandbox: a deployment launches, gets its name in the launcher's zone and a certificate on the machine, and is reached |

C and F2 share an owner and run in sequence because they change the same functions of
`infra/manager.py`, `infra/provider_client.py`, `config_service.py` and
`tests/fake_config_service.py`, and the AWS plugin's DNS code moves between them. Until D
issues tokens, I uses a test issuer: a signing key and a discovery document served from a public
storage container in the sandbox, the token minted by a test helper. Until C lands, J reads back
`addresses` and names the VM under `sslip`.

### 6.4 Interfaces between packages

- **A to everyone: the wire.** A publishes, with its first staged build, one JSON fixture per
  type under `tests/fixtures/contract/` and the `FakeProvider` that emits them
  (`tlc_plugin_sdk.infrastructure.testing`). Each file is `{type, route, example}`;
  `capabilities.aws.json`, `capabilities.aws-deployments.json`, `capabilities.azure.json`,
  `capabilities.runpod.json` and `capabilities.machines.json` are the declarations of 5.2 and
  5.3, the first as AWS is while the transitional surface exists and the second as it is with
  the deployment group. `tests/contract_fixtures.py --write` regenerates them and
  `tests/test_infra_fixtures.py` fails when a type and its fixture drift. B1 and C test
  against `FakeProvider` in process, as the host's lockstep tests do today
  (`compute:infra/provider_client.py:10-17`). E1 and E2 render from fixtures and need no running
  plugin. F to J are done when `check_provider` passes for every operation they list
  (`sdk:infrastructure/testing.py:608-618`).
- **C to D: the Config Service API of section 4.** C extends the host's fake
  (`compute:tests/fake_config_service.py`) to section 4 and builds against it; D builds to the
  same section. The first joint run is the acceptance run.

### 6.5 Versions

- **Nothing here is constrained by a release.** No released SDK has an infrastructure or
  Connections surface: the latest on PyPI is 0.4.0, which has no `infrastructure` package, and
  the `0.5.0` heading in the changelog was never published (`sdk:CHANGELOG.md:548`). No released
  Compute Service has one either: 1.1 depends on `3lc-compute-plugin-sdk>=0.3.3,<0.4.0` and has
  no infrastructure manager. So generation 2 breaks nothing that shipped.
- **SDK 0.8.0** is contract generation 2, cut after milestone 2 with the transitional surface
  gone. Milestone 1 runs on staged 0.8 development builds.
- **Compute Service 1.3** speaks generation 2 and only it: it depends on
  `3lc-compute-plugin-sdk>=0.8,<0.9` and refuses to register an infrastructure plugin whose
  capabilities do not say `contract: 2`, with a sentence naming the plugin to update.
- **Every generation 2 plugin** declares `min_service_version = "1.3"` and depends on
  `3lc-compute-plugin-sdk>=0.8.0,<0.9.0`.
- **A deployment** runs the Compute Service line of its launcher
  (`compute:docs/internal/cloud-workspace.md:39-46`), so a 1.3 launcher creates 1.3 deployments
  that install generation 2 plugins from the catalog. No mixed pair arises from a launch.

**If Compute 1.2 and SDK 0.7 are released first.** Then their shapes become released ones: the
Hub must keep its current Connection form for a 1.2 Compute Service until the floor reaches
1.3, the Config Service must keep emitting `metadata` and `configuration` (4.4), and the catalog
must keep 0.7-line builds of the infrastructure plugins for 1.2 hosts. Cheap steps that bound
that, for the release's owner to take or leave:

- In the SDK 0.7.0 README and changelog, mark `tlc_plugin_sdk.infrastructure` and
  `tlc_plugin_sdk.connections` provisional: changing in 0.8 without a deprecation period.
- In the Compute Service and Hub 1.2 release notes, label Connections, GPU nodes through a
  Connection and managed deployments a preview.
- Publish the infrastructure plugins for 1.2 with an upper bound on the service version, so a
  1.3 host is never offered them.

---

## 7. Open decisions

What is still open. Settled and reflected above: the token is pushed by the host; DNS is
written through a facet on a plugin; shared-ingress keys and names stay as the provider
computes them today; prepare and a synchronous create; the wire kinds keep their names; storage
rules are URL patterns; the method is derived with the alternatives behind a disclosure; a
launcher without a zone names by address; the owner-credentials facet is removed.

1. **Who validates account and access fields.**
   (a) The Config Service stores bounded opaque maps; the plugin validates grammar, the Config
   Service validates equality and immutability. (b) The Config Service keeps a model per
   provider. (c) The host attests the plugin's field schema and the Config Service validates
   against it.
   **Recommend (a).** (b) puts every new provider into the Config Service; (c) adds a trust
   hop for little. It needs the Config Service owner's agreement that today's ARN and stack
   checks are not relied on as a control against a faulty host. Blocks D, E2.

2. **Which grant a deployment's read-only and prepare calls ride.**
   (a) As in 3.1: options, check and sizes on `connection.verify`, prepare on the launch's
   `workspace.create`. (b) Open `node.describe` to launchers for the read-only calls. (c) A new
   operation for prepare.
   **Recommend (a).** It is what the Config Service allows a launcher today and needs no change
   there. Its weakness is that `connection.verify` is administration that does not count as
   use, so placement lookups leave no trace in `last_used_at`; (b) fixes that at the cost of a
   Config Service change. Blocks C, D.

3. **Where the record ledger lives.** The options and the recommendation (owner records in the
   zone as the claim, the list on the deployment record) are in 2.6. It needs the Config
   Service owner to accept the bounded `ingress` map as its home. Blocks C.

4. **The identity-token interface of the Config Service.** 2.9 states what a plugin relies on.
   How a host asks (a field on the grant request, or its own endpoint) and how subjects are
   bound to organizations are the Config Service owner's. Blocks I.

5. **Lowering the storage-rule limit from 50 to 20.** It follows from the 8 KiB binding. If a
   customer needs more, the alternative is to stop sending rules in the binding and have the
   plugin read them from the installed grant instead. Blocks D's back-fill rule.

6. **Whether 1.2 and 0.7 ship before this.** Decides whether the dual support of 6.5 is real.
   Blocks nothing in milestone 1.

Decided while building the SDK package (A), each the smaller of its options. They stand unless
reopened.

7. **`Locations` has typed rows.** 2.4 said only that `Locations` is `Datacenters` renamed, and
   `Datacenters` carried untyped rows plus a provider's saved placement setting, which 2.0
   rules out. (a) Typed `Location` rows `{id, label, available, capacity}` and a `default`;
   a provider's placement setting stays on its own routes. (b) Keep the untyped rows under the
   new name. **Decided (a).** Touches G (RunPod maps its datacenter rows) and the Hub's node
   dialog.

8. **`DeploymentListing` is a list of `DeploymentInstance`.** 2.5 named the type without
   defining it. (a) `{provider_id, deployment_id, name, owner, size, state, location,
   addresses}`, the `WorkspaceInstance` fields in this contract's words plus the `3lc-id` tag a
   launcher matches against its registry. (b) Reuse `WorkspaceInstance`. **Decided (a).**
   Touches C, F2 and J.

9. **A plugin names its two private transitional operations.** The SDK lists an operation
   when it mounted the route, and it does not mount the AWS plugin's private name-check and
   sweep routes. (a) The plugin sets `WorkspaceFacet.private_operations`; the SDK lists the
   ids it names. (b) The SDK inspects the plugin's handler list for the two paths.
   **Decided (a).** Touches F1 (one class attribute) and B1 (reads `operations`).

10. **The inspecting `GET` of node access carries the storage roots as JSON in the query.**
    3.1 gave the `GET` a `NodeAccessRequest` and a `GET` has no body. (a) `?storage_roots=`, a
    JSON list of `StorageRoot`. (b) Make the inspection a `POST` with an `apply` flag.
    **Decided (a).** It keeps "GET inspects, POST applies". Touches B1.

---

## Migration from 0.7

For someone porting a plugin or a host. Nothing below was ever in a released SDK: the
infrastructure and Connections surfaces exist only on the unreleased 0.5 to 0.7 lines.

**Removed**

- Every `extra` field. Use the typed field (`PresignResponse.cors`), or serve the value from the
  plugin's own route. `WorkspaceInstance.extra` stays until milestone 2.
- `CapabilitiesResponse.facets`, `facets_reported`, `lists_workspaces`, `flavors`, `missing`,
  `region`, `workspace_fields`. Use `operations`, `nodes`, `deployments`.
- `CreateNodeResponse.managed_by`, `ProjectStorage` and `CreateNodeRequest.project_storage`.
- `CreateNodeRequest.flavor` and `.workspace`; `CreateNodeResponse.services`, `.ingress`,
  `.placement`; `WorkspaceRequest`. (After milestone 2. Until then `flavor` is `"workspace"` or
  empty.)
- `WorkspaceFacet`, `WorkspaceInstance`, `WorkspaceListing`, `WorkspacePlacement`. (After
  milestone 2.)
- `LegacyOwnerCredentialsFacet`, `OwnerCredentialsDescriptor`, `LoginDescriptor`,
  `RoleDescriptor`, `tlc_plugin_sdk.infrastructure.legacy`, and the request-carried
  `credentials` and `provider_configs`.
- `connections.AwsSession`.
- `ConnectionBinding.metadata`; `.configuration` after milestone 2.
- `GpuCatalog`, `CpuCatalog`, `preflight_query`, the `gpu_type` and `gpu_types` aliases.
- `InfrastructurePlugin.implemented_facets`, the `FACET_*` constants, and the route-group
  builders `core_handlers`, `catalog_handlers` and `legacy_handlers`.
- `Ambient.region`. Read `Ambient.defaults`.
- The storage routes without a Connection: each of the twelve now answers 400 without one.
- `dns_zone_id` and `dns_zone_name` on a create request.

**Renamed or reshaped**

| 0.7 | 0.8 |
|---|---|
| `SettingsField` in `missing_fields` (`placeholder`) | `FormField` (`example`); `SettingsField` stays as the settings layer's own type |
| `CapabilitiesResponse(**self.settings.readiness(s))` | `CapabilitiesResponse(..., **self.readiness())` |
| `CapabilitiesResponse.node_types`, `pricing`, `node_type_label`, `node_type_connections` | `nodes.types`, `nodes.pricing`, `nodes.type_label`, `nodes.type_connections` |
| `preflight`, `preflight_with_context`, `GET /infra/preflight` | `check_node`, `POST /infra/nodes/check` |
| `connection_check`, `connection_check_with_context`, `GET /infra/connection/check` | `verify_connection`, `POST /infra/connection/verify` |
| `ConnectionCheckRequest` | `VerifyRequest`, `NodeAccessRequest` |
| `ConnectionCheckResponse` (`metadata`) | `ConnectionVerification` (`account`, `observation`, `facts`) |
| `gpu_catalog`, `cpu_catalog`, `GET /infra/gpu-catalog`, `/infra/cpu-catalog` | `catalog`, `POST /infra/catalog` |
| `datacenters`, `Datacenters` (untyped rows), `GET /infra/datacenters` | `locations`, `Locations` (typed `Location` rows), `GET /infra/catalog/locations` |
| `list_workspaces`, `GET /infra/workspaces` | `list_deployments`, `GET /infra/deployments` |
| `workspace_placement`, `GET /infra/workspace-placement` | `deployment_options`, `POST /infra/deployments/options` |
| `workspace_diagnostics`, `GET /infra/workspace-diagnostics/{id}` | `deployment_state(diagnostics="detailed")` |
| `workspace_retain_failed`, `POST /infra/workspace-retain/{id}` | `retain_deployment`, `POST /infra/deployments/{id}/retain` |
| `create_node` with `flavor == "workspace"` | `create_deployment`, `POST /infra/deployments` |
| `register_resolver(provider, kind, fn(binding))` | `register_resolver(provider, fn(ResolveRequest))` |
| `InfrastructurePlugin` with four abstract node methods | `InfrastructurePlugin` plus the `NodeProvider` mixin |
| Error body `{detail}` | `{detail, code, remedy, retry_after_s}` |
| `routes.answer` raising litestar's `HTTPException` | raising `routes.ProviderHTTPError`; mount a private route with `exception_handlers=routes.error_handlers()` |
| A storage call without a Connection | every storage call carries one |

**Added**

`NodeProvider`, `DeploymentProvider`, `DnsFacet`; `describe_grant`, `prepare_grant_update`,
`storage_candidates` as typed operations; `ProviderCredential`, `ResolveRequest`,
`credential_as`, `SecretToken.fields`; `PermissionDenied`, `CredentialExpired`, `RateLimited`,
`ErrorAnswer`; `OPERATIONS`, `provided_operations`, `capabilities_problems`,
`InfrastructurePlugin.readiness`; `FakeProvider` for every operation, `connection_headers`, `acting_as`, and
the conformance groups `connect`, `check`, `deployments` and `dns`;
`CreateNodeRequest.kind`, `.location`, `.host_id`; `CapabilitiesResponse.contract`,
`operations`, `account`, `access_methods`, `host`, `permissions`, `nodes`, `deployments`.
