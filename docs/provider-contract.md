---
orphan: true
---

# Infrastructure provider contract

**Status: proposal.** This specifies contract generation 2 of the infrastructure plugin surface
(`tlc_plugin_sdk.infrastructure` and `tlc_plugin_sdk.connections`), to ship as SDK 0.8. It is
written so that the SDK, the Compute Service (the host), the Hub, the Config Service and each
provider plugin can be built against it independently. Nothing in it is implemented.

The contract serves three goals a person has on any cloud: connect to cloud storage, start a GPU
node, start a managed deployment. A fourth group, connect an account, is what the other three
stand on. Everything above the plugin (host, Hub, Config Service) handles provider values as data
it stores and shows and never interprets.

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
service above the plugin. A Connection names exactly one account.

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
own deployment identity and never assumes anything (`compute:docs/internal/launcher-and-deployments.md:48-51`).

**Launcher.** A Compute Service that creates deployments in other organizations' accounts. It
owns each deployment's public name for the deployment's whole life: the hostname is a record in
a DNS zone the launcher is configured with (`compute:docs/internal/launcher-and-deployments.md:43-44`).
Which zone, and what writes to it, are launcher settings; no provider assumes them.

| Term | AWS | Azure |
|---|---|---|
| Account | Account id (12 digits), optional default region | Tenant id, subscription id, resource group, optional default location |
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
  peer's shape. `to_dict` omits a key whose presence carries meaning when it is unset.
- New optional fields and new operations may be added in a later minor without a change from a
  provider that does not use them. Nothing in a request is ever required of a provider that did
  not declare the operation it belongs to.
- A provider value a person must see comes with its display text from the provider. The Hub
  never parses a provider identifier.
- There is no untyped `extra`. See section 3 for what replaces it.

An error answer is `{"detail": str, "code": str, "remedy": str}`. `detail` is the sentence a
person reads, as today (`sdk:infrastructure/routes.py:121-162`); `code` and `remedy` are new and
are specified in 2.4.

### 2.1 Capabilities declaration

`GET /infra/capabilities`, never sent a Connection (see 2.3, rule 1).

```python
CONTRACT_GENERATION = 2

FieldKind = Literal["text", "secret", "select", "multiline"]
ConnectionKind = Literal["AMBIENT", "KEYLESS", "SECRET"]
Purpose = Literal["deployments", "nodes", "storage"]


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
    secret: bool = False               # masked input, never echoed, never stored outside a sealed value
    help: str = ""                     # where the value is found, in a sentence
    href: str = ""                     # https link to where the value is found
    example: str = ""                  # shown as the placeholder
    pattern: str = ""                  # regex the whole value must match; "" accepts anything
    max_length: int = 256              # 1..2048
    choices: list[Choice] = field(default_factory=list)   # kind == "select" only
    default: str = ""


@dataclass
class AccountSchema:
    noun: str                          # "AWS account", "Azure subscription and resource group"
    fields: list[FormField] = field(default_factory=list)   # [] when the secret is the account
    help: str = ""


@dataclass
class IdentityTokenSpec:
    """The host must send a signed identity token for the Connection with every call (2.3)."""

    audience: str                      # the `aud` the provider's token exchange expects


@dataclass
class AccessMethod:
    kind: ConnectionKind
    label: str                         # "Uses access you grant to 3LC"
    summary: str = ""                  # one sentence shown under the label
    available: bool = True             # whether this host can use the method at all
    unavailable_reason: str = ""       # the sentence when it cannot
    preferred: bool = False            # at most one True: the method a new Connection gets
    fields: list[FormField] = field(default_factory=list)       # asked before the grant
    paste_back: list[FormField] = field(default_factory=list)   # asked after the grant ran
    grants: bool = False               # `describe_grant` applies to this method
    identity_token: IdentityTokenSpec | None = None             # KEYLESS only


@dataclass
class HostIdentity:
    """Who this host is to the provider. Ambient-only; see 2.3 rule 1."""

    principal: str                     # stable name a grant trusts; "" when the provider names none
    display: str = ""                  # what a person recognises
    deployment_of: dict[str, str] = field(default_factory=dict)
    # On a deployment: the account and access fields of the Connection it was launched through,
    # merged into one flat map. A Connection whose stored fields all match belongs to this host.


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
    help: str = ""
    patterns: list[Choice] = field(default_factory=list)
    # Wildcard forms the editor offers, id = an example ("s3://team-*"), label = what it means.
    max_rules: int = 50


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
    storage_access: list[Literal["identity", "lease", "ambient"]] = field(default_factory=list)
    interruption_notice: bool = False
    type_connections: dict[str, str] = field(default_factory=dict)   # node type -> Connection id


@dataclass
class DeploymentCapabilities:
    certificate: Literal["machine", "provider"]
    shared_ingress: bool = False       # deployments share an ingress; `check` answers `ingress_key`


@dataclass
class CapabilitiesResponse:
    provider: str                      # the Connection provider slug; equals the plugin id
    display_name: str                  # "AWS"
    contract: int = CONTRACT_GENERATION
    ready: bool = False
    not_ready_reason: str = ""
    missing_fields: list[FormField] = field(default_factory=list)   # plugin settings still needed
    default_location: str = ""
    requires_connection: bool = False
    account: AccountSchema | None = None          # None: this provider takes no Connection
    access_methods: list[AccessMethod] = field(default_factory=list)
    host: HostIdentity | None = None
    permissions: PermissionSchema = field(default_factory=PermissionSchema)
    nodes: NodeCapabilities | None = None          # None: the provider starts no nodes
    deployments: DeploymentCapabilities | None = None
    storage: StorageCapabilities | None = None     # filled by the SDK from StorageFacet, unchanged
    operations: list[str] = field(default_factory=list)   # filled by the SDK; see section 3
    display: dict[str, str] = field(default_factory=dict)  # see section 3
```

Validation, enforced by the SDK before answering and asserted by the conformance kit:
`provider` matches `^[a-z0-9][a-z0-9._-]*$`; field keys are unique per list; `secret` is true
exactly when `kind == "secret"`; at most one access method per kind and at most one `preferred`;
`identity_token` only on `KEYLESS`; `paste_back` only when `grants`; every
`PermissionOption.key` is unique; `nodes` is set exactly when the class is a `NodeProvider`, and
`deployments` exactly when it is a `DeploymentProvider`.

Why each part exists:

- `account` and `access_methods[].fields` replace the role-ARN form. Today the Hub composes an
  ARN from `role_arn_template`, `account_id_label`, `account_id_pattern` and `role_name`
  (`aws:plugin.py:333-340`, `hub:static/js/connection-create.js:1386-1390`, `:2178-2191`) and the
  Config Service validates the ARN's shape (`cs:api/hub/connections.py:117`).
- `access_methods` replaces `connection_kinds` (`aws:plugin.py:237`,
  `hub:static/js/connection-create.js:1375-1377`), `connection_onboarding_unavailable`
  (`aws:plugin.py:354-356`) and the Hub's two hard-coded access cards
  (`hub:static/js/connection-create.js:359-365`).
- `host` replaces `host_identity`, `host_principal` and `host_connection`
  (`aws:plugin.py:359-371`), which the host reads to decide which Connections act for another
  deployment (`compute:infra/manager.py:2892-2909`, `compute:connections.py:213-236`).
- `permissions` replaces `permission_switches`, `location_schema`, `storage_access_modes` and the
  default `configuration` (`aws:plugin.py:243-332`, `hub:static/js/connection-create.js:1219`,
  `:1731`, `:1822`).
- `nodes.type_connections` is the typed place for a provider whose node types each have a
  credential of their own (a machine and its ssh key).
- `deployments.certificate` tells the launcher whether a name must resolve to the machine before
  its services can answer (`machine`), or whether the provider terminates TLS itself
  (`provider`, which usually needs `deployment.prepare`).

**The method a person gets.** The host offers a method when the provider says `available` and
the host's own rules allow it: a host that lends no identity of its own (a demo or launcher
Compute) never offers `AMBIENT` (`compute:connections.py:179-199`), and a `KEYLESS` method with
`identity_token` is offered only when the host's Config Service can mint identity tokens (2.3).
Among what remains, a new Connection gets the `preferred` method, else the first listed. When
more than one remains the Hub shows the derived method as a fact with the others behind "use
another way"; when one remains nothing is asked.

### 2.2 Connect an account

