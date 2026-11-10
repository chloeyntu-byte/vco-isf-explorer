"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");
const APP_PATH = path.join(ROOT, "isf_tool", "ui", "app.js");

function uiHarness(elements = {}) {
  const source = fs.readFileSync(APP_PATH, "utf8");
  const end = source.lastIndexOf("})();");
  const storage = new Map();
  const document = {
    readyState: "loading", body: { dataset: {} }, addEventListener() {},
    getElementById(id) { return elements[id] || null; },
    querySelector() { return null; }, querySelectorAll(selector) { return selector === "[data-mapping-key]" ? Object.values(elements).filter(node => node.dataset && node.dataset.mappingKey) : []; }
  };
  const window = {
    location: { search: "" }, addEventListener() {}, setTimeout, clearTimeout,
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, value); }
    }
  };
  const context = vm.createContext({ document, window, console, URL, URLSearchParams,
    AbortController, Blob, navigator: {}, setTimeout, clearTimeout,
    FormData: class FormData { constructor(form) { this.values = form.values || {}; } get(key) { return this.values[key]; } }
  });
  const expose = "globalThis.__ui = { state, flatConfig, configPayload, collectConfig, maestroPairPayload, cleanMappingOverrides, mappingCandidateNames, mappingSelectionLines, mappingSelectionHtml, mappingTableHtml, signalCatalogHtml, signalCatalogSummary, executionProblemsHtml, openSettingsTarget, renderPreflight, crossTbRows, updateMappingOverride, clearPortableBinding, invalidatePreflight, inspectMaestroPair, requestPreflight, renderPortableMappings, renderWaveformExchange, roleDetailHtml, setApi(fn) { apiRequest = fn; } };\n";
  vm.runInContext(source.slice(0, end) + expose + source.slice(end), context, { filename: APP_PATH });
  return { ui: context.__ui, storage };
}

function plain(value) { return JSON.parse(JSON.stringify(value)); }

test("binding detail distinguishes current design from each actual history view, timestamp and hash without blocking replay", () => {
  const {ui} = uiHarness();
  const report = {selected_test:{name:"test", design:{lib:"L", cell:"VCO", view:"new_schematic"}, analyses:[]}, history_manifest:{source_netlists:[
    {point_id:0, corner:"tt", source_design:{library:"L",cell:"VCO",view:"old_schematic"}, source_mtime:0, sha256:"0123456789abcdef"},
    {point_id:1, corner:"ss", source_design:{library:"L",cell:"VCO",view:"new_schematic"}, source_mtime:1, sha256:"abcdef0123456789"}
  ]}};
  const history = {serverHistory:{name:"history",path:"/history",complete_runs:2,checked_runs:2}, bindingReady:true};
  const before = JSON.stringify(report);
  let html = ui.roleDetailHtml(report,"vco",history);
  assert.match(html,/当前 test 设计 · L\/VCO\/new_schematic/);
  assert.match(html,/实际 history 网表 · 2 条 · 与当前设计存在差异/);
  assert.match(html,/点 0 \/ corner tt · L\/VCO\/old_schematic/);
  assert.match(html,/1970-01-01T00:00:00.000Z.*0123456789ab/);
  assert.match(html,/点 1 \/ corner ss · L\/VCO\/new_schematic/);
  assert.match(html,/不禁止合法历史回放/);
  assert.equal(JSON.stringify(report),before);
  history.bindingReady = false;
  html = ui.roleDetailHtml(report,"vco",history);
  assert.match(html,/不能由当前设计推定/);
  assert.doesNotMatch(html,/old_schematic/);
  history.serverHistory.history_manifest = {source_netlists:[{point_id:2,source_design:{view:null},source_mtime:1e100}]};
  html = ui.roleDetailHtml(report,"vco",history);
  assert.match(html,/部分来源未知/);
  assert.match(html,/网表时间 · 未知 · SHA256 未知/);
  assert.match(fs.readFileSync(path.join(__dirname,"../isf_tool/ui/index.html"),"utf8"),/VCO 私有运行副本.*不会修改原 TB/s);
});

