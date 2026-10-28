// Copyright 2026 3LC Inc.
// SPDX-License-Identifier: Apache-2.0
//
// The browser-side plugin contract — ships in 3lc-compute-plugin-sdk 0.3.0.
//
// This file DECLARES the JavaScript surface a plugin's `ui.html` programs against.
// It does not implement it:
//
//   * `PLUGIN_API`, `TlcApi`, `TlcData` are IMPLEMENTED by the 3LC Hub frontend,
//     which builds `window.PLUGIN_API` when it mounts a plugin fragment.
//
//   * `window.PluginJobs` SHIPS FROM THIS PACKAGE: it is the
//     injectable client in `tlc_plugin_sdk.shared.job_tracker`
//     (`JOB_TRACKER_JS`), auto-injected by the host's `/ui` handler (and idempotent
//     if a plugin also injects it by hand). It is NOT part of the host bridge — it
//     is layered on top of `PLUGIN_API`.
//
// One contract axis: this browser surface and the Python surface share the single
// `SDK_CONTRACT_VERSION` (the package version); the host compares compatibility on
// MAJOR.MINOR. Bump the package version when this surface changes.
//
// USAGE from a plain-JS `ui.html` (no build step):
//
//   /// <reference types="3lc-compute-plugin-sdk/contract/plugin-api" />
//   // (the on-disk dir is the IMPORT name `tlc_plugin_sdk`; a `/// <reference
//   //  types>` resolves via typeRoots, not jsconfig `paths`.)
//   // or by relative path to this file:
//   /// <reference path="../contract/plugin-api.d.ts" />
//
//   var API = window.PLUGIN_API;          // typed
//   API.authFetch(API.getConfig('compute_service_url') + '/api/plugins/x/compute');
//   PluginJobs.run('my-plugin', { table_url: url }, { onDone: function (job) {} });

// ── Object / Compute service method bags (TlcApi) ──────────────────────────────

/**
 * Compute-service method bag, exposed as `PLUGIN_API.compute`.
 * Currently only `getHealth()` (GET /health).
 */
export interface TlcComputeService {
  getHealth(): Promise<object>;
}

/**
 * Object-service method bag, exposed as `PLUGIN_API.objects`.
 * All methods go through `authFetch` against the object-service URL; object URLs
 * are encoded via `TlcApi.encodeObjectUrl`.
 */
export interface TlcObjectService {
  getStatus(): Promise<object>;
  getTableIndex(): Promise<object>;
  getRunIndex(): Promise<object>;
  getConfiguration(): Promise<object>;
  getObject(objectUrl: string): Promise<object>;
  deleteObject(objectUrl: string): Promise<Response>;
  patchObject(objectUrl: string, patchData: object): Promise<object>;
  reindex(force?: boolean): Promise<object>;
}

/** Custom non-standard fetch options layered on top of the standard `RequestInit`. */
export interface PluginFetchOptions extends RequestInit {
  /**
   * Abort the request after this many milliseconds (default 10000). Custom,
   * non-standard option — deleted before the underlying `fetch()` call. Ignored
   * when the caller supplies its own `signal`.
   */
  timeout?: number;
  /**
   * When true, resolve with the `Response` on a non-ok HTTP status instead of
   * rejecting — the caller inspects `response.ok` / `response.status` itself.
   * Custom, non-standard option — deleted before the underlying `fetch()` call.
   */
  allowErrorStatus?: boolean;
}

// ── TlcData (cached project/table/run indexing tables) ─────────────────────────

/**
 * The root a project lives under (the Object Service's project root or one of
 * its scan URLs). Resolved by the frontend from `api://Configuration` plus the
 * object URLs themselves; hosts predating SDK 0.2 never set it, so
 * treat every location member as optional.
 */
export interface TlcDataLocation {
  /** The root URL as configured or inferred (e.g. "s3://bucket/projects"). */
  root: string;
  /** Canonical prefix (file:// stripped, one trailing slash) — use for equality tests. */
  key: string;
  /** URL scheme: 'file', 's3', 'gs', ... */
  scheme: string;
  /** Short display name: alias name > bucket/last path segment > 'Local'. */
  label: string;
  /** True for the Object Service's primary project root. */
  is_default: boolean;
  /** True when `label` came from a configured alias. */
  is_alias: boolean;
}

/** Per-location slice of a project's contents (a project can span several roots). */
export interface TlcDataProjectLocation {
  location: TlcDataLocation;
  table_count: number;
  run_count: number;
}

export interface TlcDataProject {
  project_name: string;
  table_count: number;
  run_count: number;
  dataset_count: number;
  last_modified: number;
  /** Locations this project's contents resolve to. Absent on hosts predating SDK 0.2. */
  locations?: TlcDataProjectLocation[];
}

export interface TlcDataTable {
  url: string;
  project_name: string;
  dataset_name: string;
  table_name: string;
  row_count: number;
  created: string;
  description: string;
  type: string;
  is_url_writable: boolean;
  input_table_urls: string[];
  /** Root this table lives under; null if unresolvable. Absent on hosts predating SDK 0.2. */
  location?: TlcDataLocation | null;
}

export interface TlcDataRun {
  url: string;
  project_name: string;
  run_name: string;
  status: string;
  status_code: number;
  created: string;
  last_modified: string;
  description: string;
  constants: object;
  metrics: any[];
  is_url_writable: boolean;
  /** Root this run lives under; null if unresolvable. Absent on hosts predating SDK 0.2. */
  location?: TlcDataLocation | null;
}

export interface TlcDataSummary {
  project_count: number;
  table_count: number;
  run_count: number;
}