```python
StorageAccessMode = Literal["read", "read_write_no_delete", "read_write"]


@dataclass
class StorageRule:
    location: str                      # a URL or URL pattern in the provider's grammar
    access: StorageAccessMode = "read"


@dataclass
class ConnectionPermissions:
    """What 3LC may do in the account: the three ticks, plus provider-declared options."""

    deployments: bool = False
    nodes: bool = False
    storage: list[StorageRule] = field(default_factory=list)
    options: dict[str, bool] = field(default_factory=dict)   # keys from PermissionSchema.options


@dataclass
class TokenIdentity:
    issuer: str                        # https URL of the identity-token issuer
    subject: str                       # the one subject minted for this Connection
    audience: str


@dataclass
class GrantRequest:
    connection_id: str
    kind: ConnectionKind
    account: dict[str, str]
    access: dict[str, str]             # the method's `fields` as entered
    permissions: ConnectionPermissions
    external_id: str = ""              # the Config Service's nonce for this Connection
    identity: TokenIdentity | None = None   # set when the method declares `identity_token`
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
    steps: list[str] = field(default_factory=list)            # what to do, in order
    creates: dict[str, list[str]] = field(default_factory=dict)   # Purpose -> plain sentences
    trusts: str = ""                   # the principal, or issuer and subject, the grant trusts
    trust_help: str = ""
    files: list[GrantFile] = field(default_factory=list)      # for creating it by hand
    command: str = ""                  # a ready-to-paste command using `files`
    permissions: ConnectionPermissions = field(default_factory=ConnectionPermissions)   # normalised
    access: dict[str, str] = field(default_factory=dict)      # method fields as the provider normalised them
    version: str = ""                  # the grant template's version
    console_url: str = ""              # where the installed grant can be inspected
    notes: list[str] = field(default_factory=list)
    wait_help: str = ""
    verify_help: str = ""
    revoke_help: str = ""


GrantUpdateStatus = Literal[
    "waiting", "preparing", "ready", "applying", "applied", "no_changes", "failed", "permission_required"
]


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
    detail: str = ""
    review_url: str = ""               # where the customer reviews and applies the change
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
    facts: dict[str, str] = field(default_factory=dict)   # display-only


@dataclass
class StorageCandidate:
    location: str                      # a rule location the editor can offer
    label: str = ""


@dataclass
class StorageCandidates:
    items: list[StorageCandidate] = field(default_factory=list)
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
  (`sdk:docs/plugin-guide.md:764-771`). The Hub stores `Grant.access` on the Connection before
  the person follows the link, then asks for `paste_back` values when the method declares any.
- **`prepare_grant_update`** is for a provider whose cloud has a reviewed change (a change set).
  It runs under the Connection and prepares, never executes (`aws:connection_updates.py:42-174`).
  A provider without one does not implement it; its update is `describe_grant(action="update")`.
- **`verify_connection`** runs under the Connection, after the SDK resolved it. A step that fails
  raises; a sample that could not be confirmed is a `warn` check; a refused operation is an
  `error` check, which the host records as a failed verification
  (`compute:infra/manager.py:2697-2705`). `account` is what the cloud says, so the Config Service
  can compare it with what was stored without knowing the provider's grammar (section 4).
  `observation` replaces `metadata.configuration_observation` (`compute:infra/manager.py:2694-2696`,
  `cs:identity/connection_configuration.py:97-102`); the AWS-only `stack` answer
  (`aws:plugin.py:131-151`) becomes check rows and `facts`.
- **`connection_node_access`** is unchanged in shape (`sdk:infrastructure/types.py:648-719`): it
  is how a machine on its own sign-in sets up a scoped node identity directly, when it has the
  rights to. `NodeAccessRequest` is today's `ConnectionCheckRequest` renamed, with
  `project_root_url` replaced by `storage_roots`.
- **`storage_candidates`** runs under the Connection with a discovery-only grant and returns
  names, never contents (`aws:provider.py:2485-2508`).

### 2.3 The credential seam

What a request carries, all host-owned headers a host strips from callers
(`compute:connections.py:65-74`, `:361-372`):

| Header | Carries | When |
|---|---|---|
| `x-3lc-connection` | `ConnectionBinding` as JSON | Every call that acts on an account |
| `x-3lc-connection-use` | `ConnectionUse` as JSON | Optional; the resource and the person the call is for |
| `x-3lc-connection-token` | `IdentityToken` as JSON | `KEYLESS`, when the method declares `identity_token` |
| `x-tlc-bound-credential` | `SecretToken` as JSON | `SECRET` |

```python
@dataclass(frozen=True)
class ConnectionBinding:
    id: str
    provider: str
    kind: ConnectionKind
    account: dict[str, str] = field(default_factory=dict)
    access: dict[str, str] = field(default_factory=dict)       # non-secret method fields
    external_id: str = ""                                      # KEYLESS; the Config Service's nonce
    trusted_principal: str = ""                                # HostIdentity.principal recorded at setup
    permissions: ConnectionPermissions | None = None           # installed; None before a first verification
    grant_ref: str = ""                                        # GrantObservation.resource_ref, when installed


@dataclass(frozen=True, repr=False)
class IdentityToken:
    connection_id: str
    audience: str
    token: str                         # a signed JWT; never in a repr
    expires_at: str                    # ISO-8601


class ProviderCredential(Protocol):
    """What a resolver returns. The class is the provider plugin's own; the SDK names no vendor."""

    expires_at: str                    # ISO-8601, or "" when it does not expire

    def secret_values(self) -> list[str]: ...   # scrubbed from every error answer


@dataclass(frozen=True)
class ResolveRequest:
    binding: ConnectionBinding
    use: ConnectionUse | None
    identity_token: IdentityToken | None


Resolver = Callable[[ResolveRequest], ProviderCredential]

def register_resolver(provider: str, resolver: Resolver) -> None: ...   # KEYLESS only
def current_connection() -> ConnectionBinding | None: ...
def current_use() -> ConnectionUse | None: ...
def current_credential(service: str | None = None) -> Ambient | SecretToken | ProviderCredential | None: ...
def credential_as(cls: type[T]) -> T: ...   # the request's credential as `cls`, else CredentialUnavailable
```

How each kind resolves, in the worker, before the handler runs
(`sdk:connections.py:545-610`):

- `AMBIENT` resolves in the SDK to `Ambient(provider, account)` and cannot be replaced
  (`sdk:connections.py:373-375`). The plugin turns it into its cloud library's default chain.
  A plugin that sees it must not fall back to anything saved in its settings.
- `KEYLESS` calls the plugin's resolver with a `ResolveRequest`, in a worker thread. The AWS
  resolver returns its own `AwsSession` holding STS session keys (`aws:provider.py:345-482`);
  the Azure resolver exchanges `identity_token.token` at the tenant in `binding.account` and
  returns its own token credential. A method that declares `identity_token` and a request
  without the header, or with a token for another Connection, answers 424 from the SDK.
- `SECRET` is not resolved: the value arrives beside the binding and is bound as a
  `SecretToken`. The binding and the value must name the same Connection and provider, else 424.
  A multi-field secret (an API key and a storage key pair) is a JSON object whose keys are the
  method's `secret` fields; `SecretToken.fields()` returns it as a `dict[str, str]` and raises
  `CredentialUnavailable` for any other shape, as service tokens already do
  (`sdk:connections.py:118-124`, `:436-474`).

`AwsSession` leaves the SDK (`sdk:connections.py:282-302`); `ResolvedCredential` no longer lists
a vendor type (`sdk:connections.py:344`).

**How a plugin gets a signed identity token.** It does not ask. A plugin worker has no channel
back to its host today, and this contract adds none. The provider declares the need
(`AccessMethod.identity_token.audience`); the host, which already asks the Config Service for a
grant before every call (`compute:config_service.py:716-724`), obtains a token for that
Connection and audience and sends it in `x-3lc-connection-token`. What a plugin may rely on:
the token is a JWT signed by the issuer named in `GrantRequest.identity.issuer`, with `sub`
equal to `GrantRequest.identity.subject`, `aud` equal to the declared audience, and at least 60
seconds of life left when the request arrives. How the Config Service mints it is not part of
this contract.

**Caching and refresh rules.**

1. **Host identity is ambient-only.** `HostIdentity`, and anything else a grant will trust, is
   read from the machine's own credential chain and never from a request's keys, its Connection
   or a resolved credential. A cache of it is keyed by the ambient credential itself, never by
   region or by request (`aws:provider.py:4972-4975`, `:4992-5028`). The SDK enforces the
   precondition: `GET /infra/capabilities` and `POST /infra/connection/grant` answer 400 when
   any of the four headers above is present, so those handlers can never observe a Connection.
2. A resolved credential belongs to one request. The SDK holds it in a context variable for the
   handler and resets it after (`sdk:connections.py:600-606`); it never writes one to
   `os.environ`, a file or the plugin's settings.
3. A plugin may keep an exchanged credential until 60 seconds before its `expires_at`, in
   memory, keyed by a digest of the whole binding plus `ConnectionUse.source_identity`. A changed
   binding is a different key. It must drop the entry on any authentication failure.
4. An identity token and a secret value are never cached by a plugin. The host may reuse an
   identity token for the same Connection and audience until 60 seconds before it expires.
5. Clients built from a credential are cached per credential digest, never per region alone
   (`aws:provider.py:598-664`). Anything cached per account (quotas, offerings, prices that
   differ per account) is keyed by the account the credential belongs to, and skipped when that
   cannot be named (`aws:provider.py:671-680`).
6. Background work a request starts (a transfer, a bundle) keeps that request's credential and
   must finish within its lifetime; it fails with a remedy sentence when the credential expires,
   and is never continued on the host's own identity.
7. A deployment acting on the Connection it was launched through acts directly, as itself; a
   resolver returns `Ambient` for it and assumes nothing (`aws:provider.py:400-404`). A binding
   whose `trusted_principal` is set and is not this host's `HostIdentity.principal` is refused
   before the cloud is asked (`aws:provider.py:323-342`).

### 2.4 Start a node

A provider that starts nodes adds the `NodeProvider` mixin.

```python
@dataclass
class CreateNodeRequest:
    node_id: str
    node_type: str
    token: str                         # the node agent's token; scrubbed from error answers
    kind: Literal["gpu", "cpu"] = "gpu"
    env: dict[str, str] = field(default_factory=dict)
    agent_port: int = 8800
    ports: list[int] = field(default_factory=list)
    idle_ttl_s: float = 1800.0
    owner: str = ""
    pricing: Literal["", "on_demand", "spot"] = ""
    compute_spec: str = ""
    placement: dict[str, str] = field(default_factory=dict)       # keys the provider's options declared
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
    placement: dict[str, str] = field(default_factory=dict)
    identity: str = ""                 # the node identity attached, for display
    storage_access: Literal["identity", "lease", "ambient", "none"] = "none"


