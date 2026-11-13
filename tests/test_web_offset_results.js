"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ROOT = path.resolve(__dirname, "..");
const APP = path.join(ROOT, "isf_tool/ui/app.js");
const plain = value => JSON.parse(JSON.stringify(value));

function node(value = "") {
  let html = "", text = "";
  return {
    value, dataset: {}, hidden: false, disabled: false, attributes: {}, listeners: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {} },
    set innerHTML(value) { html = value; text = ""; const options = [...value.matchAll(/<option value="([^"]*)"([^>]*)>/g)]; if (options.length) this.value = (options.find(option => /selected/.test(option[2])) || options[0])[1]; },
    get innerHTML() { return html; },
    set textContent(value) { text = String(value); html = ""; }, get textContent() { return text; },
    setAttribute(key, value) { this.attributes[key] = value; }, removeAttribute(key) { delete this.attributes[key]; },
    addEventListener(key, callback) { this.listeners[key] = callback; },
    querySelectorAll() { return []; }, querySelector() { return null; }
  };
}

function harness(elements = {}, selectors = {}) {
  const events = {};
  const document = {
    readyState: "loading", body: { dataset: {} },
    getElementById(id) { return elements[id] || null; },
    querySelector() { return null; }, querySelectorAll(selector) { return selectors[selector] || []; },
    addEventListener(name, callback) { events[name] = callback; }
  };
  const storage = new Map();
  const window = { location: { search: "" }, addEventListener() {}, setTimeout, clearTimeout,
    localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) } };
  const context = vm.createContext({ document, window, console, URL, URLSearchParams, AbortController, Blob,
    setTimeout, clearTimeout, navigator: {},
    FormData: class { constructor(form) { this.values = form.values || {}; } get(name) { return this.values[name]; } }
  });
  const source = fs.readFileSync(APP, "utf8"), end = source.lastIndexOf("})();");
  const expose = "globalThis.ui = {state,flatConfig,collectConfig,configPayload,offsetSpec,offsetAnalysis,updateOffsetMode,requestOffsetPreview,offsetTaskCount,previewFirstOffset,normalizeResults,noiseSeries,resultNoiseSeries,predictedNoiseDbc,thermalBandDbc,noiseContributionRows,noisePieData,workspaceStateConfig,workspaceStateView,saveWorkspaceState,loadWorkspaceState,loadRemoteResults,initWorkspaceStates,executionRequestConfig,requestPreflight,attachFrequencyInteraction,resultOperatingPoints,selectResultSample,frequencyChartMarkup,renderResults,renderFlickerResults,renderMetricTables,metricDefinition,metricHelpHtml,initMetricHelp,initResults,mappingFieldGroup,groupedMappingHtml,canStopAfterOffset,requestStopAfterOffset,jobFromResponse,renderJob,setApi(fn){apiRequest=fn;}};\n";
  vm.runInContext(source.slice(0, end) + expose + "ui.startRemoteRun = startRemoteRun;\n" + source.slice(end), context, { filename: APP });
  return { ui: context.ui, events, elements, storage, document, window };
}

function fixture() {
  const result = { summary: [], points: {}, dual_tb: { metadata: {} }, offset_sweep: { complete: false, offsets_hz: [1000, 10000, 100000], completed_offsets_hz: [1000, 100000] } };
  for (const op of ["SW0", "SW1"]) for (const offset of [1000, 100000]) {
    const point = op + "@" + offset;
    const shift = op === "SW1" ? 2 : 0;
    result.summary.push({ point, operating_point_id: op, corner: "same", offset_Hz: offset,
      operating_parameters: { SW: op === "SW1" ? 1 : 0 }, f0_Hz: 1e9,
      dynamic_dBc_Hz: -123 + shift, spectre_fn_dBc_Hz: -124 + shift, spectre_total_dBc_Hz: -110 + shift, dynamic_error_dB: 1,
      spectre_noise_reference: {scope: "whole_vco", schema_version: 1},
      predicted_pair_dBc_Hz: -120 + shift, spectre_pair_dBc_Hz: -121 + shift,
      pair_model: "symmetric_independent", numerical_quality_pass: true, pm_magnitude_projection_valid: false,
      spectre_fn_pm_asd_V_rtHz: 1e-6 });
    result.points[point] = { workpoint: [], harmonics: [0, 1].map(k => ({
      k, Q_real_V_rtHz: k ? -9 : 10, Q_imag_V_rtHz: 0, Q_mag_V_rtHz: k ? 9 : 10,
      cum_real_V_rtHz: k ? 1 : 10, cum_imag_V_rtHz: 0, cumulative_error_dB: k ? 1 : 20,
      harmonic_equivalent_dBc_Hz: -90 - k, cumulative_dBc_Hz: k ? -123 + shift : -90,
      pair_cumulative_dBc_Hz: k ? -120 + shift : -87,
      pair_cumulative_error_dB: k ? 1 : 34 - shift,
      A_k: null, delta_k_deg: null
    })) };
  }
  return result;
}

test("offset modes retain original recipe and discard stale derived offset lists", () => {
  const { ui } = harness();
  const raw = { analysis: { offset: { value: 10, unit: "kHz" }, offsets: { mode: "log", start: "1k", stop: "100M", points_per_decade: 1, offsets_hz: [3], values: [3] } } };
  ui.state.rawConfig = raw;
  const config = ui.flatConfig(raw);
  assert.equal(config.offsetMode, "log");
  assert.deepEqual(plain(ui.configPayload(config).analysis.offsets), { mode: "log", start: "1k", stop: "100M", points_per_decade: 1 });
  config.offsetValues = "1k,10k,1M,100M";
  for (const [mode, extra] of [["single", {}], ["list", { values: "1k,10k,1M,100M" }], ["linear", { start: "1k", stop: "100M", spacing: "intervals", step: "1k", intervals: 5 }]]) {
    config.offsetMode = mode;
    assert.deepEqual(plain(ui.offsetSpec(config)), { mode, ...extra });
  }
  assert.equal(ui.offsetAnalysis(config).offset.hz, 10000);
  ui.state.rawConfig = { offsetMode: "log", offsetStart: "5k", analysis: raw.analysis };
  config.offsetMode = "single";
  assert.equal(ui.flatConfig(ui.configPayload(config)).offsetMode, "single");
});

test("backend owns point expansion; preview renders actual Hz and physical task count without resetting consent", async () => {
  const elements = { "offset-preview": node() };
  const { ui } = harness(elements);
  ui.state.apiOnline = true;
  ui.state.currentMaestroPair = { pair: { operating_points: [{}, {}] }, roles: { flicker: { points: new Array(25) } } };
  const config = ui.flatConfig({}); config.offsetMode = "log"; config.allowExecute = true;
  const oldPreflight = { ok: true }; ui.state.currentPreflight = oldPreflight;
  let request;
  ui.setApi(async (url, options) => { request = [url, plain(options.body)]; return { ok: true, offsets_hz: [1e3, 1e4, 1e5, 1e6, 1e7, 1e8] }; });
  await ui.requestOffsetPreview(config);
  assert.equal(request[0], "/api/offsets");
  assert.equal(request[1].analysis.offsets.points_per_decade, 1);
  assert.equal(ui.previewFirstOffset(config), 1000);
  assert.match(elements["offset-preview"].innerHTML, /2 actual operating points × 6 offsets = 12/);
  assert.match(elements["offset-preview"].innerHTML, /100000000 Hz/);
  assert.equal(config.allowExecute, true);
  assert.equal(ui.state.currentPreflight, oldPreflight);
  assert.equal(ui.offsetTaskCount(null, [1, 2]).evaluations, null);
});

test("late or illegal previews cannot replace current points", async () => {
  const { ui } = harness({ "offset-preview": node() });
  ui.state.apiOnline = true;
  const resolve = [];
  ui.setApi(() => new Promise(done => resolve.push(done)));
  const first = ui.requestOffsetPreview(ui.flatConfig({ offsetMode: "list", offsetValues: "1k" }));
  const last = ui.requestOffsetPreview(ui.flatConfig({ offsetMode: "list", offsetValues: "10k" }));
  resolve[1]({ ok: true, offsets_hz: [10000] }); await last;
  resolve[0]({ ok: true, offsets_hz: [1000] }); await first;
  assert.deepEqual(plain(ui.state.offsetPreview.values), [10000]);
  for (const invalid of [[0], [null], [Infinity], new Array(257).fill(1000)]) {
    ui.setApi(async () => ({ ok: true, offsets_hz: invalid }));
    assert.equal(await ui.requestOffsetPreview(ui.flatConfig({})), null);
    assert.equal(ui.state.offsetPreview, null);
  }
});

test("inactive offset controls do not block form validation and retain their values", () => {
  const control = node("bad-but-inactive"), single = node(), step = node(), intervals = node();
  single.dataset.offsetModes = "single"; single.querySelectorAll = () => [control];
  step.dataset.offsetSpacing = "step"; intervals.dataset.offsetSpacing = "intervals";
  const { ui } = harness({ "config-form": { values: { offsetMode: "log" }, elements: { namedItem: name => name === "offsetValue" ? { value: "10" } : null } } }, { "[data-offset-modes]": [single], "[data-offset-spacing]": [step, intervals] });
  ui.updateOffsetMode({ offsetMode: "log", offsetSpacing: "intervals" });
  assert.equal(single.hidden, true); assert.equal(control.disabled, true);
  assert.equal(control.value, "bad-but-inactive");
  assert.equal(ui.collectConfig().offsetValue, 10);
  ui.updateOffsetMode({ offsetMode: "single" });
  assert.equal(control.disabled, false);
});

