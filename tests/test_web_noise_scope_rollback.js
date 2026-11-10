"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const appPath = path.join(root, "isf_tool/ui/app.js");
const plain = value => JSON.parse(JSON.stringify(value));

function harness(elements = {}) {
  const storage = new Map();
  const context = vm.createContext({
    document: {readyState: "loading", body: {dataset: {}}, addEventListener() {},
      getElementById(id) { return elements[id] || null; }, querySelector() { return null; }, querySelectorAll() { return []; }},
    window: {location: {search: ""}, addEventListener() {}, setTimeout, clearTimeout,
      localStorage: {getItem(key) { return storage.get(key) || null; }, setItem(key, value) { storage.set(key, value); }}},
    console, URL, URLSearchParams, AbortController, Blob, navigator: {}, setTimeout, clearTimeout
  });
  const source = fs.readFileSync(appPath, "utf8"), end = source.lastIndexOf("})();");
  const expose = "globalThis.ui = {state,flatConfig,configPayload,maestroPairPayload,workspaceStateConfig,thermalResult,predictedNoiseDbc,noiseContributionRows,noiseSummaryForDisplay,renderMetricTables,resultNoiseSeries,noisePieData};\n";
  vm.runInContext(source.slice(0, end) + expose + source.slice(end), context, {filename: appPath});
  return context.ui;
}