@dataclass
class Interruption:
    deadline: str                      # ISO-8601; when the provider takes the machine
    reason: str = ""


@dataclass
class NodeStateResponse:
    state: NodeProviderState           # pending, running, exited, terminated, gone, unknown
    detail: str = ""
    interruption: Interruption | None = None
    bootstrap_history: list[str] = field(default_factory=list)
    bootstrap_detail: str = ""
    bootstrap_failed: bool | None = None
    bootstrap_stage: str = ""
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class NodeCheckRequest:
    node_type: str = ""
    pricing: str = ""
    placement: dict[str, str] = field(default_factory=dict)
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

- **`kind`** replaces `flavor` (`sdk:infrastructure/types.py:407`). A node is `gpu` or `cpu`; a
  deployment is not a node flavor and has its own group (2.5).
- **Identity attach.** The request carries no identity. The provider attaches the node identity
  the Connection's grant created, which it derives from the binding
  (`aws:provider.py:1033-1070`), and says which in `identity`. It never copies a credential onto
  the node: credential variables in `env` are dropped (`aws:provider.py:1012-1031`).
- **Storage roots** replace `ProjectStorage` (`sdk:infrastructure/types.py:239-264`,
  `compute:infra/manager.py:940-949`). They say what the node's identity should at least reach,
  and are a hint, never a boundary the provider restricts by. `storage_access` says how the node
  reaches them: through its attached `identity`, through a `lease` the host hands it, with what
  the machine already has (`ambient`), or not at all. `lease` is named so a provider can declare
  it; the lease itself is a host and node-agent mechanism outside this contract.
- **Interruption.** A provider that offers `spot` and can tell when a node is being taken
  declares `nodes.interruption_notice` and reports it in `NodeStateResponse.interruption`, both
  ahead of time when the cloud gives notice and after the fact (`aws:provider.py:110-116`). What
  the host and the node agent do with it is theirs.
- **`check_node`** replaces `preflight` and `preflight_with_context`
  (`sdk:infrastructure/plugin.py:124-136`, `sdk:infrastructure/routes.py:244-253`), with the
  host's storage roots in the body rather than `project_root_url` in a query.

**Typed errors.** A provider raises one of these; the route layer answers its status with
`{detail, code, remedy}`, scrubbed of `secret_values()` and of the request's own secrets
(`sdk:infrastructure/routes.py:121-162`, `:266-270`). The host branches on `code`, never on the
words of a sentence: today it searches provider sentences for `AccessDenied`
(`compute:infra/manager.py:672-688`).

| Class | Status | `code` | The remedy sentence says |
|---|---|---|---|
| `InvalidRequest` | 400 | `invalid_request` | What to change in the request |
| `PermissionDenied` (new) | 403 | `permission_denied` | Which permission is missing and where it is granted ("Turn on GPU nodes for this Connection and apply the update in your account.") |
| `NotFound` | 404 | `not_found` | Nothing; the detail names what is gone |
| `NotConfigured` | 409 | `not_configured` | Which setting or setup step is missing |
| `Conflict` | 409 | `conflict` | What is in the way |
| `QuotaExceeded` (new) | 409 | `quota_exceeded` | Which quota, its current value, and where an increase is requested |
| `CredentialUnavailable` | 424 | `credential_unavailable` | Why the Connection could not be used from this host |
| `NotSupported` | 501 | `not_supported` | Nothing |
| `ProviderError` | 502 | `provider_error` | Whatever the provider can say; may be empty |
| `CapacityUnavailable` (new) | 503 | `capacity_unavailable` | Another size, location or pricing to try |

`ProviderError` gains `code: str` and `remedy: str = ""`. Any other exception still answers 502
through `describe_error` (`sdk:infrastructure/plugin.py:190-208`) with `code = "provider_error"`.

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
    hourly: float | None = None        # on demand, USD
    hourly_spot: float | None = None
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
    offers: list[Offer] = field(default_factory=list)
    location: str = ""
    error: str = ""                    # a failed lookup is an empty list with the reason


class CatalogFacet(ABC):
    @abstractmethod
    def catalog(self, request: CatalogRequest) -> Catalog: ...
    def locations(self, *, node_type: str = "") -> Locations: ...   # today's Datacenters, renamed
```

### 2.5 Start a deployment

A provider that hosts deployments adds the `DeploymentProvider` mixin. A deployment is created
only by a launcher, under a `workspace.create` grant (`compute:docs/internal/deployment-lifecycle.md:3-6`).

```python
@dataclass
class PlacementChoice:
    id: str
    label: str = ""
    requires: dict[str, str] = field(default_factory=dict)   # selectable only with these earlier choices


@dataclass
class PlacementField:
    key: str                           # the key sent back in `placement`
    label: str                         # "Region", "Virtual network"
    choices: list[PlacementChoice] = field(default_factory=list)
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
class DeploymentHostnames:
    """Allocated by the launcher. The provider never derives a name."""

    object_service: str                # FQDN
    compute_service: str               # FQDN
    namespace: str = ""                # the parent name deployments on one shared ingress have in common


@dataclass
class DeploymentBootstrap:
    record_id: str                     # the Config Service's deployment record the machine becomes
    config_service_url: str
    token: str = field(default="", repr=False)   # single use; scrubbed from error answers
    launcher_service_id: str = ""


@dataclass
class DeploymentCheckRequest:
    label: str                         # the DNS label the launcher derived from the name
    size: str = ""
    placement: dict[str, str] = field(default_factory=dict)
    storage: DeploymentStorage = field(default_factory=DeploymentStorage)


@dataclass
class DeploymentCheck:
    ok: bool
    reason: str = ""
    ingress_key: str = ""              # same key = same shared ingress; "" = its own address
    checks: list[PreflightCheck] = field(default_factory=list)


@dataclass
class DeploymentRequest:
    deployment_id: str                 # the launcher's durable id; the provider tags resources with it
    name: str                          # as the person typed it, for display and tags
    hostnames: DeploymentHostnames
    bootstrap: DeploymentBootstrap
    size: str = ""                     # an Offer.id of catalog(purpose="deployment"); "" = default
    placement: dict[str, str] = field(default_factory=dict)
    storage: DeploymentStorage = field(default_factory=DeploymentStorage)
    compute_spec: str = ""
    env: dict[str, str] = field(default_factory=dict)
    plugins: list[str] = field(default_factory=list)   # install sources; [] installs the default roster
    owner: str = ""
    mode: Literal["full", "demo"] = "full"
    detailed_diagnostics: bool = False


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
class Address:
    type: Literal["ipv4", "ipv6", "hostname"]
    value: str


@dataclass
class DeploymentResponse:
    provider_id: str
    addresses: list[Address]           # what the launcher points both hostnames at
    endpoints: dict[str, str]          # object_service_url, compute_service_url; https
    location: str = ""
    placement: dict[str, str] = field(default_factory=dict)   # where it actually went
    hourly_rate: float | None = None
    resource_ref: str = ""             # opaque: the provider's reference to what it created
    failure: str = ""                  # resources exist but the launch failed; the launcher records, then deletes
    display: dict[str, str] = field(default_factory=dict)


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