test("operating points and offsets are independent; selecting a sample never mixes offsets in comparison", () => {
  const elements = { "result-point-select": node(), "result-offset-select": node("100000") };
  const { ui } = harness(elements), raw = fixture();
  let result = ui.normalizeResults(raw, "job", "SW1");
  assert.equal(result.point, "SW1@100000");
  assert.equal(result.operatingPointId, "SW1");
  assert.deepEqual(plain(result.charts.comparison.labels), ["SW0", "SW1"]);
  assert.equal((elements["result-point-select"].innerHTML.match(/<option/g) || []).length, 2);
  assert.equal((elements["result-offset-select"].innerHTML.match(/<option/g) || []).length, 2);
  assert.equal(result.errors[0].qMag, 10);
  assert.equal(result.errors[0].qPhase, 0);
  assert.equal(result.errors[1].qPhase, 180);
  assert.equal(result.errors[1].cumulativeErrorDb, 1);
  elements["result-offset-select"].value = "1000";
  result = ui.normalizeResults(raw, "job", "SW1");
  assert.equal(result.point, "SW1@1000");
  assert.equal(result.metrics.offsetHz, 1000);
  assert.equal(ui.selectResultSample(raw, "SW0@100000", "").sample.point, "SW0@100000");
});

test("phase, harmonic and cumulative curves use backend dBc exactly; missing offsets create gaps", () => {
  const { ui } = harness(), raw = fixture();
  const curves = ui.noiseSeries(raw, "SW0", "single", 1);
  assert.deepEqual(plain(curves.phase[0].values), [{ x: 1000, y: -123 }, { x: 10000, y: null }, { x: 100000, y: -123 }]);
  assert.deepEqual(plain(curves.harmonics.map(curve => curve.values[0].y)), [-90, -91, -123, -123]);
  assert.equal(ui.noiseSeries(raw, "SW0", "pair", null).phase[0].values[0].y, -120);
  delete raw.points["SW0@100000"].harmonics[1].cumulative_dBc_Hz;
  assert.equal(ui.noiseSeries(raw, "SW0", "single", 1).harmonics.at(-1).values.at(-1).y, null);
  raw.summary[0].spectre_total_dBc_Hz = null;
  assert.equal(ui.noiseSeries(raw, "SW0", "single").phase[1].values[0].y, null);
});

test("frequency SVG displays actual markers and breaks paths across missing data; no fabricated zero", () => {
  const { ui } = harness();
  const html = ui.frequencyChartMarkup([{ name: '<img onerror="x">', color: "#123", values: [{ x: 1000, y: -120 }, { x: 10000, y: null }, { x: 100000, y: -140 }] }], true);
  assert.equal((html.match(/<circle/g) || []).length, 2);
  assert.match(html, /d=" M[^"]+ M/);
  assert.doesNotMatch(html, /class="chart-line" d="[^"]+ L|<img/);
  assert.match(html, /&lt;img/);
  assert.match(html, /tabindex="0"/);
  assert.match(html, /Log axis/);
  const missing = ui.frequencyChartMarkup([{ name: "missing", values: [{ x: 1, y: null }] }], true);
  assert.doesNotMatch(missing, /<svg|<circle/);
  assert.match(missing, /missing values are not zero-filled/);
});

test("DOM render updates dBc scope, partial status, quality and all field tables; stale values clear", () => {
  const elements = Object.fromEntries(["phase-noise-chart", "harmonic-noise-chart", "metric-calculated-dbc", "metric-spectre-dbc", "noise-scope-note", "sample-quality-note", "result-operating-parameters", "sweep-result-note", "result-metric-tables", "calculated-dbc-help", "spectre-dbc-help"].map(id => [id, node()]));
  elements["noise-kind"] = node("flicker"); elements["cumulative-k-select"] = node("1");
  const { ui } = harness(elements), result = ui.normalizeResults(fixture(), "job", "SW0");
  ui.renderFlickerResults(result);
  assert.equal(elements["metric-calculated-dbc"].textContent, "-120.000 dBc/Hz");
  assert.equal(elements["metric-spectre-dbc"].textContent, "-110.000 dBc/Hz");
  assert.match(elements["sweep-result-note"].textContent, /Partial results.*2\/3/);
  assert.match(elements["sample-quality-note"].textContent, /PM approximation: Failed.*Numerical quality: Passed.*HBAC Secondary check: Not provided/);
  assert.match(elements["result-metric-tables"].innerHTML, /data-metric-key="Q_real_V_rtHz"/);
  assert.match(elements["result-metric-tables"].innerHTML, />N\/A</);
  elements["noise-kind"].value = "both"; ui.renderFlickerResults(result);
  assert.equal(elements["metric-calculated-dbc"].textContent, "N/A");
  assert.equal(elements["spectre-dbc-help"].dataset.metricKey, "spectre_total_dBc_Hz");
  assert.equal(elements["metric-spectre-dbc"].textContent, "-110.000 dBc/Hz");
  assert.match(elements["noise-scope-note"].textContent, /symmetric prediction for independent devices/);
  assert.match(elements["noise-scope-note"].textContent, /thermal.*N\/A/);
  ui.renderFlickerResults({ error: "missing" });
  assert.equal(elements["metric-calculated-dbc"].textContent, "N/A");
  assert.equal(elements["result-metric-tables"].textContent, "");
});

test("shared metric help opens on native button activation and safely describes aliases", async () => {
  const elements = { "metric-help-dialog": node(), "metric-help-content": node(), "close-metric-help": node() };
  elements["metric-help-dialog"].showModal = function () { this.open = true; };
  elements["metric-help-dialog"].close = function () { this.open = false; };
  const { ui, events } = harness(elements);
  ui.setApi(async url => { assert.equal(url, "/metric_catalog.json"); return JSON.parse(fs.readFileSync(path.join(ROOT, "isf_tool/ui/metric_catalog.json"))); });
  ui.initMetricHelp();
  await events.click({ target: { closest() { return { dataset: { metricKey: "Qk_real_V_rtHz" } }; } } });
  assert.equal(elements["metric-help-dialog"].open, true);
  assert.match(elements["metric-help-content"].innerHTML, /Qk real part|Q real part/);
  assert.match(elements["metric-help-content"].innerHTML, /coherent/);
  elements["close-metric-help"].listeners.click();
  assert.equal(elements["metric-help-dialog"].open, false);
  ui.state.metricCatalog.metrics.bad = { label: '<img onerror="x">', meaning: "<script>" };
  assert.doesNotMatch(ui.metricHelpHtml("bad"), /<img|<script/);
});

test("result-page DOM event wiring changes offset and scope without replacing the selected operating point", () => {
  const elements = Object.fromEntries(["result-job-select", "result-point-select", "result-offset-select", "phase-noise-chart", "harmonic-noise-chart", "metric-calculated-dbc", "metric-spectre-dbc"].map(id => [id, node()]));
  elements["noise-kind"] = node("both"); elements["cumulative-k-select"] = node("");
  const { ui } = harness(elements);
  ui.initResults();
  ui.state.currentRawResults = fixture();
  elements["result-point-select"].value = "SW1";
  elements["result-point-select"].listeners.change();
  assert.equal(ui.state.currentResults.point, "SW1@1000");
  elements["result-offset-select"].value = "100000";
  elements["result-offset-select"].listeners.change();
  assert.equal(ui.state.currentResults.point, "SW1@100000");
  assert.equal(elements["result-point-select"].value, "SW1");
  elements["noise-kind"].value = "flicker";
  elements["noise-kind"].listeners.change();
  assert.equal(elements["metric-calculated-dbc"].textContent, "-118.000 dBc/Hz");
  assert.equal(elements["metric-spectre-dbc"].textContent, "-108.000 dBc/Hz");
});

test("legacy reference never masquerades as total noise; contribution rows escape source names", () => {
  const elements = Object.fromEntries(["noise-contribution-table", "noise-contribution-offset", "metric-spectre-dbc", "noise-scope-note", "result-metric-tables"].map(id => [id, node()]));
  const { ui } = harness(elements), raw = fixture();
  raw.summary.forEach(row => delete row.spectre_total_dBc_Hz);
  ui.renderFlickerResults(ui.normalizeResults(raw, "job", "SW0"));
  assert.equal(elements["metric-spectre-dbc"].textContent, "N/A");
  assert.match(elements["noise-contribution-table"].innerHTML, /did not collect VCO total phase noise/);
  assert.match(elements["noise-contribution-table"].innerHTML, /-121.0000/);
  raw.summary[0].spectre_total_dBc_Hz = -99;
  delete raw.summary[0].spectre_noise_reference;
  ui.renderFlickerResults(ui.normalizeResults(raw, "job", "SW0"));
  assert.equal(elements["metric-spectre-dbc"].textContent, "N/A");
  assert.doesNotMatch(elements["result-metric-tables"].innerHTML, />-99</);
  assert.equal(raw.summary[0].spectre_total_dBc_Hz, -99);
  raw.summary[0].spectre_noise_reference = {scope: "whole_vco", schema_version: 1};
  raw.summary[0].carrier_peak_V = 1;
  raw.summary[0].spectre_total_pm_asd_V_rtHz = 2e-6;
  raw.summary[0].spectre_total_dBc_Hz = 10 * Math.log10(8e-12);
  raw.summary[0].spectre_noise_contributions = [{device:'<img onerror="bad">',mechanism:'fn',psd_V2_Hz:1e-12}];
  ui.renderFlickerResults(ui.normalizeResults(raw, "job", "SW0"));
  assert.match(elements["noise-contribution-table"].innerHTML, /25.0000/);
  assert.match(elements["noise-contribution-table"].innerHTML, /&lt;img/);
  assert.doesNotMatch(elements["noise-contribution-table"].innerHTML, /<img/);
  ui.renderFlickerResults({error:"missing"});
  assert.equal(elements["noise-contribution-table"].textContent, "Waiting for actual VCO simulation results");
});