/**
 * The global `TlcData` helper (cached indexing tables). Referenced from
 * `PLUGIN_API.data`. Implemented by the frontend (data-helpers.js).
 */
export interface TlcData {
  /** Fetch and cache both indexing tables; dedupes concurrent callers; refetches when stale. */
  load(): Promise<void>;
  /** Mark the cache stale so the next `load()` refetches; old data stays readable until replaced. */
  invalidate(): void;
  /** Raw rows from the cached TableIndexingTable response. */
  allTableRows(): object[];
  /** Raw rows from the cached RunIndexingTable response. */
  allRunRows(): object[];
  /**
   * Map a run status code to a name ('completed','empty','running','collecting',
   * 'post_processing','paused','cancelled', else 'unknown').
   */
  runStatusName(statusCode: number | null | undefined): string;
  /** Per-project rollup from both indexing tables, sorted by last_modified descending. */
  getProjects(): TlcDataProject[];
  /** Tables (optionally filtered by project), with table_name derived from the URL. */
  getTables(projectName?: string): TlcDataTable[];
  /** `getTables()` grouped by dataset_name ('(ungrouped)' when none). */
  getTablesByDataset(projectName?: string): { [datasetName: string]: TlcDataTable[] };
  /** Runs (optionally filtered by project), with run_name derived from the URL and status mapped. */
  getRuns(projectName?: string): TlcDataRun[];
  /** Dashboard summary counts. */
  getSummary(): TlcDataSummary;
  /**
   * Resolve which root an object URL lives under (longest-prefix match against
   * configured roots, falling back to inference from the URL structure).
   * Absent on hosts predating SDK 0.2 — feature-detect before calling.
   */
  resolveLocation?(url: string, projectName?: string): TlcDataLocation | null;
  /**
   * All locations relevant to this install: configured roots plus roots inferred
   * from the loaded data. Absent on hosts predating SDK 0.2.
   */
  getLocations?(): TlcDataLocation[];
}

// ── Optional vendored third-party libs ─────────────────────────────────────────

/**
 * Third-party libraries pulled from `window` if the host loaded them, else `null`
 * per key.
 *
 * Stability tiers (frozen contract):
 *   * `io` (socket.io client) — STABLE: the job-tracker channel rides it; the only
 *     `libs` member a plugin may depend on.
 *   * `Chart`, `cytoscape`, `html2canvas`, `PptxGenJS` — BEST-EFFORT: exposed for
 *     convenience, may be swapped/removed without a contract bump. A plugin that
 *     needs one should be prepared to vendor its own.
 */
export interface PluginLibs {
  /** socket.io client (STABLE). */
  io: any | null;
  Chart: any | null;
  html2canvas: any | null;
  PptxGenJS: any | null;
  /** Best-effort — and always `null` on the plugin page: the host does not load
   *  cytoscape into a plugin fragment, so a plugin that needs it must vendor its own. */
  cytoscape: any | null;
}

// ── TlcLocation (shared location renderers) ─────────────────────────────────────

/**
 * The global `TlcLocation` helper: shared renderers for project locations.
 * Referenced from `PLUGIN_API.location`. Implemented by the frontend
 * (location.js). Every renderer returns '' when the install has a single root
 * (`isMultiRoot() === false`), so output can be concatenated unconditionally.
 */
export interface TlcLocationApi {
  /** True when the install has more than one known root. */
  isMultiRoot(): boolean;
  /**
   * True when the given rows (each optionally carrying `.location`) span more than
   * one root — the per-row chip rule (spread *within the shown list*, so a chip is
   * hidden when every visible row lives under the same root).
   */
  rowsSpanLocations(rows: Array<{ location?: TlcDataLocation | null }>): boolean;
  /** Inline SVG string: folder glyph for 'file', cylinder for bucket schemes. */
  iconSvg(scheme: string): string;
  /** Display form of a location label: left-ellipsized past 20 chars so the distinctive tail survives. */
  shortLabel(label: string): string;
  /** Small chip (icon + label, root in tooltip) for one location; '' when hidden. */
  chipHtml(loc: TlcDataLocation | null): string;
  /** Chip for a project rollup: its location, or "N locations"; '' when hidden. */
  projectChipHtml(project: TlcDataProject | null): string;
  /** Muted mono path line for project cards; '' when hidden. */
  pathLineHtml(project: TlcDataProject | null): string;
}

// ── PLUGIN_API — the single host -> fragment bridge ────────────────────────────

/** Launch context: what the user launched the plugin against. */
export interface PluginContext {
  /** Selected resource kind ('run','table',...) or `null` when launched bare. */
  resourceType: string | null;
  /** Selected 3LC object URLs (default `[]`). */
  resourceUrls: string[];
  /** Launch project name ('' when none). */
  projectName: string;
}

export interface PluginGuideTip {
  id: string;
  target: string;
  title: string;
  body: string;
  task?: string;
  experience?: string;
}

export interface PluginGuide {
  readonly version: 1;
  register(tips: readonly PluginGuideTip[]): void;
  complete(tipId: string): void;
  dispose(): void;
}

/** Where jobs from this page run: the host-owned "Run on:" choice (`PluginApi.getRunTarget`). */
export interface PluginRunTarget {
  /** `'local'`: the compute host's own workers. `'node'`: a remote node. */
  target: 'local' | 'node';
  /** The node's id, for a node target. */
  node_id?: string;
  /** False while a chosen node's worker is still being prepared. */
  ready: boolean;
  /**
   * The Hub's name for the target: the node's label, or its name for the compute host
   * ("This machine", "Your deployment (…)"). Absent on older frontends.
   */
  label?: string;
  /** The node's stage/write folder, when the host knows it. */
  files_root?: string;
  /** Explicit folders offered by this machine; [] means none, absent means older host/agent. */
  browse_roots?: string[];
}

