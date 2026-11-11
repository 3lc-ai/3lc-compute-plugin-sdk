# API reference

The public surface of `tlc_plugin_sdk`. Everything below is generated from the
package's own docstrings.

## The contract

```{eval-rst}
.. autoclass:: tlc_plugin_sdk.ComputePlugin
   :members:

.. autoclass:: tlc_plugin_sdk.JobContext
   :members:

.. autoclass:: tlc_plugin_sdk.JobIdentity
   :members:

.. autodata:: tlc_plugin_sdk.SDK_CONTRACT_VERSION
```

`SDK_CONTRACT_VERSION` is the one contract axis: this package's own version — the
dependency *pin* a plugin resolves against (`3lc-compute-plugin-sdk>=X,<Y`) and the version
of both the Python surface (`ComputePlugin` / `JobContext`) and the browser surface
(`PLUGIN_API` / `PluginJobs` / `TlcData`, declared in
`tlc_plugin_sdk/contract/plugin-api.d.ts`). The host and frontend implement a range and
compare compatibility on `MAJOR.MINOR`. See `docs/plugin-guide.md` → "Version & Compatibility".

The `.d.ts` ships in the wheel at
`<site-packages>/tlc_plugin_sdk/contract/plugin-api.d.ts`; a plain-JS `ui.html` references
it with `/// <reference types="3lc-compute-plugin-sdk/contract/plugin-api" />`.

## The browser contract

The browser surface a plugin's `ui.html` programs against — `PLUGIN_API`, `PluginJobs`,
and the `TlcData` helper — is declared in `plugin-api.d.ts`.

```{eval-rst}
.. automodule documents every exported symbol in the file, so new contract
   interfaces appear here automatically. The module key is the source filename
   with TypeDoc's trailing ``.ts`` stripped: ``plugin-api.d.ts`` -> ``plugin-api.d``.
.. js:automodule:: plugin-api.d
```

## Infrastructure plugins

The typed provider contract: the base, the facets a provider opts into, the wire dataclasses,
the error family, the legacy request-credential context, and the conformance kit.

```{eval-rst}
.. automodule:: tlc_plugin_sdk.infrastructure.plugin
   :members:

.. automodule:: tlc_plugin_sdk.infrastructure.facets
   :members:

.. automodule:: tlc_plugin_sdk.infrastructure.types
   :members:

.. automodule:: tlc_plugin_sdk.infrastructure.errors
   :members:

.. automodule:: tlc_plugin_sdk.infrastructure.legacy
   :members:

.. automodule:: tlc_plugin_sdk.infrastructure.testing
   :members:
```

## The harness

A plugin's own routes, called in-process without a compute service.

```{eval-rst}
.. automodule:: tlc_plugin_sdk.harness
   :members:
```

## Run input lineage

`tlc_plugin_sdk.shared.run_inputs.run_inputs(run, record=None)` resolves exact
input table URLs. Explicit run inputs retain their original indices and recorded `name`, when present.
Inputs in the project dataset layout also expose `dataset_name`, allowing orchestration to
select the same dataset across input reordering and table revisions. Neither field implies
that a dataset is for training or validation; choose it from verified project evidence. For older
runs without explicit inputs, it resolves foreign references from each metrics
table and deduplicates by absolute URL. Value-map keys belong to individual
metrics tables and are never treated as global split identifiers. Each result
contains `index`, `table_url`, `rows` (when recorded), and `source` (`run` or
`metrics`). An optional current JSON record avoids stale mutable run metadata.

New trainers should call `run.add_input_table` on their exact source tables and
`run.set_parameters` with their resolved settings before collecting metrics.

## The UI lint

A UI fragment checked against the Hub's styling conventions.

```{eval-rst}
.. automodule:: tlc_plugin_sdk.lint
   :members:
```

## The worker

```{eval-rst}
.. automodule:: tlc_plugin_sdk.worker
   :members:
```

## Shared utilities

Plugin-facing helpers that depend only on `tlc` + the standard library — staged
here so out-of-process (venv) plugin workers can import them without pulling in
the service.

