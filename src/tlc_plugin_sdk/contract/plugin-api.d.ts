// =============================================================================
// <copyright>
// Copyright (c) 2026 3LC Inc. All rights reserved.
//
// All rights are reserved. Reproduction or transmission in whole or in part, in
// any form or by any means, electronic, mechanical or otherwise, is prohibited
// without the prior written permission of the copyright owner.
// </copyright>
// =============================================================================
//
// JS_CONTRACT 0.2 — the browser-side plugin contract.
//
// This file DECLARES the JavaScript surface a plugin's `ui.html` programs against.
// It does not implement it:
//
//   * `PLUGIN_API`, `TlcApi`, `TlcData` are IMPLEMENTED by the Hub frontend
//     (3lc-hub-frontend/frontend/static/js/plugin-loader.js `mountPlugin`,
//     api-client.js, data-helpers.js). The frontend builds `window.PLUGIN_API`
//     when it mounts a plugin fragment.
//
//   * `window.PluginJobs` SHIPS FROM THIS PACKAGE (`3lc-plugin-sdk`): it is the
//     injectable client in `tlc_plugin_sdk.shared.job_tracker`
//     (`JOB_TRACKER_JS`), which a plugin injects into its fragment via
//     `inject_scripts(raw, job_tracker_script())`. It is NOT part of the host
//     bridge — it is layered on top of `PLUGIN_API`.
//
// This declaration is the source of truth for `JS_CONTRACT` (matches
// `tlc_plugin_sdk.JS_CONTRACT`). It increments independently of the Python
// contract; bump the package version when it moves.
//
// USAGE from a plain-JS `ui.html` (no build step):
//
//   /// <reference types="tlc_plugin_sdk/contract/plugin-api" />
//   // (the IMPORT name `tlc_plugin_sdk`, not the distribution name
//   //  `3lc-plugin-sdk` — the on-disk dir is `tlc_plugin_sdk`, and a
//   //  `/// <reference types>` resolves via typeRoots, not jsconfig `paths`.)
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
interface TlcComputeService {
  getHealth(): Promise<object>;
}

/**
 * Object-service method bag, exposed as `PLUGIN_API.objects`.
 * All methods go through `authFetch` against the object-service URL; object URLs
 * are encoded via `TlcApi.encodeObjectUrl`.
 */
interface TlcObjectService {
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
interface PluginFetchOptions extends RequestInit {
  /**
   * Abort the request after this many milliseconds (default 10000). Custom,
   * non-standard option — deleted before the underlying `fetch()` call. Ignored
   * when the caller supplies its own `signal`.
   */
  timeout?: number;
}

// ── TlcData (cached project/table/run indexing tables) ─────────────────────────

interface TlcDataProject {
  project_name: string;
  table_count: number;
  run_count: number;
  dataset_count: number;
  last_modified: number;
}

interface TlcDataTable {
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
}

interface TlcDataRun {
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
}

interface TlcDataSummary {
  project_count: number;
  table_count: number;
  run_count: number;
}

/**
 * The global `TlcData` helper (cached indexing tables). Referenced from
 * `PLUGIN_API.data`. Implemented by the frontend (data-helpers.js).
 */
interface TlcData {
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
interface PluginLibs {
  /** socket.io client (STABLE). */
  io: any | null;
  Chart: any | null;
  html2canvas: any | null;
  PptxGenJS: any | null;
  cytoscape: any | null;
}

// ── PLUGIN_API — the single host -> fragment bridge ────────────────────────────

/** Launch context: what the user launched the plugin against. */
interface PluginContext {
  /** Selected resource kind ('run','table',...) or `null` when launched bare. */
  resourceType: string | null;
  /** Selected 3LC object URLs (default `[]`). */
  resourceUrls: string[];
  /** Launch project name ('' when none). */
  projectName: string;
}

/**
 * The single host -> fragment JS contract. The frontend injects this as
 * `window.PLUGIN_API` when it mounts a plugin fragment; a fragment should reach
 * for nothing else. Many plugins alias it: `var API = window.PLUGIN_API`.
 */
interface PluginApi {
  /** Launch context (resource type/urls + project). */
  context: PluginContext;

  /**
   * The JS_CONTRACT version this host implements (e.g. "0.2"), so a fragment can
   * feature-detect the bridge. Derived by the frontend from the installed
   * `3lc-plugin-sdk` (`tlc_plugin_sdk.JS_CONTRACT`) — never a hardcoded literal —
   * and surfaced via `<body data-contract-version>`. '' if the host predates it.
   */
  contractVersion: string;