/** One piece of data a planned run reads or writes (`PluginRunPlan.refs`). */
export interface PluginRunPlanRef {
  /** `"alias:<TOKEN>@<source>"` or `"field:<dotted key>"`. */
  id: string;
  kind: 'alias' | 'field';
  /** The alias token, for an alias ref. */
  token?: string;
  /** The dotted run-body key, for a field ref. */
  field?: string;
  /** Every table that uses this ref, for an alias ref. */
  tables?: string[];
  /** Where the data resolves today: a bucket URL, a path on the host or the node, or ''. */
  source: string;
  where: 'bucket' | 'host' | 'node' | 'web' | 'missing';
  /** How the run gets it: as it is, a choice to make, a sync to the node, a path to ask for, or not at all. */
  reach: 'in_place' | 'choose' | 'sync' | 'ask_path' | 'refused';
  /** The answers the Hub may offer: a subset of `stream`, `copy`, `on_node`, `path`. */
  options: Array<'stream' | 'copy' | 'on_node' | 'path'>;
  bytes?: number;
  digest?: string;
  already_staged?: boolean;
  node_path?: string;
  /** A sentence for people. */
  detail: string;
}

/** What the host says a run's data needs before it can run on the current target (`PluginApi.planRun`). */
export interface PluginRunPlan {
  /** Resolved destination at review time; optional on older hosts. */
  project_root_url?: string;
  target: { kind: 'host' | 'node'; node_id: string; label: string };
  refs: PluginRunPlanRef[];
  /** Sentences for what cannot be crossed (e.g. a host folder on a node run). */
  boundaries: string[];
  warnings: string[];
  /** No boundary, and no ref that needs an answer. */
  ready: boolean;
  legacy: boolean;
}

/**
 * A finished action's result as `PluginApi.showResult` draws it: the dict a plugin's run or
 * route already returns. Other fields (`table_url`, `project_name`, ...) are allowed and ignored.
 */
export interface PluginResult {
  /** True draws the success style; false or absent draws the error style. */
  success?: boolean;
  /** The one-line outcome, shown in bold. */
  message?: string;
  /** Drawn under the message as `key: value` pairs (objects as JSON); null/undefined values are skipped. */
  details?: Record<string, unknown> | null;
  [key: string]: unknown;
}

/** Options for `PluginApi.showResult`. */
export interface PluginResultOptions {
  /**
   * HTML drawn between the message and the details: "View in Project" links, follow-up buttons,
   * guidance. Inserted as HTML, so escape every value you interpolate.
   */
  extra?: string;
  /** False leaves out the close button. Default true. */
  dismissible?: boolean;
}

/**
 * The single host -> fragment JS contract. The frontend injects this as
 * `window.PLUGIN_API` when it mounts a plugin fragment; a fragment should reach
 * for nothing else. Many plugins alias it: `var API = window.PLUGIN_API`.
 */
export interface PluginApi {
  /** Launch context (resource type/urls + project). */
  context: PluginContext;

  /**
   * The SDK contract version this host implements, as "MAJOR.MINOR" (e.g. "0.3"),
   * so a fragment can feature-detect the bridge. The frontend declares which
   * contract it implements (its `IMPLEMENTED_SDK_CONTRACT`) and surfaces it via
   * `<body data-contract-version>`. '' if the host predates it.
   */
  contractVersion: string;
  guide?: PluginGuide | null;

  /**
   * Return a configured value by key. `dashboard_url` has its trailing slash
   * stripped; `compute_service_url` is the GPU/CPU-routed service for THIS plugin;
   * `object_service_url` comes from `TlcApi`. `deployment_name` is the label the Hub
   * shows for the deployment this fragment's compute service belongs to (a managed
   * deployment's label, the demo deployment, or "Custom Deployment · host") — '' on a
   * host that predates it. These four keys are the only ones recognized — any other
   * key returns ''.
   */
  getConfig(key: "dashboard_url" | "compute_service_url" | "object_service_url" | "deployment_name"): string;

  /**
   * A cost label for a node, from the hourly quote the host saved when the node was
   * created (`hourly_rate`, a provider's `CreateNodeResponse.hourly_rate`) or from this
   * browser's cached quote for the same node. No pricing request is made. Returns a
   * USD/hour label ("≈ $1.21/h", "Spot estimate $0.30/h") or "Cost unavailable".
   * `node` is a record as `GET /api/infra/nodes` returns it; `node_type` is the node
   * kind the provider was asked for. Optional — older hosts do not define it.
   */
  nodeHourlyCost?(node: {
    id?: string;
    node_id?: string;
    plugin_id?: string;
    node_type?: string;
    pricing?: string;
    created_at?: number;
    hourly_rate?: number | null;
  }): string;

  /**
   * Create a node through the host's own flow, for an infrastructure plugin page's "Spin up": the
   * host asks for any settings the provider reports missing, runs its preflight, picks the Connection
   * (the only one offered, or the person chooses), creates the node, and shows a refusal in its own
   * dialog. `request` carries the page's create fields (e.g. `gpu_type`, `pricing`); the host sets
   * `plugin_id` to `providerId` and owns `connection_id`. `onNote` receives progress lines.
   *
   * Resolves the created node record, or `false` when the person cancels or compute refuses.
   * Optional: older hosts do not define it; a page that finds it absent posts to
   * `POST /api/infra/nodes` itself.
   */
  createNode?(providerId: string, request?: { [field: string]: unknown }, onNote?: (text: string) => void): Promise<object | false>;