class DeploymentProvider(ABC):
    def deployment_options(self, selected: dict[str, str]) -> DeploymentOptions: ...
    def check_deployment(self, request: DeploymentCheckRequest) -> DeploymentCheck: ...
    def prepare_deployment(self, request: DeploymentRequest) -> DeploymentPrepared: ...
    @abstractmethod
    def create_deployment(self, request: DeploymentRequest) -> DeploymentResponse: ...
    @abstractmethod
    def deployment_state(self, provider_id: str, *, diagnostics: str = "") -> NodeStateResponse: ...
    @abstractmethod
    def delete_deployment(self, provider_id: str) -> NodeStateResponse: ...   # idempotent
    def retain_deployment(self, provider_id: str) -> NodeStateResponse: ...
    def list_deployments(self, *, owner: str = "") -> DeploymentListing: ...
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
3. **Allocate.** The launcher allocates the hostnames (2.6) and checks they are free in its zone.
4. **Prepare**, only when the provider lists `deployment.prepare`. Called with the full request,
   repeatedly, until `ready` or `failed`. It is idempotent, may create what deployments share (a
   load balancer, a certificate), and answers the DNS records that must exist for it to finish.
   The launcher writes those records and calls again after `retry_after_s`. Every record name
   must be under `hostnames.namespace`; the launcher refuses any other. These records are shared
   and stay when a deployment goes. This step exists because a provider-managed certificate is
   validated through the launcher's zone while it is being created: today the provider writes
   that record itself and waits inside the create call for up to 15 minutes
   (`aws:ingress.py:593-655`, `:407-418`).
5. **Create.** One call that returns when the machine exists. It answers `addresses`; the
   launcher writes one record per hostname per address (A, AAAA or CNAME) and records them
   against the deployment. With `certificate == "machine"` the machine requests its certificates
   itself once its names resolve to it, so its startup must wait for that rather than fail on
   the first attempt. `failure` set means resources exist and the launch did not complete: the
   launcher saves its report, removes the records and deletes (`compute:infra/manager.py:3821-3823`).
6. **Readiness** is the launcher's: both `endpoints` answer (`compute:docs/internal/cloud-workspace.md:22-29`),
   while `deployment_state(diagnostics="baseline")` is followed for the reason a boot fails.
   Each endpoint's host must be one of the allocated hostnames; the launcher refuses others.

Afterwards:

- **Delete.** The launcher removes the deployment's records first, then calls
  `delete_deployment`, so a name never points at an address about to be freed
  (`aws:sweep.py:19-21`). Idempotent; a refused step raises and the launcher retries.
- **Retain** removes whatever routes traffic to a failed machine and keeps the machine
  (`aws:plugin.py:457-468`); the launcher removes the records before calling it.
- **State** with `diagnostics="detailed"` is the separately granted collector
  (`sdk:infrastructure/facets.py:244-246`).
- **List** answers every deployment the account runs, tracked by the launcher or not
  (`sdk:infrastructure/facets.py:253-261`).
- **Sweep** covers what a launch or teardown that never finished left in the account. The
  provider releases what no `keep` item owns after `grace_s`, and reports `held_addresses` so the
  launcher can tell which names in its zone point at nothing (`aws:sweep.py:14-27`,
  `compute:infra/manager.py:3257-3410`). It never touches DNS.

`DeploymentRequest` carries no zone id, no VPC, no region as typed fields: placement is the
provider's keys, and the zone is the launcher's.

### 2.6 What the host owns

**Hostname allocation.** The launcher turns a deployment's name into a DNS label and refuses
what cannot be one: lower case, runs of other characters become one hyphen, at most 63
characters, not a punycode label, not a reserved label. These rules and the reserved list move
from the AWS plugin to the host unchanged (`aws:hostnames.py:54-179`). With `zone` the
launcher's zone name:

| `ingress_key` | `object_service` | `compute_service` | `namespace` |
|---|---|---|---|
| `""` | `os.<label>.<zone>` | `compute.<label>.<zone>` | `""` |
| set | `os-<label>.<ns>.<zone>` | `compute-<label>.<ns>.<zone>` | `<ns>.<zone>` |

`<ns>` is `i-` followed by the first 20 hex characters of
`sha256(launcher_service_id, plugin_id, connection account, ingress_key)`. Deriving it from the
Connection's account in the host is what guarantees that two customers never share a namespace,
and so that a wildcard certificate in one customer's account never covers another's names. Both
shapes exist today (`aws:hostnames.py:210-241`, `aws:ingress.py:558`, `:667-673`).

**The DNS zone.** The launcher decides every record, keeps the ledger of what it wrote for
which deployment, claims a name by creating an owner record beside it, and removes exactly what
it wrote. The write itself goes through the plugin the launcher names in a setting, with the
launcher's own identity and never a Connection. That plugin implements the DNS facet:

```python
@dataclass
class DnsZone:
    id: str                            # opaque to the host; read by the DNS plugin
    name: str                          # "deploy.example.com"


@dataclass
class DnsChange:
    zone: DnsZone
    create: list[DnsRecord] = field(default_factory=list)
    delete: list[DnsRecord] = field(default_factory=list)


@dataclass
class DnsRecords:
    records: list[DnsRecord] = field(default_factory=list)
    truncated: bool = False


class DnsFacet(ABC):
    @abstractmethod
    def list_records(self, zone: DnsZone, *, under: str = "") -> DnsRecords: ...
    @abstractmethod
    def change_records(self, change: DnsChange) -> None: ...
```

`change_records` is one atomic change. A create of a name and type that already exists with the
same values succeeds; with other values it raises `Conflict` and nothing in the change is
applied, so two launches racing for a name cannot both win. A delete removes a record only when
it matches exactly as listed, and a missing one is success. Only `A`, `AAAA`, `CNAME` and `TXT`
are ever written. These are the semantics the AWS plugin implements for itself today
(`aws:hostnames.py:305-336`, `aws:ingress.py:421-435`, `:819-857`). Both routes answer 400 when
a Connection header is present: the zone is the launcher's, and the customer's account has no
say over it (`aws:provider.py:210-220`).

The DNS plugin and the deployment provider are independent: an Azure deployment gets its name
in a Route 53 zone because the launcher's DNS plugin is the AWS one.

**Launcher settings.**

| Setting | Environment | Meaning |
|---|---|---|
| `compute-service.infra.deployment-dns-plugin` (new) | `TLC_COMPUTE_DEPLOYMENT_DNS_PLUGIN` | The id of the plugin whose DNS facet writes the zone. Empty: this host launches no deployments. |
| `compute-service.infra.deployment-dns-zone-id` | `TLC_COMPUTE_DEPLOYMENT_DNS_ZONE_ID` | The zone's id, in the DNS plugin's terms. Opaque to the host. |
| `compute-service.infra.deployment-dns-zone-name` | `TLC_COMPUTE_DEPLOYMENT_DNS_ZONE_NAME` | The zone's name. |
| `compute-service.infra.deployment-sweep-interval` | (unchanged) | How often the zone and the accounts are swept. |
| `TLC_COMPUTE_LAUNCHER_KEY` | (unchanged) | The launcher's Config Service key. |

The two zone settings exist (`compute:host_options.py:100-101`, `:272-291`) and stop meaning
"a Route 53 hosted zone the provider writes"; they stop being sent to any provider
(`compute:infra/manager.py:3753-3758`).

**Also the host's:** asking the Config Service for the grant and the identity token before each
call; the deployment record and its reports; choosing where a job runs; the node agent; moving
data to a node. The node agent's storage probe and copy tools understand only `s3`
(`compute:node_agent/__init__.py:292`, `:1739`); making them neutral is host work that this
contract only supplies inputs for (`storage_roots`, `storage_access`).

### 2.7 Reach storage

`StorageFacet` and its twelve routes are unchanged (`sdk:infrastructure/facets.py:54-189`,
`sdk:infrastructure/routes.py:362-499`): they are already provider-neutral, declare their own
optional parts in `StorageCapabilities` (`sdk:infrastructure/types.py:788-857`), and act through
a Connection like every other call. Two things change around them. How a location is written
moves from `connection_defaults.location_schema` to `PermissionSchema.storage`. And the upper
bound on what a Connection may reach is `ConnectionPermissions.storage`, which the provider
checks a project root and data locations against before it creates anything
(`aws:workspace.py:259-290`).

---

## 3. Route table

Every route is under the plugin's worker. "Connection" says whether the host sends the binding:
*never* answers 400 when one is present, *required* answers 400 when absent. "Grant" is the
Config Service operation the host asks for first (`compute:config_service.py:90-107`).