test("contribution table follows top selectors without a duplicate offset selector", () => {
  const elements = Object.fromEntries(["result-job-select", "result-point-select", "result-offset-select", "noise-contribution-table", "noise-contribution-pies"].map(id => [id,node()]));
  const {ui} = harness(elements);
  ui.initResults(); ui.state.currentRawResults = fixture();
  elements["result-point-select"].value = "SW1";
  elements["result-point-select"].listeners.change();
  elements["result-offset-select"].value = "100000";
  elements["result-offset-select"].listeners.change();
  assert.equal(ui.state.currentResults.point, "SW1@100000");
  assert.equal(elements["result-point-select"].value, "SW1");
  assert.equal(elements["result-offset-select"].value, "100000");
  assert.match(elements["noise-contribution-table"].innerHTML, /SW1.*100 kHz/);
  assert.match(elements["noise-contribution-pies"].innerHTML, /calculated.*Simulation/s);
  assert.doesNotMatch(fs.readFileSync(path.join(ROOT, "isf_tool/ui/results.html"), "utf8"), /noise-contribution-offset/);
});

test("advanced groups keep invalid and unknown overrides visible, fold references, retain transfer remeasurement help", () => {
  const { ui } = harness();
  assert.equal(ui.mappingFieldGroup({ key: "x", active: false, status: "ok" }, {}), "reference");
  assert.equal(ui.mappingFieldGroup({ key: "x", active: false, status: "invalid" }, {}), "main");
  const html = ui.groupedMappingHtml([
    { key: "transfer_output", active: true, group: "signal", consumption_mode: "remeasure_complex", used_by: ["HBAC"], help: "mag_pxf0 identifies I4; remeasure the full complex response in the current differential direction; do not use the original single-ended magnitude" },
    { key: "unused", group: "reference", active: false, help: "reference only" }
  ], "vco", { obsolete: "old" }, false);
  assert.match(html, /Consumption: remeasure_complex/);
  assert.match(html, /remeasure the full complex response in the current differential direction/);
  assert.ok(html.indexOf('data-mapping-key="obsolete"') < html.indexOf('<details'));
  assert.ok(html.indexOf('data-mapping-key="unused"') > html.indexOf('<details'));
});

test("safe stop is sweep-only and partial interrupted jobs retain an enabled completed-results link", async () => {
  const elements = { "stop-after-offset": node(), "view-results": node(), "job-status": node() };
  const { ui } = harness(elements); ui.state.apiOnline = true;
  assert.equal(ui.canStopAfterOffset({ status: "running", isOffsetSweep: false }), false);
  let calls = 0;
  ui.setApi(async (url, options) => { calls++; assert.equal(url, "/api/jobs/a%2Fb/stop-after-offset"); assert.equal(options.method, "POST"); return { ok: true }; });
  ui.state.currentJob = ui.jobFromResponse({ id: "a/b", status: "running", is_offset_sweep: true, offset_sweep: { offsets_hz: [1, 2], completed_offsets_hz: [1] }, result_available: true });
  assert.equal(await ui.requestStopAfterOffset(), true);
  assert.equal(ui.state.currentJob.offsetSweep.stop_requested, true);
  assert.equal(elements["stop-after-offset"].disabled, true);
  assert.equal(await ui.requestStopAfterOffset(), false);
  assert.equal(calls, 1);
  ui.renderJob(ui.jobFromResponse({ id: "partial", status: "interrupted", is_offset_sweep: true, result_available: true, resume_available: true }));
  assert.equal(elements["job-status"].textContent, "INTERRUPTED");
  assert.equal(elements["view-results"].attributes["aria-disabled"], "false");
  assert.equal(elements["view-results"].textContent, "View completed offsets");
  assert.equal(elements["stop-after-offset"].hidden, true);
});