  /**
   * Draw the host's own node list in `element`, for an infrastructure plugin page: this compute's
   * nodes of `pluginId` (this plugin when omitted), each with its state, hardware and cost, startup
   * history, per-node auto-off, rename and Terminate/Dismiss, and below them the provider's History
   * ledger, which includes nodes that are long gone. It is the same component the Hub's Deployments page
   * draws, refreshed every few seconds while the tab is visible, and it stops once `element` leaves
   * the page. `openNodes` (default true) and `openHistory` (default false) set which of its two
   * collapsible cards start open. Returns a handle whose `refresh()` repaints now (after the page's
   * own Spin up, say) and whose `destroy()` stops it.
   *
   * Optional: older hosts do not define it; a page that finds it absent can point to the Hub's
   * Deployments page, where the same nodes are listed.
   */
  mountNodes?(element: HTMLElement, options?: { pluginId?: string; openNodes?: boolean; openHistory?: boolean }): { refresh(): void; destroy(): void };

  /**
   * Let the person choose which of their SECRET Connections for `service` (a service slug the
   * plugin's manifest lists under `[runtime] credentials`, e.g. `"huggingface"`) this plugin uses —
   * allowing an existing Connection for this plugin, or adding a token, in the host's own dialog.
   * Resolves the chosen Connection id, or `null` when the person cancels. The choice is remembered
   * per plugin and service; the host then sends it with this plugin's runs and with its calls to the
   * routes the manifest lists under `[runtime] credential_routes`, and the worker sees the value as
   * `connections.current_credential()`. The fragment never sees the value.
   *
   * Optional: older hosts do not define it. Feature-detect.
   */
  chooseCredential?(service: string): Promise<string | null>;

  /**
   * The Connection id remembered for `service` (see `chooseCredential`), or `null` when none was
   * chosen. Optional: older hosts do not define it.
   */
  credential?(service: string): string | null;

  /**
   * The Dashboard link for a table or a run, built the way every Hub page builds it — the base this
   * browser is on, plus `object_service`, so the Dashboard opens against this deployment's data rather
   * than its own default. Prefer it over concatenating `getConfig('dashboard_url')`, which produces a
   * link that works on a laptop and points at the wrong endpoint anywhere else.
   *
   * Returns '' when the deployment has no Dashboard configured. Older hosts do not define this — a
   * plugin that wants to run on them should feature-detect.
   */
  dashboardUrl(params: { table?: string; project?: string; run?: string }): string;

  /**
   * Authenticated `fetch`. Injects `Authorization` (from `TlcAuth`) and a default
   * `Accept: application/json`; sets `Content-Type: application/json` when the body
   * is a string. Aborts after `options.timeout` ms (default 10000) unless the
   * caller supplies a `signal`. Rejects non-ok responses with the parsed
   * detail/message. The most-used bridge member.
   */
  authFetch(url: string, options?: PluginFetchOptions): Promise<Response>;

  /**
   * `authFetch` against the compute-service base URL. `path` is joined to the
   * compute-service root; when `requiresGpu` is given and a CPU/GPU counterpart
   * service is configured, routes to the matching service. Most plugins instead
   * build URLs from `getConfig('compute_service_url')` and call `authFetch`.
   */
  computeFetch(path: string, options?: PluginFetchOptions, requiresGpu?: boolean): Promise<Response>;

  /**
   * Where jobs from this page currently execute: the host-owned "Run on:" choice.
   * `{target: 'local', ready: true}` on hosts without remote GPU nodes, when
   * "This machine" is selected, or on older frontends (feature-detect the member).
   * `ready` is false while a chosen node's worker is still being prepared. `label`
   * is the Hub's name for the target and `files_root` the folder a node's disk is
   * browsed under; both are absent on older frontends. The shared data-source picker
   * and alias card follow this (see the plugin guide's "Data inputs and run targets").
   */
  getRunTarget?(): PluginRunTarget;

  /**
   * Register a callback fired whenever the "Run on:" choice or its readiness
   * changes — re-validate any selected data via `checkDataForRunTarget`.
   * Listeners are cleared when the fragment unmounts.
   */
  onRunTargetChange?(callback: () => void): void;

  /**
   * Selection-time data verdict for the current run target: can this table work
   * there, and what will happen to its data? `ok: false` means a node run WILL
   * fail (e.g. rows carry local paths with no alias); `notes` are user-facing
   * strings (upcoming copies, copy-vs-stream choices) to render next to the
   * picker. Always `{ok: true, notes: []}` for the local target and on older
   * hosts. Call it from the data picker's change handler — before Train.
   */
  checkDataForRunTarget?(tableUrl: string): Promise<{ ok: boolean; notes: string[] }>;

  /**
   * `true` when the host itself annotates every table input in the fragment with
   * the `checkDataForRunTarget` verdict (fields whose value has a `/tables/` segment
   * or whose id/name says "table"; `data-run-target-check="table"|"off"` opts a field
   * in or out). A fragment that wires `checkDataForRunTarget` to its own inputs can
   * skip that when this is set, so the note is not shown twice. Absent on older hosts.
   */
  hostChecksTableInputs?: boolean;