function transferFamilyField() {
  const members = [["harm_mag_m1", -1], ["harm_mag_0", 0], ["harm_mag_1", 1]].map(([name, sideband]) => ({
    name, sideband, source: "I4", analysis: "hbxf", expression: 'harmonic(abs(getData("/I4" ?result "hbxf")) ' + sideband + ')'
  }));
  const family = { kind: "transfer_family", family_name: "harm_mag", source: "I4", analysis: "hbxf",
    anchor_output: "harm_mag_0", sidebands: [-1, 0, 1], members, errors: [] };
  return { key: "transfer_output", value: "harm_mag", detected: "harm_mag", status: "auto",
    candidates: ["harm_mag", "harm_mag_m1", "harm_mag_0", "harm_mag_1", "unrelated_output"],
    candidate_details: { harm_mag: family, harm_mag_0: Object.assign({}, family, { kind: "transfer_output", members: [members[1]], sidebands: [0] }) },
    selected_details: family };
}

test("transfer selector compacts backend members and retains exact old selections without mutating the catalog", () => {
  const { ui } = uiHarness();
  const field = transferFamilyField();
  const before = plain(field);
  assert.deepEqual(plain(ui.mappingCandidateNames(field)), ["harm_mag", "unrelated_output"]);
  assert.deepEqual(plain(ui.mappingCandidateNames(field, "harm_mag_0")), ["harm_mag", "harm_mag_0", "unrelated_output"]);
  assert.deepEqual(plain(ui.mappingCandidateNames(field, null, true)), field.candidates);
  assert.deepEqual(field, before);
  const html = ui.mappingTableHtml([field], "vco", {}, false);
  assert.match(html, /option value="harm_mag"/);
  assert.doesNotMatch(html, /option value="harm_mag_0"/);
  assert.match(ui.mappingTableHtml([field], "vco", { transfer_output: "harm_mag_0" }, false), /option value="harm_mag_0"/);
});

test("series are not guessed from spelling and unresolved members remain individually available", () => {
  const { ui } = uiHarness();
  const field = transferFamilyField();
  field.candidate_details.harm_mag.errors = ["成员表达式不可读"];
  assert.deepEqual(plain(ui.mappingCandidateNames(field)), field.candidates);
  delete field.candidate_details;
  assert.deepEqual(plain(ui.mappingCandidateNames(field)), field.candidates);
});

test("selected series details expose members and expression sidebands but do not claim circuit verification", () => {
  const { ui } = uiHarness();
  const field = transferFamilyField();
  const text = ui.mappingSelectionLines(field).join("\n");
  for (const value of ["系列 harm_mag", "3 个已发现成员", "原始电流源：I4", "分析：hbxf", "读取代表项：harm_mag_0", "harm_mag_m1 → 边带 -1", "边带 = k − 1", "电路对应关系由用户确认"]) assert.ok(text.includes(value));
  assert.doesNotMatch(text, /支路核验通过/);
  const exact = ui.mappingSelectionLines(field, "harm_mag_0").join("\n");
  assert.match(exact, /精确输出：harm_mag_0/);
  assert.match(exact, /尚待应用验证/);
  const unknown = ui.mappingSelectionLines(field, "other_name").join("\n");
  assert.match(unknown, /新输入/);
  assert.doesNotMatch(unknown, /I4|harm_mag/);
});

