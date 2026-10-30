# Plugin browser runtime

This guide describes the SDK 0.6.2 frame runtime and protocol 1. It supplements
[the Python author guide](plugin-guide.md) and the [typed API](api.md).
It requires a Compute host serving this SDK runtime and a Hub implementing the
frame bridge. Installing a newer SDK in a plugin alone does not upgrade the host.

## Ownership and trust

Compute serves the SDK's `shared.frame_runtime.frame_runtime_script()` asset.
The Hub embeds a sandboxed page on the Compute origin and supplies the plugin
fragment, theme, launch context and a private MessagePort. The runtime accepts
initialization only from the expected parent origin. It installs `window.PLUGIN_API`
before evaluating the fragment's scripts, in document order.

The Hub owns sign-in and all authorization decisions. The SDK provides convenience,
serialization and lifecycle handling; it is not an authorization boundary. Every
bridge operation must be validated by the Hub, and Compute must independently
validate the caller, plugin and resource ownership. Plugins never receive the Hub's
bearer token, password, signing keys or credential values. Credential selection
returns an identifier that a server-side operation can resolve with authorization.

This isolates plugin pages from the Hub origin. Plugins on one Compute origin still
share that origin and its browser storage; this is not mutual plugin isolation or
a sandbox for hostile Python. Python dependencies execute with worker permissions.
Do not store secrets in browser storage or treat manifest capabilities as proof of
server-side permission. Third-party scripts execute with the plugin's authority;
pin and review dependencies, avoid rendering untrusted HTML, and use `textContent`.
External network access remains subject to browser policy and deployment CSP.

## Public API

Capture `const API = window.PLUGIN_API` inside your fragment. Prefer these names:

| Surface | Purpose |
| --- | --- |
| `compute.request(url, options)` | Authenticated, host-mediated request to an allowed Compute route; returns a Response |
| `jobs.start/run/track/list/on/connect` | Shared job lifecycle and subscriptions; automatically installed |
| `jobs.cancel(jobId)` | Explicit user cancellation through the trusted host confirmation flow |
| `pickers.table()` | Host table picker; URL or null on dismissal |
| `credentials.choose/remembered` | Select or recall a credential identifier, never a secret |
| `nodes.create/mount` | Host provisioning flow and frame-local node list |
| `run.plan/checkTable` | Host planning and table accessibility checks |
| `nav.go/open/onChange` | Host navigation, external link and unsubscribe-able navigation events |
| `nav.state` | Optional history updates; requires `history` capability |
| `identity.name` | Optional display name; requires `identity:name` capability |
| `data` | Frame-local snapshot of the host's data catalog; load before synchronous getters |
| `ui.toast`, `format.date`, `dashboardUrl` | Presentation helpers |
| `libs` | Available shared libraries; check for null before using |
| `getRunTarget/onRunTargetChange` | Selected execution target and changes |
| `lifecycle.onDispose(fn)` | Register cleanup; returns unregister function |

The `.d.ts` shipped in `tlc_plugin_sdk/contract/plugin-api.d.ts` is the precise
signature reference. `protocol` identifies the wire format; `frameVersion` identifies
the SDK runtime; `contractVersion` identifies the host-advertised contract. Feature
detect optional members. An unsupported operation must fail visibly, not fall back
to direct credential handling. `guide` and `location` are unavailable in this runtime.

Flat aliases such as `authFetch`, `navigate`, `showToast` and the injected
`window.PluginJobs` remain for existing plugins. New code should use the namespaces.
Temporary globals including `TlcData`, `CancelJob` and `openTablePicker` are migration
compatibility only. Never access `TlcApi`, `TlcAuth`, the parent DOM or Hub cookies.

## Requests, errors and capabilities

```javascript
const API = window.PLUGIN_API;
const controller = new AbortController();
API.lifecycle.onDispose(() => controller.abort());
try {
  const response = await API.compute.request('/api/plugins/my-plugin/compute', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ greeting: 'hello' }), signal: controller.signal
  });
  const result = await response.json();
  output.textContent = JSON.stringify(result);
} catch (error) {
  if (error.code !== 'aborted' && error.code !== 'closed') {
    output.textContent = error.message;
  }
}
```

