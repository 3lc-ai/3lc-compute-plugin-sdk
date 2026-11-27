# Instructions for plugin-authoring agents

Use this alongside [the author guide](plugin-guide.md), [browser runtime](browser-runtime.md)
and [API reference](api.md). These public documents are the contract; examples and
older plugin code may predate isolation. Read the target repository's AGENTS.md and
build configuration before edits. Do not import host internals or copy a private API.

## Implement the requested plugin

1. Establish plugin ID, purpose, inputs, outputs, expected duration and compute needs
   from the request and repository. Ask only for missing decisions that affect behavior.
2. Preserve repository conventions. Rename template identifiers consistently in the
   Python package, manifest, entrypoint, routes, UI and tests. Keep metadata in
   `plugin.toml`; use the SDK's supported Python base class and JobContext.
3. Keep server-side data access in the 3lc Python SDK. Long operations use background
   jobs, durable progress/results and cooperative explicit cancellation. A UI or socket
   disconnect must not terminate the job. Never add cleanup that cancels remote work.
   When a job creates a 3LC run, call `ctx.result(str(run.url))` immediately after creation,
   before training, prediction or collection. This links queue progress and the actual run
   name to the run row; it does not finish the job. Forward an `on_run(url)` callback from
   trainer adapters rather than parsing log text or guessing a URL from the requested name.
4. Capture `window.PLUGIN_API`. Use `http.request`, `jobs`, `pickers`, `credentials`,
   `nodes`, `run`, `nav`, `data` and `lifecycle`; consult the type declarations for exact
   signatures. No Hub globals, parent DOM access, browser secrets or custom auth flows.
5. Use least-privilege manifest capabilities, reject invalid server inputs and verify
   ownership server-side. Client validation is for usability, never authorization.
