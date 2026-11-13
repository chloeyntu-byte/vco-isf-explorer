"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");
const APP_PATH = path.join(ROOT, "isf_tool", "ui", "app.js");
const SETUP_PATH = path.join(ROOT, "isf_tool", "ui", "index.html");
const DISCOVER_PATH = path.join(ROOT, "isf_tool", "ui", "discover.html");
const RESULTS_PATH = path.join(ROOT, "isf_tool", "ui", "results.html");
const INSPECT_PATH = path.join(ROOT, "isf_tool", "ui", "inspect.html");
const ERRORS_PATH = path.join(ROOT, "isf_tool", "ui", "errors.html");
const CATALOG_PATH = path.join(ROOT, "isf_tool", "ui", "error_catalog.json");

function loadUiFunctions(elements = {}) {
  const source = fs.readFileSync(APP_PATH, "utf8");
  const end = source.lastIndexOf("})();");
  assert.notEqual(end, -1, "app.js IIFE terminator must exist");
  const instrumented = source.slice(0, end) +
    "globalThis.__uiTest = { normalizeResults, currentJobId, flatConfig, configPayload, persistentConfig, activeJobRows, maestroPairPayload, selectHistoryCandidate, historyOptionLabel, invalidateMaestroHistory, maestroBindingReady, closureGateEvidence, formatOffsetHz, state };\n" +
    source.slice(end);
  const storage = new Map();
  const document = {
    readyState: "loading",
    body: { dataset: {} },
    addEventListener() {},
    getElementById(id) { return elements[id] || null; },
    querySelector() { return null; },
    querySelectorAll() { return []; }
  };
  const window = {
    location: { search: "" },
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, value); }
    },
    addEventListener() {},
    setTimeout,
    clearTimeout
  };
  const context = vm.createContext({
    AbortController,
    Blob,
    URL,
    URLSearchParams,
    console,
    document,
    navigator: {},
    setTimeout,
    clearTimeout,
    window
  });
  vm.runInContext(instrumented, context, { filename: APP_PATH });
  return context.__uiTest;
}

test("owned UI contains no demo evidence or hard-coded result gate", () => {
  const app = fs.readFileSync(APP_PATH, "utf8");
  const discover = fs.readFileSync(DISCOVER_PATH, "utf8");
  const results = fs.readFileSync(RESULTS_PATH, "utf8");
  const inspect = fs.readFileSync(INSPECT_PATH, "utf8");
  assert.doesNotMatch(app + discover + results + inspect, /demo-isf|DEMO_FILES|DEMO_RESULTS|createDemoResults/i);
  assert.doesNotMatch(results, /id="closure-status"[^>]*>[^<]*(?:PASS|WARN)/);
  assert.doesNotMatch(inspect, /id="export-prefix"[^>]+value="[^"]+"/);
});

test("setup starts from exactly two Maestro bindings and keeps overrides advanced", () => {
  const setup = fs.readFileSync(SETUP_PATH, "utf8");
  const app = fs.readFileSync(APP_PATH, "utf8");
  assert.match(setup, /id="run-preflight"/);
  assert.match(setup, /class="card advanced-settings"/);
  assert.match(setup, /name="mainTb"[^>]*required/);
  assert.match(setup, /name="replicaTb"[^>]*required/);
  assert.match(setup, /id="main-fingerprint"/);
  assert.match(setup, /id="main-history-fingerprint"/);
  assert.match(setup, /id="replica-fingerprint"/);
  assert.match(setup, /id="replica-history-fingerprint"/);
  assert.match(setup, /id="main-history"[^>]*name="mainHistory"/);
  assert.match(setup, /id="replica-history"[^>]*name="replicaHistory"/);
  assert.match(setup, /Detect & bind current settings/);
  assert.match(setup, /a unique complete candidate is selected automatically, select manually when multiple complete candidates exist, then detect again/);
  assert.doesNotMatch(setup, /Click again to explicitly accept/);
  assert.match(setup, /name="projectRoot"/);
  assert.match(setup, /name="adapterMode"/);
  assert.match(setup, /name="pdkRoot"/);
  assert.match(app, /\/api\/maestro\/pair/);
  assert.match(app, /\/api\/maestro\/discover/);
  assert.match(app, /mode === "maestro_pair" \? "dual TB primary workflow"/);
  assert.doesNotMatch(setup, /name="sourceRoot"/);
  assert.doesNotMatch(setup + app, /\/project\/isf_runs|createDemoResults/i);
});