  /**
   * Plan a run body against the current run target, side-effect free: which data the run
   * reads and writes (the manifest's `data_inputs` / `data_outputs` and the aliases of the
   * tables it names), where each piece is, and what the run would need answered. The Hub
   * does this itself before every run and asks the person; a fragment calls it only to
   * preview. Its presence also means the Hub asks where a run's data is, so the deprecated
   * override card draws nothing. On a Hub 1.2 host it always resolves to a plan; it rejects
   * when the Compute Service refuses or cannot plan the run (a missing plan route is an
   * error, never a null). What a run reads where is the host's to stamp at dispatch: the
   * browser never sends `_alias_overrides`. Absent on older frontends.
   */
  planRun?(body: Record<string, unknown>): Promise<PluginRunPlan>;

  /** Reference to `TlcApi.computeService` (currently only `getHealth()`). */
  compute: TlcComputeService;

  /** Reference to `TlcApi.objectService`. Plugins usually reach data via `authFetch`. */
  objects: TlcObjectService;

  /** Reference to the global `TlcData` helper (`null` if `TlcData` is undefined at mount). */
  data: TlcData | null;

  /** Reference to the global `TlcLocation` helper (`null` if undefined at mount). */
  location: TlcLocationApi | null;

  /** Optional vendored third-party libraries (each `null` if the host didn't load it). */
  libs: PluginLibs;

  /** The DOM element the fragment was mounted into. Plugins scope their queries to it. */
  container: HTMLElement;

  /** Navigate the host to a path (sets `window.location.href = path`). */
  navigate(path: string): void;

  /** Show a host toast notification; no-op fallback if the host's `showToast` is unavailable. */
  showToast(message: string, type?: string): void;

  /**
   * Draw a finished action's result into `target`, replacing its content: the Hub's result box,
   * in the success or error style, with the message, `options.extra`, the `details` pairs and a
   * close button (✕) that removes the box. Scrolls the box into view and returns it. Use it for
   * every "done" or "failed" message a fragment shows after a run, so results look and close the
   * same in every plugin. Absent on older frontends — feature-detect.
   */
  showResult?(target: HTMLElement, result: PluginResult, options?: PluginResultOptions): HTMLElement | null;

  /**
   * Return an SVG icon string. With no id (or the current plugin's id) returns the
   * plugin manifest's `icon_svg` when present; otherwise delegates to
   * `TlcIcons.get(id || pluginId)`, or '' if `TlcIcons` is undefined.
   */
  getIcon(id?: string): string;
}

// ── window.PluginJobs — SDK-injected job-tracker client ────────────────────────

/** The opaque generic job object delivered to `PluginJobs` handlers. */
export interface PluginJobUpdate {
  /** 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'. */
  status?: string;
  /** Job id. */
  id?: string;
  title?: string;
  /** Secondary status line (the current progress label); NOT the failure message. */
  subtitle?: string;
  /** Failure message on a failed job (host >= 0.6); empty/absent otherwise. */
  error?: string;
  progress?: { percent?: number; label?: string; timing?: any };
  metrics?: Array<{ label?: string; value?: any }>;
  run_url?: string;
  [key: string]: any;
}

export interface PluginJobHandlers {
  /** Fires on every `job_update`. */
  onUpdate?: (job: PluginJobUpdate) => void;
  /** Fires on terminal `completed` / `cancelled`. */
  onDone?: (job: PluginJobUpdate) => void;
  /** Fires on terminal `failed`. */
  onError?: (job: PluginJobUpdate) => void;
}

/** The parsed response of the generic `POST /api/plugins/{id}/run` route. */
export interface PluginRunResponse {
  job_id?: string;
  status?: string;
  namespace?: string;
  error?: string;
}

/**
 * The job-tracker client. SHIPS FROM `3lc-compute-plugin-sdk`
 * (`tlc_plugin_sdk.shared.job_tracker`, `JOB_TRACKER_JS`) — auto-injected into
 * every fragment by the SDK's `/ui` handler (`build_plugin_app`); a manual
 * `inject_scripts(raw, job_tracker_script())` is harmless but no longer needed.
 * NOT part of the host `PLUGIN_API` bridge; it is layered on top of it.
 */
export interface PluginJobsApi {
  /**
   * Start a job and track it on the generic `job_update` channel. Pre-subscribes
   * on the default namespace `'/' + pluginId` (corrected to `resp.namespace` if it
   * differs) and buffers events so a job completing before its id is known still
   * delivers a terminal callback. Defaults `params.project_name` from
   * `PLUGIN_API.context.projectName`. `onDone` fires on completed/cancelled,
   * `onError` on failed. Most-used member.
   */
  run(pluginId: string, params?: object, handlers?: PluginJobHandlers): Promise<PluginRunResponse>;

  /**
   * `POST {compute}/api/plugins/{pluginId}/run` with `params` as JSON. Defaults
   * `params.project_name` from the launch context. Lower-level building block under
   * `run()`.
   */
  start(pluginId: string, params?: object): Promise<PluginRunResponse>;

  /**
   * Subscribe to `job_update` for a single `jobId` on a known namespace; returns an
   * unsubscribe function. Filters by `job.id`, fires `onDone` on completed/cancelled
   * and `onError` on failed, then auto-unsubscribes.
   */
  track(namespace: string, jobId: string, handlers?: PluginJobHandlers): () => void;

  /**
   * Open (or reuse) the namespace socket now. `track()`/`on()` connect lazily on first
   * use and SocketIO does not replay server→client events to a client that was not yet
   * connected — call this on mount when the fragment wants custom events from the first
   * second, or starts jobs by other means than `run()` (which connects for you).
   * Returns false when `PLUGIN_API.libs.io` is unavailable.
   */
  connect(namespace: string): boolean;

  /** `POST {compute}/api/plugins/jobs/{jobId}/cancel` with body '{}'. */
  cancel(jobId: string): Promise<{ cancelled?: boolean }>;