test("HTML provides distinct selectors and accessible metric controls without fixed operating-point count", () => {
  const setup = fs.readFileSync(path.join(ROOT, "isf_tool/ui/index.html"), "utf8");
  const result = fs.readFileSync(path.join(ROOT, "isf_tool/ui/results.html"), "utf8");
  assert.doesNotMatch(setup, /<\/option\s+value/);
  for (const name of ["offsetMode", "offsetValues", "offsetStart", "offsetStop", "offsetSpacing", "offsetStep", "offsetIntervals", "offsetPerDecade"]) assert.ok(setup.includes('name="' + name + '"'));
  for (const id of ["result-point-select", "result-offset-select", "noise-kind", "cumulative-k-select", "metric-help-dialog"]) assert.ok(result.includes('id="' + id + '"'));
  assert.doesNotMatch(result, /id="noise-scope"|Single-device error|single-device dynamic/);
  for (const file of fs.readdirSync(path.join(ROOT, "isf_tool/ui")).filter(file => file.endsWith(".html"))) {
    const html = fs.readFileSync(path.join(ROOT, "isf_tool/ui", file), "utf8");
    assert.doesNotMatch(html, /<(?:input|select|button)\b[^>]*(?:name|id)=["'](?:executionMode|allowExecute|execution-mode|allow-execute|noise-scope)["']/);
    assert.doesNotMatch(html, /<option\b[^>]*value=["'](?:dry_run|live|plan|pair)["']/);
    assert.doesNotMatch(html, /<option\b[^>]*>\s*(?:single device|two-device|Dry|Live|Plan)\s*<\/option>/i);
  }
  assert.doesNotMatch(result, /six|6 operating points|six-point|6-point/);
  assert.match(result, /<button[^>]*data-metric-key/);
  for (const html of [setup, result]) { const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]); assert.equal(new Set(ids).size, ids.length); }
});

function thermalFixture() {
  const raw = fixture();
  for (const row of raw.summary) {
    row.carrier_peak_V = 2;
    row.spectre_total_pm_asd_V_rtHz = Math.sqrt(40e-12);
    row.spectre_noise_contributions = [
      { device: "M1.rg", mechanism: "rn", psd_V2_Hz: 4e-12 },
      { device: "L1", mechanism: "rn", psd_V2_Hz: 8e-12 },
      { device: "unmodeled.diode", mechanism: "id", psd_V2_Hz: 2e-12 }
    ];
    row.thermal_noise = {
      schema_version: 1, status: "available", scope: "gm_pair_and_lc_network",
      passive_scope: "passive_only_lc_roots_v1",
      predicted_psd_V2_Hz: 12e-12, phase_noise_dBc_Hz: 10 * Math.log10(6e-12),
      sources: [
        { device: "M1.rg", mechanism: "rn", psd_V2_Hz: 4e-12, phase_noise_dBc_Hz: 10 * Math.log10(2e-12), input_band_contributions: [{ input_band: -1, psd_V2_Hz: 1e-12 }, { input_band: 1, psd_V2_Hz: 3e-12 }] },
        { device: "L1", mechanism: "rn", psd_V2_Hz: 8e-12, phase_noise_dBc_Hz: 10 * Math.log10(4e-12), input_band_contributions: [{ input_band: -1, psd_V2_Hz: 2e-12 }, { input_band: 1, psd_V2_Hz: 6e-12 }] }
      ]
    };
  }
  return raw;
}

test("thermal and flicker combine powers, never dB or amplitudes; absent thermal is not zero", () => {
  const { ui } = harness(), raw = thermalFixture(), row = raw.summary[0];
  assert.equal(ui.predictedNoiseDbc(row, "flicker"), -120);
  assert.equal(ui.predictedNoiseDbc(row, "thermal"), row.thermal_noise.phase_noise_dBc_Hz);
  assert.ok(Math.abs(ui.predictedNoiseDbc(row, "both") - 10 * Math.log10(7e-12)) < 1e-10);
  const original = JSON.stringify(row);
  ui.resultNoiseSeries(raw, "SW0", "both", 1);
  assert.equal(JSON.stringify(row), original, "presentation must not mutate source results");
  for (const invalid of [undefined, {}, { ...row.thermal_noise, status: "unsupported" }, { ...row.thermal_noise, scope: "whole_vco" }, { ...row.thermal_noise, schema_version: 9 }]) {
    row.thermal_noise = invalid;
    assert.equal(ui.predictedNoiseDbc(row, "both"), null);
    assert.equal(ui.predictedNoiseDbc(row, "thermal"), null);
    assert.equal(ui.predictedNoiseDbc(row, "flicker"), -120);
  }
});

test("quality reports thermal failure separately even when flicker passes and thermal data are available", () => {
  const elements = { "sample-quality-note": node(), "noise-kind": node("both") };
  const { ui } = harness(elements), raw = thermalFixture(), row = raw.summary[0];
  row.thermal_noise.numerical_quality = { pass: false, issues: ["thermal convergence warning"] };
  row.numerical_quality_issues = "flicker audit note";
  for (const kind of ["both", "thermal", "flicker"]) {
    elements["noise-kind"].value = kind;
    ui.renderFlickerResults(ui.normalizeResults(raw, "SW0@1000"));
    const text = elements["sample-quality-note"].textContent;
    assert.match(text, /Flicker Numerical quality: Passed.*Thermal Numerical quality: Failed/);
    assert.match(text, /Flicker Issues: flicker audit note.*Thermal Issues: .*thermal convergence warning/);
    assert.doesNotMatch(text, /Thermal Numerical quality: Passed/);
  }
});

test("thermal quality is unknown without explicit boolean evidence; available does not imply pass", () => {
  const elements = { "sample-quality-note": node() };
  const { ui } = harness(elements), raw = thermalFixture(), row = raw.summary[0];
  for (const quality of [undefined, null, false, 0, "", {}, { pass: null }, { pass: "true" }, { pass: 1 }]) {
    row.thermal_noise.numerical_quality = quality;
    ui.renderFlickerResults(ui.normalizeResults(raw, "SW0@1000"));
    assert.match(elements["sample-quality-note"].textContent, /Flicker Numerical quality: Passed.*Thermal Numerical quality: Not provided/);
  }
  row.thermal_noise.numerical_quality = { pass: true, issues: [] };
  row.numerical_quality_pass = false;
  ui.renderFlickerResults(ui.normalizeResults(raw, "SW0@1000"));
  assert.match(elements["sample-quality-note"].textContent, /Flicker Numerical quality: Failed.*Thermal Numerical quality: Passed/);
  assert.doesNotMatch(elements["sample-quality-note"].textContent, /Thermal Issues/);
  row.thermal_noise.status = "unsupported";
  ui.renderFlickerResults(ui.normalizeResults(raw, "SW0@1000"));
  assert.match(elements["sample-quality-note"].textContent, /Thermal Numerical quality: Not provided/);
  delete row.thermal_noise;
  delete row.numerical_quality_pass;
  ui.renderFlickerResults(ui.normalizeResults(raw, "SW0@1000"));
  assert.match(elements["sample-quality-note"].textContent, /Flicker Numerical quality: Not provided.*Thermal Numerical quality: Not provided/);
});

test("actual thermal and both views disclose recorded finite input-band budget without guessing legacy range", () => {
  const elements = {"noise-kind":node("both"),"noise-scope-note":node()}, {ui} = harness(elements), raw = thermalFixture();
  raw.summary[0].thermal_noise.input_max_harmonic = 3;
  ui.renderFlickerResults(ui.normalizeResults(raw,"job","SW0@1000"));
  assert.match(elements["noise-scope-note"].textContent,/independent input bands \|m\|≤3; higher bands are excluded/);
  delete raw.summary[0].thermal_noise.input_max_harmonic;
  elements["noise-kind"].value = "thermal";
  ui.renderFlickerResults(ui.normalizeResults(raw,"job","SW0@1000"));
  assert.match(elements["noise-scope-note"].textContent,/actual input-band limits not recorded/);
  elements["noise-kind"].value = "flicker";
  ui.renderFlickerResults(ui.normalizeResults(raw,"job","SW0@1000"));
  assert.doesNotMatch(elements["noise-scope-note"].textContent,/finite-band budget|actual input-band limits not recorded/);
});

test("thermal input bands sum independent source powers and remain distinct from coherent flicker harmonics", () => {
  const { ui } = harness(), raw = thermalFixture(), row = raw.summary[0];
  assert.ok(Math.abs(ui.thermalBandDbc(row, -1) - 10 * Math.log10(1.5e-12)) < 1e-10);
  assert.ok(Math.abs(ui.thermalBandDbc(row, 1) - 10 * Math.log10(4.5e-12)) < 1e-10);
  const all = ui.resultNoiseSeries(raw, "SW0", "both", 1);
  assert.deepEqual(plain(all.phase.map(series => series.id)), ["flicker", "thermal", "predicted-total", "spectre-total"]);
  assert.equal(all.phase[0].values[0].y, -120);
  assert.equal(all.phase[0].values[1].y, null);
  assert.equal(all.phase[3].values[0].y, -110);
  assert.deepEqual(plain(all.harmonics.map(series => series.id)), ["combined-k-1", "predicted-total"]);
  const expected = 10 * Math.log10(2 * Math.pow(10, -91 / 10) + 6e-12);
  assert.ok(Math.abs(all.harmonics[0].values[0].y - expected) < 1e-10);
  const onlyThermal = ui.resultNoiseSeries(raw, "SW0", "thermal", 1);
  assert.deepEqual(plain(onlyThermal.harmonics.map(series => series.id)), ["thermal-band-1", "thermal"]);
  assert.ok(Math.abs(onlyThermal.harmonics[0].values[0].y - 10 * Math.log10(6e-12)) < 1e-10);
  assert.ok(onlyThermal.harmonics.every(series => series.name.includes("Thermal") || series.name.includes("thermal")));
  assert.equal(ui.resultNoiseSeries(raw, "SW0", "flicker").phase.length, 2);
  delete row.thermal_noise.sources[0].input_band_contributions;
  assert.equal(ui.thermalBandDbc(row, 1), null);
  assert.equal(ui.thermalBandDbc(row, 99), null);
});

test("thermal comparison requires exact source names and mechanisms, not similar names or row order", () => {
  const { ui } = harness(), raw = thermalFixture(), row = raw.summary[0];
  let rows = ui.noiseContributionRows(row);
  assert.ok(Math.abs(rows[2].simulated_dbc - rows[2].calculated_dbc) < 1e-10);
  assert.equal(rows.filter(item => item.kind === "source" && item.calculated_dbc !== null).length, 2);
  assert.equal(rows.find(item => item.device === "unmodeled.diode").calculated_dbc, null);
  assert.ok(Math.abs(rows.find(item => item.device === "M1.rg").fraction_pct - 10) < 1e-10);
  row.spectre_noise_contributions.reverse();
  assert.equal(ui.noiseContributionRows(row)[2].simulated_dbc, rows[2].simulated_dbc);
  row.spectre_noise_contributions.find(item => item.device === "M1.rg").device = "M10.rg";
  rows = ui.noiseContributionRows(row);
  assert.equal(rows[2].simulated_dbc, null);
  assert.equal(rows.find(item => item.device === "M10.rg").calculated_dbc, null);
  assert.equal(rows.find(item => item.device === "M1.rg").simulated_dbc, null);
});

test("actual both harmonic graph combines only explicit matching k powers and retains the coherent total", () => {
  const elements = {"harmonic-noise-chart": node(), "noise-kind": node("both"), "cumulative-k-select": node("1")};
  const {ui} = harness(elements), raw = thermalFixture(), row = raw.summary[0];
  ui.renderFlickerResults(ui.normalizeResults(raw, "fixture", row.point));
  let html = elements["harmonic-noise-chart"].innerHTML;
  assert.match(html, /k=1 · Flicker equivalent \+ thermal \|m\| power/);
  assert.match(html, /ISF flicker \+ thermal · power sum/);
  assert.doesNotMatch(html, /Flicker k=1 single order|Thermal independent input bands/);
  assert.equal(elements["cumulative-k-select"].disabled, true);
  const series = ui.resultNoiseSeries(raw, "SW0", "both");
  assert.equal(series.harmonics[1].values[0].y, ui.predictedNoiseDbc(row, "both"));
  assert.notEqual(series.harmonics[0].values[0].y, series.harmonics[1].values[0].y);
  const harmonic = raw.points[row.point].harmonics[1];
  harmonic.harmonic_equivalent_dBc_Hz = null;
  harmonic.Q_mag_V_rtHz = 0; harmonic.Q_real_V_rtHz = 0;
  assert.ok(Math.abs(ui.resultNoiseSeries(raw, "SW0", "both").harmonics[0].values[0].y - ui.thermalBandDbc(row, -1) - 10 * Math.log10(4)) < 1e-10);
  harmonic.Q_mag_V_rtHz = null; harmonic.Q_real_V_rtHz = null;
  assert.equal(ui.resultNoiseSeries(raw, "SW0", "both").harmonics[0].values[0].y, null);
  harmonic.harmonic_equivalent_dBc_Hz = -91;
  row.thermal_noise.sources.forEach(source => source.input_band_contributions.forEach(part => part.psd_V2_Hz = 0));
  assert.ok(Math.abs(ui.resultNoiseSeries(raw, "SW0", "both").harmonics[0].values[0].y - (-91 + 10 * Math.log10(2))) < 1e-10);
  delete row.thermal_noise.sources[0].input_band_contributions;
  assert.equal(ui.resultNoiseSeries(raw, "SW0", "both").harmonics[0].values[0].y, null);
});

test("actual parallel pies contain unique independent PSD sources, not aggregate or missing-as-zero", () => {
  const elements = {"noise-contribution-pies": node(), "noise-contribution-table": node(), "noise-kind": node("both")};
  const {ui} = harness(elements), raw = thermalFixture(), row = raw.summary[0];
  row.thermal_noise.target_noise_trace = "device.target";
  row.thermal_noise.peer_noise_trace = "device.peer";
  row.thermal_noise.pair_model = "symmetric_independent";
  row.dynamic_pm_asd_V_rtHz = 2e-6;
  row.spectre_noise_contributions.push({device:"aggregate", mechanism:"totalsrc", psd_V2_Hz:99},
    {device:"aggregate2", mechanism:"id", noise_family:"aggregate", psd_V2_Hz:88},
    {device:"device.target", mechanism:"fn", psd_V2_Hz:4e-12},
    {device:"device.peer", mechanism:"fn", psd_V2_Hz:4e-12},
    {device:"missing", mechanism:"rn", psd_V2_Hz:null},
    {device:"zero", mechanism:"rn", psd_V2_Hz:0});
  const before = JSON.stringify(raw);
  ui.renderFlickerResults(ui.normalizeResults(raw, "fixture", row.point));
  const data = ui.noisePieData(row, "both");
  assert.equal(data.calculated.slices.length, 4);
  assert.ok(Math.abs(data.calculated.total - 20e-12) < 1e-25);
  assert.ok(Math.abs(data.simulated.total - 22e-12) < 1e-25);
  let html = elements["noise-contribution-pies"].innerHTML;
  assert.equal((html.match(/<svg/g) || []).length, 2);
  assert.doesNotMatch(html, /<span>aggregate(?:2)? ·/);
  assert.match(html, /missing.*N\/A|N\/A.*missing/s);
  assert.match(html, /zero.*0\.00%/s);
  assert.match(html, /V²\/Hz/);
  assert.equal(JSON.stringify(raw), before);
  elements["noise-kind"].value = "thermal";
  ui.renderFlickerResults(ui.normalizeResults(raw, "fixture", row.point));
  html = elements["noise-contribution-pies"].innerHTML;
  assert.doesNotMatch(html, /unmodeled\.diode|device\.target|device\.peer/);
  row.spectre_noise_contributions.push({...row.spectre_noise_contributions[0]});
  const duplicates = ui.noisePieData(row, "thermal");
  assert.ok(!duplicates.simulated.slices.some(item => item.label === "M1.rg · rn"));
  assert.match(duplicates.simulated.missing.join(" "), /M1\.rg.*duplicate sources/);
  delete row.thermal_noise.peer_noise_trace;
  assert.match(ui.noisePieData(row, "both").calculated.missing.join(" "), /Paired sources/);
  delete row.spectre_noise_reference;
  ui.renderFlickerResults(ui.normalizeResults(raw, "fixture", row.point));
  assert.match(elements["noise-contribution-pies"].innerHTML, /same VCO Independent-source reference not provided in result/);
  assert.ok(ui.noisePieData(row, "thermal").calculated.total > 0);
});

function stateElements() {
  return Object.fromEntries(["workspace-state-name", "workspace-state-save", "workspace-state-load", "workspace-state-refresh", "workspace-state-list", "workspace-state-status", "result-job-select", "result-point-select", "result-offset-select", "noise-kind", "noise-x-scale", "cumulative-k-select", "metric-calculated-dbc", "noise-contribution-pies"].map(id => [id, node()]));
}

test("parallel pie colors use the shared source-key union even when power ranks are reversed", () => {
  const elements = {"noise-contribution-pies":node(),"noise-contribution-table":node(),"noise-kind":node("thermal")};
  const {ui} = harness(elements), raw = thermalFixture(), row = raw.summary[0];
  row.spectre_noise_contributions[0].psd_V2_Hz = 100e-12;
  row.spectre_noise_contributions[1].psd_V2_Hz = 1e-12;
  row.spectre_noise_contributions.push({device:"A.reference-only",mechanism:"rn",psd_V2_Hz:2e-12});
  const before = JSON.stringify(raw);
  ui.renderFlickerResults(ui.normalizeResults(raw,"job",row.point));
  const data = ui.noisePieData(row,"thermal");
  assert.equal(data.calculated.slices[0].label,"L1 · rn");
  assert.equal(data.simulated.slices[0].label,"M1.rg · rn");
  const figures = [...elements["noise-contribution-pies"].innerHTML.matchAll(/<figure[^>]*>(.*?)<\/figure>/gs)].map(match => match[1]);
  assert.equal(figures.length,2);
  const legends = figures.map(figure => Object.fromEntries([...figure.matchAll(/<i style="background:([^\"]+)"[^>]*><\/i><span>(.*?) · [0-9.]+e[+-][0-9]+ V²\/Hz/g)].map(match => [match[2],match[1]])));
  for (const slice of data.calculated.slices) {
    assert.equal(slice.color_index,data.simulated.slices.find(other => other.key === slice.key).color_index);
    assert.ok(legends[0][slice.label]);
    assert.equal(legends[0][slice.label],legends[1][slice.label]);
  }
  assert.notEqual(legends[0]["L1 · rn"],legends[0]["M1.rg · rn"]);
  assert.equal(JSON.stringify(raw),before);
});

test("actual source table retains computed value with one invalid reference but does not guess among duplicate references", () => {
  const elements = {"noise-contribution-table":node(),"noise-kind":node("thermal")};
  const {ui} = harness(elements);
  for (const invalid of [null,undefined,-1,"bad",Infinity]) {
    const raw = thermalFixture(), row = raw.summary[0], reference = row.spectre_noise_contributions[0];
    reference.psd_V2_Hz = invalid;
    ui.renderFlickerResults(ui.normalizeResults(raw,"job",row.point));
    let html = elements["noise-contribution-table"].innerHTML;
    assert.match(html,/M1\.rg · rn \(invalid simulation reference\)<\/td><td>rn<\/td><td>N\/A<\/td><td>-116\.9897<\/td>/);
    assert.match(html,/Thermal Matched-source comparison \(reference sources incomplete\)/);
    row.spectre_noise_contributions.push({...reference});
    ui.renderFlickerResults(ui.normalizeResults(raw,"job",row.point));
    html = elements["noise-contribution-table"].innerHTML;
    assert.doesNotMatch(html,/M1\.rg · rn/);
    assert.match(html,/Thermal Matched-source comparison \(reference sources incomplete\)/);
    assert.equal(row.thermal_noise.sources[0].phase_noise_dBc_Hz,10 * Math.log10(2e-12));
  }
});

function savedStateFixture() {
  return {schema_version:1, name:"portable", config:{testbenches:{vco:{maestro_dir:"/vco", binding_fingerprint:"old-vco", history_fingerprint:"old-history", mapping_overrides:{vgs_output:"arbitrary"}}, replica:{maestro_dir:"/noise", binding_fingerprint:"old-noise"}}, analysis:{offsets:{mode:"list", values:"1k,100k"}}, execution:{allow_execute:true}, extension:[{allowExecute:true, execution_consent:true}]}, view:{job_id:"job-owned", point:"SW1@100000", operating_point_id:"SW1", offset_hz:100000, noise_kind:"thermal", x_scale:"linear", cumulative_k:1}};
}

test("actual save button persists settings and proven result selection without nested execution authority", async () => {
  const elements = stateElements(), {ui} = harness(elements), raw = thermalFixture();
  raw.provenance = {job_id:"job-owned", manifest_sha256:"abc"};
  ui.state.rawConfig = savedStateFixture().config;
  ui.state.apiOnline = true;
  ui.renderResults(ui.normalizeResults(raw, "job-owned", "SW1@100000"));
  elements["noise-kind"].value = "thermal"; elements["noise-x-scale"].value = "linear";
  elements["workspace-state-name"].value = " portable ";
  const calls = [];
  ui.setApi(async (url, options) => {calls.push([url, options]); return url === "/api/states" && options ? {id:"saved.json", path:"/workspace/states/saved.json"} : {states:[{id:"saved.json", name:"portable", saved_at:"now"}]};});
  ui.initWorkspaceStates();
  await elements["workspace-state-save"].listeners.click();
  const payload = plain(calls[0][1].body);
  assert.equal(payload.config.execution.allow_execute, false);
  assert.equal(payload.config.extension[0].allowExecute, false);
  assert.equal(payload.config.extension[0].execution_consent, false);
  assert.equal(payload.config.testbenches.vco.mapping_overrides.vgs_output, "arbitrary");
  assert.equal(payload.view.point, "SW1@100000");
  assert.equal(payload.view.job_id, "job-owned");
  assert.equal(payload.name, "portable");
  assert.deepEqual(calls.map(item => item[0]), ["/api/states", "/api/states"]);
  assert.match(elements["workspace-state-status"].textContent, /Saved.*excludes execution consent/);
  assert.equal(ui.state.rawConfig.execution.allow_execute, true);
  raw.provenance.job_id = "foreign";
  ui.renderResults(ui.normalizeResults(raw, "job-owned", "SW1@100000"));
  assert.deepEqual(plain(ui.workspaceStateView()), {});
});

test("actual load button unconfirms TBs and restores exact same-job controls without config write, preflight or simulation", async () => {
  const elements = stateElements(), {ui, document, storage} = harness(elements), raw = thermalFixture();
  document.body.dataset.page = "results";
  raw.provenance = {job_id:"job-owned", manifest_sha256:"abc"};
  ui.state.apiOnline = true;
  ui.state.currentPreflight = {ok:true};
  elements["workspace-state-list"].value = "saved.json";
  elements["result-offset-select"].value = "1000";
  const calls = [], saved = savedStateFixture();
  ui.setApi(async url => {calls.push(url); return url === "/api/jobs" ? {jobs:[]} : url.startsWith("/api/states/") ? {state:saved, path:"/workspace/states/saved.json"} : raw;});
  ui.initWorkspaceStates();
  await elements["workspace-state-load"].listeners.click();
  assert.deepEqual(calls, ["/api/states/saved.json", "/api/jobs", "/api/results/job-owned"]);
  assert.equal(ui.state.rawConfig.testbenches.vco.binding_fingerprint, "");
  assert.equal(ui.state.rawConfig.testbenches.vco.history_fingerprint, "");
  assert.equal(ui.state.rawConfig.testbenches.replica.binding_fingerprint, "");
  assert.equal(ui.state.rawConfig.testbenches.vco.mapping_overrides.vgs_output, "arbitrary");
  assert.equal(ui.state.rawConfig.execution.allow_execute, false);
  assert.equal(ui.state.currentPreflight, null);
  assert.equal(ui.state.currentResults.point, "SW1@100000");
  assert.equal(elements["result-point-select"].value, "SW1");
  assert.equal(elements["result-offset-select"].value, "100000");
  assert.equal(elements["noise-kind"].value, "thermal");
  assert.equal(elements["noise-x-scale"].value, "linear");
  assert.equal(JSON.parse(storage.get("isf-tool:workspace-state")).loaded, true);
  assert.match(elements["workspace-state-status"].textContent, /verified.*No preflight or simulation started/);
  assert.equal(saved.config.execution.allow_execute, true);
});

test("state result restoration rejects foreign jobs, changed or ambiguous samples instead of choosing a fallback", async () => {
  for (const mutate of [raw => raw.provenance.job_id = "other", raw => raw.summary.splice(3,1), raw => raw.summary[3].offset_Hz = 200000, raw => raw.summary.push({...raw.summary[3]})]) {
    const elements = stateElements(), {ui, document} = harness(elements), raw = thermalFixture();
    document.body.dataset.page = "results"; ui.state.apiOnline = true;
    raw.provenance = {job_id:"job-owned"}; mutate(raw);
    ui.setApi(async url => url === "/api/jobs" ? {jobs:[]} : url.startsWith("/api/states/") ? {state:savedStateFixture()} : raw);
    await ui.loadWorkspaceState("saved.json");
    assert.ok(ui.state.currentResults.error);
    assert.equal(elements["metric-calculated-dbc"].textContent, "N/A");
    assert.match(elements["workspace-state-status"].textContent, /Not replaced with another job or frequency point/);
    assert.equal(ui.state.rawConfig.testbenches.vco.maestro_dir, "/vco");
  }
});

test("state loading preserves newer local edits and page boot suppresses automatic preflight after restoration", async () => {
  const elements = stateElements(), {ui} = harness(elements);
  ui.state.apiOnline = true;
  let done;
  ui.setApi(() => new Promise(resolve => done = resolve));
  const loading = ui.loadWorkspaceState("saved.json");
  ui.state.setupRevision += 1;
  done({state:savedStateFixture()});
  assert.equal(await loading, null);
  assert.equal(ui.state.rawConfig, null);
  const app = fs.readFileSync(APP, "utf8"), css = fs.readFileSync(path.join(ROOT, "isf_tool/ui/styles.css"), "utf8");
  assert.match(app, /if \(!restored\) requestPreflight/);
  assert.match(app, /!state\.setupDirty && !restored\) loadRemoteConfig/);
  assert.match(css, /html\s*\{[^}]*overflow-y:\s*auto/s);
  assert.match(css, /body\s*\{[^}]*height:\s*auto;[^}]*overflow:\s*visible/s);
});