test("series member details escape markup and preserve missing-member/read diagnostics", () => {
  const { ui } = uiHarness();
  const field = transferFamilyField();
  field.candidate_details.harm_mag.members[0].expression = '<script>bad()</script>';
  field.candidate_details.harm_mag.errors = ["成员读取失败"];
  field.candidate_details.harm_mag.missing_sidebands = [2, 3];
  const html = ui.mappingSelectionHtml(field);
  assert.match(html, /读取提示：成员读取失败/);
  assert.match(html, /缺少边带：2, 3/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});

test("nonzero series representatives and informational notes do not imply physical rejection", () => {
  const { ui } = uiHarness();
  const field = transferFamilyField();
  field.candidate_details.harm_mag.anchor_output = "harm_mag_1";
  field.candidate_details.harm_mag.notes = ["没有边带0；按用户选择读取代表项"];
  assert.deepEqual(plain(ui.mappingCandidateNames(field)), ["harm_mag", "unrelated_output"]);
  const text = ui.mappingSelectionLines(field).join("\n");
  assert.match(text, /读取代表项：harm_mag_1/);
  assert.match(text, /系列提示（不阻止运行）/);
  assert.doesNotMatch(text, /边带 0 锚点/);
});

test("family and exact overrides retain their spelling in saved and run configurations", () => {
  const { ui } = uiHarness();
  ui.state.rawConfig = { testbenches: { vco: { mapping_overrides: { transfer_output: "harm_mag", transfer_phase_output: "ph_pxf0" } } } };
  const payload = ui.configPayload(ui.flatConfig(ui.state.rawConfig));
  assert.deepEqual(plain(payload.testbenches.vco.mapping_overrides), { transfer_output: "harm_mag", transfer_phase_output: "ph_pxf0" });
});

test("test-local values with global evidence do not falsely request global migration", () => {
  const { ui } = uiHarness();
  for (const evidence of [{ shadows_global: true }, { global_expression: "0" }]) {
    const html = ui.mappingTableHtml([{ key: "vgs_variable", value: "VGS", selected_details: Object.assign({ scope: "test" }, evidence) }], "flicker", {}, false);
    assert.match(html, /全局已存在 \/ Test 保留同名值/);
    assert.doesNotMatch(html, /需改为全局/);
  }
  assert.match(ui.mappingTableHtml([{ key: "vgs_variable", value: "VGS", selected_details: { scope: "test" } }], "flicker", {}, false), /需改为全局/);
});

test("phase details disclose reference-only role even with independently described candidates", () => {
  const { ui } = uiHarness();
  const field = transferFamilyField();
  field.key = "transfer_phase_output";
  const notice = "参考来源；本次幅相从实际传递源重测完整复数，不合成不同来源。";
  field.selected_details = Object.assign({}, field.selected_details, { role_consumption: notice });
  assert.equal(field.candidate_details.harm_mag.role_consumption, undefined);
  assert.ok(ui.mappingSelectionLines(field).includes("实际使用：" + notice));
  assert.ok(ui.mappingTableHtml([field], "vco", {}, false).includes(notice));
});

test("editable combobox shows selected name and offers unrelated aliases without semantic filtering", () => {
  const { ui } = uiHarness({ "config-form": { values: { mainTb: "/vco", replicaTb: "/noise", executionMode: "dry_run" } } });
  const field = { key: "vgs_output", label: "栅源波形", value: "A1", detected: "A1", required: true,
    candidates: ["A1", "totally_unrelated", "B7"], maestro_presence: "named_output",
    selected_details: { expression: 'v("G")-v("S")' } };
  const html = ui.mappingTableHtml([field], "vco", {}, false);
  assert.match(html, /role="combobox"/);
  assert.match(html, /value="A1"/);
  assert.match(html, /option value="totally_unrelated"/);
  assert.match(html, /Maestro 命名输出/);
  ui.updateMappingOverride("vco", "vgs_output", "totally_unrelated");
  ui.updateMappingOverride("flicker", "dc_sense_source", "branch_q7");
  ui.updateMappingOverride("flicker", "vgs_pwl_variable", "file_alias_q");
  const config = ui.collectConfig();
  const payload = ui.maestroPairPayload(config, false);
  assert.equal(payload.testbenches.vco.mapping_overrides.vgs_output, "totally_unrelated");
  assert.equal(payload.testbenches.replica.mapping_overrides.dc_sense_source, "branch_q7");
  assert.equal(payload.testbenches.replica.mapping_overrides.vgs_pwl_variable, "file_alias_q");
  const rerender = ui.mappingTableHtml([field], "vco", ui.state.mappingOverrides.vco, false);
  assert.match(rerender, /value="totally_unrelated"/);
  assert.equal(ui.state.mappingOverrides.vco.vgs_output, "totally_unrelated");
});

test("waveform exchange exposes source derivation, global destination, relative file and blockers", () => {
  const panel = { innerHTML: "" };
  const { ui } = uiHarness({ "waveform-exchange": panel });
  ui.renderWaveformExchange(null);
  assert.match(panel.innerHTML, /旧绑定不证明/);
  ui.renderWaveformExchange({ ok: false, checked: "static_prepare", summary: "VBS 来源未保存", path_policy: "relative_to_run",
    checks: [{ id: "vbs", status: "fail", detail: "必须保存实际端口" }],
    rows: [{ semantic: "VBS", source_kind: "script_derived", source_expression: 'V("B")-V("S")',
      destination_variable: "bias_c", destination_source: "port_c", pwl_variable: "file_c", scope: "global",
      relative_file: "waveforms/vbs.pwl", status: "fail", detail: '<script>bad()</script>' }] });
  for (const name of ["script_derived", "port_c", "file_c", "global", "waveforms/vbs.pwl", "必须保存实际端口"]) assert.ok(panel.innerHTML.includes(name));
  assert.match(panel.innerHTML, /不表示已完成新仿真/);
  assert.doesNotMatch(panel.innerHTML, /<script>/);
  assert.match(panel.innerHTML, /&lt;script&gt;/);
});

test("generated waveform variable fallback is displayed without becoming a manual override", () => {
  const panel = { innerHTML: "" };
  const { ui } = uiHarness({ "flicker-mapping-fields": panel });
  ui.renderPortableMappings({ roles: { flicker: { mapping_fields: [{ key: "vgs_pwl_variable", value: null,
    candidate_kind: "variable", maestro_presence: "script_derived", status: "missing", active: true }] } },
    waveform_exchange: { rows: [{ semantic: "VGS", pwl_variable: "ISF_VGS_PWL_FILE" }] } });
  assert.match(panel.innerHTML, /value="ISF_VGS_PWL_FILE"/);
  assert.match(panel.innerHTML, /本次脚本生成；Maestro 尚未声明/);
  assert.match(panel.innerHTML, /生成波形文件变量/);
  assert.doesNotMatch(panel.innerHTML, /器件端口求差/);
  assert.deepEqual(plain(ui.state.mappingOverrides.flicker), {});
});

test("manual names and execution settings survive save, inspect and run payloads", () => {
  const { ui } = uiHarness();
  const raw = {
    testbenches: {
      vco: { maestro_dir: "/new/vco/maestro", mapping_overrides: { output_p: "VOUT_P", vgs_output: "HB_TIME_VGS", vds_output: "HB_TIME_VDS", noise_output: "HB_PN_VCO_10k" } },
      replica: { maestro_dir: "/new/flicker/maestro", mapping_overrides: { vgs_variable: "BIAS_GATE", vds_variable: "BIAS_DRAIN", noise_output: "my_noise" } }
    },
    portable_setup: { mode: "auto", overrides: { "main.terminals.source": "TAIL_NEW", "engine.model_file": "/private/pdk/model.scs" } },
    vendor_extension: { retain: true }
  };
  ui.state.rawConfig = raw;
  const flat = ui.flatConfig(raw);
  const payload = ui.configPayload(flat);
  const inspect = ui.maestroPairPayload(flat, false);
  assert.deepEqual(plain(payload.testbenches.vco.mapping_overrides), raw.testbenches.vco.mapping_overrides);
  assert.deepEqual(plain(payload.testbenches.replica.mapping_overrides), raw.testbenches.replica.mapping_overrides);
  assert.deepEqual(plain(inspect.testbenches.vco.mapping_overrides), raw.testbenches.vco.mapping_overrides);
  assert.deepEqual(plain(inspect.portable_setup), raw.portable_setup);
  assert.deepEqual(plain(payload.portable_setup), raw.portable_setup);
  assert.equal(payload.vendor_extension.retain, true);
});

test("clearing an override restores auto instead of resurrecting persisted names", () => {
  const { ui } = uiHarness();
  const raw = { testbenches: { vco: { mapping_overrides: { vgs_output: "OLD_WAVE" } }, replica: { mapping_overrides: { vgs_variable: "OLD_BIAS" } } }, portable_setup: { overrides: { "main.terminals.source": "OLD_NET" } } };
  ui.state.rawConfig = raw;
  const flat = ui.flatConfig(raw);
  flat.mainMappingOverrides = {};
  flat.replicaMappingOverrides = { vgs_variable: " " };
  flat.portableSetup = { mode: "auto", overrides: {} };
  const payload = ui.configPayload(flat);
  assert.deepEqual(plain(payload.testbenches.vco.mapping_overrides), {});
  assert.deepEqual(plain(payload.testbenches.replica.mapping_overrides), {});
  assert.deepEqual(plain(payload.portable_setup.overrides), {});
  assert.equal(raw.testbenches.vco.mapping_overrides.vgs_output, "OLD_WAVE");
});

test("form edits reach payloads and invalidate both in-memory and persistent preflight", () => {
  const elements = { "config-form": { values: { mainTb: "/vco", replicaTb: "/flicker", executionMode: "dry_run" } }, "main-fingerprint": { value: "bound" }, "main-history-fingerprint": { value: "history" } };
  const { ui, storage } = uiHarness(elements);
  ui.state.currentPreflight = { ok: true };
  storage.set("isf-tool:preflight", JSON.stringify({ ok: true }));
  ui.updateMappingOverride("vco", "vgs_output", "  RENAMED_HB_VGS  ");
  ui.updateMappingOverride("execution", "main.injection.negative", "TAIL_NODE");
  const payload = ui.configPayload(ui.collectConfig());
  assert.equal(payload.testbenches.vco.mapping_overrides.vgs_output, "RENAMED_HB_VGS");
  assert.equal(payload.portable_setup.overrides["main.injection.negative"], "TAIL_NODE");
  assert.equal(ui.state.currentPreflight, null);
  assert.equal(storage.get("isf-tool:preflight"), "null");
  assert.equal(elements["main-fingerprint"].value, "");
  assert.equal(elements["main-history-fingerprint"].value, "");
  ui.updateMappingOverride("vco", "vgs_output", "");
  assert.deepEqual(plain(ui.configPayload(ui.collectConfig()).testbenches.vco.mapping_overrides), {});
});

test("cross-TB links follow physical meanings even when names differ", () => {
  const { ui } = uiHarness();
  const report = { roles: { vco: { mapping_fields: [
    { key: "vgs_output", detected: "HB_GATE_TO_SOURCE" }, { key: "vds_output", detected: "HB_DRAIN_TO_SOURCE" }
  ] }, flicker: { mapping_fields: [
    { key: "vgs_variable", detected: "GATE_BIAS" }, { key: "vds_variable", detected: "DRAIN_BIAS" }
  ] } }, cross_tb_mapping: [
    { semantic: "VGS", vco_key: "vgs_output", replica_key: "vgs_variable", vco_value: "HB_GATE_TO_SOURCE", replica_value: "GATE_BIAS", status: "ok" },
    { semantic: "VDS", vco_key: "vds_output", replica_key: "vds_variable", vco_value: "HB_DRAIN_TO_SOURCE", replica_value: "DRAIN_BIAS", status: "ok" }
  ] };
  ui.state.mappingOverrides.vco = { vgs_output: "HB_TIME_VGS" };
  ui.state.mappingOverrides.flicker = { vgs_variable: "VGS", vds_variable: "VDS" };
  const rows = plain(ui.crossTbRows(report, true));
  assert.deepEqual(rows.map(row => [row.semantic, row.vco_value, row.replica_value]), [
    ["VGS", "HB_TIME_VGS", "VGS"], ["VDS", "HB_DRAIN_TO_SOURCE", "VDS"]
  ]);
  assert.equal(rows[0].status, "pending");
});

test("mapping and complete signal catalog escape names, expressions and provenance", () => {
  const { ui } = uiHarness();
  const danger = '\"><img src=x onerror="bad()">';
  const mapping = ui.mappingTableHtml([{ key: danger, label: danger, value: danger, detected: danger, candidates: [danger], source: danger, help: danger }], "vco", {}, false);
  const catalog = ui.signalCatalogHtml([
    { kind: "output", name: "HB_TIME_VGS", expression: danger, source: danger },
    { kind: "net", name: "VOUT_P", details: { pin: danger }, source: "schematic/netlist" },
    { kind: "variable", name: "VGS", expression: "0.8", source: "Maestro" },
    { kind: "device", name: "CORE.MN1", source: "netlist" },
    { kind: "analysis", name: "hbnoise", source: "Maestro" }
  ], "");
  assert.doesNotMatch(mapping + catalog, /<img/);
  assert.match(mapping + catalog, /&lt;img/);
  assert.match(catalog, /5 \/ 5/);
  for (const name of ["HB_TIME_VGS", "VOUT_P", "VGS", "CORE.MN1", "hbnoise"]) assert.ok(catalog.includes(name));
  const filtered = ui.signalCatalogHtml([{ kind: "output", name: "OUT", expression: "custom(VGS)", source: "Maestro" }], "custom");
  assert.match(filtered, /1 \/ 1/);
});

test("saved unknown overrides remain visible and read-only execution fields cannot be edited", () => {
  const { ui } = uiHarness();
  const html = ui.mappingTableHtml([{ key: "locked", label: "目标器件", detected: "M1", value: "M1", editable: false }], "execution", { obsolete: "OLD_NAME" }, false);
  assert.match(html, /obsolete/);
  assert.match(html, /OLD_NAME/);
  assert.match(html, /名称已变化/);
  assert.match(html, /data-mapping-key="locked"[^>]* disabled/);
});

test("an expert file disables all generated execution suggestions and labels them inactive", () => {
  const { ui } = uiHarness();
  const html = ui.mappingTableHtml([
    { key: "main.terminals.source", label: "源端", value: "TAIL", detected: "TAIL" },
    { key: "engine.model_file", label: "模型", value: "model.scs", detected: "model.scs" }
  ], "execution", { "main.terminals.source": "OVERRIDDEN_TAIL" }, true);
  assert.equal((html.match(/<input[^>]+ disabled/g) || []).length, 2);
  assert.doesNotMatch(html, /data-mapping-reset/);
  assert.match(html, /参考值（未生效）/);
  assert.match(html, /专家文件优先；此项不生效/);
});

test("changing a binding drops its stale mappings and execution setup with a visible explanation", () => {
  const elements = {};
  for (const id of ["main-fingerprint", "main-history-fingerprint", "replica-fingerprint", "replica-history-fingerprint", "main-device", "engine-config-path", "pdk-name", "pdk-root"]) elements[id] = { value: "old" };
  elements["main-history"] = { value: "old-rdb", innerHTML: "" };
  elements["mapping-change-notice"] = { hidden: true, textContent: "" };
  const { ui, storage } = uiHarness(elements);
  ui.state.mappingOverrides = { vco: { output_p: "OLD_OUT" }, flicker: { vgs_variable: "KEEP_THIS_TB" } };
  ui.state.portableSetup.overrides = { "main.terminals.source": "OLD_SOURCE" };
  ui.state.currentMaestroPair = { ok: true };
  ui.state.currentPreflight = { ok: true };
  ui.clearPortableBinding("vco", true);
  assert.deepEqual(plain(ui.state.mappingOverrides.vco), {});
  assert.equal(ui.state.mappingOverrides.flicker.vgs_variable, "KEEP_THIS_TB");
  assert.deepEqual(plain(ui.state.portableSetup.overrides), {});
  assert.equal(elements["main-history"].value, "");
  assert.equal(elements["engine-config-path"].value, "");
  assert.equal(elements["pdk-root"].value, "");
  assert.equal(elements["replica-fingerprint"].value, "old");
  assert.equal(elements["mapping-change-notice"].hidden, false);
  assert.match(elements["mapping-change-notice"].textContent, /已清除/);
  assert.equal(ui.state.currentMaestroPair, null);
  assert.equal(storage.get("isf-tool:preflight"), "null");
});

test("late inspect and preflight responses cannot bind the previous form state", async () => {
  const { ui } = uiHarness();
  ui.state.apiOnline = true;
  let finish;
  ui.setApi(() => new Promise(resolve => { finish = resolve; }));
  const pending = ui.inspectMaestroPair(ui.flatConfig({}), true, false);
  ui.invalidatePreflight("changed TB");
  finish({ ok: true, roles: {} });
  assert.equal(await pending, null);
  assert.equal(ui.state.currentMaestroPair, null);
  const preflight = ui.requestPreflight(ui.flatConfig({}), false);
  ui.invalidatePreflight("changed mapping");
  finish({ ok: true, status: "pass" });
  assert.equal(await preflight, null);
  assert.equal(ui.state.currentPreflight, null);
});

test("automatic and expert controls live in Advanced and normal use never requires JSON", () => {
  const html = fs.readFileSync(path.join(ROOT, "isf_tool", "ui", "index.html"), "utf8");
  const advanced = html.indexOf('id="signal-settings"');
  for (const id of ["main-test", "main-history", "replica-test", "replica-history", "vco-mapping-fields", "cross-tb-mapping", "portable-execution-fields", "engine-config-path", "spectre-executable", "harmonics"]) {
    assert.ok(html.indexOf('id="' + id + '"') > advanced, id + " must be in Advanced");
  }
  const ids = Array.from(html.matchAll(/\bid="([^"]+)"/g), match => match[1]);
  assert.equal(new Set(ids).size, ids.length, "form controls must have unique IDs");
  assert.doesNotMatch(html, /bundled Live 必填/);
  assert.match(html, /无需手工编写配置文件/);
  assert.match(html, /开始运行.*私有运行目录调用 Spectre/);
  assert.doesNotMatch(html, /id="execution-mode"|id="allow-execute"/);
});

test("complete catalog is folded diagnostics with explicit name categories", () => {
  const html = fs.readFileSync(path.join(ROOT, "isf_tool", "ui", "index.html"), "utf8");
  assert.match(html, /<details class="nested-details catalog-diagnostics">/);
  assert.doesNotMatch(html, /<details[^>]*catalog-diagnostics[^>]*\bopen\b/);
  assert.match(html, /本次实际使用与手动覆盖/);
  for (const role of ["vco", "flicker"]) {
    assert.match(html, new RegExp('id="' + role + '-catalog-section"'));
    assert.ok(html.includes('data-catalog-kind="' + role + '"'));
  }
  assert.match(html, /查看节点、输出、变量/);
  assert.match(html, /不会改动 schematic 或 Maestro 原文件/);
});

test("catalog categories preserve arbitrary net, output, expression and design variable names", () => {
  const { ui } = uiHarness();
  const catalog = [
    { kind: "net", name: "VOUT_P", source: "netlist" },
    { kind: "net", name: "VOUT_P", source: "Maestro" },
    { kind: "output", name: "HB_PN_VCO_10k", expression: "value(pn('hbnoise) 1e4)" },
    { kind: "variable", name: "customGateBias", expression: "0.9" },
    { kind: "parameter", name: "deviceLength" },
    { kind: "save", name: "mn1:ids" },
    { kind: "trace", name: "customSavedTrace" }
  ];
  assert.match(ui.signalCatalogSummary(catalog), /节点 1 项 · Maestro 输出 1 项 · Maestro 设计变量 1 项 · 网表参数 1 项/);
  const outputs = ui.signalCatalogHtml(catalog, "10k", "output");
  assert.match(outputs, /HB_PN_VCO_10k/);
  assert.match(outputs, /value\(pn/);
  assert.match(outputs, /1 \/ 7/);
  assert.doesNotMatch(outputs, /VOUT_P|customGateBias/);
  assert.match(ui.signalCatalogHtml(catalog, "", "variable"), /customGateBias/);
  assert.match(ui.signalCatalogHtml(catalog, "", "save"), /2 \/ 7/);
});

test("preflight shows concrete execution failures with safe field-specific actions", () => {
  const elements = { "preflight-required": { innerHTML: "" }, "preflight-checks": { innerHTML: "" } };
  const { ui } = uiHarness(elements);
  const danger = '\"><img src=x onerror="bad()">';
  const portable = {
    ok: false,
    fields: [{ key: "engine.device_subckt_file", label: "内部器件模型文件" }],
    errors: [{ field: "engine.device_subckt_file", message: "无法在当前模型中唯一识别内部 MOS" }, { field: danger, message: danger }]
  };
  ui.renderPreflight({ ok: false, required_user_fields: ["portable_setup"], checks: [{ label: "仿真执行设置", status: "fail", detail: "查看缺少的内部模型" }], resolved_config: { detection: { portable_setup: portable } } });
  const html = elements["preflight-required"].innerHTML;
  assert.match(html, /内部器件模型文件/);
  assert.match(html, /无法在当前模型中唯一识别内部 MOS/);
  assert.match(html, /data-settings-field="engine.device_subckt_file"/);
  assert.match(html, /定位并修改此项/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
});

test("failure actions open Advanced and focus the exact execution or mapping field", () => {
  let focused = "";
  const advanced = { open: false, tagName: "DETAILS", parentElement: null };
  const elements = {
    "signal-settings": advanced,
    "field-a": { dataset: { mappingScope: "vco", mappingKey: "vgs_output" }, parentElement: advanced, scrollIntoView() {}, focus() { focused = "vgs_output"; } },
    "field-b": { dataset: { mappingScope: "execution", mappingKey: "engine.device_subckt_file" }, parentElement: advanced, scrollIntoView() {}, focus() { focused = "model"; } }
  };
  const { ui } = uiHarness(elements);
  assert.equal(ui.openSettingsTarget("testbenches.vco.mapping_overrides.vgs_output"), true);
  assert.equal(focused, "vgs_output");
  assert.equal(advanced.open, true);
  assert.equal(ui.openSettingsTarget("engine.device_subckt_file"), true);
  assert.equal(focused, "model");
});

test("Chinese helper text and preflight details have room to wrap", () => {
  const css = fs.readFileSync(path.join(ROOT, "isf_tool", "ui", "styles.css"), "utf8");
  assert.match(css, /--sans: "WenQuanYi Micro Hei", "Noto Sans CJK SC"/);
  assert.match(css, /\.field > small\s*\{[^}]*display: block;[^}]*margin-top: 9px;[^}]*line-height: 1\.85;/);
  assert.match(css, /\.preflight-check small\s*\{[^}]*white-space: pre-wrap;[^}]*overflow-wrap: anywhere;/);
  assert.doesNotMatch(css.match(/\.preflight-check small\s*\{[^}]*\}/)[0], /nowrap|ellipsis/);
});