  /**
   * `GET {compute}/api/plugins/jobs` → the host's generic job records, resolved to
   * `PluginJobUpdate[]` and optionally filtered (client-side) to one plugin by id.
   * Use it to seed a freshly-mounted fragment from the durable job list — the
   * fragment is torn down on navigation and `job_update` is live-only. See the
   * guide's "Job page is a launcher" section.
   */
  list(pluginId?: string): Promise<PluginJobUpdate[]>;

  /**
   * Subscribe to a CUSTOM `ctx.emit()` event (not the generic `job_update`) on the
   * plugin's namespace; returns an unsubscribe function. For rich per-job detail the
   * flat generic schema can't carry (result payloads, loss curves). Subscribe before
   * `run()` so the socket is connected when the event fires.
   */
  on(namespace: string, event: string, handler: (payload: any) => void): () => void;
}

// ── Ambient globals ────────────────────────────────────────────────────────────

/**
 * `TlcApi` — the frontend API client (api-client.js). Plugins normally reach it
 * through `PLUGIN_API` rather than directly, but it is an ambient global.
 */
export interface TlcApi {
  computeService: TlcComputeService;
  objectService: TlcObjectService;
  authFetch(url: string, options?: PluginFetchOptions): Promise<Response>;
  computeFetch(path: string, options?: PluginFetchOptions, requiresGpu?: boolean): Promise<Response>;
  /** Resolved Object Service base URL (trailing slash stripped). */
  readonly objectServiceUrl: string;
  /** Resolves once compute-mode detection (GET /health -> mode/version) completes. */
  waitForMode(): Promise<void>;
}

// ── The value picker (Hub 1.2 and later) ────────────────────────────────────────

/**
 * A row of a `TlcPicker.open()` list: an option (`value`, `label`), a header (`{group}`) or a
 * rule (`{separator: true}`). `onPick` and `remove.run` get a copy of the item with all of its
 * fields, so it may carry fields of the caller's own.
 */
export interface TlcPickerItem {
  value?: string;
  label?: string;
  /** Muted text at the row's end, such as "2 days ago". */
  hint?: string;
  disabled?: boolean;
  /** A header row with this text. */
  group?: string;
  /** A rule between rows. */
  separator?: boolean;
  /**
   * Makes the row removable: a trash button on hover, in the column under the chevron, or Delete
   * on the active row (Shift+Delete when the anchor is a text field). Without `confirm` it runs
   * `run` at once, and the row dims meanwhile; with it, the row asks first. When `run` resolves
   * the row goes away and the list stays open; when it rejects the row comes back with the error
   * as its hint.
   */
  remove?: {
    /** The trash button's title and the confirming button's text; "Delete" by default. */
    label?: string;
    /** A question to ask in the row first, such as "Delete this preset?"; none by default. */
    confirm?: string;
    run(item: TlcPickerItem): unknown;
  };
  [key: string]: unknown;
}

export interface TlcPickerOptions {
  items: TlcPickerItem[];
  /** The value of the row that gets the ✓ and is active first. */
  value?: string;
  /** Called with the picked item, after the list closed. */
  onPick?(item: TlcPickerItem): void;
  /** Called once when the list closes, picked or not. */
  onClose?(): void;
}

/**
 * `TlcPicker` — the list a dropdown opens in the Hub (value-picker.js). Every single-choice
 * `<select>` in a fragment opens it by itself; `open()` shows it for a menu that is not a
 * select. Focus stays on the anchor, which carries `aria-expanded`, `aria-controls` and
 * `aria-activedescendant` while the list is open. A Hub before 1.2 has no `TlcPicker`.
 *
 * A row has the ✓ on the left, then the label and the hint; in a list with removable rows, the
 * trash is in a column on the right, under the chevron. A select's own options can be such rows:
 * `<option data-hint="2 days ago">` shows a muted hint, and `<option data-removable>` gets the
 * trash once the page has said how to remove an option with `onRemove()`.
 */
export interface TlcPickerApi {
  /** Show the list under `anchor`, closing any other list first. */
  open(anchor: HTMLElement, opts: TlcPickerOptions): { close(): void };
  /** Close the open list, if there is one. */
  close(): void;
  /** Whether the list is open under `anchor`, or open at all without one. */
  isOpen(anchor?: HTMLElement): boolean;
  /**
   * How to remove an option of `select` with `data-removable`: the list's trash calls `run(option)`
   * at once. The list stays open; the page removes the option itself, and the list follows the
   * options as they are. A rejection shows its message in the row. `null` takes it back. Not on a
   * Hub 1.2 build from before it; feature-detect it.
   */
  onRemove?(select: HTMLSelectElement, run: ((option: HTMLOptionElement) => Promise<unknown>) | null): void;
}

// ── The table field (Hub 1.2 and later) ─────────────────────────────────────────

/**
 * `<tlc-table-field>` — a table URL field with its Browse… button (table-field.js in the Hub).
 * Browse…, or a double-click in the empty field, opens the Hub's table picker for it; a pick fills
 * the field and fires `input` and `change`, which bubble from the inner input through the element.
 * The element is the field: read and write `value` by its id.
 *
 * It is form-associated, so a `<label for>` labels it. The picker tags the table it holds with that
 * label (or the `label` attribute), without a trailing "URL", in the lists the plugin's other table
 * fields open. Attributes: `placeholder`, `value` (the first value), `required`, `disabled`, `label`.
 *
 * A Hub before 1.2 does not define it: a plugin that uses it sets `min_frontend_version = "1.2.0"`.
 */