test("state loaded on setup rechecks the same result across pages and retains selection when later saving settings", async () => {
  const elements = stateElements(), {ui, document, storage} = harness(elements), raw = thermalFixture();
  document.body.dataset.page = "setup"; ui.state.apiOnline = true;
  raw.provenance = {job_id:"job-owned"};
  const calls = [];
  ui.setApi(async url => { calls.push(url); return url === "/api/jobs" ? {jobs:[]} : url.startsWith("/api/states/") ? {state:savedStateFixture()} : raw; });
  await ui.loadWorkspaceState("saved.json");
  assert.equal(ui.state.currentResults, null);
  assert.equal(ui.workspaceStateView().point, "SW1@100000");
  document.body.dataset.page = "results";
  ui.initResults();
  await ui.loadRemoteResults(false);
  assert.equal(ui.state.currentResults.point,"SW1@100000");
  assert.equal(elements["noise-kind"].value,"thermal");
  assert.equal(elements["noise-x-scale"].value,"linear");
  assert.deepEqual(calls,["/api/states/saved.json","/api/jobs","/api/results/job-owned","/api/results/job-owned"]);
  assert.equal(JSON.parse(storage.get("isf-tool:result-view")).point,"SW1@100000");
});

test("a late failed result fetch from an older loaded state cannot clear the newer restored state", async () => {
  const elements = stateElements(), {ui, document} = harness(elements), old = savedStateFixture(), next = savedStateFixture(), raw = thermalFixture();
  document.body.dataset.page = "results"; ui.state.apiOnline = true;
  next.view.job_id = "new-job"; next.config.testbenches.vco.maestro_dir = "/new-vco";
  raw.provenance = {job_id:"new-job"};
  let fail, started;
  const waiting = new Promise(resolve => started = resolve);
  ui.setApi(async url => {
    if (url === "/api/jobs") return {jobs:[]};
    if (url === "/api/states/old.json") return {state:old};
    if (url === "/api/states/new.json") return {state:next};
    if (url === "/api/results/job-owned") return new Promise((resolve,reject) => { fail = reject; started(); });
    return raw;
  });
  const older = ui.loadWorkspaceState("old.json");
  await waiting;
  await ui.loadWorkspaceState("new.json");
  assert.equal(typeof fail,"function");
  fail(new Error("old result unavailable"));
  await older;
  assert.equal(ui.state.currentResults.id,"new-job");
  assert.equal(ui.state.rawConfig.testbenches.vco.maestro_dir,"/new-vco");
  assert.match(elements["workspace-state-status"].textContent,/verified.*new-job/);
});