test("Web API calls carry the per-process launch token", () => {
  const app = fs.readFileSync(APP_PATH, "utf8");
  assert.match(app, /window\.sessionStorage\.setItem\("isf-tool:api-token", token\)/);
  assert.match(app, /window\.history\.replaceState/);
  assert.match(app, /fetchOptions\.headers\["X-ISF-Token"\] = API_TOKEN/);
  assert.match(app, /headers\["X-ISF-Token"\] = API_TOKEN/);
  assert.doesNotMatch(app, /localStorage\.setItem\("isf-tool:api-token"/);
});

test("execution consent is neither restored nor retained in persistent config", () => {
  const { flatConfig, persistentConfig } = loadUiFunctions();
  assert.equal(flatConfig({ allowExecute: true }).allowExecute, false);
  assert.equal(flatConfig({ allow_execute: true }).allowExecute, false);
  assert.equal(flatConfig({ execution: { dry_run: false, allow_execute: true } }).allowExecute, false);

  const original = {
    allowExecute: true,
    allow_execute: true,
    execution: { dry_run: false, allow_execute: true },
    vendor_extension: { keep: true }
  };
  const stored = persistentConfig(original);
  assert.equal(stored.allowExecute, false);
  assert.equal(stored.allow_execute, false);
  assert.equal(stored.execution.allow_execute, false);
  assert.equal(stored.execution.dry_run, false);
  assert.equal(stored.vendor_extension.keep, true);
  assert.equal(original.allowExecute, true, "sanitizer must be pure");
  assert.equal(original.execution.allow_execute, true, "nested input must not be mutated");

  const report = persistentConfig({
    resolved_config: { execution: { allow_execute: true } }
  });
  assert.equal(report.resolved_config.execution.allow_execute, false);
});

test("activeJobRows identifies only queued and running jobs", () => {
  const { activeJobRows } = loadUiFunctions();
  const active = activeJobRows({ jobs: [
    { id: "queued-job", status: "queued" },
    { id: "running-job", state: "RUNNING" },
    { id: "done-job", status: "succeeded" },
    { id: "failed-job", status: "failed" }
  ] });
  assert.deepEqual(Array.from(active, (job) => job.id), ["queued-job", "running-job"]);
  assert.deepEqual(Array.from(activeJobRows(null)), []);
});

test("explicit start and resume clicks need no second confirmation and preserve the resume contract", () => {
  const app = fs.readFileSync(APP_PATH, "utf8");
  assert.match(app, /apiRequest\("\/api\/jobs"[\s\S]*activeJobRows\(jobs\)[\s\S]*var requestConfig = executionRequestConfig\(config\)/);
  assert.match(app, /Object\.assign\(\{\}, config, \{ executionMode: "live", allowExecute: true \}\)/);
  assert.doesNotMatch(app.slice(app.indexOf("async function startRemoteRun"), app.indexOf("function initDiscover")), /window\.confirm/);
  const start = app.indexOf('var resumeButton = byId("resume-job");', app.indexOf("function initDiscover"));
  const resume = app.slice(start, app.indexOf('if (state.apiOnline && !readStorage', start));
  assert.doesNotMatch(resume, /window\.confirm|configPayload|executionRequestConfig|writeStorage|allow_execute/);
  assert.match(resume, /addEventListener\("click"[\s\S]*activeJobRows\(jobs\)[\s\S]*encodeURIComponent\(id\) \+ "\/resume"[\s\S]*body: \{ background: true \}/);
});

test("actual resume listener waits for a click, keeps original payload, and honors active jobs and backend refusal", async () => {
  const app = fs.readFileSync(APP_PATH, "utf8");
  const start = app.indexOf('var resumeButton = byId("resume-job");', app.indexOf("function initDiscover"));
  const listener = app.slice(start, app.indexOf('if (state.apiOnline && !readStorage', start));
  const {activeJobRows} = loadUiFunctions();
  for (const scenario of ["success", "queued", "running", "backend-refused", "missing-id", "pending"]) {
    let click, polls = 0, refreshes = 0;
    const requests = [], failures = [], busy = [];
    const button = {addEventListener(event, callback) { assert.equal(event, "click"); click = callback; }};
    const context = {byId: id => id === "resume-job" ? button : null,
      state: {currentJob: scenario === "missing-id" ? null : {id: "frozen/job"}, executionPending: scenario === "pending", workspaceStateSequence:0},
      setButtonBusy: (_button, value) => busy.push(value), activeJobRows,
      window: {confirm() { throw new Error("unexpected confirmation"); }},
      toast() {}, reportError: error => failures.push(error),
      refreshRemoteJob: async () => { refreshes++; }, scheduleJobPoll: () => { polls++; },
      apiRequest: async (path, options) => {
        assert.equal(context.state.executionPending,true);
        requests.push([path, JSON.parse(JSON.stringify(options))]);
        if (path === "/api/jobs") return {jobs: ["queued", "running"].includes(scenario) ? [{id: "already-active", status: scenario}] : []};
        if (scenario === "backend-refused") throw new Error("frozen contract rejected");
        return {ok: true};
      }
    };
    vm.runInNewContext(listener, context);
    assert.equal(requests.length, 0, "registering/loading must not auto-resume");
    await click();
    const posts = requests.filter(([path]) => path.endsWith("/resume"));
    if (["success", "backend-refused"].includes(scenario)) {
      assert.deepEqual(posts, [["/api/jobs/frozen%2Fjob/resume", {method: "POST", body: {background: true}, timeout: 10000}]]);
    } else assert.equal(posts.length, 0);
    assert.equal(polls, scenario === "success" ? 1 : 0);
    assert.equal(refreshes, scenario === "success" ? 1 : 0);
    assert.equal(failures.length, ["backend-refused", "missing-id"].includes(scenario) ? 1 : 0);
    assert.deepEqual(busy, ["missing-id","pending"].includes(scenario) ? [] : [true, false]);
    assert.equal(context.state.executionPending,scenario === "pending");
  }
});

test("history selection auto-picks only one complete RDB and keeps multiple choices manual", () => {
  const { selectHistoryCandidate, historyOptionLabel } = loadUiFunctions();
  const first = {
    name: "Interactive.0",
    path: "/exact/vco/results/maestro/Interactive.0.rdb",
    complete_runs: 2,
    checked_runs: 2,
    fully_complete: true
  };
  const second = {
    name: "Interactive.1",
    path: "/exact/vco/results/maestro/Interactive.1.rdb",
    complete_runs: 3,
    checked_runs: 3,
    fully_complete: true
  };
  const incomplete = {
    name: "Interactive.2",
    path: "/exact/vco/results/maestro/Interactive.2.rdb",
    complete_runs: 1,
    checked_runs: 2,
    fully_complete: false
  };

  const unique = selectHistoryCandidate([first, incomplete], "");
  assert.equal(unique.selected.path, first.path);
  assert.equal(unique.requiresManual, false);

  const multiple = selectHistoryCandidate([first, second, incomplete], "");
  assert.equal(multiple.selected, null);
  assert.equal(multiple.completeCount, 2);
  assert.equal(multiple.requiresManual, true);

  const requested = selectHistoryCandidate([first, second], second.path);
  assert.equal(requested.selected.path, second.path);
  assert.equal(requested.requiresManual, false);
  assert.equal(
    historyOptionLabel(second),
    "Interactive.1 · /exact/vco/results/maestro/Interactive.1.rdb · complete/checked 3/3"
  );
});

test("history and both fingerprints survive inspect, bind, and preflight payloads", () => {
  const { flatConfig, configPayload, maestroPairPayload, maestroBindingReady, state } = loadUiFunctions();
  const raw = {
    schema_version: 2,
    testbenches: {
      vco: {
        maestro_dir: "/maestro/vco",
        test: "vco_nominal",
        history: "/maestro/vco/results/maestro/Interactive.4.rdb",
        binding_fingerprint: "vco-binding-fp",
        history_fingerprint: "vco-history-fp"
      },
      replica: {
        maestro_dir: "/maestro/flicker",
        test: "flicker_nominal",
        history: "/maestro/flicker/results/maestro/Interactive.7.rdb",
        binding_fingerprint: "flicker-binding-fp",
        history_fingerprint: "flicker-history-fp"
      }
    }
  };
  state.rawConfig = raw;
  const config = flatConfig(raw);
  assert.equal(config.mainHistory, raw.testbenches.vco.history);
  assert.equal(config.replicaHistory, raw.testbenches.replica.history);
  assert.equal(config.mainHistoryFingerprint, "vco-history-fp");
  assert.equal(config.replicaHistoryFingerprint, "flicker-history-fp");
  assert.equal(maestroBindingReady(config), true);

  const inspectPayload = maestroPairPayload(config, true);
  assert.equal(inspectPayload.testbenches.vco.history, raw.testbenches.vco.history);
  assert.equal(inspectPayload.testbenches.vco.history_fingerprint, "vco-history-fp");
  assert.equal(inspectPayload.testbenches.replica.history, raw.testbenches.replica.history);
  assert.equal(inspectPayload.testbenches.replica.history_fingerprint, "flicker-history-fp");

  const preflightConfig = configPayload(config);
  assert.equal(preflightConfig.testbenches.vco.history, raw.testbenches.vco.history);
  assert.equal(preflightConfig.testbenches.vco.history_fingerprint, "vco-history-fp");
  assert.equal(preflightConfig.testbenches.replica.history, raw.testbenches.replica.history);
  assert.equal(preflightConfig.testbenches.replica.history_fingerprint, "flicker-history-fp");
});

test("changing a history invalidates its binding and history fingerprints", () => {
  const elements = {
    "main-history": {
      value: "/exact/vco/results/maestro/Interactive.9.rdb",
      selectedIndex: 0,
      options: [{ dataset: { historyName: "Interactive.9", complete: "4", checked: "4" } }]
    },
    "main-fingerprint": { value: "old-binding-fp" },
    "main-history-fingerprint": { value: "old-history-fp" },
    "vco-bind-status": { textContent: "Bound", className: "tag" },
    "vco-bind-detail": { innerHTML: "old detail" },
    "preflight-status": { textContent: "PASS", className: "status-pill success" },
    "preflight-required": { textContent: "no manual fields required" },
    "inspect-maestro-pair": { textContent: "Detect & bind current settings" }
  };
  const { invalidateMaestroHistory } = loadUiFunctions(elements);
  invalidateMaestroHistory("vco");

  assert.equal(elements["main-fingerprint"].value, "");
  assert.equal(elements["main-history-fingerprint"].value, "");
  assert.equal(elements["vco-bind-status"].textContent, "Detection required again");
  assert.match(elements["vco-bind-detail"].innerHTML, /Interactive\.9/);
  assert.match(elements["vco-bind-detail"].innerHTML, /\/exact\/vco\/results\/maestro\/Interactive\.9\.rdb/);
  assert.match(elements["vco-bind-detail"].innerHTML, /complete\/checked · 4\/4/);
  assert.match(elements["vco-bind-detail"].innerHTML, /history fingerprint · Detection required again/);
  assert.equal(elements["preflight-status"].textContent, "Rerun required");
  assert.equal(elements["inspect-maestro-pair"].textContent, "Detect & bind current history");
});

test("run page renders the bound pair instead of generic discovery candidates", () => {
  const discover = fs.readFileSync(DISCOVER_PATH, "utf8");
  const app = fs.readFileSync(APP_PATH, "utf8");
  assert.match(discover, /Bound inputs/);
  assert.match(discover, /VCO Maestro/);
  assert.match(discover, /Flicker-noise Maestro/);
  assert.match(app, /detected\.maestro_pair \|\| cachedPreflight\.detected\.inputs/);
  assert.doesNotMatch(discover, /Path from[^<]*unique candidate/);
});

test("V2 diagnostics and recovery controls are locally packaged", () => {
  const app = fs.readFileSync(APP_PATH, "utf8");
  const discover = fs.readFileSync(DISCOVER_PATH, "utf8");
  const errors = fs.readFileSync(ERRORS_PATH, "utf8");
  const catalog = JSON.parse(fs.readFileSync(CATALOG_PATH, "utf8"));
  assert.match(discover, /id="resume-job"/);
  assert.match(discover, /id="job-error-panel"/);
  assert.match(errors, /id="error-catalog-list"/);
  assert.ok(catalog.entries.some((entry) => entry.code === "ISF-E5002"));
  assert.match(app, /_updatePhaseCursor/);
  assert.match(app, /highlightHarmonic/);
  assert.match(app, /\/api\/preflight/);
});

test("generic files require explicit verified status", () => {
  const { normalizeResults } = loadUiFunctions();
  const result = normalizeResults({
    job_id: "generic-job",
    files: [
      { name: "missing.csv", path: "/tmp/missing.csv" },
      { name: "pending.csv", path: "/tmp/pending.csv", status: "pending" },
      { name: "verified.csv", path: "/tmp/verified.csv", status: "verified" }
    ]
  }, "generic-job");
  assert.deepEqual(Array.from(result.files, (file) => file.name), ["verified.csv"]);
});

test("formal canonical job keeps only verified provenance files", () => {
  const { normalizeResults } = loadUiFunctions();
  const jobId = "formal-canonical-job";
  const payload = {
    job_id: jobId,
    run_kind: "signoff",
    dual_tb: {
      signoff: true,
      metadata: {},
      vco_rows: 42,
      replica_rows: 24,
      workpoint_rows: 1542
    },
    summary: Array.from({ length: 6 }, (_, index) => ({
      point: "point-" + index,
      f0_Hz: 5.5e9,
      dynamic_error_dB: 0.1 * index,
      pair_error_dB: -0.1 * index
    })),
    points: Object.fromEntries(Array.from({ length: 6 }, (_, index) => [
      "point-" + index,
      { workpoint: [], harmonics: [] }
    ])),
    provenance: {
      job_id: jobId,
      manifest_sha256: "a".repeat(64),
      psf_runs: 12,
      files: [
        "vco_results.csv",
        "replica_results.csv",
        "workpoint.csv",
        "metadata.json",
        "manifest.json"
      ].map((name, index) => ({
        name,
        path: "/verified/" + name,
        bytes: 100 + index,
        sha256: String(index).repeat(64),
        status: "verified"
      })).concat([{ name: "pending.csv", path: "/pending.csv", status: "pending" }])
    }
  };
  payload.files = [{ name: "generic-should-not-leak.csv", status: "verified" }];
  const result = normalizeResults(payload, jobId);
  assert.equal(result.id, jobId);
  assert.equal(result.summaryRows.length, 6);
  assert.equal(result.signoff, true);
  assert.equal(result.files.length, 5);
  assert.ok(result.files.every((file) => file.status === "verified"));
  assert.ok(!result.files.some((file) => file.name === "generic-should-not-leak.csv"));
});

test("advanced fields survive while the primary UI enforces Maestro-pair input", () => {
  const { flatConfig, configPayload, state } = loadUiFunctions();
  const raw = {
    schema_version: 2,
    project_root: "/project",
    execution: {
      dry_run: true,
      adapter_mode: "protocol",
      adapters: {
        vco: {
          kind: "protocol",
          submit_command: ["submit", "--token", "{job_id}"],
          poll_command: ["poll", "{scheduler_job_id}"]
        }
      }
    },
    input_policy: {
      preferred: "canonical",
      allowed: ["canonical"],
      allow_degraded: false
    },
    auto: {
      vco_command: ["python", "runner.py", "--stage", "vco"],
      timeout_s: 1234
    },
    mapping: {
      target_device: "XM.GM0",
      custom_unit_contract: "keep-me"
    },
    profiles: {
      environment: {
        executables: {
          spectre: "/eda/spectre",
          ocean: "/eda/ocean"
        }
      },
      design: {
        mode: "manual",
        variant: "class-f"
      }
    },
    vendor_extension: { revision: 7 }
  };
  state.rawConfig = raw;
  const payload = configPayload(flatConfig(raw));
  assert.equal(payload.execution.adapters.vco.kind, "protocol");
  assert.deepEqual(
    Array.from(payload.execution.adapters.vco.poll_command),
    ["poll", "{scheduler_job_id}"]
  );
  assert.equal(payload.mapping.custom_unit_contract, "keep-me");
  assert.equal(payload.profiles.environment.executables.ocean, "/eda/ocean");
  assert.equal(payload.profiles.design.mode, "manual");
  assert.equal(payload.profiles.design.variant, "class-f");
  assert.equal(payload.source_mode, "maestro_pair");
  assert.equal(payload.input_policy.preferred, "maestro-pair");
  assert.ok(Array.from(payload.input_policy.allowed).includes("maestro-pair"));
  assert.deepEqual(
    Array.from(payload.auto.vco_command),
    ["python", "runner.py", "--stage", "vco"]
  );
  assert.equal(payload.vendor_extension.revision, 7);
});

test("hidden profiles.auto_detect=false survives the setup round trip", () => {
  const { flatConfig, configPayload, state } = loadUiFunctions();
  const raw = {
    schema_version: 2,
    profiles: {
      auto_detect: false,
      environment: { mode: "manual" }
    },
    execution: { dry_run: true }
  };
  state.rawConfig = raw;
  const payload = configPayload(flatConfig(raw));
  assert.equal(payload.profiles.auto_detect, false);
  assert.equal(payload.profiles.environment.mode, "manual");
});

test("missing closure metric stays N/A and metadata cannot self-claim signoff", () => {
  const { normalizeResults } = loadUiFunctions();
  const jobId = "untrusted-import";
  const result = normalizeResults({
    job_id: jobId,
    run_kind: "signoff",
    dual_tb: {
      signoff: true,
      metadata: { run_kind: "signoff", signoff: true }
    },
    summary: [{ point: "p0", f0_Hz: 5e9 }],
    points: { p0: { workpoint: [], harmonics: [] } },
    provenance: null
  }, jobId);
  assert.equal(result.metrics.errorDb, null);
  assert.equal(result.signoff, false);
  assert.equal(result.runKind, "replay");
});

test("null and empty dynamic errors cannot become zero-valued closure evidence", () => {
  const { normalizeResults, closureGateEvidence } = loadUiFunctions();
  [null, ""].forEach((missingValue, index) => {
    const point = "missing-" + index;
    const result = normalizeResults({
      job_id: "missing-dynamic-" + index,
      summary: [
        {
          point,
          f0_Hz: 5e9,
          dc_error_dB: missingValue,
          delay_error_dB: missingValue,
          dynamic_error_dB: missingValue,
          pair_error_dB: missingValue
        },
        {
          point: "valid-" + index,
          f0_Hz: 5e9,
          dc_error_dB: 0.5,
          delay_error_dB: 0.4,
          dynamic_error_dB: 0.25,
          pair_error_dB: 0.3
        }
      ],
      points: {
        [point]: { workpoint: [], harmonics: [] },
        ["valid-" + index]: { workpoint: [], harmonics: [] }
      }
    }, "missing-dynamic-" + index);
    assert.equal(result.metrics.errorDb, null);
    assert.equal(result.metrics.pairErrorDb, null);
    assert.equal(result.metrics.trendDb, null);
    assert.ok(Number.isNaN(result.charts.comparison.series[0].values[0]));
    assert.equal(result.charts.comparison.series.length, 1);
    assert.equal(result.charts.comparison.series[0].name, "two-device flicker");
    const evidence = closureGateEvidence(result.summaryRows, result.metrics.trendDb);
    assert.equal(evidence.maxAbs, null);
    assert.equal(evidence.trend, null);
    assert.equal(evidence.absPass, false);
    assert.equal(evidence.trendPass, false);
  });
});

test("results offset copy uses result or explicit config without a fabricated default", () => {
  const html = fs.readFileSync(RESULTS_PATH, "utf8");
  assert.match(html, /id="comparison-offset">offset Not provided</);
  assert.doesNotMatch(html, /contribution\s*·\s*10 kHz/);

  const { normalizeResults, formatOffsetHz, state } = loadUiFunctions();
  const fromResult = normalizeResults({
    job_id: "offset-result",
    summary: [{ point: "p0", f0_Hz: 5e9, offset_Hz: 25000 }],
    points: { p0: { workpoint: [], harmonics: [] } }
  }, "offset-result");
  assert.equal(fromResult.metrics.offsetHz, 25000);
  assert.equal(formatOffsetHz(fromResult.metrics.offsetHz), "25 kHz");

  state.rawConfig = { analysis: { offset: { value: 2.5, unit: "MHz" } } };
  const fromConfig = normalizeResults({ job_id: "offset-config" }, "offset-config");
  assert.equal(fromConfig.metrics.offsetHz, 2.5e6);
  assert.equal(formatOffsetHz(fromConfig.metrics.offsetHz), "2.5 MHz");

  state.rawConfig = {};
  const missing = normalizeResults({ job_id: "offset-missing" }, "offset-missing");
  assert.equal(missing.metrics.offsetHz, null);
  assert.equal(formatOffsetHz(missing.metrics.offsetHz), "offset Not provided");
});