| Operation id | Method and path | Request | Response | Connection | Grant |
|---|---|---|---|---|---|
| (always) | `GET /infra/capabilities` | | `CapabilitiesResponse` | never | |
| `connection.grant` | `POST /infra/connection/grant` | `GrantRequest` | `Grant` | never | |
| `connection.grant_update` | `POST /infra/connection/grant/update` | `GrantUpdateRequest` | `GrantUpdate` | required | `connection.prepare` |
| `connection.verify` | `POST /infra/connection/verify` | `VerifyRequest` | `ConnectionVerification` | required | `connection.verify` |
| `connection.node_access` | `GET`, `POST /infra/connection/node-access` | `NodeAccessRequest` | `ConnectionNodeAccess` | required | `connection.verify` |
| `storage.candidates` | `GET /infra/storage/candidates` | | `StorageCandidates` | required | `storage.discover` |
| `node.create` | `POST /infra/nodes` | `CreateNodeRequest` | `CreateNodeResponse` (201) | optional | `node.create` |
| `node.state` | `GET /infra/nodes/{provider_id}` | | `NodeStateResponse` | optional | `node.describe` |
| `node.delete` | `DELETE /infra/nodes/{provider_id}` | | `NodeStateResponse` | optional | `node.terminate` |
| `node.check` | `POST /infra/nodes/check` | `NodeCheckRequest` | `PreflightResponse` | optional | `node.create` |
| `node.diagnostics` | `GET /infra/nodes/{provider_id}?diagnostics=true` | | `NodeStateResponse` | optional | `node.describe` |
| `catalog` | `POST /infra/catalog` | `CatalogRequest` | `Catalog` | optional | |
| `catalog.locations` | `GET /infra/catalog/locations?node_type=` | | `Locations` | optional | |
| `deployment.options` | `POST /infra/deployments/options` | `{selected}` | `DeploymentOptions` | required | `workspace.create` |
| `deployment.check` | `POST /infra/deployments/check` | `DeploymentCheckRequest` | `DeploymentCheck` | required | `workspace.create` |
| `deployment.prepare` | `POST /infra/deployments/prepare` | `DeploymentRequest` | `DeploymentPrepared` | required | `workspace.create` |
| `deployment.create` | `POST /infra/deployments` | `DeploymentRequest` | `DeploymentResponse` (201) | required | `workspace.create` |
| `deployment.state` | `GET /infra/deployments/{provider_id}?diagnostics=` | | `NodeStateResponse` | required | `node.describe`, `workspace.diagnose` for `detailed` |
| `deployment.delete` | `DELETE /infra/deployments/{provider_id}` | | `NodeStateResponse` | required | `workspace.teardown` |
| `deployment.retain` | `POST /infra/deployments/{provider_id}/retain` | | `NodeStateResponse` | required | `workspace.retain` |
| `deployment.list` | `GET /infra/deployments?owner=` | | `DeploymentListing` | optional | |
| `deployment.sweep` | `POST /infra/deployments/sweep` | `SweepRequest` | `SweepReport` | optional | `node.describe` |
| `storage` | the twelve `/infra/storage*` routes | unchanged | unchanged | required | `storage.*` |
| `dns` | `GET /infra/dns/records?zone_id=&zone_name=&under=` | | `DnsRecords` | never | |
| `dns` | `POST /infra/dns/changes` | `DnsChange` | `{}` | never | |
| (settings) | `GET`, `POST /settings` | unchanged | unchanged | never | |

**Required and optional.**

- `GET /infra/capabilities` is the only route every infrastructure plugin serves.
- A `NodeProvider` must serve `node.create`, `node.state` and `node.delete`. A
  `DeploymentProvider` must serve `deployment.create`, `deployment.state` and
  `deployment.delete`. A `StorageFacet`, `CatalogFacet` or `DnsFacet` must serve its abstract
  methods. A plugin may be any combination, including none (a DNS-only plugin).
- Everything else is optional.

**How optionality is declared.** The SDK fills `capabilities.operations` with the id of every
operation whose route it mounted, and mounts a route only when the plugin's class provides the
method: a mixin's abstract methods, or an optional method the class overrides. This is how the
legacy sign-in routes are mounted today (`sdk:infrastructure/routes.py:643-677`) and it replaces
both `facets` (`sdk:infrastructure/plugin.py:227-229`) and the nine booleans the AWS plugin puts
in `extra` (`aws:plugin.py:226-236`), which the host reads by name
(`compute:infra/manager.py:1693`, `:1696`, `:2791`, `:2814`, `:2871`, `:2877`, `:3239`, `:3304`).
The host calls an operation only when it is listed; an unlisted one is never probed, and a
route that is not mounted answers 404. An operation with nothing to say (a provider with no
checks) is simply not implemented.

**The fate of `extra`.** `CapabilitiesResponse.extra`, `StorageItem.extra`, `Region.extra`,
`StorageListing.extra`, `PresignResponse.extra` and `WorkspaceInstance.extra` are removed
(`sdk:infrastructure/types.py:897`, `:982`, `:1019`, `:1051`, `:1168`, `:1414`). In their place
`CapabilitiesResponse.display` and `DeploymentResponse.display` are `dict[str, str]` with one
rule: display-only. The host stores and forwards them, the Hub may show them as labelled rows,
and neither ever branches on a key. The SDK's lockstep test asserts that the host's source
reads no key of `display`. A plugin that needs to tell its own page something does so on its
own routes (`sdk:infrastructure/plugin.py:66-70`), which the host proxies and never calls.

---

## 4. Config Service shape

**Proposal for the Config Service owner.** This section says what the contract needs of the
Config Service and one way to give it. The storage model, the token issuer and the migration
are that service's to design.

### 4.1 What the contract needs

1. A Connection for any provider slug, not only `aws` (`cs:api/hub/connections.py:86`, `:685-686`).
2. An account reference and access-method fields stored without the Config Service knowing
   their grammar (`cs:api/hub/connections.py:115-123`).
3. Access fields that can be written after create and before a first verification (paste-back
   values), and that are frozen after it.
4. Permissions and their observation without bucket grammar or CloudFormation checks
   (`cs:identity/connection_configuration.py:28-102`, `:188-204`).
5. For a `KEYLESS` Connection: a subject, and a way for the host to obtain a signed identity
   token for it with a given audience.
6. A deployment record that takes any provider's placement keys and resource reference
   (`cs:identity/launch_reports.py:65-70`, `cs:api/hub/workspaces.py:128`).

### 4.2 Proposed request and stored shape

```python
FieldMap = dict[str, str]     # at most 16 keys; key ^[a-z][a-z0-9_]{0,39}$; value 1..2048 chars, no control characters


class StorageRule(StrictModel):
    location: str = Field(min_length=1, max_length=1024)   # no control characters, no credentials
    access: Literal["read", "read_write_no_delete", "read_write"]


class ConnectionPermissions(StrictModel):
    deployments: bool = False
    nodes: bool = False
    storage: list[StorageRule] = Field(default_factory=list, max_length=50)   # canonical order, deduplicated
    options: dict[str, bool] = Field(default_factory=dict, max_length=16)


class CreateConnection(BaseModel):
    provider: str                      # ^[a-z0-9][a-z0-9._-]*$, as today; no fixed list
    kind: Literal["AMBIENT", "KEYLESS", "SECRET"]
    account: FieldMap = {}
    access: FieldMap = {}
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

- `external_id` is generated here for `KEYLESS` and never settable (`cs:api/hub/connections.py:465-466`, `:691`).
- `account` is immutable from create. `access` is writable by a Connection administrator while
  the status is `PENDING` and frozen at the first `VERIFIED`; this replaces the fixed
  `BINDING_FIELDS` list (`cs:api/hub/connections.py:91`, `:779-782`). A rebind is a new Connection.
- A verification is recorded as `VERIFIED` only when the reported `account` equals the stored
  one, key for key. This replaces the ARN arithmetic of `identity_failure`
  (`cs:identity/connection_configuration.py:150-164`).
- An observation is recorded as installed only when its `account` equals the stored one and its
  `status` is `complete`; the revision and generation checks stay as they are
  (`cs:identity/connection_configuration.py:167-227`). Whether the reference belongs to the
  account is the provider's check, made before it reports.
- `setup_identity` and `setup_service_id` stay as they are (`cs:api/hub/connections.py:94-112`);
  the binding carries the first as `trusted_principal`.
- `SECRET` rules are unchanged; a multi-field secret is one sealed JSON value.

What is no longer validated here: the shape of an account id, a role ARN, a bucket name, a
stack ARN, a region code. `PROVIDERS` and the three per-kind metadata models go
(`cs:api/hub/connections.py:86`, `:115-138`); `CloudConfiguration` and its bucket scopes become
`ConnectionPermissions` (`cs:identity/connection_configuration.py:28-94`).

The grant response's `connection` gains `account`, `access`, `external_id`,
`trusted_principal`, `permissions` and `grant_ref` beside what it carries today
(`cs:identity/connections.py:116-131`).

### 4.3 Identity tokens (interface only)

- Creating a `KEYLESS` Connection answers `onboarding: {external_id, subject, issuer}`. The
  subject is unique to the Connection and bound to the organization that created it, which is
  the confused-deputy guard on a cloud with no external id.
- A grant request may carry `identity_token_audience`. The answer then carries
  `identity_token: {token, expires_at}`: a JWT signed by `issuer` with that `sub` and `aud`. It
  is issued under exactly the checks the grant itself passed, to the host that asked.
- The issuer publishes its keys at the standard discovery path under `issuer`, reachable from
  the public internet, since the customer's tenant fetches them.

### 4.4 Deployment record

`placement` takes any `FieldMap` (`cs:identity/launch_reports.py:65-70`,
`compute:config_service.py:884`); `stack_id` is read as an opaque resource reference
(`cs:api/hub/workspaces.py:128`, `cs:db/models/workspace.py:119`); `ingress` stays a bounded
display map.

### 4.5 Existing Connections and older services

- **Stored rows.** Every stored cloud Connection is an AWS one with `metadata.role_arn`,
  `metadata.external_id` and optional `region`, `session_duration_s`, `node_instance_profile`.
  A one-time back-fill gives each row `account = {account_id, region?}` and
  `access = {role_name, session_duration_s?}`, both read out of the ARN, and converts an
  installed `storage_access` into `ConnectionPermissions` (a bucket scope becomes
  `s3://bucket/prefix`, a pattern `s3://prefix*`, an account scope `s3://*`). Nothing a row
  identifies changes, so its grants resolve to the same role.