test("state restore refuses local and authoritative active jobs without losing task, polling or configuration", async () => {
  for (const mode of ["local", "pending", "remote", "unknown"]) {
    const elements = stateElements(), {ui} = harness(elements), original = {marker:"keep"};
    ui.state.apiOnline = true; ui.state.rawConfig = original; ui.state.pollTimer = 123;
    const job = {id:"existing",status:mode === "local" ? "running" : "failed"};
    ui.state.currentJob = job; ui.state.executionPending = mode === "pending";
    const calls = [];
    ui.setApi(async url => {calls.push(url); return url === "/api/jobs" ? mode === "unknown" ? {} : {jobs:[{id:"active",status:"queued"}]} : {state:savedStateFixture()};});
    await assert.rejects(ui.loadWorkspaceState("saved.json"),/job is active|run is being submitted/);
    assert.equal(ui.state.rawConfig,original);
    assert.equal(ui.state.currentJob,job);
    assert.equal(ui.state.pollTimer,123);
    assert.deepEqual(calls,["local","pending"].includes(mode) ? [] : ["/api/states/saved.json","/api/jobs"]);
  }
});

test("actual start marks in-flight authority until completion and prevents a concurrent state load", async () => {
  const {ui} = harness(); ui.state.apiOnline = true;
  let finish;
  ui.setApi(async url => {assert.equal(url,"/api/jobs"); return new Promise(resolve => finish = resolve);});
  const starting = ui.startRemoteRun(node());
  assert.equal(ui.state.executionPending,true);
  await assert.rejects(ui.loadWorkspaceState("saved.json"),/run is being submitted/);
  finish({jobs:[{id:"already-running",status:"running"}]});
  await starting;
  assert.equal(ui.state.executionPending,false);
  assert.equal(ui.state.workspaceStateSequence,1);
});

test("state load rechecks local execution and edit identity after authoritative jobs return", async () => {
  for (const mode of ["pending","edit"]) {
    const {ui} = harness(), original = {marker:"keep"}; ui.state.apiOnline = true; ui.state.rawConfig = original;
    let finish, entered;
    const waiting = new Promise(resolve => entered = resolve);
    ui.setApi(async url => url === "/api/jobs" ? new Promise(resolve => {finish=resolve; entered();}) : {state:savedStateFixture()});
    const loading = ui.loadWorkspaceState("saved.json");
    await waiting;
    if (mode === "pending") ui.state.executionPending = true;
    else ui.state.setupRevision += 1;
    finish({jobs:[]});
    if (mode === "pending") await assert.rejects(loading,/job is active/);
    else assert.equal(await loading,null);
    assert.equal(ui.state.rawConfig,original);
  }
});

test("actual result renderer includes combined harmonic total and maps flicker source calculations only by explicit pair evidence", () => {
  const elements = { "harmonic-noise-chart": node(), "noise-contribution-table": node(), "noise-kind": node("both") };
  const { ui } = harness(elements), raw = thermalFixture(), row = raw.summary[0];
  row.thermal_noise.target_noise_trace = "arbitrary.target";
  row.thermal_noise.peer_noise_trace = "arbitrary.peer";
  row.thermal_noise.pair_model = "symmetric_independent";
  row.spectre_noise_contributions.push(...["arbitrary.target", "arbitrary.peer", "M1", "arbitrary.peer.other"].map(device => ({device, mechanism: "fn", psd_V2_Hz: 1e-12})));
  function render() {
    ui.renderFlickerResults(ui.normalizeResults(raw, "fixture", row.point));
    return elements["noise-contribution-table"].innerHTML.match(/<tr class="noise-row-source">.*?<\/tr>/g);
  }
  function sourceRow(rows, device) { return rows.find(html => html.includes('<td>' + device + ' · fn</td>')); }
  let rows = render();
  assert.match(elements["harmonic-noise-chart"].innerHTML, /ISF flicker \+ thermal · power sum/);
  assert.match(sourceRow(rows, "arbitrary.target"), /<td>-123\.0000<\/td>/);
  assert.match(sourceRow(rows, "arbitrary.peer"), /<td>-123\.0000<\/td>/);
  assert.match(sourceRow(rows, "M1"), /<td>N\/A<\/td>/);
  assert.match(sourceRow(rows, "arbitrary.peer.other"), /<td>N\/A<\/td>/);
  delete row.thermal_noise.pair_model;
  rows = render();
  assert.match(sourceRow(rows, "arbitrary.target"), /<td>-123\.0000<\/td>/);
  assert.match(sourceRow(rows, "arbitrary.peer"), /<td>N\/A<\/td>/);
  row.thermal_noise.pair_model = "symmetric_independent";
  for (const field of ["pair_model", "predicted_pair_dBc_Hz"]) {
    const saved = row[field]; delete row[field];
    assert.match(sourceRow(render(), "arbitrary.peer"), /<td>N\/A<\/td>/);
    row[field] = saved;
  }
  delete row.thermal_noise.target_noise_trace;
  assert.match(sourceRow(render(), "arbitrary.peer"), /<td>N\/A<\/td>/);
  delete row.thermal_noise.peer_noise_trace;
  rows = render();
  assert.match(sourceRow(rows, "arbitrary.target"), /<td>N\/A<\/td>/);
  assert.match(sourceRow(rows, "arbitrary.peer"), /<td>N\/A<\/td>/);
});