6. Use the [shared styling contract](browser-runtime.md#shared-styling) and safe text rendering.
   The Hub supplies ordered design/component CSS and theme updates; its shell, navigation
   and page layout stay outside the frame. Keep the frame canvas transparent and use
   themed component surfaces. Include loading, success, refusal,
   dismissal and failure states. Handle rejected promises. Clean up local resources
   using `lifecycle.onDispose`, preserving remote jobs.
7. For saved setups, use the [shared preset bar](plugin-guide.md#presets).
   Save and explicitly restore the whole form, including tables, project and run name.
   External table/folder launches start fresh from defaults plus those inputs, visibly modified;
   they must never resume an old setup. Bare/project launches may resume their scoped draft.
   For primary-table-driven forms, use the shared `tableContext` / `tableResolved` integration:
   derive the project on manual table changes, retain settings within a source project, and reset
   to defaults with Undo across projects. Return source identity through `table_project_context`
   after opening the table with 3LC. Saved preset picks preserve explicit output overrides.
   Clear drafts only after a job is accepted. Saving a preset and submitting a job are separate actions.

## Verification required before delivery

Run the repository's lint, type checks and tests, then build/install its wheel to
check manifest and UI assets. Test meaningful behavior rather than source-string
assertions alone. Exercise the actual frame runtime with a mock host or a compatible
local Hub/Compute deployment; never claim a stub proves backend authorization.

Cover success, permission denial, malformed responses, abort/timeout, duplicate
clicks, early job completion, remount/recovery, reconnect and renewal under load.
Verify cleanup disconnects viewers and removes handlers but emits no cancellation.
Test explicit cancellation independently. For shared runtime changes retain both
simulated long-duration renewal tests and real transport tests. Record actual test
commands/results and any entitlement, platform or external-service limitations.

Do not switch shared development branches or restart shared services without the
repository/user authorization. Do not print environment secrets. Commit and push
only when authorized, and keep PR descriptions aligned with the final implementation.

## Completion report

Describe changed behavior, affected runtime/dependency versions, compatibility
aliases retained, test results and remaining rollout gates. Keep artifact publication,
fleet migration and real overnight soak distinct from completed local validation.
Do not claim the isolation track is closed while those gates remain.

## Host-mediated agent operations (0.7.1)

`JobContext.request_host(operation, payload, timeout=120)` requests a deployment
operation over the worker's authenticated control transport. It is synchronous
and cancellation-aware; asynchronous plugin code can call it through
`asyncio.to_thread`. Up to 16 requests may be pending per job, with an 8 MiB serialized
payload/response limit and at most a one-hour deadline. A timeout means the
operation's outcome is unknown: do not retry a side effect with a new request ID.
The SDK preserves pending IDs during transport reconnect/recovery.

This is an optional host capability, advertised as `host_requests` in worker
health. Hosts decide which jobs and operations are authorized. The API is not a
way to request a bearer, key or arbitrary host URL. Plugins must fail clearly
when their required host service is absent. Agents should keep credentials,
model calls and durable spending limits at the deployment while running their
loop and bounded local computation on the selected worker.

MCP knowledge declarations are relative to the installed plugin package. Include
the files in the wheel. Only declared Markdown files within that package are
served; symlink escapes, oversized files and excessive page counts are refused.
Tool input schemas describe the actual native plugin operation. Advisory MCP
annotations never grant authority or waive confirmation for mutations.

## Weighted selections

`tlc_plugin_sdk.shared.weights.weight_summary(table)` returns bounded counts for
an exact revision; `sample_weights(table)` reads only the schema-declared weight
column. Neither decodes images. Missing weights mean one; invalid weights fail.
Sparse null cells use the column schema default, as the row view does.
Physical rows, positive-weight rows and weight sum are distinct. For derived edits,
publish an `EditedTable` referencing the original table, preserving lineage and
bulk data. Keep existing zero weights zero when reducing a training selection.

## Published MCP contracts

SDK 0.7.6 adds `McpTool.output_schema` and `load_mcp_section(raw, directory)` for
packaged `tools_file` JSON catalogs. Titles and boolean safety annotations survive
normalization. Publish stable contracts, return changing values as data, and test
result shapes. See [MCP tools](plugin-guide.md#mcp-tools-what-an-agent-may-ask-your-plugin-to-do)
for the canonical contract, prepared jobs and resource-link results.

## Stateful agent modules

Use `runtime.node_workload = "agent"` for modules that share the deployment's reserved
Agent node, chosen only by an administrator in Settings → AI/Assistant. Do not add
module or project node selectors. Keep training and bulk processing as compute workloads with their own
destination. See [Stateful Agent nodes](plugin-guide.md#stateful-agent-nodes) for
minimum versions, role enforcement and restart behavior. Preserve project/root
launch context; do not follow the global compute-node choice in a stateful module.

### Asynchronous provider connection capacity

`tlc_plugin_sdk.ai.http.ClientPool(max_connections=64, max_keepalive_connections=16)`
accepts explicit per-job connection limits. Callers with high asynchronous model
concurrency may increase them (maximum 4096), while retaining provider rate limits
and budget admission. This does not require one CPU thread per connection. Keep
CPU/image processing in a separate bounded executor. Bind the pool on one event
loop and close it when the job ends. Request cookies and authorization remain
isolated between calls; transport reuse does not share a client session.

### Table lineage in project reads

`ProjectCatalog.table_info(url)` describes the exact requested revision. Its
`parent_table_urls` contains absolute native input-table URLs, and `is_original`
is true only when the table has no inputs. Use these facts for lineage-sensitive
operations; do not infer originality from a table's name or replace the requested
revision with `latest_revision_url`. These fields are available from SDK 0.7.14.

## Generated Python isolation

`tlc_plugin_sdk.agent_sandbox` (SDK 0.7.17) supplies the shared OS boundary for
Assistant/Flow and Refine. `Policy.for_workspace(path)` limits access to runtime
files and a task workspace. `wrap(argv, policy)` returns the command and mechanism
(`bubblewrap`, `seatbelt`, or `none`). Callers must refuse `none`, use the
credential-free `environment()`, set HOME/TMPDIR inside the workspace, and enforce
timeouts and process-group cleanup. Never run generated code directly on failure.

Use `read_private(workspace, relative_path, maximum_bytes)` for bounded regular-file
results; it rejects symlinks (including directory components) and hardlinks.
Keep all credentials and network transport in trusted parent code. The existing
`data_broker`/`data_access` modules provide read-only native 3LC access through a
bounded parent-side RPC channel; do not pass broker credentials into generated code.

### Native continuation receipts

A job tool may declare `job.continuation`, an optional **GET** route using the same
path/query rules as a read-only tool. The host supplies the original tool arguments
and `original_operation_id` from its saved job output. The resolver must authenticate
the caller and attest the exact persisted native operation chain; matching a UI link
is not sufficient. Never start work, change a review, or cancel a job in this route.

The version 1 response contains `version: 1`, an opaque `scope_key`, ordered
`attempts` (each with `operation_id`, `parent_operation_id` and `job_id`),
`completes_original_effect` and `result` in the original job tool's output schema.
The first attempt must be the requested original operation. The last must be the
unique current descendant. Check immutable inputs, selectors, instructions, policy,
owner, project and target across the chain. Reject missing records, siblings, changed
scopes and unsettled requests. Return `completes_original_effect: false` for active,
failed or partial work. Terminal job success does not imply review, publication or
other downstream gates are complete. Hosts independently verify authorized native
job receipts and adopt only completed effects; the original failed attempt stays in
history. Browser teardown still must never cancel remote work.

An attempt may additionally attest `effect_complete: false` when a terminal worker
job ended normally but the native operation stopped short (for example, at its
cost cap). This allows a host `completed` receipt for that parent, while retaining
the requirement for a verified, fully completed descendant. Omission does not
make this assertion. Never use it for an active parent or a completed effect;
the plugin must prove the persisted native outcome and settled requests first.

For a wait step, a native status result may supply the generic `progress.label`
and `subtitle` presentation fields (for example “Waiting for review” and the
remaining count). The Hub renders these only for that execution's waiting action.
Keep the machine-readable completion conditions independent of these labels;
finishing an AI job is not a human-review sign-off. Supply `ui_link` for the exact
review so users can resolve the wait directly.

A status result may additionally supply `ui_links` (up to four) with the same
`{label, search}` fields as `ui_link`, and optional `target: "_blank"`. These
secondary destinations remain visible on the Flow card. The Hub always binds
them to the result's plugin and the open project. For an external reviewer link,
add `share: {path: "/reviews/<id>/share-link?project_scope=<scope>", body: {...}}`.
The Hub POSTs this relative plugin path on an explicit **Copy link** click, on the
receipt's exact node, and copies the returned `{url: "https://..."}`. The plugin
must check sharing permissions, scope the link to the intended review/filter,
reuse or rotate revocable links, and return a current reachable address. Never
put a public capability key or service credential into a Flow status receipt.
Status polling must not enable public sharing or create reviewer links.

### Automation setup handoff (SDK 0.7.19)

Declare the optional `automations` UI capability and feature-detect
`PLUGIN_API.automations`. `await API.automations.configure({templateId, settings})`
opens a Hub-owned template setup for the current project and compute destination.
The template must be offered by this plugin in the host's template catalog; settings
must match its declared fields. Pass connection identifiers, never credentials.

The host stores an owner-scoped setup intent for 48 hours, so refresh does not lose
its inputs. The user can change them, review the native steps, save the automation
and explicitly enable it. Calling `configure` never starts a job or grants ongoing
authority. A missing capability or unavailable template must produce an actionable
message; do not silently substitute a one-off job or a browser-only timer.

See [Build a plugin that works manually and in Flow](automation-ready-plugins.md)
for the implementation sequence, complete handoff example and release checks.