```{eval-rst}
.. automodule:: tlc_plugin_sdk.shared.aliases
   :members:

.. automodule:: tlc_plugin_sdk.shared.config_store
   :members:

.. automodule:: tlc_plugin_sdk.shared.generic_job
   :members:

.. automodule:: tlc_plugin_sdk.shared.images
   :members:

.. automodule:: tlc_plugin_sdk.shared.labels
   :members:

.. automodule:: tlc_plugin_sdk.shared.modality
   :members:

.. automodule:: tlc_plugin_sdk.shared.model_storage
   :members:

.. automodule:: tlc_plugin_sdk.shared.naming
   :members:

.. automodule:: tlc_plugin_sdk.shared.settings
   :members:

.. automodule:: tlc_plugin_sdk.shared.url_utils
   :members:
```


### Deployment placement and diagnostics

`WorkspaceFacet.workspace_placement(region="", vpc_id="")` backs
`GET /infra/workspace-placement`. It executes under the request Connection and returns
`WorkspacePlacement`: region, network, zone and subnet choices; defaults; shared and deployment
cost estimates; warnings; and blocking preflight findings. Choices use `id` and `name`, with
subnets referring to `network_id` and `zone_id`. Unsupported providers return 501.

`WorkspaceRequest` adds `subnet_id`, `availability_zone`, `launcher_service_id` and
`detailed_diagnostics`. The host supplies the authenticated launcher identity; providers must
not treat a user-supplied identity as authorization. Explicit placement must be validated and
never silently replaced by another location. `CreateNodeResponse.placement` records actual
`account_id`, `region`, `vpc_id`, `subnet_id`, and `availability_zone`. Its `ingress` object
records shared ingress identity and provider recovery metadata; it must contain no credentials.

`NodeStateResponse.bootstrap_stage` names the current startup stage. `diagnostics` contains
bounded entries with `source`, `status`, and `detail`, including unavailable sources. Providers
must redact secrets before returning diagnostics. A host can preserve these observations
before cleanup; requesting detailed diagnostics never grants permission to run arbitrary commands.

`WorkspaceFacet.workspace_retain_failed(provider_id)` backs the host-only
`POST /infra/workspace-retain/{provider_id}` operation. Providers remove public service routing
while preserving the machine and shared infrastructure; the host must authorize this separately
and enforce its persisted expiry. Unsupported providers return 501 and the host cleans up rather
than retaining an exposed failure. Detailed collection uses the separate host-only
`GET /infra/workspace-diagnostics/{provider_id}` hook under a diagnostics grant.

## Recovering a worker job after disconnect

SDK 0.6.1+ workers advertise `job_status_recovery` in `/health.capabilities`. The authenticated,
reserved `GET /jobs/{job_id}/status` route returns `{job_id, state, revision, events}`. `events`
contains current generic progress, latest metrics, result and terminal `done` or `error`.
It is a bounded snapshot, not an event history: custom events and intermediate logs are not replayed.
A controller may poll this route after losing the run stream, using the original job ID. It must
not POST the job again to reconnect. Unknown IDs return 404; normal worker-token guards apply.

The worker persists an initial running marker and a terminal snapshot under the job's state
folder (`worker-status.json`). If a new worker process finds a running marker without the live
job, it reports that the worker exited before saving the outcome (for example, a crash or OOM).
Granted token values are masked in status responses and saved snapshots. Recent completions
are also retained in memory so a disk-write error does not hide a terminal event. Removing the
job's state folder removes its saved outcome; these small records follow the state folder's
retention. Recoverability therefore lasts only while the worker's state storage is retained.

## Frame runtime asset

```{eval-rst}
.. automodule:: tlc_plugin_sdk.shared.frame_runtime
   :members:
```

The host serves this assembled asset; plugins never bundle their own copy.
See [browser runtime](browser-runtime.md) for protocol and release requirements.

### `JobContext.request_host(operation, payload, timeout=120)`