export interface TlcTableFieldElement extends HTMLElement {
  /** The table URL. Setting it fires no event, as on an `<input>`. */
  value: string;
  disabled: boolean;
  required: boolean;
  /** What the table picker tags this field's table with; '' when the field has no label. */
  readonly labelText: string;
  /**
   * This field's selection besides its value: the picker, opened from this field, marks these tables
   * as its own, like its current table. A field that adds to a list sets it to the list's tables, e.g.
   * `{url, label: 'Added'}`, so the picker shows what is in the list. Reading it gives a copy; assign
   * a new list to change it.
   */
  tagged: Array<{ url: string; label?: string }>;
}

// ── The preset bar (`shared.preset_ui`) ─────────────────────────────────────────

/**
 * What `_tlcPluginPresets(opts)` needs from a plugin. The bar renders into `mount`; the plugin's
 * config CRUD routes under `apiPath` (`GET`, `POST`, `GET /{id}`, `POST /{id}/delete`) store the
 * presets. `collect` and `apply` move the whole form, inputs included, in and out; `inputs` names
 * the keys that are inputs, which `contextInputs()` replaces for the page's life when the plugin
 * was opened from a table. See the plugin guide, "Presets".
 */
export interface TlcPresetOptions {
  pluginId: string;
  apiPath: string;
  mount: string;
  /** Id of the form's root: `input` and `change` events under it mark the form as changed. */
  formRoot?: string;
  /** The form's values, without `id` and `name`. */
  collect(): Record<string, any>;
  /** Fill the form; return a promise when that takes a round trip. */
  apply(values: Record<string, any>): void | Promise<unknown>;
  defaults(): Record<string, any>;
  inputs?: string[];
  /** The launch context's inputs, or null when the page was opened bare. */
  contextInputs?(): Record<string, any> | null;
  /** The suggested name for a new preset. */
  autoName?(values: Record<string, any>): string;
  /** A short hint shown after the name in the list, e.g. "yolo11n · detect". */
  /** Shown after the name; a list of parts is joined with " · ", leaving out the empty ones. */
  summary?(values: Record<string, any>): string | Array<string | null | undefined>;
  /** The old config bar's localStorage key; its remembered config opens once, as the draft takes over. */
  legacyKey?: string;
  /** Milliseconds before a change is written as the draft (default 300). */
  draftDelay?: number;
}

/** The bar `_tlcPluginPresets()` returns. */
export interface TlcPresetControl {
  /** Render, load the list and open on the draft, the old key, or the defaults, plus the context's inputs. */
  init(): Promise<void>;
  /** The form changed in a way no DOM event reports (JS state): recompute and keep the draft. */
  changed(): void;
  refresh(): Promise<void>;
  /** Apply a preset by id, or Default settings with ''. */
  pick(id: string): Promise<void>;
  openSave(): void;
  closeSave(): void;
  /** Save under the name in the field; false when a taken name is still to be confirmed, or on an error. */
  save(): Promise<boolean>;
  /** Delete a preset, the selected one without an id. Asks nothing; false when it failed. */
  remove(id?: string): Promise<boolean>;
  origin(): { kind: 'defaults' | 'preset'; id: string; name: string };
  modified(): boolean;
  /** The selected preset's id while the form is unchanged from it, else ''. */
  presetId(): string;
  /** Its name likewise: send it as `project_config.name` so the job card can show it. */
  presetName(): string;
  contextInputs(): Record<string, any> | null;
}