  /**
   * Return a configured URL by key. `dashboard_url` has its trailing slash
   * stripped; `compute_service_url` is the GPU/CPU-routed service for THIS plugin;
   * `object_service_url` comes from `TlcApi`. These three keys are the only ones
   * recognized — any other key returns ''.
   */
  getConfig(key: "dashboard_url" | "compute_service_url" | "object_service_url"): string;

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

  /** Reference to `TlcApi.computeService` (currently only `getHealth()`). */
  compute: TlcComputeService;

  /** Reference to `TlcApi.objectService`. Plugins usually reach data via `authFetch`. */
  objects: TlcObjectService;

  /** Reference to the global `TlcData` helper (`null` if `TlcData` is undefined at mount). */
  data: TlcData | null;

  /** Optional vendored third-party libraries (each `null` if the host didn't load it). */
  libs: PluginLibs;

  /** The DOM element the fragment was mounted into. Plugins scope their queries to it. */
  container: HTMLElement;

  /** Navigate the host to a path (sets `window.location.href = path`). */
  navigate(path: string): void;

  /** Show a host toast notification; no-op fallback if the host's `showToast` is unavailable. */
  showToast(message: string, type?: string): void;

  /**
   * Return an SVG icon string. With no id (or the current plugin's id) returns the
   * plugin manifest's `icon_svg` when present; otherwise delegates to
   * `TlcIcons.get(id || pluginId)`, or '' if `TlcIcons` is undefined.
   */
  getIcon(id?: string): string;
}

// ── window.PluginJobs — SDK-injected job-tracker client ────────────────────────

/** The opaque generic job object delivered to `PluginJobs` handlers. */
interface PluginJobUpdate {
  /** 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'. */
  status?: string;
  /** Job id. */
  id?: string;
  title?: string;
  /** Failure message rides here on error. */
  subtitle?: string;
  progress?: { percent?: number; label?: string; timing?: any };
  metrics?: Array<{ label?: string; value?: any }>;
  run_url?: string;
  [key: string]: any;
}

interface PluginJobHandlers {
  /** Fires on every `job_update`. */
  onUpdate?: (job: PluginJobUpdate) => void;
  /** Fires on terminal `completed` / `cancelled`. */
  onDone?: (job: PluginJobUpdate) => void;
  /** Fires on terminal `failed`. */
  onError?: (job: PluginJobUpdate) => void;
}

/** The parsed response of the generic `POST /api/plugins/{id}/run` route. */
interface PluginRunResponse {
  job_id?: string;
  status?: string;
  namespace?: string;
  error?: string;
}

/**
 * The job-tracker client. SHIPS FROM `3lc-plugin-sdk`
 * (`tlc_plugin_sdk.shared.job_tracker`, `JOB_TRACKER_JS`) — injected into a
 * plugin's fragment via `inject_scripts(raw, job_tracker_script())`. NOT part of
 * the host `PLUGIN_API` bridge; it is layered on top of it.
 */
interface PluginJobsApi {
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

  /** `POST {compute}/api/plugins/jobs/{jobId}/cancel` with body '{}'. */
  cancel(jobId: string): Promise<{ cancelled?: boolean }>;

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
interface TlcApi {
  computeService: TlcComputeService;
  objectService: TlcObjectService;
  authFetch(url: string, options?: PluginFetchOptions): Promise<Response>;
  computeFetch(path: string, options?: PluginFetchOptions, requiresGpu?: boolean): Promise<Response>;
  /** Resolved Object Service base URL (trailing slash stripped). */
  readonly objectServiceUrl: string;
  /** Resolves once compute-mode detection (GET /health -> mode/version) completes. */
  waitForMode(): Promise<void>;
}

declare global {
  /** Injected by the frontend when a plugin fragment is mounted. */
  const PLUGIN_API: PluginApi;

  /** Injected by `3lc-plugin-sdk` (`shared.job_tracker`) into the plugin fragment. */
  const PluginJobs: PluginJobsApi;

  /** Ambient frontend API client. */
  const TlcApi: TlcApi;

  interface Window {
    PLUGIN_API: PluginApi;
    PluginJobs: PluginJobsApi;
    TlcApi: TlcApi;
  }
}

export {};
