# Build a plugin that works manually and in Flow

A user should be able to complete a useful task in a plugin, then reuse that task
in an automation without reconstructing settings in chat. The plugin owns its
configuration, execution and evidence. The Hub owns project navigation, the
workflow view and the explicit decision to enable recurring work.

## 1. Keep one execution contract

Use the same validated configuration and native job implementation for the plugin
form, MCP tools and Flow. Declare tool input/output schemas, read-only annotations
and native job dispatch. Do not build a separate automation-only execution path.

Persist the effective configuration with the resulting native operation or run:
exact input Table revisions, model/checkpoint, relevant settings, output scope and
execution target. Expose a read-only tool to recover that recipe. Return warnings
when older records are incomplete; do not fill missing scientific choices with
plausible defaults. Reusing a preset that has since changed is not the same as
reusing the completed operation.

For training, distinguish the input that should change on each trigger from the
validation revision and other settings that should remain fixed. A new training
revision must not silently move the validation set. Resolve or reject ambiguous
“latest” inputs before offering a reproducible draft.

## 2. Offer an explicit setup handoff

SDK **0.7.19** adds the optional `automations` browser capability. Add it to the
existing capability list; keep other capabilities your plugin needs:

```toml
[ui]
capabilities = ["automations"]
```

Feature-detect it and handle a refused handoff. Put **Automate this** next to a
successful saved result or a validated, saved observation configuration. Explain
the proposed trigger in one sentence, such as “Train when a new version of this
dataset is published.” Keep the normal result action available.

```javascript
const API = window.PLUGIN_API;
automateButton.hidden = !API.automations;
automateButton.addEventListener("click", async () => {
  automateButton.disabled = true;
  try {
    await API.automations.configure({
      templateId: "train_dataset_revisions",
      project: { name: savedProjectName, rootUrl: savedProjectRootUrl },
      settings: { source_run_url: completedJob.run_url }
    });
  } catch (error) {
    statusElement.textContent = error.message;
  } finally {
    automateButton.disabled = false;
  }
});
```

This example uses the YOLO-owned training template; another plugin must use a
template offered by **its own plugin ID**. The host catalog declares the template's
fields, required actions and offering plugin. A new template must be integrated
into that catalog before a plugin can request it. Declaring the browser capability
does **not** register arbitrary templates, and the SDK currently has no plugin
manifest field for registering a template. Do not invent one or call another
plugin's template as a shortcut. The browser remains generic and needs no
plugin-specific rendering branch.

`configure({templateId, settings, project?})` returns `{opened, id}` after the Hub opens
setup. It creates an owner-scoped setup intent, valid for 48 hours. The Hub supplies
the current project, storage root and compute target, validates the offering plugin
and available template, and rejects unsupported fields or conflicting context.
A tool opened directly from the sidebar has no launch project. Supply optional
`project: {name, rootUrl}` from the saved result or explicitly selected form scope.
The host rejects a conflict with an existing launch project; it never uses the
last visited project as a fallback. Draft resolution still validates the actual
saved inputs before any work can be enabled.

Settings contain ordinary identifiers and values, never credentials or signed
review URLs. Pass Connection IDs for external services.

The user reviews the trigger, steps, destination and permissions in Hub. The draft
starts disabled; saving and enabling are separate user actions. `configure` does
not start a job, enable a watcher, grant ongoing permission, or submit a chat
message. On an older host or without Flow, manual work must remain usable. Explain
an unavailable capability; never substitute a timer or start a one-off job.

## 3. Make repeated work durable

Observe external input with a read-only status tool. Report what is being watched,
how many eligible inputs are ready, the threshold, and why it is waiting. A browser
tab must not be required for observation or execution.

Use a stable input key for a frozen batch and consume it only according to the
native persisted result. A retry, page refresh or service restart must not admit
the same batch twice. Do not reinterpret an in-progress batch using a newly changed
folder listing or model. Specify whether existing inputs are baselined or included
when an automation is enabled; never silently replay historical data.

Keep original attempts and receipts. An error before any mutating request and an
unknown outcome after a lost response need different recovery paths. Never claim
that no work started merely because a request timed out. A plugin with native
resume can use the [continuation contract](agent-guide.md#native-continuation-receipts)
to attest completion without replaying successful work.

## 4. Give Home enough evidence to be useful

Flow renders generic stages and status. Report facts for the **current operation**:

| State | What the plugin supplies | Useful user action |
|---|---|---|
| Observing | Source, ready count, threshold and wait reason | Open observation configuration |
| Queued/running | Native job ID, current progress label, actual metrics | Open the exact job or run |
| Waiting for review | Remaining cases and machine-readable review gate | Open that review |
| Failed | Clear cause, exact failed attempt, supported recovery | Inspect or resume the attempt |
| Completed | Typed output Table/run/reference and measured result | Open the result |

Use `JobContext` progress, metrics and result methods for jobs, and documented
generic status/receipt fields for waits. Do not estimate training progress from
the number of completed workflow steps. Do not recycle metrics from a previous
execution or turn absent metrics into zeroes. A completed AI job does not mean
human review or publication is complete.

For a review wait, return `progress.label`, a concise `subtitle` and a scoped
`ui_link`. Optional `ui_links` expose up to four secondary destinations, such as
external review or verification of automatically accepted changes. The host binds
links to the receipt's plugin, project and node. A `share` descriptor requests a
current shareable URL only after an explicit Copy link action; polling must not
enable public sharing. See the [status/link contract](agent-guide.md#native-continuation-receipts).

Human review and admin completion are server-enforced operations. If the plugin
supports accepting remaining suggestions, show a preview of what will change,
enforce the administrator role, and preserve an audit trail. Do not treat “no
suggestion” as a new label. Fixing existing labels and labeling an unlabeled image
require different handling. Publication remains a distinct, verifiable result.

Use typed output URLs to open the exact run or Table revision. Link completed work
to its results, not an empty queue. Model files and reference artifacts should open
their appropriate native destination, not pretend to be training runs.

## 5. Test the manual-to-automation journey

Before releasing a participating plugin, verify:

1. Manual execution and MCP use the same validation and effective configuration.
2. The saved result can reconstruct an exact recipe; incomplete legacy records
   produce an actionable explanation rather than guessed settings.
3. “Automate this” passes only supported fields, preserves project/target context,
   and opens a disabled draft without starting work. Cancel and reload are safe.
4. Missing capabilities, missing modules, wrong owners, wrong projects, invalid
   fields and expired intents produce recoverable errors.
5. Enabling handles existing input according to the declared policy. New input is
   processed once across polling, retries and restart.
6. Progress, waiting review, result links and copy links refer to the exact current
   execution. Finished AI work cannot bypass review or publication.
7. A 1920×1080 browser-content review and a smaller viewport show the primary action
   without clipped buttons or page-wide overflow; plugin teardown leaves jobs alive.

Use SDK harness and browser-runtime tests for contract behavior. Exercise the
integrated Hub handoff as well: unit tests alone cannot establish that navigation,
project binding, setup, progress and result selection work together.

Current examples: YOLO reuses a completed training recipe; Driftcatcher Inference
reuses an observation configuration; Refine reports review/publication gates and
scoped review/verification links. An image admission decision remains an image
decision: annotations and revised datasets belong in native 3LC Tables.