test("real result renderer exposes combined harmonic total and explicit target/peer flicker values", {skip: !process.env.ISF_REAL_RESULT_PATH}, () => {
  const raw = JSON.parse(fs.readFileSync(process.env.ISF_REAL_RESULT_PATH, "utf8"));
  const elements = { "phase-noise-chart": node(), "harmonic-noise-chart": node(), "noise-contribution-table": node(), "noise-contribution-pies": node(), "result-metric-tables": node(), "noise-kind": node("both") };
  const { ui } = harness(elements);
  for (const row of raw.summary) {
    const thermal = row.thermal_noise;
    assert.equal(thermal.status, "available");
    assert.equal(thermal.pair_model, "symmetric_independent");
    ui.renderFlickerResults(ui.normalizeResults(raw, "real-result-audit", row.point));
    assert.match(elements["phase-noise-chart"].innerHTML, /ISF flicker \+ thermal · power sum/);
    assert.match(elements["harmonic-noise-chart"].innerHTML, /ISF flicker \+ thermal · power sum/);
    assert.match(elements["harmonic-noise-chart"].innerHTML, /Flicker equivalent \+ thermal \|m\| power/);
    assert.equal((elements["noise-contribution-pies"].innerHTML.match(/<svg/g) || []).length, 2);
    const pies = ui.noisePieData(row,"both");
    assert.equal(pies.calculated.missing.length,0);
    assert.equal(pies.simulated.missing.length,0);
    const expectedPsd = thermal.sources.reduce((sum,source) => sum + source.psd_V2_Hz,0) + 2 * row.dynamic_pm_asd_V_rtHz ** 2;
    assert.ok(Math.abs(pies.calculated.total / expectedPsd - 1) < 1e-12);
    assert.ok(pies.calculated.slices.every(slice => !/totalsrc/.test(slice.label)));
    assert.doesNotMatch(elements["result-metric-tables"].innerHTML, /data-metric-key="(?:dynamic_dBc_Hz|spectre_fn_dBc_Hz|dynamic_error_dB|harmonic_equivalent_dBc_Hz|cumulative_dBc_Hz|cumulative_error_dB)"/);
    assert.match(elements["result-metric-tables"].innerHTML, /data-metric-key="pair_harmonic_equivalent_dBc_Hz"/);
    assert.match(elements["result-metric-tables"].innerHTML, /data-metric-key="Q_real_V_rtHz"/);
    const sources = elements["noise-contribution-table"].innerHTML.match(/<tr class="noise-row-source">.*?<\/tr>/g);
    for (const device of [thermal.target_noise_trace, thermal.peer_noise_trace]) {
      assert.ok(device);
      const html = sources.find(item => item.includes('<td>' + device + ' · fn</td>'));
      assert.ok(html, 'missing explicit flicker source ' + device);
      assert.ok(html.includes('<td>' + row.dynamic_dBc_Hz.toFixed(4) + '</td>'));
    }
  }
});

test("actual result renderer hides legacy device-PN comparisons while preserving pair values, physical diagnostics, and raw exports", () => {
  const elements = { "result-metric-tables": node(), "noise-kind": node("both") };
  const { ui } = harness(elements), raw = thermalFixture(), row = raw.summary[0];
  const hidden = ["dc_dBc_Hz", "delay_dBc_Hz", "dynamic_dBc_Hz", "spectre_fn_dBc_Hz",
    "dc_pm_asd_V_rtHz", "delay_pm_asd_V_rtHz", "dynamic_pm_asd_V_rtHz",
    "spectre_fn_pm_asd_V_rtHz", "spectre_pm_asd_V_rtHz", "second_device_predicted_pm_asd_V_rtHz",
    "dc_error_dB", "delay_error_dB", "dynamic_error_dB", "dynamic_error_db", "errorDb",
    "harmonic_equivalent_dBc_Hz", "cumulative_dBc_Hz", "cumulative_error_dB",
    "cumulative_error_db", "cumulativeErrorDb", "noise_scope"];
  hidden.forEach(key => { row[key] = 1; });
  row.mDC_cycle_mean_A_rtHz = 1e-8;
  row.mDyn_rms_A_rtHz = 2e-8;
  row.spectre_am_to_pm_dB = -40;
  raw.points[row.point].harmonics[0].pair_harmonic_equivalent_dBc_Hz = -87;
  const original = JSON.stringify(raw);
  ui.renderFlickerResults(ui.normalizeResults(raw, "fixture", row.point));
  const html = elements["result-metric-tables"].innerHTML;
  hidden.forEach(key => assert.ok(!html.includes('data-metric-key="' + key + '"'), key + ' should not be displayed'));
  for (const key of ["predicted_pair_dBc_Hz", "pair_harmonic_equivalent_dBc_Hz", "pair_cumulative_dBc_Hz", "pair_cumulative_error_dB", "mDC_cycle_mean_A_rtHz", "mDyn_rms_A_rtHz", "A_k", "Q_real_V_rtHz", "cum_real_V_rtHz", "spectre_am_to_pm_dB", "numerical_quality_pass"]) assert.ok(html.includes('data-metric-key="' + key + '"'), key + ' must remain visible');
  assert.equal(JSON.stringify(raw), original, 'presentation filtering must not change source/export data');
});

test("full result renderer presents ten harmonic columns with pair noise values and matching pair help keys", () => {
  const elements = { "error-table-body": node(), "noise-kind": node("both") };
  const { ui } = harness(elements), raw = thermalFixture();
  const result = ui.normalizeResults(raw, "fixture", raw.summary[0].point);
  ui.renderResults(result);
  const rows = elements["error-table-body"].innerHTML.match(/<tr\b.*?<\/tr>/g);
  assert.equal(rows.length, 2);
  rows.forEach(html => assert.equal((html.match(/<td\b/g) || []).length, 10));
  assert.ok(rows[1].endsWith('<td class="numeric">-120.000</td></tr>'));
  assert.doesNotMatch(elements["error-table-body"].innerHTML, /delta-good|delta-bad/);
  const page = fs.readFileSync(path.join(ROOT, "isf_tool/ui/results.html"), "utf8");
  assert.match(page, /data-metric-key="pair_harmonic_equivalent_dBc_Hz"/);
  assert.match(page, /data-metric-key="pair_cumulative_dBc_Hz"/);
  assert.doesNotMatch(page, /data-metric-key="(?:harmonic_equivalent_dBc_Hz|cumulative_dBc_Hz|cumulative_error_dB)"/);
  assert.match(page, /raw representative-side diagnostics, not claimed as a two-device amplitude/);
  assert.doesNotMatch(page, /id="closure-status"|absolute closure|cross-point trend/);
});

test("actual contribution rendering refuses partial, duplicate, or invalid same-source references without dropping predicted total", () => {
  for (const flaw of ["missing", "duplicate", "negative", "invalid", "duplicate_prediction"]) {
    const elements = { "noise-contribution-table": node(), "metric-thermal-error": node(), "noise-kind": node("both") };
    const { ui } = harness(elements), raw = thermalFixture(), row = raw.summary[0];
    if (flaw === "missing") row.spectre_noise_contributions.shift();
    if (flaw === "duplicate") row.spectre_noise_contributions.push({...row.spectre_noise_contributions[0]});
    if (flaw === "negative") row.spectre_noise_contributions[0].psd_V2_Hz = -1;
    if (flaw === "invalid") row.spectre_noise_contributions[0].psd_V2_Hz = null;
    if (flaw === "duplicate_prediction") row.thermal_noise.sources.push({...row.thermal_noise.sources[0]});
    ui.renderFlickerResults(ui.normalizeResults(raw, "fixture", row.point));
    const html = elements["noise-contribution-table"].innerHTML;
    const aggregate = html.match(/<tr class="noise-row-aggregate"><td>Thermal Matched-source comparison.*?<\/tr>/)[0];
    assert.match(aggregate, /reference sources incomplete/);
    assert.ok(aggregate.includes('<td>N/A</td><td>' + row.thermal_noise.phase_noise_dBc_Hz.toFixed(4) + '</td><td>N/A</td>'), flaw);
    assert.equal(elements["metric-thermal-error"].textContent, "N/A");
    if (flaw.startsWith("duplicate")) {
      const rows = html.match(/<tr class="noise-row-source">.*?<\/tr>/g).filter(item => item.includes('<td>M1.rg · rn</td>'));
      rows.forEach(item => assert.match(item, /<td>N\/A<\/td>/));
    }
  }
});

test("duplicate flicker reference identity has no per-source computed value in actual rendered table", () => {
  const elements = { "noise-contribution-table": node(), "noise-kind": node("both") };
  const { ui } = harness(elements), raw = thermalFixture(), row = raw.summary[0];
  row.thermal_noise.target_noise_trace = "known.target";
  row.thermal_noise.peer_noise_trace = "known.peer";
  row.thermal_noise.pair_model = "symmetric_independent";
  for (const device of ["known.target", "known.peer"]) row.spectre_noise_contributions.push(...[1, 2].map(() => ({device, mechanism: "fn", psd_V2_Hz: 1e-12})));
  ui.renderFlickerResults(ui.normalizeResults(raw, "fixture", row.point));
  const rows = elements["noise-contribution-table"].innerHTML.match(/<tr class="noise-row-source">.*?<\/tr>/g).filter(item => item.includes('<td>fn</td>'));
  assert.equal(rows.length, 4);
  rows.forEach(item => assert.match(item, /<td>N\/A<\/td>/));
});