Request an authorized deployment operation over the existing worker channel.
Returns the JSON result, raises on refusal/cancellation, and raises `TimeoutError`
when the outcome is unknown. Up to 16 pending requests per job, at most 8 MiB each and
3,600 seconds. The host validates the job and chooses the authority; no secret
is implied by this API. See the agent guide for reconnect and retry semantics.


### Credential-free Python data access

`tlc_plugin_sdk.data_access.install(transport, bootstrap, workspace)` installs the
real 3LC SDK's read adapters inside an OS-isolated Python process. The embedding
host supplies a synchronous, bounded transport and a claims-only SDK identity
handoff. It must authorize each request against the current job owner and its
configured data roots. The adapter is not a sandbox or an authorization boundary.

Supported operations are `read`, `exists`, `is_dir`, `stat`, and `list`. Reads use
base64 chunks with a verified total size (64 MiB maximum); file, S3, GCS and Azure
ADLS URLs pass through the host. Only workspace files can be written. Refresh the
installation for each turn of a reused kernel; never put provider keys, storage
credentials, or service tokens in the bootstrap. See the implementation's
`Transport` type for the request/response contract.

### Verified caller membership

Plugins that enforce organization or team roles declare `caller_profile = true`
in `[runtime]`. Inside a route, `tlc_plugin_sdk.caller.current_caller()` names the
person and organization, and `current_profile()` returns their verified
`org_role`, `team_ids`, and email. The host checks membership with Config Service;
it may cache successful checks for at most 15 seconds. A failed or expired lookup
fails closed. No user bearer is forwarded. Service-level `admin` access must not
be interpreted as organization administrator membership.

Missing caller context does not authorize acting as the worker's service account.
Jobs use `ctx.identity` for attribution. Plugin routes remain behind the worker
control boundary; neither identity nor profile headers are browser-controlled.

### Deployment controller services (0.7.3)

A plugin with a deployment-owned controller declares `host_services = ["controller"]`
in `[runtime]`, `target_bound = false` in `[ui]`, and the implementing host's minimum
service version. Its jobs receive no Connection values or storage binding. The host
keeps their grants and routes `ctx.request_host("plugin.request", payload)` to the
same plugin's private `POST /_host/job` endpoint on the deployment.

The endpoint receives `{request, job_id, node_id, capability, credentials}`: only
`request` is node-controlled. The host supplies every other field. The controller
must authorize the run capability against the verified caller, constrain permitted
operations, and overwrite job identity/target fields. It must never return grants,
bearers or a general filesystem/network proxy. On job completion the host sends
`{action: "close", job_id}`. Implement bounded leases as a cleanup fallback.

The host blocks `/_host/*` in public plugin routing and ignores node-session hints
for controller routes. Operations are reauthorized against the initiating sign-in;
expired/revoked authority stops the job and requires a fresh authorized resume.
Model Connection grants are refreshed at most every 15 seconds; an expired grant
or failed refresh never falls back to a previously resolved secret.

## Sample weights

```{eval-rst}
.. automodule:: tlc_plugin_sdk.shared.weights
   :members:
```

## Published MCP contracts

SDK 0.7.6 adds `McpTool.output_schema` and `load_mcp_section(raw, directory)` for
packaged `tools_file` JSON catalogs. Titles and boolean safety annotations survive
normalization. Publish stable contracts, return changing values as data, and test
result shapes. See [MCP tools](plugin-guide.md#mcp-tools-what-an-agent-may-ask-your-plugin-to-do)
for the canonical contract, prepared jobs and resource-link results.

### Asynchronous provider connection capacity

`tlc_plugin_sdk.ai.http.ClientPool(max_connections=64, max_keepalive_connections=16)`
accepts explicit per-job connection limits. Callers with high asynchronous model
concurrency may increase them (maximum 4096), while retaining provider rate limits
and budget admission. This does not require one CPU thread per connection. Keep
CPU/image processing in a separate bounded executor. Bind the pool on one event
loop and close it when the job ends. Request cookies and authorization remain
isolated between calls; transport reuse does not share a client session.
