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