test("whitespace target or peer names never authorize flicker source calculations in actual rendering", () => {
  for (const [target, peer] of [[" \t ", "known.peer"], ["known.target", " \t "]]) {
    const elements = { "noise-contribution-table": node(), "noise-kind": node("both") };
    const { ui } = harness(elements), raw = thermalFixture(), row = raw.summary[0];
    row.thermal_noise.target_noise_trace = target;
    row.thermal_noise.peer_noise_trace = peer;
    row.thermal_noise.pair_model = "symmetric_independent";
    row.spectre_noise_contributions.push(...[target, peer].map(device => ({device, mechanism: "fn", psd_V2_Hz: 1e-12})));
    ui.renderFlickerResults(ui.normalizeResults(raw, "fixture", row.point));
    const rows = elements["noise-contribution-table"].innerHTML.match(/<tr class="noise-row-source">.*?<\/tr>/g).filter(item => item.includes('<td>fn</td>'));
    const targetRow = rows.find(item => item.includes('<td>' + target + ' · fn</td>'));
    const peerRow = rows.find(item => item.includes('<td>' + peer + ' · fn</td>'));
    assert.match(peerRow, /<td>N\/A<\/td>/);
    if (target.trim()) assert.match(targetRow, /<td>-123\.0000<\/td>/);
    else assert.match(targetRow, /<td>N\/A<\/td>/);
  }
});

test("thermal-only sources require typed, resistive, or exact predicted evidence; shot and unknown id are not thermal", () => {
  const { ui } = harness(), row = thermalFixture().summary[0];
  row.spectre_noise_contributions.push(
    { device: "channel", mechanism: "id", noise_family: "channel_thermal", psd_V2_Hz: 3e-12 },
    { device: "gate", mechanism: "rg", noise_family: "resistive_thermal", psd_V2_Hz: 1e-12 },
    { device: "diode", mechanism: "id", noise_family: "shot", psd_V2_Hz: 5e-12 },
    { device: "M1.looks_like_mos", mechanism: "id", noise_family: "unclassified", psd_V2_Hz: 7e-12 },
    { device: "untyped_mos", mechanism: "id", psd_V2_Hz: 6e-12 },
    { device: "flicker", mechanism: "fn", noise_family: "flicker", psd_V2_Hz: 9e-12 },
    { device: "predicted_mos", mechanism: "id", psd_V2_Hz: 2e-12 },
    { device: "predicted_mos", mechanism: "other", psd_V2_Hz: 2e-12 }
  );
  row.thermal_noise.sources.push(
    { device: "predicted_mos", mechanism: "id", phase_noise_dBc_Hz: -120 },
    { device: "prediction_without_reference", mechanism: "id", phase_noise_dBc_Hz: -125 }
  );
  const original = JSON.stringify(row);
  const details = kind => ui.noiseContributionRows(row, kind).filter(item => item.kind === "source");
  assert.deepEqual(plain(details("thermal").map(item => item.device).sort()), ["L1", "M1.rg", "channel", "gate", "predicted_mos", "prediction_without_reference"]);
  assert.deepEqual(plain(details("flicker").map(item => item.device)), ["flicker"]);
  assert.equal(details("both").length, row.spectre_noise_contributions.length + 1);
  assert.ok(details("thermal").find(item => item.device === "prediction_without_reference").simulated_dbc === null);
  assert.ok(details("both").find(item => item.device === "diode").calculated_dbc === null);
  assert.equal(JSON.stringify(row), original);
  delete row.thermal_noise;
  assert.deepEqual(plain(details("thermal").map(item => item.device).sort()), ["L1", "M1.rg", "channel", "gate"]);
});

test("noise-type selection refreshes source details on the first change and preserves whole-VCO reference", () => {
  const elements = Object.fromEntries(["noise-contribution-table", "noise-contribution-offset", "metric-thermal-error"].map(id => [id, node()]));
  elements["noise-kind"] = node("thermal");
  const { ui } = harness(elements), raw = thermalFixture();
  raw.summary[0].spectre_noise_contributions.push({ device: "flicker_device", mechanism: "fn", psd_V2_Hz: 1e-12 });
  const result = ui.normalizeResults(raw, "SW0@1000");
  ui.renderFlickerResults(result);
  let html = elements["noise-contribution-table"].innerHTML;
  assert.match(html, /L1 · rn/);
  assert.doesNotMatch(html, /unmodeled\.diode|flicker_device|independent device pair flicker comparison/);
  assert.match(html, /VCO simulated total/);
  const error = elements["metric-thermal-error"].textContent;
  elements["noise-kind"].value = "flicker";
  ui.renderFlickerResults(result);
  html = elements["noise-contribution-table"].innerHTML;
  assert.match(html, /flicker_device · fn/);
  assert.doesNotMatch(html, /L1 · rn|unmodeled\.diode|Thermal Matched-source comparison/);
  assert.equal(elements["metric-thermal-error"].textContent, error);
  elements["noise-kind"].value = "both";
  ui.renderFlickerResults(result);
  assert.match(elements["noise-contribution-table"].innerHTML, /unmodeled\.diode · id/);
});

test("empty thermal filter reports missing classified details rather than claiming the result has no sources", () => {
  const elements = { "noise-contribution-table": node(), "noise-kind": node("thermal") };
  const { ui } = harness(elements), raw = thermalFixture();
  delete raw.summary[0].thermal_noise;
  raw.summary[0].spectre_noise_contributions = [{ device: "diode", mechanism: "id", noise_family: "shot", psd_V2_Hz: 2e-12 }];
  ui.renderFlickerResults(ui.normalizeResults(raw, "SW0@1000"));
  assert.match(elements["noise-contribution-table"].innerHTML, /No device\/mechanism details available for this filter/);
  assert.doesNotMatch(elements["noise-contribution-table"].innerHTML, /diode · id/);
  assert.match(elements["noise-contribution-table"].innerHTML, /N\/A/);
});

test("whole-VCO provenance marker still gates main references and thermal same-source reference tables", () => {
  const { ui } = harness(), raw = thermalFixture(), row = raw.summary[0];
  delete row.spectre_noise_reference;
  assert.equal(ui.resultNoiseSeries(raw, "SW0", "both").phase.at(-1).values[0].y, null);
  const rows = ui.noiseContributionRows(row);
  assert.equal(rows[0].simulated_dbc, null);
  assert.equal(rows[2].simulated_dbc, null);
  assert.ok(rows.filter(item => item.kind === "source").every(item => item.simulated_dbc === null));
});

test("explicit actions create isolated live authority, passive page checks neither execute nor persist it", async () => {
  const { ui } = harness();
  const config = ui.flatConfig({ execution: { dry_run: true, allow_execute: true } });
  ui.state.apiOnline = true;
  let requests = [];
  ui.setApi(async (path, options) => { requests.push([path, plain(options.body)]); return { ok: true, checks: [] }; });
  assert.equal(await ui.requestPreflight(config, false), null);
  assert.equal(requests.length, 0);
  const request = ui.executionRequestConfig(config);
  assert.notEqual(request, config);
  assert.equal(config.allowExecute, false);
  assert.equal(request.executionMode, "live");
  assert.equal(request.allowExecute, true);
  await ui.requestPreflight(request, false);
  assert.equal(requests[0][1].config.execution.allow_execute, true);
  assert.equal(requests[0][1].config.execution.dry_run, false);
  assert.equal(ui.configPayload(config).execution.allow_execute, false);
  assert.equal(ui.flatConfig(request).allowExecute, false, "restoring a request must strip consent");
});

test("SVG markers preserve exact data, distinguish line styles, and expose accessible highlight controls", () => {
  const { ui } = harness(), raw = thermalFixture();
  const html = ui.frequencyChartMarkup(ui.resultNoiseSeries(raw, "SW0", "both").phase, true);
  assert.match(html, /data-hz="1000" data-dbc="-120"/);
  assert.match(html, /frequency-legend-button[^>]*aria-pressed="false"/);
  assert.match(html, /class="frequency-hit-line"/);
  assert.match(html, /stroke-dasharray="12 3 2 3"/);
  assert.match(html, /Flicker|flicker/);
  assert.match(html, /thermal/);
  assert.doesNotMatch(html, /Amplifier Noise Floor|ω1/);
});

test("frequency pointer interaction highlights selected series and only reports existing samples", () => {
  const tooltip = node(), classes = new Set();
  tooltip.classList = { add: key => classes.add(key), remove: key => classes.delete(key) };
  function group(index, tagName) {
    const item = node(); item.dataset.seriesIndex = String(index); item.tagName = tagName;
    item.classes = new Set(); item.classList.toggle = (name, yes) => yes ? item.classes.add(name) : item.classes.delete(name);
    return item;
  }
  const curve0 = group(0, "g"), curve1 = group(1, "g"), legend0 = group(0, "button");
  const marker = { closest: () => curve0, getBoundingClientRect: () => ({ left: 100, top: 80, width: 8, height: 8 }), getAttribute: () => "measured · 1234 Hz · -111.1234 dBc/Hz" };
  const svg = { querySelectorAll: () => [marker] };
  const panel = node(); panel.querySelector = () => svg; panel.querySelectorAll = () => [curve0, curve1, legend0];
  const { ui } = harness({ "chart-tooltip": tooltip });
  ui.attachFrequencyInteraction(panel);
  panel.onclick({ target: { closest: () => legend0 } });
  assert.ok(curve0.classes.has("is-highlighted")); assert.ok(curve1.classes.has("is-muted"));
  assert.equal(legend0.attributes["aria-pressed"], "true");
  panel.onpointermove({ clientX: 104, clientY: 84 });
  assert.equal(tooltip.textContent, "measured · 1234 Hz · -111.1234 dBc/Hz");
  assert.ok(classes.has("is-visible"));
  panel.onpointermove({ clientX: 300, clientY: 200 });
  assert.ok(!classes.has("is-visible"));
  panel.onclick({ target: { closest: () => legend0 } });
  assert.ok(!curve1.classes.has("is-muted"));
  ui.attachFrequencyInteraction(panel);
  assert.ok(!classes.has("is-visible"), "redrawing must not retain stale tooltips");
});