- **Reading back unchanged.** For an `aws` row the API keeps answering `metadata` and
  `configuration_state` exactly as now, computed from the new columns, and keeps accepting the
  current create and patch bodies for `provider == "aws"`. A client that only knows the current
  shape sees no difference.
- **A Compute Service at the floor.** No released Compute Service reads Connections: the last
  release is 1.1 (`compute:CHANGELOG.md:151`), which has no Connections client at all. The
  1.2 line is the first that does; it reads `connection.metadata` and
  `connection.configuration` from a grant (`compute:config_service.py:609-632`). Once 1.2 is
  released it is the floor, and the Config Service must keep emitting those two keys for `aws`
  Connections for as long as 1.2 is supported. A 1.2 Compute Service is offered only `aws`
  Connections it can use (it lists by provider, `compute:connections.py:296-304`), so a
  Connection of another provider is invisible to it and harmless.

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
| `region` | `sdk:infrastructure/types.py:891` | `default_location` |
| `workspace_fields` | `sdk:infrastructure/types.py:892` | removed with the legacy facet |
| `facets`, `lists_workspaces`, `facets_reported` | `sdk:infrastructure/types.py:894-900` | `operations` |
| `requires_connection` | `sdk:infrastructure/types.py:896` | unchanged |
| `extra` | `sdk:infrastructure/types.py:897` | typed fields, or `display` |
| `SettingsField` | `sdk:infrastructure/types.py:185-232` | `FormField` (`placeholder` is `example`) |
| `ProjectStorage` | `sdk:infrastructure/types.py:239-264` | `storage_roots: list[StorageRoot]` |
| `CreateNodeRequest.flavor`, `.workspace` | `sdk:infrastructure/types.py:407-413` | `kind`; `DeploymentRequest` |
| `CreateNodeResponse.services`, `.ingress`, `.managed_by` | `sdk:infrastructure/types.py:478-485` | `DeploymentResponse.endpoints`, `.display`; removed |
| `ConnectionCheckRequest` | `sdk:infrastructure/types.py:700-719` | `VerifyRequest`, `NodeAccessRequest` |
| `ConnectionCheckResponse` | `sdk:infrastructure/types.py:722-766` | `ConnectionVerification` |
| `GpuCatalog`, `CpuCatalog` | `sdk:infrastructure/types.py:1306-1355` | `Catalog` with a `purpose` |
| `Datacenters` | `sdk:infrastructure/types.py:1358-1398` | `Locations` |
| `WorkspaceFacet` | `sdk:infrastructure/facets.py:230-261` | `DeploymentProvider` |
| `WorkspaceInstance`, `WorkspaceListing` | `sdk:infrastructure/types.py:1404-1459` | `DeploymentListing` |
| `WorkspacePlacement` | `sdk:infrastructure/types.py:1576-1620` | `DeploymentOptions` |
| `StorageFacet` and its types | `sdk:infrastructure/facets.py:54-189` | unchanged, minus `extra` |
| `LegacyOwnerCredentialsFacet` and its types | `sdk:infrastructure/facets.py:264-373`, `sdk:infrastructure/legacy.py` | removed (open decision 8) |
| `Ambient`, `SecretToken` | `sdk:connections.py:269-341` | kept; `Ambient` gains `account`, `SecretToken` gains `fields()` |
| `AwsSession` | `sdk:connections.py:282-302` | moves into the AWS plugin as its `ProviderCredential` |
| `ConnectionBinding.metadata`, `.configuration` | `sdk:connections.py:131-184` | `account`, `access`, `external_id`, `trusted_principal`; `permissions` |
| `register_resolver(provider, kind, fn(binding))` | `sdk:connections.py:358-376` | `register_resolver(provider, fn(ResolveRequest))` |
| `resolve()`, `connection_middleware` | `sdk:connections.py:379-397`, `:545-610` | same roles; also bind the identity token and refuse Connection headers on ambient-only routes |
| `x-3lc-connection`, `x-3lc-connection-use`, `x-tlc-bound-credential` | `sdk:connections.py:94-111` | unchanged; `x-3lc-connection-token` added |
| `ProviderError` and subclasses | `sdk:infrastructure/errors.py:39-76` | gain `code`, `remedy`; three new subclasses |

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
| `dns_zone_id`, `dns_zone_name` | removed; the launcher sends `hostnames` |
| `detailed_diagnostics` | `detailed_diagnostics` |
| `public` | removed; no provider reads it |
| `seed_provider_configs`, transient `credentials`, `provider_configs` | removed with the legacy facet |

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
| `role_arn_template`, `account_id_label`, `account_id_pattern`, `account_id_hint`, `account_help` | `aws:plugin.py:333-340` | `account.fields`; the plugin composes the ARN |
| `node_instance_profile_template`, `deployment_instance_profile_template` | `aws:plugin.py:334-335` | derived in the plugin from the binding |
| `location_schema`, `location_fields`, `storage_access_modes`, `storage_access_max_rules`, `storage_access_max_prefix_length` | `aws:plugin.py:241-320` | `permissions.storage` |
| `permission_switches` | `aws:plugin.py:270-312` | `permissions.options` |
| `configuration` (defaults) | `aws:plugin.py:321-332` | `PermissionOption.default` |
| `storage_candidates` | `aws:plugin.py:313` | `operations` lists `storage.candidates` |
| `template_version` | `aws:plugin.py:336` | `Grant.version`, `GrantObservation.version` |
| `summary`, `quick_create_help`, `cloud_name`, `setup_help` | `aws:plugin.py:341-345` | `AccessMethod.summary`, `Grant.steps`, `display_name` |
| `host_identity`, `host_principal`, `host_connection`, `host_is_deployment` | `aws:plugin.py:242`, `:359-371` | `host` |
| `not_ready_reason` | `aws:plugin.py:351` | `not_ready_reason` |
| `workspace_sizes` | `aws:plugin.py:225` | `catalog(purpose="deployment")` |
| `AwsConnectionCheckResponse.stack` | `aws:plugin.py:131-151` | check rows, `observation`, `facts` |
| `_ambient_route53` and every Route 53 call | `aws:provider.py:210-220`, `:3758-3844`, `aws:ingress.py:647`, `:693`, `:824`, `aws:sweep.py:439` | the AWS plugin's `DnsFacet`, called by the launcher |
| `hostnames.py` label rules and reserved labels | `aws:hostnames.py:54-179` | the host |
| `ingress.identity` (the namespace hash) | `aws:ingress.py:241-244`, `:558` | `DeploymentCheck.ingress_key`; the host derives the namespace |
| `_trust_statements`, `_connection_template` | `aws:provider.py:3226`, `:5530` | stay in the plugin, behind `describe_grant` |

**Host**

| Today | Where it is | Becomes |
|---|---|---|
| `caps.raw.get(...)` for nine flags | `compute:infra/manager.py:1693-3304` | `operations` |
| `host_scope` reading `host_principal`, `host_connection` | `compute:infra/manager.py:2892-2909` | `capabilities.host` |
| `acts_elsewhere` comparing `metadata` | `compute:connections.py:213-236` | compares `account` and `access` with `host.deployment_of`, `trusted_principal` with `host.principal` |
| `connection_onboarding(kind, external_id, metadata, stack_id, ...)` | `compute:infra/manager.py:2829-2890` | builds a `GrantRequest` |
| `verify_connection` reading `metadata.configuration_observation` | `compute:infra/manager.py:2694-2696` | reads `observation`, reports `account` |
| `_is_permission_refusal` matching words | `compute:infra/manager.py:672-688` | `code == "permission_denied"` |
| `_deployment_dns_zone` injected into the request | `compute:infra/manager.py:952-968`, `:3753-3758` | the launcher's DNS module |
| `check_deployment_name` | `compute:infra/manager.py:3209-3251` | label rules and zone lookup in the host, then `deployment.check` |
| `sweep_deployment_names` | `compute:infra/manager.py:3257-3410` | zone sweep in the host, `deployment.sweep` per Connection |
| `_WORKSPACE_KEYS` | `compute:infra/manager.py:250-269` | `DeploymentRequest` fields |
| `_provision` for a workspace | `compute:infra/manager.py:3716-3824` | check, allocate, prepare, create, write records |
| `WORKSPACE_PLACEMENT_KEYS` | `compute:config_service.py:884` | any placement keys |
| `report_workspace(stack_id=...)` | `compute:config_service.py:890-969` | sends `DeploymentResponse.resource_ref` |
| `deployment-dns-zone-id`, `-name` options | `compute:host_options.py:100-101` | kept; `deployment-dns-plugin` added |
| `ProviderClient` and `TIMEOUTS` | `compute:infra/provider_client.py:63-90` | one entry per operation id |
| Visitor capability allow-list by `extra` key | `compute:routes/infra.py:452-476` | by typed field: `account`, `access_methods`, `permissions`, `deployments`, `operations`, `host.principal` |