declare global {
  interface HTMLElementTagNameMap {
    'tlc-table-field': TlcTableFieldElement;
  }

  /** Injected by the frontend when a plugin fragment is mounted. */
  const PLUGIN_API: PluginApi;

  /** The Hub's list for dropdowns; feature-detect it (`typeof TlcPicker !== 'undefined'`). */
  const TlcPicker: TlcPickerApi | undefined;

  /** Injected by `3lc-compute-plugin-sdk` (`shared.job_tracker`) into the plugin fragment. */
  const PluginJobs: PluginJobsApi;

  /** Ambient frontend API client. */
  const TlcApi: TlcApi;

  // ── Legacy globals (stable through 0.x, namespaced rename planned) ────────────
  //
  // Globals a plugin's `ui.html` may already call bare. They are part of the shipped
  // surface and stay working through the 0.x line, but a namespaced rename is planned
  // (e.g. under `PLUGIN_API`), so treat them as legacy: prefer the documented bridge
  // where one exists. The `_tlc*` helpers are injected by the shared UI scripts
  // (`shared.alias_ui` / `shared.alias_override_ui` / `shared.data_source_ui` /
  // `shared.config_ui` / `shared.preset_ui`); the picker/cancel/cssVar globals come from the frontend.
  // NOTE: there is no bare `showToast` — use `PLUGIN_API.showToast`.

  /**
   * Open the shared table-picker overlay, writing the choice back into `targetInputId`. On a Hub 1.2
   * or later, prefer `<tlc-table-field>`, which opens it itself (see `TlcTableFieldElement`).
   */
  function openTablePicker(targetInputId: string): void;
  /** Close the shared table-picker overlay. */
  function closeTablePicker(): void;
  /** Read a CSS custom property (`--name`) resolved on the document root. */
  function cssVar(name: string): string;

  /** Shared cancel-confirmation dialog for a running job. */
  const CancelJob: {
    show(jobId: string, opts?: { onCancelled?: (result: any) => void; [key: string]: any }): void;
  };

  // Injected by the shared alias / data-source / config UI scripts (idPrefix scopes
  // each widget instance to its own DOM). Legacy — see the note above.
  /** Deprecated (`shared.alias_override_ui`): '' on a Hub with `PLUGIN_API.planRun`. */
  function _tlcAliasOverrideHtml(idPrefix: string): string;
  /**
   * Deprecated compatibility no-op. Permanent transfers belong in Storage, not alias settings.
   */
  function _tlcAliasReviewCopy(
    idPrefix: string,
    projectName: string,
    folderValue: string,
    pluginId?: string,
    rootOverride?: string,
    opts?: { copyOffer?: boolean },
  ): void;
  /** Empty when valid; otherwise explains why the durable alias cannot use this project location. */
  function _tlcAliasLocationError(idPrefix: string): string;
  function _tlcAliasSettingsHtml(idPrefix: string, projectValue: string, folderValue: string): string;
  /**
   * Keep the alias token, source root and mapping in step with input/change events.
   * Suggest the source folder name; preserve manual edits. rootInputId optionally binds placement updates.
   * Legacy pluginId/opts arguments are ignored.
   */
  function _tlcBindAliasAutoUpdate(
    idPrefix: string,
    projectInputId: string,
    folderInputId?: string | null,
    pluginId?: string,
    rootInputId?: string | null,
    opts?: { copyOffer?: boolean },
  ): void;
  /** Deprecated (`shared.alias_override_ui`). */
  function _tlcBindAliasOverrideToggle(idPrefix: string): void;
  function _tlcBindAliasToggle(idPrefix: string): void;
  /** Bind the "Create project in" select; resolves the selected root once the roots are known. */
  function _tlcBindProjectLocation(idPrefix: string, pluginId?: string): Promise<string>;
  /**
   * Bind a data-source field: a file chooser under it. `config.mode`: `'file'` (default) or
   * `'folder'` (`'dir'` reads as `'folder'`); `config.accept`: a glob list (`'*.csv,*.tsv'`);
   * `config.purpose`: `'input'` (default), or `'output'` for a Save dialog, whose name box starts
   * with `config.suggestedName` and whose pick is `<folder shown>/<name>` (a file output adds the
   * first extension `accept` names when the name has none; a folder output with an empty box picks
   * the folder shown). `mode`, `accept` and `suggestedName` may be functions, read on every open,
   * for a form whose choice changes them. The places follow `PLUGIN_API.getRunTarget()`.
   */
  function _tlcBindDataSource(
    idPrefix: string,
    computeUrl: string,
    pluginId: string,
    config?: {
      accept?: string | (() => string);
      mode?: 'file' | 'folder' | 'dir' | (() => 'file' | 'folder' | 'dir');
      purpose?: 'input' | 'output';
      suggestedName?: string | (() => string);
    },
  ): void;
  /** True when the compute URL is a loopback address: the compute host is this browser's machine. */
  function _tlcComputeIsHere(computeUrl: string): boolean;
  /** The field's markup: the text input (id `idPrefix`), Browse…, and the panel. Bind it once it is in the DOM. */
  function _tlcDataSourceHtml(
    idPrefix: string,
    config?: { placeholder?: string; accept?: string; allowUpload?: boolean; mode?: 'file' | 'folder' | 'dir' },
  ): string;
  function _tlcDefaultAliasToken(projectName: string): string;
  /** The compute host's default project root (`GET /api/deployment/storage`), '' when it cannot say. */
  function _tlcDefaultProjectRoot(): Promise<string>;
  /** Deprecated (`shared.alias_override_ui`): hides the card on a Hub with `PLUGIN_API.planRun`. */
  function _tlcFetchAndPopulateOverrides(idPrefix: string, tableUrl: string, savedOverrides?: any): void;
  /** Deprecated (`shared.alias_override_ui`). */
  function _tlcGetAliasOverrides(idPrefix: string): any;
  function _tlcGetAliasValues(idPrefix: string): any;
  function _tlcGetDataSourceValue(idPrefix: string): string;
  /** The root to send as an override: '' when the selection is the host's default. */
  function _tlcGetProjectRoot(idPrefix: string): string;
  function _tlcPluginConfig(opts?: any): any;
  /** The preset bar (`shared.preset_ui`); supersedes `_tlcPluginConfig`. */
  function _tlcPluginPresets(opts: TlcPresetOptions): TlcPresetControl;
  /** The "Create project in" field's HTML; bind it with `_tlcBindProjectLocation`. */
  function _tlcProjectLocationHtml(idPrefix: string): string;
  /** Deprecated (`shared.alias_override_ui`). */
  function _tlcRestoreAliasOverrides(idPrefix: string, saved?: any): void;
  /** What a project root is, for a label: "S3 bucket — s3://…", or the compute host's name for a path. */
  function _tlcRootLabel(url: string): string;
  /** `PLUGIN_API.getRunTarget()` normalized (a node choice without an id is local), or null without one. */
  function _tlcRunTarget(): {
    target: 'local' | 'node';
    node_id: string;
    ready: boolean;
    label: string;
    files_root: string;
    browse_roots?: string[];
  } | null;
  /** Where the table will be written, whichever option is selected ('' before the roots are known). */
  function _tlcSelectedProjectRoot(idPrefix: string): string;
  function _tlcSetAliasRoot(idPrefix: string, rootPath: string): void;
  function _tlcSetDataSourceValue(idPrefix: string, value: string): void;
  /** `'local'`, or `'scheme://bucket'`: what decides whether two places share storage. */
  function _tlcStorageOf(pathOrUrl: string): string;
  function _tlcSyncAliasFromForm(idPrefix: string, projectId: string, folderId: string): void;

  interface Window {
    PLUGIN_API: PluginApi;
    PluginJobs: PluginJobsApi;
    TlcApi: TlcApi;
    TlcPicker?: TlcPickerApi;
  }
}

export {};