`timeout` is milliseconds. Ordinary requests default to a bounded wait; do not run
training inside a long HTTP request. Use background jobs. Errors have `code` and
`message`, and HTTP failures may include `status`. Handle rejection and restore
busy controls. `allowErrorStatus` lets callers inspect an HTTP error Response.
An abort or timeout ends the request/bridge wait; it does not promise to roll back
server work already accepted. Never automatically retry non-idempotent job starts
or provisioning after an ambiguous response; reconcile status first.

Own plugin routes are the default. Declare additional needs in the manifest:

```toml
[ui]
capabilities = ["calls:other-plugin", "history", "identity:name"]
```

Declare only necessary capabilities. They enable specific bridge operations, not
arbitrary proxying, credentials, socket emission or authorization bypass. The host
may still refuse the request based on identity, ownership or deployment policy.

## Long-running jobs and reconnects

```javascript
const API = window.PLUGIN_API;
const namespace = '/my-plugin';
API.jobs.connect(namespace);
const existing = await API.jobs.list('my-plugin');
for (const job of existing) render(job);
const off = API.jobs.on(namespace, 'job_update', render);
API.lifecycle.onDispose(off);
// In an explicit Run button handler:
await API.jobs.run('my-plugin', parameters, {
  onUpdate: render, onDone: render, onError: render
});
// In an explicit Cancel button handler only:
await API.jobs.cancel(jobId);
```

`run` subscribes before starting and buffers events that arrive before the start
response. `track` and `on` return unsubscribe functions. Render by job ID and make
terminal rendering idempotent: list snapshots and live events can overlap. A
missing network update does not mean a job failed. Recover state with `list`.

The runtime obtains one-use, namespace-bound socket tickets through the Hub and
renews established connections in place, with jitter and transient retries. It
refreshes job snapshots after reconnect and gives concurrent live updates precedence.
Plugins use `API.libs.io` or the job helpers; never create their own ticket renewal
loop or put a bearer token in Socket.IO auth. An expired viewer lease may disconnect
the viewer, but must not cancel a running job. The worker owns job execution.

Navigation, page disposal, browser sleep, socket disconnect, renewal failure and
closing the Hub are **not cancellation requests**. Dispose listeners, local timers,
observers and mounted widgets. Do not call `jobs.cancel` from cleanup, unload,
visibilitychange or error handlers. Register widget `destroy()` with `onDispose`.
A Python job must cooperate with explicit cancellation through `JobContext`, report
progress/results through the durable job lifecycle, and avoid tying task lifetime
to the HTTP request or socket lifetime. See the author guide for Python examples.

## Migration and release checklist

1. Upgrade and test the Compute runtime and compatible Hub bridge together. Upgrade
   the plugin's SDK dependency and regenerate its lockfile with the approved index.
2. Replace host globals and flat aliases with the namespaced API. Remove manual
   `job_tracker_script()` injection when requiring the new runtime; legacy injection
   is idempotent while migrating. Do not bundle a second frame runtime in a plugin.
3. Declare cross-plugin/history/identity capabilities, handle refusals, and use
   supported navigation. Verify frame styles in both themes and external links.
4. Register all local cleanup; preserve jobs on teardown. Test returned-to-page
   recovery, reconnect, renewal failure and expiry, and explicit cancellation separately.
5. Build the SDK wheel and assert it contains the runtime and type assets. Run
   Python tests, type checking, browser contract tests and host integration tests.
   Exercise concurrent renewal while jobs remain running, and an actual long soak
   before production rollout. A simulated clock test is not an overnight soak.
6. Publish the SDK artifact before releasing consumers that require it. Temporary
   Git revision pins in development must be replaced with a published compatible
   dependency before packaging/releasing consumers.

## Troubleshooting

- No API or blank page: inspect frame initialization, parent origin allowlist,
  protocol compatibility and script loading. Do not weaken origin validation.
- Refused route: check own-plugin path, manifest capability and server permission.
- No live updates: verify the namespace, compatible Socket.IO library and ticket
  renewal; recover the durable list. Do not restart the job as a reconnect strategy.
- Missing helper: check the **host's** runtime version, not only the plugin venv.
- Lost connection during start: inspect the job list before offering a retry.