**Hub**

| Today | Where it is | Becomes |
|---|---|---|
| Two hard-coded access cards | `hub:static/js/connection-create.js:359-365` | rendered from `access_methods` |
| Account id and role name form | `hub:static/js/connection-create.js:2178-2191` | rendered from `account.fields` and the method's `fields` |
| `composedRoleArn` | `hub:static/js/connection-create.js:1386-1390` | removed |
| Reading `connection_defaults` | `hub:static/js/connection-create.js:814-824` | reads the typed capabilities |
| Bucket-name editor | `hub:static/js/connection-create.js:197`, `:284`, `:1812` | location editor from `permissions.storage` |
| Onboarding panel reading 25 keys | `hub:static/js/connection-create.js:692-701`, `:1949-2091` | renders `Grant` |
| Four placement selects | `hub:static/js/deployment-launch.js:138` | rendered from `DeploymentOptions.placement` |
| `location(placement)` | `hub:static/js/deployment-launch.js:127-130` | `DeploymentResponse.location` |

### 5.2 The Azure reading of every operation

| Operation | Azure |
|---|---|
| `capabilities.account` | `tenant_id`, `subscription_id`, `resource_group`, optional `location` |
| `AMBIENT` method | Available when the Azure credential chain finds a sign-in (an `az login` session, or a managed identity). The plugin opts its own client and the 3LC core into the CLI sign-in. |
| `KEYLESS` method | `fields`: `identity_name`. `paste_back`: `client_id` of the control identity. `identity_token.audience`: `api://AzureADTokenExchange`. Unavailable unless the host can mint tokens. |
| `host` | The object id of the machine's own identity, for `AMBIENT` scoping; `deployment_of` on a deployment VM |
| `describe_grant` | A "Deploy to Azure" portal link to a template that creates the control, deployment and node identities, the federated credential (issuer and subject from `GrantRequest.identity`) and role assignments scoped to the resource group. `files`: the template and parameters. `command`: `az deployment group create ...`. |
| `prepare_grant_update` | Not implemented. An update is `describe_grant(action="update")`: the same template redeployed. |
| `verify_connection` | Token exchange, then read the subscription and resource group, the role assignments and the identities; `account` from what ARM answers; `observation` from the template deployment's outputs |
| `connection_node_access` | On own sign-in with rights to assign roles: create the node identity and its storage role assignment directly |
| `storage_candidates` | Storage accounts and containers in the resource group, as `abfss://` locations |
| Resolver | `ClientAssertionCredential`-style exchange of `identity_token.token` for an ARM or storage token; returns the plugin's own credential class |
| `create_node` | VM with the node identity attached at create, the agent installed by cloud-init; `storage_access = "identity"` |
| `node_state.interruption` | From the eviction the platform reports for a spot VM |
| `catalog` | VM sizes with retail prices and per-family quota; `unavailable_reason` names a zero quota |
| `QuotaExceeded` | The family and region, and that an increase is a support request in the portal |
| `deployment_options` | `location`, then `virtual_network`, then `subnet` |
| `check_deployment` | Quota and placement; `ingress_key = ""` |
| `prepare_deployment` | Not implemented |
| `create_deployment` | A public IP and a VM with the deployment identity; `addresses` is the public IPv4; `certificate = "machine"` |
| `delete_deployment`, `sweep_deployments` | Delete the VM, disk, NIC and public IP by the deployment tag; sweep releases untracked ones |
| `StorageFacet` | Containers under storage accounts in the resource group |
| `DnsFacet` | Not implemented; a launcher with an Azure DNS zone would need it |

Nothing in the table is an AWS idea under another name: there is no external id (the subject
and issuer do that work), no instance profile, no stack ARN (the reference is opaque), no
shared ingress, no change set, and no Route 53.

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
and storage over network volumes. `storage_access` is `"none"` until leases exist. It implements
no grant, no node identity and no deployments. A launcher is never granted a secret
(`cs:db/models/connection.py:105-113`), which is consistent: RunPod hosts no deployments.

---

## 6. Build plan

### 6.1 Packages

| # | Package | Repo | Delivers | Depends on |
|---|---|---|---|---|
| A | SDK contract | 3lc-compute-plugin-sdk | The types, mixins, routes, middleware, errors, `operations`, the conformance kit and `FakeProvider` for generation 2; golden wire fixtures | nothing |
| B | Host adoption | 3lc-compute | `ProviderClient` on the new routes, binding and token headers, `operations` gating, typed errors | A |
| C | Launcher DNS | 3lc-compute | Label rules, hostname allocation, the record ledger, zone sweep, the three settings, the prepare and create sequence | A |
| D | Config Service | tlc-config-service | Neutral Connection fields, observation, deployment record, identity tokens | the shapes in section 4 |
| E | Hub form | 3lc-hub-frontend | Connection form, grant panel and launch wizard rendered from capabilities | A's fixtures |
| F | AWS port | 3lc-compute-plugin-aws | The plugin on generation 2, including its `DnsFacet` | A |
| G | Machines and RunPod port | their repos | The capability shape and mixins; RunPod's secret method | A |
| H | Azure nodes | 3lc-compute-plugin-azure | Capabilities, own sign-in, the resolver, nodes, catalog, storage | A |
| I | Azure grant | 3lc-compute-plugin-azure | The template, `describe_grant`, verification | A, H's resolver |
| J | Azure deployments | 3lc-compute-plugin-azure | Options, check, create, state, delete, sweep | A, H |

### 6.2 Interfaces between packages

- **A to everyone: the wire.** A publishes, with its first staged build, one JSON fixture per
  type under `tests/fixtures/contract/` and the `FakeProvider` that emits them. These are the
  interface. B and C test against `FakeProvider` in process, as the host's lockstep tests do
  today (`compute:infra/provider_client.py:10-17`). E renders from the capability and grant
  fixtures and needs no running plugin. F to J are done when
  `tlc_plugin_sdk.infrastructure.testing.check_provider` passes for every operation they list
  (`sdk:infrastructure/testing.py:608-618`).
- **B and C to D: the Config Service API of section 4.** The host has a fake Config Service for
  its tests (`compute:tests/fake_config_service.py`); B and C extend the fake to section 4 and
  build against it. D builds to the same section. The first joint run is the acceptance run.
- **C to F: the DNS facet.** C tests against `FakeProvider`'s in-memory zone. F's facet is done
  when the conformance kit's DNS checks pass against a fake Route 53.
- **H and I to D: the identity token.** Until D issues tokens, H and I use a test issuer: a
  signing key and a discovery document served from a public storage container in the sandbox,
  with the token minted by a test helper and sent in `x-3lc-connection-token`. The resolver
  cannot tell the difference, which is the point of pushing the token.
- **I and J to C:** none beyond A. J returns `addresses`; C writes them.

### 6.3 What each can stub

| Package | Stubs | So it can start |
|---|---|---|
| B, C | Config Service (the fake), providers (`FakeProvider`) | as soon as A's types compile |
| D | Compute (its own integration tests) | now |
| E | Compute and plugins (fixtures) | as soon as A's fixtures exist |
| F | Config Service and Hub (the conformance kit and its existing tests) | as soon as A's types compile |
| H | identity token (test issuer), Hub (the conformance kit) | as soon as A's types compile; own sign-in needs nothing else |
| I | Config Service subject and issuer (constants) | with H's resolver |
| J | DNS (reads back `addresses`; names the VM by IP in the sandbox) | with H |

### 6.4 Order of merges

1. **A**, staged as an SDK 0.8 development build. Nothing else merges before it.
2. **D**, additive: the new fields and endpoints beside the current ones, the back-fill, and the
   legacy projection of 4.5. It breaks no current client, so it can go live as soon as it is
   ready and before any consumer.
3. **B, C, F and G together.** The host pins one SDK minor and the fleet moves with it
   (`compute:pyproject.toml:37-44`), so a host on 0.8 needs every infrastructure plugin on 0.8.
   F is the proof: the AWS acceptance run (connect an account, start a node and train from
   storage, start a deployment and reach it) must pass on the dev account before this step
   merges.
4. **E**, after step 3 is on the dev stack. Until then the Hub keeps its current form.
5. **H**, then **I**, then **J**, each with its own acceptance run in the sandbox.

### 6.5 Versions

- **SDK 0.8.0** is contract generation 2. It is a breaking minor; nothing it removes was in a
  released SDK except the core of 0.5 (see Migration).
- **Compute Service 1.3** is the first host that speaks generation 2, and speaks only it: it
  depends on `3lc-compute-plugin-sdk>=0.8,<0.9` and refuses to register an infrastructure plugin
  whose capabilities do not say `contract: 2`, with a sentence naming the plugin to update.