test("retired object controls are absent while state, pies, and scrolling remain", () => {
  const html = fs.readFileSync(path.join(root, "isf_tool/ui/index.html"), "utf8");
  const results = fs.readFileSync(path.join(root, "isf_tool/ui/results.html"), "utf8");
  const css = fs.readFileSync(path.join(root, "isf_tool/ui/styles.css"), "utf8");
  const script = fs.readFileSync(appPath, "utf8");
  assert.doesNotMatch(html, /additional-noise-objects|add-noise-object|附加噪声对象/);
  assert.doesNotMatch(script, /function (initNoiseObjects|renderNoiseObjects|seedNoiseObject)\(/);
  assert.doesNotMatch(css, /\.noise-object-card/);
  assert.match(html, /workspace-state-save/);
  assert.match(results, /noise-contribution-pies/);
  assert.match(css, /overflow-y: auto/);
});

test("old config and saved-state aliases cannot enable removed arrays", () => {
  const ui = harness();
  const raw = {testbenches: {vco: {mapping_overrides: {target_instance: "MAIN"}}, replica: {}},
    additional_noise_objects: [{id: "bank", enabled: true}], additionalNoiseObjects: [{enabled: true}],
    auto: {additional_noise_object_config: [{enabled: true}], timeout_s: 140},
    additional_noise_objects_report: {ok: true}, execution: {allow_execute: true}};
  const before = plain(raw);
  ui.state.rawConfig = raw;
  const flat = ui.flatConfig(raw), merged = plain(ui.configPayload(flat));
  assert.equal(flat.additionalNoiseObjects, undefined);
  assert.equal(merged.additional_noise_objects, undefined);
  assert.equal(merged.additionalNoiseObjects, undefined);
  assert.equal(merged.additional_noise_objects_report, undefined);
  assert.equal(merged.auto.additional_noise_object_config, undefined);
  assert.equal(merged.testbenches.vco.mapping_overrides.target_instance, "MAIN");
  assert.equal(plain(ui.maestroPairPayload(flat)).additional_noise_objects, undefined);
  const state = plain(ui.workspaceStateConfig(raw, true));
  assert.equal(state.additional_noise_objects, undefined);
  assert.equal(state.additionalNoiseObjects, undefined);
  assert.equal(state.auto.additional_noise_object_config, undefined);
  assert.equal(state.execution.allow_execute, false);
  assert.deepEqual(raw, before);
});

test("old passive/array scopes cannot masquerade as the narrowed gm+LC calculation", () => {
  const ui = harness();
  for (const scope of ["gm_pair_and_passive_network", "gm_pair_passives_and_selected_objects"]) {
    const summary = {predicted_pair_dBc_Hz: -130, thermal_noise: {schema_version: 1,
      status: "available", scope, phase_noise_dBc_Hz: -120}, spectre_total_dBc_Hz: -115,
      spectre_noise_reference: {schema_version: 1, scope: "whole_vco"}};
    assert.equal(ui.thermalResult(summary), null);
    assert.equal(ui.predictedNoiseDbc(summary, "thermal"), null);
    assert.equal(ui.predictedNoiseDbc(summary, "both"), null);
    assert.equal(ui.predictedNoiseDbc(summary, "flicker"), -130);
    assert.equal(summary.spectre_total_dBc_Hz, -115);
  }
  const current = {thermal_noise: {schema_version: 1, status: "available", scope: "gm_pair_and_lc_network", phase_noise_dBc_Hz: -120}};
  assert.equal(ui.predictedNoiseDbc(current, "thermal"), null);
  current.thermal_noise.passive_scope = "passive_only_lc_roots_v1";
  assert.equal(ui.predictedNoiseDbc(current, "thermal"), -120);
  current.thermal_noise.sources = [{device: "legacy.array", object_id: "bank"}];
  assert.equal(ui.predictedNoiseDbc(current, "thermal"), null);
});

test("actual summary table clears stale thermal scalar and JSON values without changing original evidence", () => {
  const panel = {innerHTML: ""}, ui = harness({"result-metric-tables": panel});
  for (const scope of ["gm_pair_and_passive_network", "gm_pair_passives_and_selected_objects"]) {
    const thermal = {schema_version: 1, status: "available", scope, phase_noise_dBc_Hz: -123.456789,
      sources: [{device: "retired.array", mechanism: "rn", psd_V2_Hz: 1e-12, object_id: "bank"}]};
    const raw = {point: "tt@1M", operating_point_id: "tt", offset_Hz: 1e6, carrier_peak_V: 1,
      predicted_pair_dBc_Hz: -130, thermal_noise: thermal, thermal_noise_json: JSON.stringify(thermal),
      thermal_dBc_Hz: -123.456789, predicted_total_dBc_Hz: -119.876543, spectre_total_dBc_Hz: -110,
      spectre_noise_reference: {schema_version: 1, scope: "whole_vco"},
      spectre_noise_contributions: [{device: "retired.array", mechanism: "rn", psd_V2_Hz: 1e-12}]};
    const before = plain(raw), shown = ui.noiseSummaryForDisplay(raw);
    assert.equal(shown.thermal_dBc_Hz, null);
    assert.equal(shown.predicted_total_dBc_Hz, null);
    assert.equal(shown.thermal_noise.status, "unavailable");
    assert.equal(shown.predicted_pair_dBc_Hz, -130);
    assert.equal(shown.spectre_total_dBc_Hz, -110);
    assert.deepEqual(plain(shown.spectre_noise_contributions), raw.spectre_noise_contributions);
    ui.renderMetricTables(raw, [{k: 0, pair_harmonic_equivalent_dBc_Hz: -135}]);
    assert.doesNotMatch(panel.innerHTML, /-123\.456789|-119\.876543/);
    assert.match(panel.innerHTML, /thermal_dBc_Hz[^]*?<td>N\/A<\/td>/);
    assert.match(panel.innerHTML, /predicted_total_dBc_Hz[^]*?<td>N\/A<\/td>/);
    assert.match(panel.innerHTML, /-130/);
    assert.match(panel.innerHTML, /-110/);
    assert.match(panel.innerHTML, /-135/);
    const result = {summary: [raw], points: {"tt@1M": {harmonics: [{k: 0, pair_harmonic_equivalent_dBc_Hz: -135}]}}};
    const curves = ui.resultNoiseSeries(result, "tt", "both", null);
    assert.equal(curves.phase.find(item => item.id === "thermal").values[0].y, null);
    assert.equal(curves.phase.find(item => item.id === "predicted-total").values[0].y, null);
    assert.equal(curves.phase.find(item => item.id === "spectre-total").values[0].y, -110);
    assert.equal(curves.harmonics.some(item => item.id.startsWith("combined-k-")), false);
    assert.equal(ui.noisePieData(raw, "thermal").calculated.slices.length, 0);
    assert.deepEqual(raw, before);
  }
});

test("current gm+LC table recomputes cached scalar totals from the same accepted budget as cards", () => {
  const ui = harness(), raw = {predicted_pair_dBc_Hz: -130, thermal_dBc_Hz: 999, predicted_total_dBc_Hz: 999,
    thermal_noise: {schema_version: 1, status: "available", scope: "gm_pair_and_lc_network",
      passive_scope: "passive_only_lc_roots_v1", phase_noise_dBc_Hz: -120}};
  const shown = ui.noiseSummaryForDisplay(raw);
  assert.equal(shown.thermal_dBc_Hz, -120);
  assert.equal(shown.predicted_total_dBc_Hz, ui.predictedNoiseDbc(raw, "both"));
  assert.equal(raw.thermal_dBc_Hz, 999);
});
