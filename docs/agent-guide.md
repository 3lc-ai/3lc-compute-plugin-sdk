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
4. Capture `window.PLUGIN_API`. Use `compute.request`, `jobs`, `pickers`, `credentials`,
   `nodes`, `run`, `nav`, `data` and `lifecycle`; consult the type declarations for exact
   signatures. No Hub globals, parent DOM access, browser secrets or custom auth flows.
5. Use least-privilege manifest capabilities, reject invalid server inputs and verify
   ownership server-side. Client validation is for usability, never authorization.
6. Use shared UI styles and safe text rendering. Include loading, success, refusal,
   dismissal and failure states. Handle rejected promises. Clean up local resources
   using `lifecycle.onDispose`, preserving remote jobs.

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