- **Every generation 2 plugin** declares `min_service_version = "1.3"` and depends on
  `3lc-compute-plugin-sdk>=0.8.0,<0.9.0`. The catalog then never offers one to a 1.2 host, and a
  1.2 host keeps resolving the 0.7-line builds of the same plugins.
- **The Hub** must keep working against every released Compute Service at or above its floor.
  Against a Compute Service whose capabilities carry `contract: 2` it renders generation 2;
  against one that does not, it keeps the current form. The current form can be deleted when the
  floor reaches 1.3.
- **The Config Service** serves both for as long as 1.2 is supported (4.5).
- **A deployment** runs the Compute Service line of its launcher
  (`compute:docs/internal/cloud-workspace.md:39-46`), so a 1.3 launcher creates 1.3 deployments
  that install generation 2 plugins from the catalog. No mixed pair arises from a launch.

---

## 7. Open decisions

Each of these changes the contract. The recommendation is what the sections above assume.

1. **How a plugin gets the identity token.**
   (a) The host pushes it in a header with every call. (b) The plugin pulls it from the host
   through a new callback channel. (c) The plugin calls the Config Service itself.
   **Recommend (a).** There is no plugin-to-host channel today and (a) needs none; the plugin
   can never ask for a token for a Connection the request was not granted; a test issuer can
   stand in trivially. Its cost is that a credential cannot be renewed inside one long request,
   which is already true of an AWS session. Blocks A (middleware), B, H, I and D's token
   endpoint.

2. **Where the DNS write lives.**
   (a) A `DnsFacet` on the plugin a launcher setting names. (b) A DNS client inside the host.
   **Recommend (a).** The host carries no cloud SDK and declares no extras
   (`compute:pyproject.toml:36-70`); the AWS plugin already holds the Route 53 code and the
   ambient-identity seam for it. (b) keeps the public SDK one facet smaller but puts a cloud
   dependency in every Compute Service. Blocks A (the facet), C, F.

3. **Shared-ingress namespaces.**
   (a) Keep them: the provider answers an `ingress_key` and the launcher derives a namespace.
   (b) Every deployment's names are directly under its own label, and a provider with a shared
   load balancer issues one certificate per deployment.
   **Recommend (a).** (b) is one concept fewer, but on AWS it adds certificate issuance to every
   launch and runs into the per-listener certificate limit. Blocks `DeploymentCheck`,
   `DeploymentHostnames`, C, F.

4. **Synchronous create with a prepare step, or an asynchronous create.**
   (a) `prepare` polled until ready, then one synchronous `create`. (b) `create` returns at once
   and the launcher polls state for addresses and records.
   **Recommend (a).** The AWS shared-ingress setup is already idempotent and re-entrant, with
   CloudFormation as its ledger (`aws:ingress.py:535-622`), so it becomes `prepare` with little
   change; (b) needs every provider to keep launch state across worker restarts. Blocks the
   deployment routes, C, F, J.

5. **Wire names of the access methods.**
   (a) Keep `AMBIENT`, `KEYLESS`, `SECRET` on the wire and change only the words a person reads.
   (b) Rename the kinds.
   **Recommend (a).** The kinds are in stored rows, grants and the host's scoping rules; the
   meaning does not change. Blocks A, D.

6. **Who validates account and access fields.**
   (a) The Config Service stores bounded opaque maps; the plugin validates grammar, the Config
   Service validates equality and immutability. (b) The Config Service keeps a model per
   provider. (c) The host attests the plugin's field schema and the Config Service validates
   against it.
   **Recommend (a).** (b) puts every new provider into the Config Service; (c) adds a trust
   hop for little. The check the Config Service keeps (the verified account equals the stored
   one) is the one that matters to it. This needs the Config Service owner's agreement that
   today's ARN and stack checks are not relied on as a control against a faulty host. Blocks D,
   E.

7. **Storage rules as URL patterns or as structured scopes.**
   (a) `location` is a URL or URL pattern in the provider's grammar. (b) A structured scope with
   neutral names (container, container prefix, account).
   **Recommend (a).** It is the same string a person sees as a project root, the Config Service
   needs no grammar, and Azure's two-level naming (account, then container) fits without a
   second scope dimension. Blocks `ConnectionPermissions`, D, E, F.

8. **The legacy owner-credentials facet.**
   (a) Remove it in 0.8: no request carries cloud keys. (b) Keep it, unchanged and dict-typed,
   outside generation 2.
   **Recommend (a)**, on the condition that no hosted flow still creates a deployment with a
   visitor's pasted keys or a signed-in session. If one does, (b) until it is retired; the
   facet is then not ported to Azure. Blocks A's scope, B, F.

9. **Whether the method is derived or chosen.**
   (a) Derived, with the other available methods behind a disclosure. (b) Always chosen, as
   today.
   **Recommend (a).** It cannot be purely derived: on a self-hosted machine with an AWS sign-in
   both own sign-in and a grant work, and they differ in what they set up. `preferred` carries
   the provider's answer. Blocks `AccessMethod`, E.

---

## Migration from 0.7

For someone porting a plugin or a host. The last released SDK is 0.5.0, whose infrastructure
surface is the four core methods, the optional preflight and `CapabilitiesResponse.extra`
(`sdk:CHANGELOG.md:548-570`); everything else below exists only on the 0.6 and 0.7 lines.

**Removed**

- `CapabilitiesResponse.extra` and every other `extra` field. Use the typed field, or `display`.
- `CapabilitiesResponse.facets`, `facets_reported`, `lists_workspaces`, `flavors`, `missing`,
  `workspace_fields`. Use `operations`, `nodes`, `deployments`.
- `CreateNodeRequest.flavor` and `.workspace`; `CreateNodeResponse.services`, `.ingress`,
  `.managed_by`; `WorkspaceRequest`; `ProjectStorage`.
- `WorkspaceFacet`, `WorkspaceInstance`, `WorkspaceListing`, `WorkspacePlacement`.
- `LegacyOwnerCredentialsFacet`, `OwnerCredentialsDescriptor`, `LoginDescriptor`,
  `RoleDescriptor`, `tlc_plugin_sdk.infrastructure.legacy`, and the request-carried
  `credentials` and `provider_configs`.
- `connections.AwsSession`.
- `ConnectionBinding.metadata` and `.configuration`.
- `GpuCatalog`, `CpuCatalog`, `preflight_query`, the `gpu_type` and `gpu_types` aliases.
- `dns_zone_id` and `dns_zone_name` anywhere in a request to a provider.

**Renamed or reshaped**

| 0.7 | 0.8 |
|---|---|
| `SettingsField` (`placeholder`) | `FormField` (`example`) |
| `preflight`, `preflight_with_context`, `GET /infra/preflight` | `check_node`, `POST /infra/nodes/check` |
| `connection_check`, `connection_check_with_context`, `GET /infra/connection/check` | `verify_connection`, `POST /infra/connection/verify` |
| `ConnectionCheckRequest` | `VerifyRequest`, `NodeAccessRequest` |
| `ConnectionCheckResponse` (`metadata`) | `ConnectionVerification` (`account`, `observation`, `facts`) |
| `gpu_catalog`, `cpu_catalog`, `GET /infra/gpu-catalog`, `/infra/cpu-catalog` | `catalog`, `POST /infra/catalog` |
| `datacenters`, `Datacenters`, `GET /infra/datacenters` | `locations`, `Locations`, `GET /infra/catalog/locations` |
| `list_workspaces`, `GET /infra/workspaces` | `list_deployments`, `GET /infra/deployments` |
| `workspace_placement`, `GET /infra/workspace-placement` | `deployment_options`, `POST /infra/deployments/options` |
| `workspace_diagnostics`, `GET /infra/workspace-diagnostics/{id}` | `deployment_state(diagnostics="detailed")` |
| `workspace_retain_failed`, `POST /infra/workspace-retain/{id}` | `retain_deployment`, `POST /infra/deployments/{id}/retain` |
| `create_node` with `flavor == "workspace"` | `create_deployment`, `POST /infra/deployments` |
| `register_resolver(provider, kind, fn(binding))` | `register_resolver(provider, fn(ResolveRequest))` |
| `InfrastructurePlugin` with four abstract node methods | `InfrastructurePlugin` plus the `NodeProvider` mixin |
| Error body `{detail}` | `{detail, code, remedy}` |

**Added**

`NodeProvider`, `DeploymentProvider`, `DnsFacet`; `describe_grant`, `prepare_grant_update`,
`storage_candidates` as typed operations; `ProviderCredential`, `ResolveRequest`,
`IdentityToken`, `credential_as`, the `x-3lc-connection-token` header; `PermissionDenied`,
`QuotaExceeded`, `CapacityUnavailable`; `NodeStateResponse.interruption`;
`CapabilitiesResponse.contract`, `operations`, `account`, `access_methods`, `host`,
`permissions`, `nodes`, `deployments`, `display`.
