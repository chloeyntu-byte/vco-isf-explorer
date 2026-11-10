(function () {
  "use strict";

  function launchApiToken() {
    var token = "";
    try {
      token = String(window.sessionStorage.getItem("isf-tool:api-token") || "");
    } catch (error) {
      token = "";
    }
    try {
      var hash = String(window.location && window.location.hash || "");
      var supplied = new URLSearchParams(hash.replace(/^#/, "")).get("token");
      if (supplied) {
        token = supplied;
        try {
          window.sessionStorage.setItem("isf-tool:api-token", token);
        } catch (storageError) {
          // The in-memory token still protects this tab when storage is disabled.
        }
        if (window.history && typeof window.history.replaceState === "function") {
          window.history.replaceState(
            null,
            document.title,
            String(window.location.pathname || "/") + String(window.location.search || "")
          );
        }
      }
    } catch (error) {
      // Missing URL/session APIs only make the protected backend unavailable.
    }
    return token;
  }

  var API_TOKEN = launchApiToken();

  var STORAGE = {
    config: "isf-tool:config",
    rawConfig: "isf-tool:raw-config-v2",
    job: "isf-tool:job",
    jobId: "isf-tool:job-id",
    preflight: "isf-tool:preflight",
    workspaceState: "isf-tool:workspace-state",
    resultView: "isf-tool:result-view"
  };

  var PAGE_ORDER = ["setup", "discover", "results", "inspect"];
  var COLORS = {
    teal: "#176c68",
    orange: "#dc7140",
    blue: "#3d6f91",
    purple: "#7660a5",
    amber: "#b97a21",
    gray: "#9caeb1"
  };

  var DEFAULT_CONFIG = {
    sourceMode: "maestro_pair",
    projectName: "",
    projectRoot: "",
    mainPattern: "",
    replicaPattern: "",
    vcoCsvPath: "",
    replicaCsvPath: "",
    workpointCsvPath: "",
    metadataJsonPath: "",
    bundlePath: "",
    mainTb: "",
    mainTest: "",
    mainHistory: "",
    mainFingerprint: "",
    mainHistoryFingerprint: "",
    mainDevice: "",
    replicaTb: "",
    replicaTest: "",
    replicaHistory: "",
    replicaFingerprint: "",
    replicaHistoryFingerprint: "",
    replicaDevice: "",
    mainMappingOverrides: {},
    replicaMappingOverrides: {},
    portableSetup: { mode: "auto", overrides: {} },
    offsetValue: 10,
    offsetUnit: "kHz",
    offsetMode: "single",
    offsetValues: "1k,10k,1M,100M",
    offsetStart: "1k",
    offsetStop: "100M",
    offsetSpacing: "intervals",
    offsetStep: "1k",
    offsetIntervals: 5,
    offsetPerDecade: 1,
    harmonics: 3,
    phaseSamples: 512,
    timeOriginSource: "shared PSF absolute t=0",
    periodCount: 8,
    executionMode: "live",
    allowExecute: false,
    engineConfigPath: "",
    runRoot: "",
    autoBundlePath: "",
    vcoCommand: "",
    exchangeCommand: "",
    replicaCommand: "",
    extractCommand: "",
    submitCommand: "",
    pdkName: "",
    pdkRoot: "",
    spectreExecutable: "",
    analysisPython: "",
    adapterMode: "bundled",
    allowDegraded: false,
    timeoutSeconds: 7200
  };

  var state = {
    apiOnline: false,
    apiMode: "checking",
    setupDirty: false,
    pollTimer: null,
    currentJob: null,
    currentResults: null,
    currentRawResults: null,
    currentPreview: null,
    currentPreflight: null,
    currentMaestroPair: null,
    mappingOverrides: { vco: {}, flicker: {} },
    portableSetup: { mode: "auto", overrides: {} },
    setupRevision: 0,
    offsetPreview: null,
    offsetPreviewSequence: 0,
    offsetPreviewTimer: null,
    metricCatalog: null,
    noiseScope: "pair",
    noiseKind: "both",
    rawConfig: null,
    phaseCharts: [],
    workspaceStateSequence: 0,
    executionPending: false
  };

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }

  function $$(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function readStorage(key, fallback) {
    try {
      var value = window.localStorage.getItem(key);
      return value === null ? fallback : JSON.parse(value);
    } catch (error) {
      return fallback;
    }
  }

  function writeStorage(key, value) {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch (error) {
      return false;
    }
    return true;
  }

  function isPlainObject(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
  }

  function deepMerge(base, overlay) {
    var output = isPlainObject(base) ? Object.assign({}, base) : {};
    Object.keys(overlay || {}).forEach(function (key) {
      var value = overlay[key];
      if (isPlainObject(value) && isPlainObject(output[key])) {
        output[key] = deepMerge(output[key], value);
      } else if (isPlainObject(value)) {
        output[key] = deepMerge({}, value);
      } else if (Array.isArray(value)) {
        output[key] = value.slice();
      } else {
        output[key] = value;
      }
    });
    return output;
  }

  function rawConfigFrom(value) {
    var source = value && (value.config || value.data) ? (value.config || value.data) : value;
    return isPlainObject(source) ? deepMerge({}, source) : {};
  }

  function persistentConfig(value) {
    var output = isPlainObject(value) ? deepMerge({}, value) : {};
    if (Object.prototype.hasOwnProperty.call(output, "allowExecute")) output.allowExecute = false;
    if (Object.prototype.hasOwnProperty.call(output, "allow_execute")) output.allow_execute = false;
    if (isPlainObject(output.execution)) output.execution.allow_execute = false;
    if (isPlainObject(output.config)) output.config = persistentConfig(output.config);
    if (isPlainObject(output.data)) output.data = persistentConfig(output.data);
    if (isPlainObject(output.resolved_config)) output.resolved_config = persistentConfig(output.resolved_config);
    return output;
  }

  function removeRetiredNoiseObjects(value) {
    if (Array.isArray(value)) return value.map(removeRetiredNoiseObjects);
    if (!isPlainObject(value)) return value;
    var output = {};
    Object.keys(value).forEach(function (key) {
      if (["__proto__", "constructor", "prototype"].indexOf(key) >= 0) return;
      if (["additionalnoiseobjects", "additionalnoiseobjectsreport", "additionalnoiseobjectconfig"].indexOf(key.replace(/_/g, "").toLowerCase()) >= 0) return;
      output[key] = removeRetiredNoiseObjects(value[key]);
    });
    return output;
  }

  function workspaceStateConfig(value, unconfirm) {
    if (Array.isArray(value)) return value.map(function (item) { return workspaceStateConfig(item, unconfirm); });
    if (!isPlainObject(value)) return value;
    var output = {};
    Object.keys(value).forEach(function (key) {
      if (["__proto__", "constructor", "prototype"].indexOf(key) >= 0) return;
      var normalized = key.replace(/_/g, "").toLowerCase();
      if (["additionalnoiseobjects", "additionalnoiseobjectsreport", "additionalnoiseobjectconfig"].indexOf(normalized) >= 0) return;
      if (["allowexecute", "executionauthorized", "executionconsent"].indexOf(normalized) >= 0) output[key] = false;
      else if (unconfirm && ["bindingfingerprint", "historyfingerprint", "mainfingerprint", "mainhistoryfingerprint", "replicafingerprint", "replicahistoryfingerprint"].indexOf(normalized) >= 0) output[key] = "";
      else output[key] = workspaceStateConfig(value[key], unconfirm);
    });
    return output;
  }

  function workspaceStateView() {
    var result = state.currentResults;
    if (!result) return readStorage(STORAGE.resultView, {});
    if (!result || result.error || result.source !== "api" || !result.rawCanonical || !result.provenance || result.provenance.job_id !== result.id) return {};
    return {job_id: result.id, point: result.point, operating_point_id: result.operatingPointId, offset_hz: result.metrics.offsetHz,
      noise_kind: byId("noise-kind") && byId("noise-kind").value || "both",
      x_scale: byId("noise-x-scale") && byId("noise-x-scale").value || "log",
      cumulative_k: byId("cumulative-k-select") && byId("cumulative-k-select").value || ""};
  }

  function workspaceStateStatus(message) {
    if (byId("workspace-state-status")) byId("workspace-state-status").textContent = message;
  }

  async function refreshWorkspaceStates(selected) {
    if (!state.apiOnline) { workspaceStateStatus("API 不可达；不能保存或读取工作区状态。"); return; }
    var payload = await apiRequest("/api/states");
    var select = byId("workspace-state-list");
    if (select) {
      selected = selected || select.value;
      select.innerHTML = '<option value="">选择已保存状态</option>' + (payload.states || []).map(function (item) {
        return '<option value="' + escapeHtml(item.id) + '"' + (item.id === selected ? ' selected' : '') + '>' + escapeHtml(item.name + " · " + item.saved_at) + '</option>';
      }).join("");
    }
    return payload;
  }

  async function saveWorkspaceState() {
    if (!state.apiOnline) throw new Error("API 不可达，未保存工作区状态");
    var config = byId("config-form") ? configPayload(collectConfig()) : state.rawConfig || readStorage(STORAGE.rawConfig, null) || configPayload(collectConfig());
    var response = await apiRequest("/api/states", {method: "POST", body: {
      name: String(byId("workspace-state-name") && byId("workspace-state-name").value || "").trim(),
      config: workspaceStateConfig(config, false), view: workspaceStateView()
    }});
    workspaceStateStatus("已保存：" + response.path + "。不包含执行授权。");
    await refreshWorkspaceStates(response.id);
    return response;
  }

  function exactWorkspaceResult(payload, view) {
    var source = payload && (payload.result || payload.data) || payload;
    var hz = finiteOrNull(view.offset_hz);
    if (!source || !source.provenance || source.provenance.job_id !== view.job_id || !view.point || !view.operating_point_id || hz === null || hz <= 0) throw new Error("保存的结果缺少同任务来源或精确频点证据");
    var groups = resultOperatingPoints(source).filter(function (group) { return group.id === view.operating_point_id; });
    var matches = groups.length === 1 ? groups[0].rows.filter(function (sample) { return sample.point === view.point && sample.offset === hz; }) : [];
    if (matches.length !== 1 || !source.points || !Object.prototype.hasOwnProperty.call(source.points, view.point)) throw new Error("保存的工况/频偏已不存在或不唯一；未替换为其它频点");
    if (byId("result-offset-select")) byId("result-offset-select").innerHTML = '<option value="' + hz + '" selected>' + escapeHtml(formatOffsetHz(hz)) + '</option>';
    var result = normalizeResults(payload, view.job_id, view.point);
    if (result.error || result.point !== view.point || result.operatingPointId !== view.operating_point_id || result.metrics.offsetHz !== hz) throw new Error("保存的结果选择与实际数据不一致");
    return result;
  }

  function applyWorkspaceResultView(view, result) {
    if (byId("noise-kind")) byId("noise-kind").value = ["both", "thermal", "flicker"].indexOf(view.noise_kind) >= 0 ? view.noise_kind : "both";
    if (byId("noise-x-scale")) byId("noise-x-scale").value = view.x_scale === "linear" ? "linear" : "log";
    var k = finiteOrNull(view.cumulative_k), harmonics = result.sampleHarmonics || [];
    if (byId("cumulative-k-select")) byId("cumulative-k-select").innerHTML = k !== null && harmonics.some(function (row) { return Number(row.k) === k; }) ? '<option value="' + k + '" selected>' + k + '</option>' : '<option value="">不显示累计曲线</option>';
  }

  async function loadWorkspaceState(id) {
    if (!state.apiOnline || !id) throw new Error("请先选择可读取的已保存状态");
    function blocked() { return state.executionPending || activeJobRows([state.currentJob]).length > 0; }
    if (blocked()) throw new Error("当前有活动任务或正在提交运行/恢复；请等待结束后再加载状态");
    var sequence = ++state.workspaceStateSequence, revision = state.setupRevision;
    var response = await apiRequest("/api/states/" + encodeURIComponent(id));
    if (sequence !== state.workspaceStateSequence || revision !== state.setupRevision) return null;
    var saved = response.state;
    if (!saved || saved.schema_version !== 1 || !isPlainObject(saved.config) || !isPlainObject(saved.view)) throw new Error("保存的状态格式不受支持");
    var jobs = await apiRequest("/api/jobs", {timeout: 5000});
    if (sequence !== state.workspaceStateSequence || revision !== state.setupRevision) return null;
    if (blocked() || !jobs || !Array.isArray(jobs.jobs) || activeJobRows(jobs).length) throw new Error("存在活动任务或无法确认任务空闲；未加载状态，也未改变当前任务视图");
    var config = workspaceStateConfig(saved.config, true), view = saved.view;
    state.rawConfig = config;
    writeStorage(STORAGE.rawConfig, config);
    writeStorage(STORAGE.config, flatConfig(config));
    writeStorage(STORAGE.workspaceState, {loaded: true, view: view});
    writeStorage(STORAGE.resultView, {});
    state.setupDirty = true;
    state.currentMaestroPair = null;
    state.currentRawResults = null;
    state.currentResults = null;
    state.currentJob = null;
    writeStorage(STORAGE.job, null);
    writeStorage(STORAGE.jobId, "");
    if (state.pollTimer) { window.clearTimeout(state.pollTimer); state.pollTimer = null; }
    invalidatePreflight("状态已加载；请重新检测并绑定当前 TB，来源确认和预检不继承。");
    populateConfig(config);
    var appliedRevision = state.setupRevision;
    if (byId("workspace-state-name")) byId("workspace-state-name").value = saved.name || "";
    if (document.body.dataset.page === "results") renderResults(unavailableResults("", "已加载设置，正在核对保存的结果选择"));
    workspaceStateStatus("已加载设置；需重新检测并绑定，不自动预检或仿真。" + (response.path ? " 来源：" + response.path : ""));
    if (!view.job_id) {
      if (document.body.dataset.page === "results") renderResults(unavailableResults("", "此状态没有可恢复的同任务结果选择"));
      return response;
    }
    try {
      var payload = await apiRequest("/api/results/" + encodeURIComponent(view.job_id), {timeout: 8000});
      if (sequence !== state.workspaceStateSequence || appliedRevision !== state.setupRevision) return null;
      var result = exactWorkspaceResult(payload, view);
      writeStorage(STORAGE.jobId, view.job_id);
      writeStorage(STORAGE.resultView, view);
      if (document.body.dataset.page === "results") {
        state.currentRawResults = payload;
        applyWorkspaceResultView(view, result);
        if (byId("result-job-select")) byId("result-job-select").innerHTML = '<option value="' + escapeHtml(view.job_id) + '">' + escapeHtml(view.job_id) + '</option>';
        renderResults(result);
        writeStorage(STORAGE.workspaceState, {loaded: true, view: {}});
      }
      workspaceStateStatus("已加载设置及已核验的结果选择：" + view.job_id + " / " + view.point + "。需重新检测绑定；未启动预检或仿真。");
    } catch (error) {
      if (sequence !== state.workspaceStateSequence || appliedRevision !== state.setupRevision) return null;
      writeStorage(STORAGE.workspaceState, {loaded: true, view: {}});
      if (document.body.dataset.page === "results") renderResults(unavailableResults(view.job_id, errorMessage(error)));
      workspaceStateStatus("设置已加载；结果选择未恢复：" + errorMessage(error) + "。没有替换成其它任务或频点。");
    }
    return response;
  }

  function initWorkspaceStates() {
    [["workspace-state-save", saveWorkspaceState], ["workspace-state-refresh", refreshWorkspaceStates], ["workspace-state-load", function () { return loadWorkspaceState(byId("workspace-state-list").value); }]].forEach(function (entry) {
      var button = byId(entry[0]);
      if (button) button.addEventListener("click", async function () {
        setButtonBusy(button, true);
        try { await entry[1](); } catch (error) { workspaceStateStatus(errorMessage(error)); }
        finally { setButtonBusy(button, false); }
      });
    });
  }

  function activeJobRows(payload) {
    var source = payload && (payload.jobs || payload.data) ? (payload.jobs || payload.data) : payload;
    var rows = Array.isArray(source) ? source : [];
    return rows.filter(function (job) {
      var status = String(job && (job.status || job.state) || "").toLowerCase();
      return status === "queued" || status === "running";
    });
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function finite(value, fallback) {
    var number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function finiteOrNull(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    var number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function dynamicErrorValues(rows) {
    return (rows || []).reduce(function (values, row) {
      var value = finiteOrNull(row && row.dynamic_error_dB);
      if (value !== null) values.push(value);
      return values;
    }, []);
  }

  function closureGateEvidence(rows, trendValue) {
    var values = dynamicErrorValues(rows);
    var complete = Array.isArray(rows) && rows.length > 0 && values.length === rows.length;
    var maxAbs = complete ? Math.max.apply(null, values.map(Math.abs)) : null;
    var trend = complete ? finiteOrNull(trendValue) : null;
    return {
      maxAbs: maxAbs,
      trend: trend,
      absPass: maxAbs !== null && maxAbs <= 3.0,
      trendPass: trend !== null && Math.abs(trend) <= 1.5
    };
  }

  function offsetObjectHz(offset) {
    if (!isPlainObject(offset)) return null;
    var direct = finiteOrNull(offset.hz !== undefined ? offset.hz : offset.offset_hz);
    if (direct !== null) return direct;
    var value = finiteOrNull(offset.value);
    var unitScale = { Hz: 1, kHz: 1000, MHz: 1000000 };
    var scale = unitScale[offset.unit];
    return value !== null && scale !== undefined ? value * scale : null;
  }

  function resultOffsetHz(source, row) {
    var metrics = source && source.metrics;
    var metadata = source && (source.metadata || (source.dual_tb && source.dual_tb.metadata));
    var config = source && (source.config || source.configuration);
    var preserved = state.rawConfig || readStorage(STORAGE.rawConfig, {});
    var candidates = [
      row && (row.offset_Hz !== undefined ? row.offset_Hz : row.offset_hz),
      metrics && (metrics.offset_Hz !== undefined ? metrics.offset_Hz : (metrics.offset_hz !== undefined ? metrics.offset_hz : metrics.offsetHz)),
      source && (source.offset_Hz !== undefined ? source.offset_Hz : source.offset_hz),
      metadata && (metadata.offset_Hz !== undefined ? metadata.offset_Hz : metadata.offset_hz),
      config && (config.offset_Hz !== undefined ? config.offset_Hz : config.offset_hz),
      preserved && (preserved.offset_Hz !== undefined ? preserved.offset_Hz : preserved.offset_hz)
    ];
    for (var index = 0; index < candidates.length; index += 1) {
      var candidate = finiteOrNull(candidates[index]);
      if (candidate !== null) return candidate;
    }
    var offsets = [
      metrics && metrics.offset,
      source && source.analysis && source.analysis.offset,
      config && config.analysis && config.analysis.offset,
      preserved && preserved.analysis && preserved.analysis.offset
    ];
    for (var offsetIndex = 0; offsetIndex < offsets.length; offsetIndex += 1) {
      var hz = offsetObjectHz(offsets[offsetIndex]);
      if (hz !== null) return hz;
    }
    return null;
  }

  function formatOffsetHz(value) {
    var hz = finiteOrNull(value);
    if (hz === null) return "offset 未提供";
    var absolute = Math.abs(hz);
    var scale = absolute >= 1000000 ? 1000000 : (absolute >= 1000 ? 1000 : 1);
    var unit = scale === 1000000 ? "MHz" : (scale === 1000 ? "kHz" : "Hz");
    return Number((hz / scale).toPrecision(6)).toString() + " " + unit;
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function statusName(value) {
    var status = String(value || "pending").toLowerCase();
    if (["ok", "done", "complete", "completed", "succeeded", "success", "success_with_warning", "verified", "passed", "pass"].indexOf(status) >= 0) return "success";
    if (["error", "errored", "failure", "failed", "fail", "cancelled", "canceled", "interrupted"].indexOf(status) >= 0) return "failed";
    if (["active", "processing", "running", "in_progress", "started"].indexOf(status) >= 0) return "running";
    return "pending";
  }

  function setApiStatus(mode, label, detail) {
    state.apiMode = mode;
    state.apiOnline = mode === "online";
    var badge = byId("api-status");
    if (!badge) return;
    badge.dataset.state = mode;
    var labelNode = $(".api-label-long", badge);
    var detailNode = byId("api-status-detail");
    if (labelNode) labelNode.textContent = label;
    if (detailNode) detailNode.textContent = detail || label;
    badge.title = detail || label;
  }

  function toast(message, type, timeout) {
    var stack = byId("toast-stack");
    if (!stack) return;
    var item = document.createElement("div");
    item.className = "toast " + (type || "success");
    var icon = type === "error"
      ? '<svg width="18" height="18" viewBox="0 0 18 18" fill="none"><circle cx="9" cy="9" r="6.5" stroke="currentColor" stroke-width="1.4"/><path d="m6.7 6.7 4.6 4.6m0-4.6-4.6 4.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>'
      : '<svg width="18" height="18" viewBox="0 0 18 18" fill="none"><circle cx="9" cy="9" r="6.5" stroke="currentColor" stroke-width="1.4"/><path d="m5.8 9 2.1 2.1 4.4-4.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    item.innerHTML = '<span class="toast-icon">' + icon + '</span><span>' + escapeHtml(message) + '</span><button type="button" aria-label="关闭">×</button>';
    stack.appendChild(item);
    var remove = function () {
      if (item.parentNode) item.parentNode.removeChild(item);
    };
    $("button", item).addEventListener("click", remove);
    window.setTimeout(remove, timeout || 3600);
  }

  async function apiRequest(path, options) {
    var opts = options || {};
    var controller = new AbortController();
    var timer = window.setTimeout(function () { controller.abort(); }, opts.timeout || 4500);
    var fetchOptions = {
      method: opts.method || "GET",
      headers: { "Accept": "application/json" },
      signal: controller.signal
    };
    if (API_TOKEN) fetchOptions.headers["X-ISF-Token"] = API_TOKEN;
    if (opts.body !== undefined) {
      fetchOptions.headers["Content-Type"] = "application/json";
      fetchOptions.body = JSON.stringify(opts.body);
    }
    try {
      var response = await window.fetch(path, fetchOptions);
      var contentType = response.headers.get("content-type") || "";
      if (response.status === 204) return {};
      var payload = contentType.indexOf("application/json") >= 0 ? await response.json() : await response.text();
      if (!response.ok || (payload && typeof payload === "object" && payload.ok === false)) {
        var detail = payload && payload.error ? payload.error : null;
        var message = detail && typeof detail === "object" ? detail.message : (typeof detail === "string" ? detail : "HTTP " + response.status + " " + response.statusText);
        var error = new Error(message || "API request failed");
        error.code = detail && detail.code ? detail.code : "ISF-E9000";
        error.kind = detail && detail.kind;
        error.details = detail && detail.details;
        error.help = detail && detail.help;
        error.httpStatus = response.status;
        throw error;
      }
      return payload;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function errorMessage(error, fallback) {
    var code = error && error.code ? "[" + error.code + "] " : "";
    return code + (error && error.message ? error.message : fallback || "操作失败");
  }

  function reportError(error, fallback) {
    var message = errorMessage(error, fallback);
    toast(message, "error", 7000);
    var panel = byId("job-error-panel");
    if (panel) {
      panel.hidden = false;
      panel.innerHTML = '<strong>' + escapeHtml(error && error.code ? error.code : "ISF-E9000") + '</strong><span>' + escapeHtml(error && error.message ? error.message : fallback || "操作失败") + '</span><a href="errors.html' + (error && error.code ? "?code=" + encodeURIComponent(error.code) : "") + '">排障说明</a>';
    }
  }

  async function checkHealth(showFeedback) {
    setApiStatus("checking", "正在检查 API", "GET /api/health");
    try {
      var health = await apiRequest("/api/health", { timeout: 2600 });
      if (health && typeof health === "object" && (health.ok === false || health.status === "error")) {
        throw new Error(health.message || "API reported unhealthy");
      }
      var version = health && typeof health === "object" ? (health.version || health.api_version || "ready") : "ready";
      setApiStatus("online", "API 已连接", "API " + version);
      if (showFeedback) toast("API 连接正常");
      return true;
    } catch (error) {
      setApiStatus("offline", "API 不可达", "后端数据未加载");
      if (showFeedback) toast("API 不可达，后端数据未加载", "error");
      return false;
    }
  }

  function setupNavigation() {
    var current = document.body.dataset.page;
    var currentIndex = PAGE_ORDER.indexOf(current);
    $$('[data-nav-page]').forEach(function (item) {
      var index = PAGE_ORDER.indexOf(item.dataset.navPage);
      item.classList.toggle("is-active", index === currentIndex);
      item.classList.toggle("is-complete", index < currentIndex);
      var link = $("a", item);
      if (link && index === currentIndex) link.setAttribute("aria-current", "step");
    });
  }

  function bindGlobalActions() {
    var tools = $(".topbar-tools");
    if (tools && !$('a[href="errors.html"]', tools)) {
      var link = document.createElement("a");
      link.className = "tool-link";
      link.href = "errors.html";
      link.textContent = "错误码";
      tools.insertBefore(link, tools.firstChild);
    }
    $$('[data-action="refresh-health"]').forEach(function (button) {
      button.addEventListener("click", function () { checkHealth(true); });
    });
    window.addEventListener("beforeunload", function () {
      if (state.pollTimer) window.clearTimeout(state.pollTimer);
    });
  }

  function flatConfig(raw) {
    var source = raw && (raw.config || raw.data) ? (raw.config || raw.data) : (raw || {});
    var testbenches = source.testbenches || source.test_benches || {};
    var main = testbenches.vco || testbenches.main || testbenches.primary || {};
    var replica = testbenches.replica || testbenches.noise || {};
    var discovery = source.discovery || {};
    var manualImport = source.manualImport || source.manual_import || source.import || {};
    var analysis = source.analysis || source.parameters || {};
    var auto = source.auto || {};
    var execution = source.execution || {};
    var profiles = source.profiles || {};
    var pdk = profiles.pdk || {};
    var inputPolicy = source.input_policy || {};
    var offset = analysis.offset || source.offset || {};
    var offsets = analysis.offsets || {};
    if (typeof offset !== "object") offset = { value: offset };
    return {
      sourceMode: "maestro_pair",
      projectName: source.projectName || source.project_name || source.name || DEFAULT_CONFIG.projectName,
      projectRoot: source.projectRoot || source.project_root || source.root || (
        Array.isArray(discovery.roots) && discovery.roots.length === 1
          ? discovery.roots[0]
          : DEFAULT_CONFIG.projectRoot
      ),
      mainPattern: source.mainPattern || discovery.main_pattern || DEFAULT_CONFIG.mainPattern,
      replicaPattern: source.replicaPattern || discovery.replica_pattern || DEFAULT_CONFIG.replicaPattern,
      vcoCsvPath: source.vcoCsvPath || source.vco_csv_path || manualImport.vco_csv_path || manualImport.vco_csv || "",
      replicaCsvPath: source.replicaCsvPath || source.replica_csv_path || manualImport.replica_csv_path || manualImport.replica_csv || "",
      workpointCsvPath: source.workpointCsvPath || source.workpoint_csv_path || manualImport.workpoint_csv_path || manualImport.metadata_csv || "",
      metadataJsonPath: source.metadataJsonPath || source.metadata_json_path || manualImport.metadata_json_path || "",
      bundlePath: source.bundlePath || source.bundle_path || manualImport.bundle_path || "",
      mainTb: source.mainTb || source.main_tb || main.maestro_dir || main.path || main.cell || DEFAULT_CONFIG.mainTb,
      mainTest: source.mainTest || source.main_test || main.test || DEFAULT_CONFIG.mainTest,
      mainHistory: source.mainHistory || source.main_history || main.history || main.selected_history || DEFAULT_CONFIG.mainHistory,
      mainFingerprint: source.mainFingerprint || source.main_fingerprint || main.binding_fingerprint || DEFAULT_CONFIG.mainFingerprint,
      mainHistoryFingerprint: source.mainHistoryFingerprint || source.main_history_fingerprint || main.history_fingerprint || DEFAULT_CONFIG.mainHistoryFingerprint,
      mainDevice: source.mainDevice || source.main_device || main.device || main.instance || DEFAULT_CONFIG.mainDevice,
      mainMappingOverrides: cleanMappingOverrides(source.mainMappingOverrides || main.mapping_overrides),
      replicaTb: source.replicaTb || source.replica_tb || replica.maestro_dir || replica.path || replica.cell || DEFAULT_CONFIG.replicaTb,
      replicaTest: source.replicaTest || source.replica_test || replica.test || DEFAULT_CONFIG.replicaTest,
      replicaHistory: source.replicaHistory || source.replica_history || replica.history || replica.selected_history || DEFAULT_CONFIG.replicaHistory,
      replicaFingerprint: source.replicaFingerprint || source.replica_fingerprint || replica.binding_fingerprint || DEFAULT_CONFIG.replicaFingerprint,
      replicaHistoryFingerprint: source.replicaHistoryFingerprint || source.replica_history_fingerprint || replica.history_fingerprint || DEFAULT_CONFIG.replicaHistoryFingerprint,
      replicaDevice: source.replicaDevice || source.replica_device || replica.device || replica.instance || DEFAULT_CONFIG.replicaDevice,
      replicaMappingOverrides: cleanMappingOverrides(source.replicaMappingOverrides || replica.mapping_overrides),
      portableSetup: normalizePortableSetup(source.portableSetup || source.portable_setup),
      offsetValue: finite(source.offsetValue || source.offset_value || offset.value, DEFAULT_CONFIG.offsetValue),
      offsetUnit: source.offsetUnit || source.offset_unit || offset.unit || DEFAULT_CONFIG.offsetUnit,
      offsetMode: source.offsetMode || offsets.mode || "single",
      offsetValues: source.offsetValues !== undefined ? source.offsetValues : Array.isArray(offsets.values) ? offsets.values.join(",") : offsets.values || DEFAULT_CONFIG.offsetValues,
      offsetStart: source.offsetStart !== undefined ? source.offsetStart : offsets.start || DEFAULT_CONFIG.offsetStart,
      offsetStop: source.offsetStop !== undefined ? source.offsetStop : offsets.stop || DEFAULT_CONFIG.offsetStop,
      offsetSpacing: source.offsetSpacing || offsets.spacing || (offsets.step ? "step" : "intervals"),
      offsetStep: source.offsetStep !== undefined ? source.offsetStep : offsets.step || DEFAULT_CONFIG.offsetStep,
      offsetIntervals: source.offsetIntervals !== undefined ? source.offsetIntervals : offsets.intervals !== undefined ? offsets.intervals : DEFAULT_CONFIG.offsetIntervals,
      offsetPerDecade: source.offsetPerDecade !== undefined ? source.offsetPerDecade : offsets.points_per_decade !== undefined ? offsets.points_per_decade : DEFAULT_CONFIG.offsetPerDecade,
      harmonics: finite(source.harmonics || source.max_harmonic || analysis.max_harmonic, DEFAULT_CONFIG.harmonics),
      phaseSamples: finite(source.phaseSamples || source.phase_samples || analysis.phase_samples, DEFAULT_CONFIG.phaseSamples),
      timeOriginSource: source.timeOriginSource || source.time_origin_source || analysis.time_origin_source || DEFAULT_CONFIG.timeOriginSource,
      periodCount: finite(source.periodCount || source.period_count || analysis.period_count, DEFAULT_CONFIG.periodCount),
      executionMode: "live",
      allowExecute: false,
      engineConfigPath: source.engineConfigPath || source.engine_config_path || source.config_path || DEFAULT_CONFIG.engineConfigPath,
      runRoot: source.runRoot || source.run_root || auto.run_root || DEFAULT_CONFIG.runRoot,
      autoBundlePath: source.autoBundlePath || source.auto_bundle_path || auto.bundle_path || DEFAULT_CONFIG.autoBundlePath,
      vcoCommand: typeof (source.vcoCommand || source.vco_command || auto.vco_command || source.submitCommand || source.submit_command) === "string" ? (source.vcoCommand || source.vco_command || auto.vco_command || source.submitCommand || source.submit_command) : "",
      exchangeCommand: typeof (source.exchangeCommand || source.exchange_command || auto.exchange_command) === "string" ? (source.exchangeCommand || source.exchange_command || auto.exchange_command) : "",
      replicaCommand: typeof (source.replicaCommand || source.replica_command || auto.replica_command || source.submitCommand || source.submit_command) === "string" ? (source.replicaCommand || source.replica_command || auto.replica_command || source.submitCommand || source.submit_command) : "",
      extractCommand: typeof (source.extractCommand || source.extract_command || auto.extract_command) === "string" ? (source.extractCommand || source.extract_command || auto.extract_command) : "",
      submitCommand: typeof (source.submitCommand || source.submit_command || source.command || auto.vco_command) === "string" ? (source.submitCommand || source.submit_command || source.command || auto.vco_command) : "",
      pdkName: source.pdkName || source.pdk_name || (typeof pdk === "object" ? pdk.name : (pdk === "auto" ? "" : pdk)) || "",
      pdkRoot: source.pdkRoot || source.pdk_root || (typeof pdk === "object" ? pdk.root : "") || "",
      spectreExecutable: source.spectreExecutable || source.spectre_executable || (profiles.environment && profiles.environment.executables && profiles.environment.executables.spectre) || "",
      analysisPython: source.analysisPython || source.analysis_python || (profiles.environment && profiles.environment.python) || "",
      adapterMode: source.adapterMode || source.adapter_mode || execution.adapter_mode || DEFAULT_CONFIG.adapterMode,
      allowDegraded: Boolean(source.allowDegraded !== undefined ? source.allowDegraded : (source.allow_degraded !== undefined ? source.allow_degraded : inputPolicy.allow_degraded)),
      timeoutSeconds: finite(source.timeoutSeconds || source.timeout_seconds || auto.timeout_s, DEFAULT_CONFIG.timeoutSeconds)
    };
  }

  function offsetSpec(config) {
    var mode = config.offsetMode || "single";
    if (mode === "list") return { mode: mode, values: config.offsetValues };
    if (mode === "linear") return { mode: mode, start: config.offsetStart, stop: config.offsetStop,
      spacing: config.offsetSpacing, step: config.offsetStep, intervals: config.offsetIntervals };
    if (mode === "log") return { mode: mode, start: config.offsetStart, stop: config.offsetStop, points_per_decade: config.offsetPerDecade };
    return { mode: mode };
  }

  function offsetAnalysis(config) {
    var scale = { Hz: 1, kHz: 1000, MHz: 1000000 };
    return { offset: { value: config.offsetValue, unit: config.offsetUnit, hz: config.offsetValue * (scale[config.offsetUnit] || 1) }, offsets: offsetSpec(config) };
  }

  function previewFirstOffset(config) {
    var preview = state.offsetPreview;
    return preview && preview.signature === JSON.stringify(offsetAnalysis(config)) && preview.values.length ? preview.values[0] : null;
  }

  function updateOffsetMode(config) {
    var mode = config.offsetMode || "single";
    $$('[data-offset-modes]').forEach(function (panel) {
      panel.hidden = panel.dataset.offsetModes.split(" ").indexOf(mode) < 0;
      $$("input, select", panel).forEach(function (input) { input.disabled = panel.hidden; });
    });
    $$('[data-offset-spacing]').forEach(function (panel) {
      panel.hidden = panel.dataset.offsetSpacing !== config.offsetSpacing;
      $$("input, select", panel).forEach(function (input) { input.disabled = mode !== "linear" || panel.hidden; });
    });
  }

  function offsetTaskCount(report, values) {
    var points = report && report.pair && report.pair.operating_points;
    if (!Array.isArray(points) || !points.length) return { operatingPoints: null, evaluations: null, offsets: values.length };
    return { operatingPoints: points.length, evaluations: points.length * values.length, offsets: values.length };
  }

  function renderOffsetPreview(error) {
    var panel = byId("offset-preview");
    if (!panel) return;
    if (error) { panel.textContent = error; panel.dataset.state = "error"; return; }
    var preview = state.offsetPreview;
    if (!preview) { panel.textContent = "正在核对实际 Hz 点表…"; panel.dataset.state = "pending"; return; }
    var count = offsetTaskCount(state.currentMaestroPair, preview.values);
    panel.dataset.state = "ready";
    panel.innerHTML = '<strong>' + count.offsets + ' 个频偏</strong><span class="offset-point-list">' + preview.values.map(function (value) { return escapeHtml(value.toPrecision(12).replace(/(?:\.0+|(?:(\.\d*?)0+))(?=e|$)/, "$1")) + ' Hz'; }).join(' · ') + '</span><span>' +
      (count.operatingPoints === null ? '工况数待双 TB 检测确认；总评估任务数暂不可用。' : escapeHtml(count.operatingPoints + ' 个实际工况 × ' + count.offsets + ' 个频偏 = ' + count.evaluations + ' 个工况/频偏评估')) +
      '</span><small>每个频偏运行一套完整标量流程，仿真量大致随频偏数增加；终点会包含在点表中。最多 256 点。</small>';
  }

  async function requestOffsetPreview(config) {
    if (state.offsetPreviewTimer) { window.clearTimeout(state.offsetPreviewTimer); state.offsetPreviewTimer = null; }
    var sequence = ++state.offsetPreviewSequence;
    var analysis = offsetAnalysis(config);
    state.offsetPreview = null;
    if (!state.apiOnline) { renderOffsetPreview("API 连接后可预览实际频偏点和任务量；当前尚未核对。"); return null; }
    renderOffsetPreview();
    try {
      var report = await apiRequest("/api/offsets", { method: "POST", body: { analysis: analysis }, timeout: 5000 });
      if (sequence !== state.offsetPreviewSequence) return null;
      var values = report.offsets_hz;
      if (!report.ok || !Array.isArray(values) || !values.length || values.length > 256 || values.some(function (value) { return typeof value !== "number" || !Number.isFinite(value) || value <= 0; })) throw new Error("频偏预览返回了无效点表");
      state.offsetPreview = { signature: JSON.stringify(analysis), values: values.slice() };
      renderOffsetPreview();
      return state.offsetPreview;
    } catch (error) {
      if (sequence === state.offsetPreviewSequence) renderOffsetPreview("请更正频偏设置：" + errorMessage(error));
      return null;
    }
  }

  function scheduleOffsetPreview() {
    if (!byId("offset-preview")) return;
    if (state.offsetPreviewTimer) window.clearTimeout(state.offsetPreviewTimer);
    state.offsetPreviewSequence += 1;
    state.offsetPreview = null;
    renderOffsetPreview();
    state.offsetPreviewTimer = window.setTimeout(function () { requestOffsetPreview(collectConfig()); }, 250);
  }

  function configPayload(config) {
    var unitScale = { Hz: 1, kHz: 1000, MHz: 1000000 };
    var preserved = state.rawConfig || readStorage(STORAGE.rawConfig, {});
    if (!isPlainObject(preserved)) preserved = {};
    var overlay = {
      schema_version: 2,
      source_mode: "maestro_pair",
      run_kind: config.executionMode === "live" ? "signoff" : (preserved.run_kind || "replay"),
      project_name: config.projectName,
      project_root: config.projectRoot,
      profiles: {
        auto_detect: isPlainObject(preserved.profiles) && preserved.profiles.auto_detect !== undefined ? preserved.profiles.auto_detect : true,
        environment: {
          python: config.analysisPython || undefined,
          executables: { spectre: config.spectreExecutable || undefined }
        },
        pdk: config.pdkName || config.pdkRoot ? { name: config.pdkName || undefined, root: config.pdkRoot || undefined } : "auto"
      },
      discovery: {
        roots: config.projectRoot ? [config.projectRoot] : [],
        main_pattern: config.mainPattern,
        replica_pattern: config.replicaPattern
      },
      input_policy: {
        preferred: "maestro-pair",
        allowed: ["maestro-pair", "rdb", "psf", "canonical"],
        allow_degraded: config.allowDegraded
      },
      manual_import: {
        vco_csv_path: config.vcoCsvPath,
        replica_csv_path: config.replicaCsvPath,
        workpoint_csv_path: config.workpointCsvPath,
        metadata_json_path: config.metadataJsonPath,
        bundle_path: config.bundlePath
      },
      testbenches: {
        vco: {
          name: "vco",
          role: "vco",
          path: config.mainTb,
          maestro_dir: config.mainTb,
          test: config.mainTest || undefined,
          history: config.mainHistory || undefined,
          binding_fingerprint: config.mainFingerprint || undefined,
          history_fingerprint: config.mainHistoryFingerprint || undefined,
          device: config.mainDevice
        },
        replica: {
          name: "replica",
          role: "flicker",
          path: config.replicaTb,
          maestro_dir: config.replicaTb,
          test: config.replicaTest || undefined,
          history: config.replicaHistory || undefined,
          binding_fingerprint: config.replicaFingerprint || undefined,
          history_fingerprint: config.replicaHistoryFingerprint || undefined,
          device: config.replicaDevice
        }
      },
      analysis: {
        offset: {
          value: config.offsetValue,
          unit: config.offsetUnit,
          hz: config.offsetValue * (unitScale[config.offsetUnit] || 1)
        },
        max_harmonic: config.harmonics,
        phase_samples: config.phaseSamples,
        time_origin_source: config.timeOriginSource,
        enforce_common_time_origin: true,
        allow_manual_shift: false,
        period_count: config.periodCount
      },
      config_path: config.engineConfigPath,
      run_root: config.runRoot || undefined,
      bundle_path: config.autoBundlePath || undefined,
      execution: {
        dry_run: config.executionMode !== "live",
        allow_execute: config.executionMode === "live" && config.allowExecute,
        background: true,
        adapter_mode: config.adapterMode,
        resume_enabled: true,
        require_preflight: true
      },
      auto: {
        bundle_path: config.autoBundlePath || undefined,
        timeout_s: config.timeoutSeconds,
        dry_run: config.executionMode !== "live"
      },
      submit_command: config.vcoCommand || preserved.submit_command
    };
    if (!config.projectRoot) delete overlay.discovery.roots;
    if (config.vcoCommand) overlay.auto.vco_command = config.vcoCommand;
    if (config.exchangeCommand) overlay.auto.exchange_command = config.exchangeCommand;
    if (config.replicaCommand) overlay.auto.replica_command = config.replicaCommand;
    if (config.extractCommand) overlay.auto.extract_command = config.extractCommand;
    if (config.adapterMode === "bundled") overlay.auto.submitter = "runSimulation";
    if (config.adapterMode === "custom") overlay.auto.submitter = "custom_shell";
    var payload = deepMerge(preserved, overlay);
    // Replace rather than deep-merge: clearing an override must restore automatic detection.
    payload.testbenches.vco.mapping_overrides = cleanMappingOverrides(config.mainMappingOverrides);
    payload.testbenches.replica.mapping_overrides = cleanMappingOverrides(config.replicaMappingOverrides);
    payload.portable_setup = normalizePortableSetup(config.portableSetup);
    // Retired object mappings must not survive a legacy config round-trip.
    payload = removeRetiredNoiseObjects(payload);
    payload.analysis.offsets = offsetSpec(config);
    // An imported legacy flat alias must not override the new recipe on reload.
    ["offsetValue", "offsetUnit", "offsetMode", "offsetValues", "offsetStart", "offsetStop", "offsetSpacing", "offsetStep", "offsetIntervals", "offsetPerDecade"].forEach(function (key) {
      if (Object.prototype.hasOwnProperty.call(payload, key)) payload[key] = config[key];
    });
    return payload;
  }

  function collectConfig() {
    var form = byId("config-form");
    if (!form) return flatConfig(readStorage(STORAGE.config, DEFAULT_CONFIG));
    var formData = new FormData(form);
    function offsetValue(name) {
      var field = form.elements && form.elements.namedItem(name);
      return field ? field.value : formData.get(name);
    }
    return {
      sourceMode: "maestro_pair",
      projectName: String(formData.get("projectName") || "").trim(),
      projectRoot: String(formData.get("projectRoot") || "").trim(),
      mainPattern: String(formData.get("mainPattern") || "").trim(),
      replicaPattern: String(formData.get("replicaPattern") || "").trim(),
      vcoCsvPath: String(formData.get("vcoCsvPath") || "").trim(),
      replicaCsvPath: String(formData.get("replicaCsvPath") || "").trim(),
      workpointCsvPath: String(formData.get("workpointCsvPath") || "").trim(),
      metadataJsonPath: String(formData.get("metadataJsonPath") || "").trim(),
      bundlePath: String(formData.get("bundlePath") || "").trim(),
      mainTb: String(formData.get("mainTb") || "").trim(),
      mainTest: String(formData.get("mainTest") || "").trim(),
      mainHistory: String(formData.get("mainHistory") || "").trim(),
      mainFingerprint: String(formData.get("mainFingerprint") || "").trim(),
      mainHistoryFingerprint: String(formData.get("mainHistoryFingerprint") || "").trim(),
      mainDevice: String(formData.get("mainDevice") || "").trim(),
      mainMappingOverrides: cleanMappingOverrides(state.mappingOverrides.vco),
      replicaTb: String(formData.get("replicaTb") || "").trim(),
      replicaTest: String(formData.get("replicaTest") || "").trim(),
      replicaHistory: String(formData.get("replicaHistory") || "").trim(),
      replicaFingerprint: String(formData.get("replicaFingerprint") || "").trim(),
      replicaHistoryFingerprint: String(formData.get("replicaHistoryFingerprint") || "").trim(),
      replicaDevice: String(formData.get("replicaDevice") || "").trim(),
      replicaMappingOverrides: cleanMappingOverrides(state.mappingOverrides.flicker),
      portableSetup: normalizePortableSetup(state.portableSetup),
      offsetValue: finite(offsetValue("offsetValue"), 10),
      offsetUnit: String(offsetValue("offsetUnit") || "kHz"),
      offsetMode: String(offsetValue("offsetMode") || "single"),
      offsetValues: String(offsetValue("offsetValues") || "").trim(),
      offsetStart: String(offsetValue("offsetStart") || "").trim(),
      offsetStop: String(offsetValue("offsetStop") || "").trim(),
      offsetSpacing: String(offsetValue("offsetSpacing") || "intervals"),
      offsetStep: String(offsetValue("offsetStep") || "").trim(),
      offsetIntervals: String(offsetValue("offsetIntervals") || ""),
      offsetPerDecade: String(offsetValue("offsetPerDecade") || ""),
      harmonics: finite(formData.get("harmonics"), 3),
      phaseSamples: finite(formData.get("phaseSamples"), 512),
      timeOriginSource: String(formData.get("timeOriginSource") || DEFAULT_CONFIG.timeOriginSource),
      periodCount: finite(formData.get("periodCount"), 8),
      executionMode: "live",
      allowExecute: false,
      engineConfigPath: String(formData.get("engineConfigPath") || "").trim(),
      runRoot: String(formData.get("runRoot") || "").trim(),
      autoBundlePath: String(formData.get("autoBundlePath") || "").trim(),
      vcoCommand: String(formData.get("vcoCommand") || formData.get("submitCommand") || "").trim(),
      exchangeCommand: String(formData.get("exchangeCommand") || "").trim(),
      replicaCommand: String(formData.get("replicaCommand") || formData.get("submitCommand") || "").trim(),
      extractCommand: String(formData.get("extractCommand") || "").trim(),
      submitCommand: String(formData.get("vcoCommand") || formData.get("submitCommand") || "").trim(),
      pdkName: String(formData.get("pdkName") || "").trim(),
      pdkRoot: String(formData.get("pdkRoot") || "").trim(),
      spectreExecutable: String(formData.get("spectreExecutable") || "").trim(),
      analysisPython: String(formData.get("analysisPython") || "").trim(),
      adapterMode: String(formData.get("adapterMode") || "bundled"),
      allowDegraded: Boolean(formData.get("allowDegraded")),
      timeoutSeconds: finite(formData.get("timeoutSeconds"), 7200)
    };
  }

  function populateConfig(raw) {
    var form = byId("config-form");
    if (!form) return;
    var config = flatConfig(raw);
    state.mappingOverrides = { vco: config.mainMappingOverrides, flicker: config.replicaMappingOverrides };
    state.portableSetup = normalizePortableSetup(config.portableSetup);
    Object.keys(config).forEach(function (name) {
      var field = form.elements[name];
      if (!field) return;
      if (field instanceof RadioNodeList) {
        Array.prototype.forEach.call(field, function (radio) { radio.checked = radio.value === String(config[name]); });
      } else if (field.type === "checkbox") {
        field.checked = Boolean(config[name]);
      } else {
        var value = config[name] == null ? "" : String(config[name]);
        if (field.tagName === "SELECT" && value && !Array.prototype.some.call(field.options, function (option) { return option.value === value; })) {
          var savedOption = document.createElement("option");
          savedOption.value = value;
          savedOption.textContent = "已保存 · " + value;
          savedOption.dataset.saved = "true";
          field.appendChild(savedOption);
        }
        field.value = value;
      }
    });
    toggleSourceMode(config.sourceMode);
    state.currentMaestroPair = null;
    invalidatePreflight("已读取绑定，请检测当前 TB 的信号名称并重新预检");
    renderPortableMappings({});
    updateOffsetMode(config);
    scheduleOffsetPreview();
    updateConfigSummary();
  }

  function toggleSourceMode(mode) {
    $$('[data-mode-panel]').forEach(function (panel) {
      var active = panel.dataset.modePanel === mode;
      panel.hidden = !active;
      $$("input, select, textarea, button", panel).forEach(function (control) {
        control.disabled = !active;
        if (control.hasAttribute("data-manual-required")) control.required = active;
      });
    });
    var tag = byId("tb-mode-tag");
    if (tag) {
      tag.textContent = mode === "maestro_pair" ? "双 TB 主链" : mode === "manual" ? "旧结果回放" : "自动识别";
      tag.className = mode === "manual" ? "tag orange" : "tag";
    }
  }

  function updateConfigSummary() {
    var config = collectConfig();
    var values = {
      "summary-project": config.projectName || "未命名项目",
      "summary-mode": "VCO + Flicker Maestro",
      "summary-main": config.mainTb || "—",
      "summary-replica": config.replicaTb || "—",
      "summary-offset": config.offsetMode && config.offsetMode !== "single" ? ({ list: "多个单点", linear: "线性频偏", log: "对数频偏" })[config.offsetMode] || "待检查" : config.offsetValue + " " + config.offsetUnit,
      "summary-harmonics": "k = 0…" + config.harmonics,
      "summary-samples": config.phaseSamples + " × " + config.periodCount + " periods"
    };
    Object.keys(values).forEach(function (id) {
      var node = byId(id);
      if (node) {
        node.textContent = values[id];
        node.title = values[id];
      }
    });
  }

  function setButtonBusy(button, busy) {
    if (!button) return;
    button.disabled = busy;
    if (busy) button.setAttribute("aria-busy", "true");
    else button.removeAttribute("aria-busy");
  }

  async function persistConfig(config, feedback) {
    writeStorage(STORAGE.workspaceState, null);
    var storedConfig = persistentConfig(config);
    writeStorage(STORAGE.config, storedConfig);
    var payload = persistentConfig(configPayload(storedConfig));
    state.rawConfig = persistentConfig(payload);
    writeStorage(STORAGE.rawConfig, state.rawConfig);
    var saveState = byId("save-state");
    if (saveState) saveState.textContent = "已保存到浏览器 · " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (!state.apiOnline) {
      if (feedback) toast("API 不可达；配置仅保存到本机", "error");
      return { config: storedConfig, localOnly: true };
    }
    try {
      var response = await apiRequest("/api/config", { method: "POST", body: payload });
      state.rawConfig = persistentConfig(rawConfigFrom(response));
      if (!Object.keys(state.rawConfig).length) state.rawConfig = deepMerge({}, payload);
      writeStorage(STORAGE.rawConfig, state.rawConfig);
      if (saveState) saveState.textContent = "配置已同步到 API";
      if (feedback) toast("配置已保存并同步到 API");
      return response || { config: storedConfig };
    } catch (error) {
      if (!error.httpStatus) setApiStatus("offline", "API 写入失败", "配置仅保存在本机");
      if (feedback) reportError(error, "配置写入失败，已保存在浏览器");
      return { config: storedConfig, localOnly: true };
    }
  }

  function renderPreflight(report) {
    if (!report || typeof report !== "object") return;
    state.currentPreflight = report;
    writeStorage(STORAGE.preflight, persistentConfig(report));
    var status = String(report.status || (report.ok ? "pass" : "fail")).toLowerCase();
    var badge = byId("preflight-status");
    if (badge) {
      badge.className = "status-pill " + (status === "pass" ? "success" : status === "fail" ? "failed" : "warning");
      badge.textContent = status.toUpperCase();
    }
    var summary = report.summary || {};
    var score = byId("preflight-summary");
    if (score) score.innerHTML = '<strong>' + finite(summary.pass, 0) + '</strong><span>PASS · ' + finite(summary.warn, 0) + ' WARN · ' + finite(summary.fail, 0) + ' FAIL</span>';
    var checks = byId("preflight-checks");
    if (checks) {
      checks.innerHTML = (report.checks || []).map(function (item) {
        var itemStatus = String(item.status || "warn").toLowerCase();
        var code = item.code ? '<a href="errors.html?code=' + encodeURIComponent(item.code) + '">' + escapeHtml(item.code) + '</a>' : "";
        return '<div class="preflight-check ' + escapeHtml(itemStatus) + '"><i aria-hidden="true"></i><span><strong>' + escapeHtml(item.label || item.id) + '</strong><small>' + escapeHtml(item.detail || "") + '</small></span>' + code + '</div>';
      }).join("") || '<div class="empty-state compact">无预检条目</div>';
    }
    var maestroPair = report.detected && report.detected.maestro_pair;
    var resolvedConfig = report.resolved_config || {};
    var portable = resolvedConfig.detection && resolvedConfig.detection.portable_setup || maestroPair && maestroPair.portable_setup;
    var required = byId("preflight-required");
    if (required) {
      var fields = report.required_user_fields || [];
      var labels = {
        config_path: "高级设置 → 自动生成的仿真执行设置",
        physical_profile: "高级设置 → 器件、端口与模型对应关系",
        portable_setup: "高级设置 → 自动生成的仿真执行设置",
        "execution.allow_execute": "本次 Live 执行授权"
      };
      var requiredText = fields.length
        ? "需手动补充：" + fields.map(function (field) { return labels[field] || field; }).join("、")
        : "无需手动补充字段";
      required.innerHTML = '<span>' + escapeHtml(requiredText) + '</span>' + executionProblemsHtml(portable);
      if (fields.some(function (field) { return ["config_path", "physical_profile", "portable_setup"].indexOf(field) >= 0; }) && !(portable && portable.errors && portable.errors.length)) {
        required.innerHTML += '<a class="settings-jump" href="index.html#portable-execution-heading" data-settings-field="portable_setup">查看具体执行设置</a>';
      }
    }
    var selected = report.detected && report.detected.inputs && report.detected.inputs.selected;
    if (selected) {
      var main = selected.vco && selected.vco.path;
      var replica = selected.replica && selected.replica.path;
      if (main && byId("summary-main")) byId("summary-main").textContent = main;
      if (replica && byId("summary-replica")) byId("summary-replica").textContent = replica;
    }
    if (maestroPair) renderMaestroPair(maestroPair, false);
    if (portable) renderPortableExecution(portable);
    return report;
  }

  function maestroPairPayload(config, includeFingerprints) {
    var unitScale = { Hz: 1, kHz: 1000, MHz: 1000000 };
    var vco = {
      path: config.mainTb,
      maestro_dir: config.mainTb,
      test: config.mainTest || undefined,
      history: config.mainHistory || undefined,
      device: config.mainDevice || undefined,
      mapping_overrides: cleanMappingOverrides(config.mainMappingOverrides)
    };
    var flicker = {
      path: config.replicaTb,
      maestro_dir: config.replicaTb,
      test: config.replicaTest || undefined,
      history: config.replicaHistory || undefined,
      device: config.replicaDevice || undefined,
      mapping_overrides: cleanMappingOverrides(config.replicaMappingOverrides)
    };
    if (includeFingerprints) {
      vco.binding_fingerprint = config.mainFingerprint || undefined;
      vco.history_fingerprint = config.mainHistoryFingerprint || undefined;
      flicker.binding_fingerprint = config.replicaFingerprint || undefined;
      flicker.history_fingerprint = config.replicaHistoryFingerprint || undefined;
    }
    return {
      testbenches: { vco: vco, replica: flicker },
      portable_setup: normalizePortableSetup(config.portableSetup),
      config_path: config.engineConfigPath || "",
      roots: config.projectRoot ? [config.projectRoot] : [],
      offset_hz: previewFirstOffset(config) || config.offsetValue * (unitScale[config.offsetUnit] || 1),
      analysis: offsetAnalysis(config),
      max_depth: 6,
      max_results: 100,
      include_histories: true
    };
  }

  function pairRoleErrors(report, configRole) {
    return (report.issues || []).filter(function (issue) {
      return issue.severity === "error" && String(issue.field || "").indexOf("testbenches." + configRole) === 0;
    });
  }

  function cleanMappingOverrides(value) {
    var result = {};
    if (!isPlainObject(value)) return result;
    Object.keys(value).forEach(function (key) {
      if (key === "__proto__" || key === "constructor" || key === "prototype") return;
      var item = value[key];
      if (item !== null && item !== undefined && (typeof item === "string" || typeof item === "number")) {
        item = String(item).trim();
        if (item) result[key] = item;
      }
    });
    return result;
  }

  function normalizePortableSetup(value) {
    return { mode: "auto", overrides: cleanMappingOverrides(value && value.overrides) };
  }

  function mappingStatusText(status) {
    return ({ detected: "自动识别", auto: "自动识别", manual: "手动覆盖", override: "手动覆盖",
      missing: "待补充", ambiguous: "多个候选，请选择", stale: "名称已变化，请重新选择",
      invalid: "请检查", optional: "可选", ok: "已对应", matched: "已对应", mapped: "已对应",
      derived: "按器件端口提取", reference: "仅表达式参考", unresolved: "尚未对应", pending: "待重新检测" })[status] || String(status || "待检测");
  }

  function mappingFieldsWithOverrides(fields, overrides) {
    var rows = Array.isArray(fields) ? fields.slice() : [];
    var present = rows.map(function (field) { return field.key; });
    Object.keys(overrides || {}).forEach(function (key) {
      if (present.indexOf(key) < 0) rows.push({ key: key, label: key, value: overrides[key], detected: "",
        status: "stale", source: "已保存的手动覆盖", help: "当前检测尚未确认这个字段；可清空恢复自动后重新检测。" });
    });
    return rows;
  }

  function mappingCandidateNames(field, selected, expandFamilies) {
    var candidates = Array.from(new Set((Array.isArray(field.candidates) ? field.candidates : []).filter(function (name) { return name !== null && name !== undefined; }).map(String)));
    var current = String(selected !== undefined && selected !== null ? selected : field.value || field.detected || "");
    var details = field.candidate_details || {};
    var hidden = new Set();
    if (!expandFamilies) Object.keys(details).forEach(function (name) {
      var info = details[name];
      if (candidates.indexOf(name) < 0 || !info || info.kind !== "transfer_family" || info.errors && info.errors.length) return;
      var members = (Array.isArray(info.members) ? info.members : []).filter(function (item) { return item && item.name; });
      if (members.length >= 2) members.forEach(function (member) { hidden.add(String(member.name)); });
    });
    var result = candidates.filter(function (name) { return !hidden.has(name) || name === current; });
    if (current && result.indexOf(current) < 0) result.push(current);
    return result;
  }

  function mappingSelectionLines(field, selected) {
    var current = String(selected !== undefined && selected !== null ? selected : field.value || field.detected || "");
    var validated = String(field.value || field.detected || "");
    var details = field.candidate_details;
    var info = details && Object.prototype.hasOwnProperty.call(details, current) ? details[current] : null;
    if (!info && current === validated) info = field.selected_details;
    if (!info || ["transfer_family", "transfer_output"].indexOf(info.kind) < 0) return current && details && current !== validated ? ["新输入尚无可核对的传递来源；请应用并验证。"] : [];
    var lines = [];
    if (current !== validated) lines.push("候选表达式说明；本次手动选择尚待应用验证。");
    var consumption = info.role_consumption || field.selected_details && field.selected_details.role_consumption;
    if (consumption) lines.push("实际使用：" + consumption);
    var members = (Array.isArray(info.members) ? info.members : []).filter(function (item) { return item && typeof item === "object"; });
    lines.push(info.kind === "transfer_family" ? "系列 " + (info.family_name || current) + "：" + members.length + " 个已发现成员；短名不是新增的 Maestro 输出。" : "精确输出：" + current + "；保留该单项选择，不自动改成系列。");
    lines.push("按所选输出定位原始来源与成员；电路对应关系由用户确认。");
    lines.push("原始电流源：" + (info.source || "未确认") + "；分析：" + (info.analysis || "未确认") + "；读取代表项：" + (info.anchor_output || "未确认"));
    if (info.sidebands && info.sidebands.length) lines.push("表达式边带：" + info.sidebands.join(", ") + "；算法使用边带 = k − 1，不能按名称后缀直接当作 k。");
    members.forEach(function (member) {
      lines.push((member.name || "未命名") + " → 边带 " + (member.sideband === undefined || member.sideband === null ? "未确认" : member.sideband) + "；源 " + (member.source || "未确认") + "；分析 " + (member.analysis || "未确认") + "；" + (member.expression || "表达式未提供"));
    });
    (info.errors || []).forEach(function (error) { if (error) lines.push("读取提示：" + error); });
    (info.notes || []).forEach(function (note) { if (note) lines.push("系列提示（不阻止运行）：" + note); });
    if (info.missing_sidebands && info.missing_sidebands.length) lines.push("缺少边带：" + info.missing_sidebands.join(", "));
    return lines;
  }

  function mappingSelectionHtml(field, selected) {
    var lines = mappingSelectionLines(field, selected);
    return lines.length ? '<summary>查看选中来源 / 系列成员</summary>' + lines.map(function (line) { return '<small>' + escapeHtml(line) + '</small>'; }).join("") : "";
  }

  function refreshMappingSelection(input, row) {
    var report = state.currentMaestroPair || {};
    var role = report.roles && report.roles[input.dataset.mappingScope] || {};
    var field = (role.mapping_fields || []).find(function (item) { return item.key === input.dataset.mappingKey; });
    var panel = row && $("[data-mapping-selection-details]", row);
    if (!panel) return;
    var html = field ? mappingSelectionHtml(field, input.value) : "";
    panel.innerHTML = html;
    panel.hidden = !html;
  }

  function mappingTableHtml(fields, scope, overrides, disabled, suffix, prepared) {
    var rows = prepared ? fields : mappingFieldsWithOverrides(fields, overrides);
    if (!rows.length) return '<div class="empty-state compact">绑定并检测后显示自动识别结果；所有需要用到的名称都可在这里核对。</div>';
    return '<div class="signal-table-scroll"><table class="signal-table mapping-table"><thead><tr><th>TB / 计算用途</th><th>TB 中实际名称（可选 / 可输入）</th><th>来源 / 缺失 / 推导</th><th>' + (disabled ? '参考值（未生效）' : '状态 / 自动候选') + '</th></tr></thead><tbody>' + rows.map(function (field, index) {
      var id = "mapping-" + scope + "-" + (suffix || "") + index;
      var manual = Object.prototype.hasOwnProperty.call(overrides || {}, field.key) ? overrides[field.key] : "";
      var editable = !disabled && field.editable !== false;
      var value = field.value === null || field.value === undefined ? "" : String(field.value);
      var selected = manual || value || field.effective_value || field.detected || "";
      var candidates = mappingCandidateNames(field, selected);
      var selectionHtml = mappingSelectionHtml(field, selected);
      var details = field.selected_details || {};
      var provenance = ({named_output: "Maestro 命名输出", script_derived: "脚本按器件端口求差", unresolved: "Maestro 缺失 / 尚未对应"})[field.maestro_presence] || "";
      if (field.maestro_presence === "script_derived" && field.candidate_kind === "variable") provenance = "脚本生成波形文件变量（Maestro 未声明）";
      var variableScope = details.scope || field.scope || "";
      var scopeText = ({global: "全局变量", test: "Test 局部变量（需改为全局）"})[variableScope] || variableScope;
      if (variableScope === "test" && (details.shadows_global || Object.prototype.hasOwnProperty.call(details, "global_expression"))) scopeText = "全局已存在 / Test 保留同名值";
      var derivation = field.effective_derivation;
      if (derivation && typeof derivation === "object") derivation = derivation.expression || derivation.description || "";
      return '<tr><td><small>' + escapeHtml(scope === "vco" ? "VCO TB" : scope === "flicker" ? "噪声 TB" : "执行设置") + '</small><label for="' + id + '">' + escapeHtml(field.label || field.key) + (field.required ? '<span class="required"> *</span>' : "") + '</label>' +
        '<small>' + escapeHtml(field.help || "") + '</small>' +
        (field.consumption_mode ? '<small>消费方式：' + escapeHtml(field.consumption_mode) + '</small>' : '') +
        (field.used_by && field.used_by.length ? '<small>用于：' + escapeHtml(field.used_by.join("；")) + '</small>' : field.group === "reference" ? '<small>仅对照，不参与计算</small>' : '') + '</td>' +
        '<td><input id="' + id + '" class="mono-input" type="text" autocomplete="off" role="combobox" aria-autocomplete="list" aria-expanded="false" data-mapping-scope="' + scope + '" data-mapping-key="' + escapeHtml(field.key) + '" data-mapping-detected="' + escapeHtml(field.detected || field.effective_value || "") +
        '" list="' + id + '-candidates" value="' + escapeHtml(selected) + '" placeholder="选择候选或填写真实名称"' + (editable ? "" : " disabled") + '>' +
        '<datalist id="' + id + '-candidates">' + candidates.map(function (candidate) { return '<option value="' + escapeHtml(candidate) + '"></option>'; }).join("") + '</datalist>' +
        '<small>' + escapeHtml((manual ? "手动选择" : "自动选择") + " · " + candidates.length + " 个候选；" + (field.candidate_details ? "系列已合并，仍可输入完整单项名称" : "允许输入其他名称")) + '</small>' +
        (selectionHtml || field.candidate_details ? '<details data-mapping-selection-details' + (selectionHtml ? "" : " hidden") + '>' + selectionHtml + '</details>' : '') +
        (editable ? '<button type="button" class="mapping-reset" data-mapping-reset="' + id + '">恢复自动</button>' : '<small>由已选信号或专家配置决定</small>') +
        '</td><td><small>' + escapeHtml([provenance, scopeText, derivation, field.source].filter(Boolean).join("；") || "等待检测") + '</small>' +
        (details.expression ? '<code>' + escapeHtml(details.expression) + '</code>' : '') + '</td><td><code data-mapping-effective>' + escapeHtml(value || field.effective_value || "未确定") + '</code><small data-mapping-status>' + escapeHtml(disabled ? "专家文件优先；此项不生效" : field.effective_value && !value ? "本次脚本生成；Maestro 尚未声明" : mappingStatusText(field.status)) + '</small><small>自动候选：' + escapeHtml(field.detected || "尚未唯一确定；可手动选择") + '</small></td></tr>';
    }).join("") + '</tbody></table></div>';
  }

  function mappingFieldGroup(field, overrides) {
    var needsAttention = ["invalid", "stale"].indexOf(field.status) >= 0 ||
      ["missing", "ambiguous"].indexOf(field.status) >= 0 && (field.required || Object.prototype.hasOwnProperty.call(overrides || {}, field.key));
    if (field.required) return "main";
    if (needsAttention) return "main";
    if (field.active === false || field.group === "reference") return "reference";
    return field.group === "execution_detail" ? "details" : "main";
  }

  function groupedMappingHtml(fields, scope, overrides, disabled) {
    var rows = mappingFieldsWithOverrides(fields, overrides);
    var groups = { main: [], details: [], reference: [] };
    rows.forEach(function (field) { groups[mappingFieldGroup(field, overrides)].push(field); });
    var html = mappingTableHtml(groups.main, scope, overrides, disabled, "main-", true);
    [["details", "本次执行细节"], ["reference", "仅参考或当前未使用的名称"]].forEach(function (entry) {
      if (groups[entry[0]].length) html += '<details class="nested-details mapping-diagnostics"><summary>' + entry[1] + '（' + groups[entry[0]].length + '）</summary>' + mappingTableHtml(groups[entry[0]], scope, overrides, disabled, entry[0] + "-", true) + '</details>';
    });
    return html;
  }

  function signalCatalogSummary(catalog) {
    var rows = Array.isArray(catalog) ? catalog : [];
    return [["net", "节点"], ["output", "Maestro 输出"], ["variable", "Maestro 设计变量"], ["parameter", "网表参数"]].map(function (entry) {
      var names = rows.filter(function (item) { return item.kind === entry[0]; }).map(function (item) { return item.name; });
      return entry[1] + " " + names.filter(function (name, index) { return names.indexOf(name) === index; }).length + " 项";
    }).join(" · ");
  }

  function signalCatalogHtml(catalog, query, kind) {
    var all = Array.isArray(catalog) ? catalog : [];
    var needle = String(query || "").trim().toLocaleLowerCase();
    var kinds = { net: "节点 net", output: "Maestro 输出 Output", variable: "Maestro 设计变量", device: "器件", analysis: "仿真分析", parameter: "网表参数", saved_signal: "保存信号", save: "保存信号", trace: "输出引用信号", source: "电压 / 电流源", include: "包含文件", subcircuit: "子电路", model: "模型" };
    var rows = all.filter(function (item) {
      var kindMatches = !kind || item.kind === kind || kind === "save" && ["saved_signal", "trace"].indexOf(item.kind) >= 0;
      return kindMatches && (!needle || [item.kind, item.name, item.expression, item.source, item.details && JSON.stringify(item.details), kinds[item.kind]].join(" ").toLocaleLowerCase().indexOf(needle) >= 0);
    });
    return '<p class="caption">显示 ' + rows.length + ' / ' + all.length + ' 项；清单保留原始名称、表达式和来源。输出名称的选择不修改原 Maestro 表达式。</p>' +
      (rows.length ? '<div class="signal-table-scroll catalog-scroll"><table class="signal-table"><thead><tr><th>类型</th><th>名称</th><th>表达式 / 详情</th><th>来源</th></tr></thead><tbody>' + rows.map(function (item) {
        var details = typeof item.details === "string" ? item.details : item.details ? JSON.stringify(item.details) : "";
        return '<tr><td>' + escapeHtml(kinds[item.kind] || item.kind || "其他") + '</td><td><code>' + escapeHtml(item.name) + '</code></td><td><code>' + escapeHtml(item.expression || "") + '</code><small>' + escapeHtml(details) + '</small></td><td>' + escapeHtml(item.source) + '</td></tr>';
      }).join("") + '</tbody></table></div>' : '<div class="empty-state compact">' + (needle || kind ? "当前清单没有这一类型或匹配的名称 / 表达式；可切回全部类型核对。" : "检测后显示完整清单") + '</div>');
  }

  function renderSignalCatalog(role, roleReport) {
    var item = roleReport || state.currentMaestroPair && state.currentMaestroPair.roles && state.currentMaestroPair.roles[role] || {};
    var filter = byId(role + "-signal-filter");
    var kind = byId(role + "-signal-kind");
    var catalog = byId(role + "-signal-catalog");
    if (catalog) catalog.innerHTML = signalCatalogHtml(item.signal_catalog, filter && filter.value, kind && kind.value);
    var summary = byId(role + "-catalog-summary");
    if (summary) summary.textContent = item.signal_catalog ? signalCatalogSummary(item.signal_catalog) : "检测后显示分类数量";
  }

  function executionProblemsHtml(portable) {
    var errors = portable && Array.isArray(portable.errors) ? portable.errors : [];
    var fields = portable && Array.isArray(portable.fields) ? portable.fields : [];
    if (!errors.length) return "";
    return '<ul class="execution-problems" aria-label="仿真执行设置具体问题">' + errors.map(function (error) {
      var field = fields.find(function (item) { return item.key === error.field; });
      var label = field && field.label || "需要确认的对应关系";
      return '<li><strong>' + escapeHtml(label) + '</strong><span>' + escapeHtml(error.message || error.field || "请检查当前对应关系") + '</span><a class="settings-jump" href="index.html#portable-execution-heading" data-settings-field="' + escapeHtml(error.field || "portable_setup") + '">定位并修改此项</a></li>';
    }).join("") + '</ul>';
  }

  function openSettingsTarget(key) {
    var advanced = byId("signal-settings");
    if (!advanced) return false;
    advanced.open = true;
    var scope = "execution";
    var mappingKey = key;
    ["vco", "replica"].forEach(function (role) {
      var prefix = "testbenches." + role + ".mapping_overrides.";
      if (key.indexOf(prefix) === 0) { scope = role === "vco" ? "vco" : "flicker"; mappingKey = key.slice(prefix.length); }
    });
    var input = $$("[data-mapping-key]").find(function (node) { return node.dataset.mappingScope === scope && node.dataset.mappingKey === mappingKey; });
    var target = input || byId(key === "catalog" ? "signal-catalog-overview" : key === "actual" ? "actual-used-mappings" : "portable-execution-heading") || advanced;
    var parent = target.parentElement;
    while (parent) { if (parent.tagName === "DETAILS") parent.open = true; parent = parent.parentElement; }
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    if (target.focus && !target.disabled) target.focus({ preventScroll: true });
    return true;
  }

  function crossTbRows(report, pending) {
    var rows = report && Array.isArray(report.cross_tb_mapping) ? report.cross_tb_mapping : ["VGS", "VDS", "VBS"].map(function (semantic) {
      return { semantic: semantic, vco_key: semantic.toLowerCase() + "_output", replica_key: semantic.toLowerCase() + "_variable", status: "pending" };
    });
    return rows.filter(function (row) {
      if (row.semantic !== "VBS") return true;
      return ["invalid", "stale"].indexOf(row.status) >= 0 || ["vco", "flicker"].some(function (role) {
        var roleReport = report && report.roles && report.roles[role] || {};
        var key = role === "vco" ? row.vco_key : row.replica_key;
        var field = (roleReport.mapping_fields || []).find(function (item) { return item.key === key; });
        return field ? field.active !== false || Boolean(state.mappingOverrides[role][key]) : Boolean(row.vco_value || row.replica_value);
      });
    }).map(function (row) {
      var copy = Object.assign({}, row);
      [["vco", "vco_key", "vco_value"], ["flicker", "replica_key", "replica_value"]].forEach(function (entry) {
        var roleReport = report && report.roles && report.roles[entry[0]] || {};
        var overrides = state.mappingOverrides[entry[0]] || {};
        var key = row[entry[1]];
        var field = (roleReport.mapping_fields || []).find(function (item) { return item.key === key; });
        copy[entry[2]] = pending
          ? overrides[key] || (field ? field.detected : row[entry[2]]) || ""
          : row[entry[2]] || (roleReport.mapping || {})[key] || "";
      });
      if (pending) copy.status = "pending";
      return copy;
    });
  }

  function renderCrossTbMapping(report, pending) {
    var panel = byId("cross-tb-mapping");
    if (!panel) return;
    panel.innerHTML = '<div class="signal-table-scroll"><table class="signal-table"><thead><tr><th>物理量</th><th>VCO 周期波形输出</th><th>→ Flicker 直流变量</th><th>状态 / 用途</th></tr></thead><tbody>' + crossTbRows(report, pending).map(function (row) {
      return '<tr><th>' + escapeHtml(row.semantic) + '</th><td><code>' + escapeHtml(row.vco_value || "待识别") + '</code><small>' + escapeHtml(row.vco_expression && row.vco_expression !== row.vco_value ? row.vco_expression : "") + '</small></td><td><code>' + escapeHtml(row.replica_value || "待识别") + '</code></td><td>' + escapeHtml(mappingStatusText(row.status)) + '<small>' + escapeHtml(row.help || "按物理量对应；名称无需相同。修改两侧对应项后重新检测。") + '</small></td></tr>';
    }).join("") + '</tbody></table></div>';
  }

  function renderPortableExecution(portable) {
    var panel = byId("portable-execution-fields");
    if (!panel) return;
    var expert = String(byId("engine-config-path") && byId("engine-config-path").value || "").trim();
    panel.innerHTML = groupedMappingHtml(portable && portable.fields, "execution", state.portableSetup.overrides, Boolean(expert));
    var status = byId("portable-execution-status");
    if (status) status.textContent = expert ? "正在使用专家配置文件：" + expert + "。下表自动建议与执行覆盖均不生效，已设为只读；清空专家路径即可恢复自动并允许编辑。" : portable && portable.ok ? "执行设置已自动生成，可逐项查看和覆盖。" : "等待补齐可明确识别的器件、节点和模型；下表会列出需要选择的项。";
    var errors = byId("portable-execution-errors");
    if (errors) errors.innerHTML = expert ? "" : executionProblemsHtml(portable);
  }

  function renderPortableMappings(report) {
    ["vco", "flicker"].forEach(function (role) {
      var item = report.roles && report.roles[role] || {};
      var panel = byId(role + "-mapping-fields");
      var fields = (item.mapping_fields || []).map(function (field) {
        var channel = role === "flicker" && (report.waveform_exchange && report.waveform_exchange.rows || []).find(function (row) { return String(row.semantic || "").toLowerCase() + "_pwl_variable" === field.key; });
        return channel ? Object.assign({}, field, { effective_value: channel.pwl_variable }) : field;
      });
      if (panel) panel.innerHTML = groupedMappingHtml(fields, role, state.mappingOverrides[role] || {}, false);
      renderSignalCatalog(role, item);
    });
    renderCrossTbMapping(report, false);
    renderWaveformExchange(report.waveform_exchange);
    renderPortableExecution(report.portable_setup);
    var summary = byId("mapping-readiness");
    if (summary) {
      var fields = ["vco", "flicker"].reduce(function (all, role) { return all.concat(report.roles && report.roles[role] && report.roles[role].mapping_fields || []); }, []);
      var unresolved = fields.filter(function (field) { return ["missing", "ambiguous", "invalid", "stale"].indexOf(field.status) >= 0 && field.required !== false; });
      var catalogs = ["vco", "flicker"].reduce(function (all, role) { return all.concat(report.roles && report.roles[role] && report.roles[role].signal_catalog || []); }, []);
      var activeCount = fields.filter(function (field) { return field.active !== false && field.group !== "reference"; }).length;
      summary.textContent = !fields.length ? "检测后显示本次实际使用的信号、注入支路和跨 TB 对应；完整名称清单保留在折叠诊断中。" : unresolved.length ? "有 " + unresolved.length + " 项用途需要手动选择；打开高级设置处理。" : "已识别 " + activeCount + " 项实际用途；可查看来源、消费方式和覆盖，完整清单在折叠诊断中。";
    }
  }

  function renderWaveformExchange(exchange) {
    var panel = byId("waveform-exchange");
    if (!panel) return;
    if (!exchange) {
      panel.innerHTML = '<p class="caption">波形传递尚未检查；已有 history 或旧绑定不证明本次闭环就绪，请重新识别并绑定。</p>';
      return;
    }
    var rows = Array.isArray(exchange.rows) ? exchange.rows : [];
    var checks = (exchange.checks || []).filter(function (item) { return ["pass", "ok"].indexOf(item.status) < 0; });
    panel.innerHTML = '<p class="caption" role="status">' + escapeHtml(exchange.summary || (exchange.ok ? "波形传递静态检查通过" : "波形传递检查未通过")) + '</p><p class="caption">' + escapeHtml(exchange.path_policy || "") + '</p>' +
      '<div class="signal-table-scroll"><table class="signal-table"><thead><tr><th>物理量</th><th>VCO 实际来源</th><th>噪声 TB 接收端 / Global variable</th><th>本次相对路径</th><th>结果 / 需处理项</th></tr></thead><tbody>' + rows.map(function (row) {
        return '<tr><th>' + escapeHtml(row.semantic) + '</th><td><code>' + escapeHtml(row.source_output || "无命名输出") + '</code><small>' + escapeHtml(row.source_expression || "") + '</small><small>' + escapeHtml(row.source_kind || "") + '</small></td><td><code>' + escapeHtml(row.destination_source || "待补充") + '</code><small>' + escapeHtml([row.destination_variable, row.pwl_variable, row.scope].filter(Boolean).join(" · ")) + '</small></td><td><code>' + escapeHtml(row.relative_file || "待生成") + '</code></td><td>' + escapeHtml(mappingStatusText(row.status)) + '<small>' + escapeHtml(row.detail || "") + '</small></td></tr>';
      }).join("") + '</tbody></table></div>' + checks.map(function (item) { return '<p class="caption">' + escapeHtml(item.detail || item.id) + '</p>'; }).join("") +
      '<p class="caption">绑定阶段验证文件生成和仿真准备；实际运行时还会检查波形文件及仿真结果，静态通过不表示已完成新仿真。</p>';
  }

  function invalidatePreflight(message) {
    state.setupRevision += 1;
    state.currentPreflight = null;
    writeStorage(STORAGE.preflight, null);
    var status = byId("preflight-status");
    if (status) { status.textContent = "需重跑"; status.className = "status-pill warning"; }
    var summary = byId("preflight-summary");
    if (summary) summary.innerHTML = '<strong>—</strong><span>设置已变化</span>';
    var checks = byId("preflight-checks");
    if (checks) checks.innerHTML = '<div class="empty-state compact">请用当前设置重新预检</div>';
    var required = byId("preflight-required");
    if (required) required.textContent = message || "设置已更改，请重新预检";
  }

  function updateMappingOverride(scope, key, value) {
    if (["vco", "flicker", "execution"].indexOf(scope) < 0 || ["__proto__", "constructor", "prototype"].indexOf(key) >= 0) return;
    var target = scope === "execution" ? state.portableSetup.overrides : state.mappingOverrides[scope];
    value = String(value || "").trim();
    if (value) target[key] = value;
    else delete target[key];
    if (scope !== "execution") clearMaestroRoleFingerprints(scope);
    invalidatePreflight("信号或执行设置已更改，请重新检测并预检");
    var summary = byId("mapping-readiness");
    if (summary) summary.textContent = "手动覆盖已更新；重新检测后会显示实际使用值和检查结果。";
    renderCrossTbMapping(state.currentMaestroPair || {}, true);
  }

  function clearPortableBinding(role, resetHistory) {
    state.mappingOverrides[role] = {};
    state.portableSetup = { mode: "auto", overrides: {} };
    clearMaestroRoleFingerprints(role);
    [role === "vco" ? "main-device" : "replica-device", "engine-config-path", "pdk-name", "pdk-root"].forEach(function (id) {
      if (byId(id)) byId(id).value = "";
    });
    if (resetHistory && byId(maestroRoleFields(role).history)) {
      byId(maestroRoleFields(role).history).innerHTML = '<option value="">重新检测可用 history</option>';
      byId(maestroRoleFields(role).history).value = "";
    }
    state.currentMaestroPair = null;
    invalidatePreflight("绑定已更改，已清除此 TB 的旧覆盖和执行设置；请重新检测");
    renderPortableMappings({});
    var notice = byId("mapping-change-notice");
    if (notice) { notice.hidden = false; notice.textContent = "绑定已变化：已清除此 TB 的手动映射、旧执行设置及 PDK 选择。请重新检测，避免沿用上一个 TB 的名称。"; }
    var status = byId(maestroRoleFields(role).status);
    if (status) { status.textContent = "需重新检测"; status.className = "tag orange"; }
  }

  function maestroRoleFields(role) {
    return role === "vco" ? {
      label: "VCO",
      history: "main-history",
      fingerprint: "main-fingerprint",
      historyFingerprint: "main-history-fingerprint",
      status: "vco-bind-status",
      detail: "vco-bind-detail"
    } : {
      label: "Flicker",
      history: "replica-history",
      fingerprint: "replica-fingerprint",
      historyFingerprint: "replica-history-fingerprint",
      status: "flicker-bind-status",
      detail: "flicker-bind-detail"
    };
  }

  function historyIsComplete(history) {
    if (!history || history.error) return false;
    if (history.fully_complete === true) return true;
    var complete = finite(history.complete_runs, 0);
    var checked = finite(history.checked_runs, 0);
    return checked > 0 && complete === checked && Array.isArray(history.source_netlists) && history.source_netlists.length === complete;
  }

  function historyOptionLabel(history) {
    return String(history && history.name || "?") + " · " + String(history && history.path || "?") +
      " · complete/checked " + finite(history && history.complete_runs, 0) + "/" + finite(history && history.checked_runs, 0);
  }

  function historyCanBind(history, testName) {
    if (!historyIsComplete(history)) return false;
    return !testName || Array.isArray(history.tests) && history.tests.indexOf(testName) >= 0;
  }

  function selectHistoryCandidate(histories, requested, testName) {
    var all = Array.isArray(histories) ? histories : [];
    var complete = all.filter(function (history) { return historyCanBind(history, testName); });
    var requestedText = String(requested || "");
    var selected = requestedText ? complete.find(function (history) {
      return String(history.path || "") === requestedText || String(history.name || "") === requestedText;
    }) : null;
    if (!selected && complete.length === 1) selected = complete[0];
    return {
      selected: selected || null,
      historyCount: all.length,
      completeCount: complete.length,
      requiresManual: !selected && complete.length > 1
    };
  }

  function clearMaestroRoleFingerprints(role) {
    var fields = maestroRoleFields(role);
    if (byId(fields.fingerprint)) byId(fields.fingerprint).value = "";
    if (byId(fields.historyFingerprint)) byId(fields.historyFingerprint).value = "";
  }

  function renderHistorySelect(roleReport, role) {
    var fields = maestroRoleFields(role);
    var select = byId(fields.history);
    var previousValue = select ? String(select.value || "") : "";
    var serverHistory = roleReport && roleReport.selected_history;
    var requested = previousValue;
    if (requested && serverHistory) requested = serverHistory.path || serverHistory.name || requested;
    var histories = roleReport && roleReport.histories || [];
    var testName = roleReport && roleReport.selected_test && roleReport.selected_test.name;
    var choice = selectHistoryCandidate(histories, requested, testName);
    var selectedValue = choice.selected ? String(choice.selected.path || choice.selected.name || "") : "";
    var placeholder = choice.completeCount > 1
      ? "请选择完整 history（" + choice.completeCount + " 个）"
      : choice.completeCount === 1 ? "唯一完整 history 已自动选择" : (choice.historyCount ? "无可绑定的完整 history" : "未发现 history");
    if (select) {
      select.innerHTML = '<option value="">' + escapeHtml(placeholder) + '</option>' + histories.map(function (history) {
        var disabled = historyCanBind(history, testName) ? "" : " disabled";
        return '<option value="' + escapeHtml(history.path || history.name || "") + '"' + disabled +
          ' data-history-name="' + escapeHtml(history.name || "") + '"' +
          ' data-complete="' + escapeHtml(finite(history.complete_runs, 0)) + '"' +
          ' data-checked="' + escapeHtml(finite(history.checked_runs, 0)) + '">' +
          escapeHtml(historyOptionLabel(history)) + '</option>';
      }).join("");
      select.value = selectedValue;
      select.title = choice.selected ? historyOptionLabel(choice.selected) : placeholder;
      if (previousValue !== selectedValue && (byId(fields.fingerprint) && byId(fields.fingerprint).value || byId(fields.historyFingerprint) && byId(fields.historyFingerprint).value)) {
        clearMaestroRoleFingerprints(role);
      }
    }
    var serverMatches = Boolean(choice.selected && serverHistory && roleReport.history_fingerprint && (
      String(choice.selected.path || "") === String(serverHistory.path || "") ||
      String(choice.selected.name || "") === String(serverHistory.name || "")
    ));
    choice.value = selectedValue;
    choice.serverHistory = serverMatches ? serverHistory : null;
    choice.bindingReady = serverMatches;
    return choice;
  }

  function roleDetailHtml(roleReport, role, historyState) {
    if (!roleReport || !roleReport.selected_test) return '<span>未找到可绑定 test</span>';
    var test = roleReport.selected_test;
    var design = test.design || {};
    var analyses = (test.analyses || []).filter(function (item) { return item.enabled; }).map(function (item) { return item.name; });
    var mapping = roleReport.mapping || {};
    var mapped = role === "vco"
      ? [mapping.output_p, mapping.output_n].filter(Boolean).join(" ↔ ")
      : [mapping.vgs_variable, mapping.vds_variable, mapping.vbs_variable].filter(Boolean).join(" / ");
    var history = historyState && (historyState.serverHistory || historyState.selected);
    var historyHtml;
    if (history) {
      historyHtml = '<span>history · ' + escapeHtml(history.name || "?") + '</span>' +
        '<span>RDB · ' + escapeHtml(history.path || "?") + '</span>' +
        '<span>complete/checked · ' + escapeHtml(finite(history.complete_runs, 0)) + '/' + escapeHtml(finite(history.checked_runs, 0)) + '</span>' +
        '<span>仿真记录 · ' + (historyState.bindingReady ? "已确认" : "待重新检测") + '</span>';
      var manifest = history.history_manifest || historyState.bindingReady && roleReport.history_manifest;
      var sourceRows = manifest && Array.isArray(manifest.source_netlists) ? manifest.source_netlists : [];
      if (sourceRows.length) {
        var mismatch = false, unknown = false;
        var details = sourceRows.map(function (source) {
          var origin = source.source_design || {};
          var complete = Boolean(origin.library && origin.cell && origin.view);
          if (!complete || !(design.lib || design.library) || !design.cell || !design.view) unknown = true;
          else if (origin.library !== (design.lib || design.library) || origin.cell !== design.cell || origin.view !== design.view) mismatch = true;
          var stamp = finiteOrNull(source.source_mtime), date = stamp !== null ? new Date(stamp * 1000) : null;
          var time = date && Number.isFinite(date.getTime()) ? date.toISOString() : "未知";
          return '<span>点 ' + escapeHtml(source.point_id === undefined || source.point_id === null ? "未知" : source.point_id) + ' / corner ' + escapeHtml(source.corner || "未知") + ' · ' + escapeHtml((origin.library || "?") + '/' + (origin.cell || "?") + '/' + (origin.view || "?")) + '</span><span>网表时间 · ' + escapeHtml(time) + ' · SHA256 ' + escapeHtml(source.sha256 ? String(source.sha256).slice(0, 12) : "未知") + '</span>';
        }).join("");
        historyHtml += '<details class="history-source-details"><summary>实际 history 网表 · ' + sourceRows.length + ' 条 · ' + (mismatch ? "与当前设计存在差异" : unknown ? "部分来源未知" : "设计来源一致") + '</summary>' + details + '<span>展示实际历史网表来源，不把当前设计视图当作旧仿真的来源；差异仅提示，不禁止合法历史回放。</span></details>';
      } else historyHtml += '<span>实际 history 网表来源 / 时间 / hash · 未提供，不能由当前设计推定</span>';
    } else if (historyState && historyState.requiresManual) {
      historyHtml = '<span>history · ' + escapeHtml(historyState.completeCount + " 个完整候选，需手动选择") + '</span>';
    } else {
      historyHtml = '<span>history · 未找到可绑定的完整 RDB</span>';
    }
    return '<strong>当前 test 设计 · ' + escapeHtml((design.lib || design.library || "?") + "/" + (design.cell || "?") + "/" + (design.view || "?")) + '</strong>' +
      '<span>test · ' + escapeHtml(test.name || "?") + '</span>' +
      '<span>analysis · ' + escapeHtml(analyses.join(", ") || "none") + '</span>' +
      '<span>主要信号 · ' + escapeHtml(mapped || "需在高级设置补充") + '</span>' + historyHtml;
  }

  function applyMaestroBindings(report) {
    var bindings = report.bindings || {};
    var vco = bindings.vco;
    var flicker = bindings.flicker;
    if (vco) {
      if (byId("main-tb")) byId("main-tb").value = vco.maestro_dir || "";
      if (byId("main-test")) byId("main-test").value = vco.test || "";
      if (byId("main-history")) byId("main-history").value = vco.history || "";
      if (byId("main-fingerprint")) byId("main-fingerprint").value = vco.binding_fingerprint || "";
      if (byId("main-history-fingerprint")) byId("main-history-fingerprint").value = vco.history_fingerprint || "";
    }
    if (flicker) {
      if (byId("replica-tb")) byId("replica-tb").value = flicker.maestro_dir || "";
      if (byId("replica-test")) byId("replica-test").value = flicker.test || "";
      if (byId("replica-history")) byId("replica-history").value = flicker.history || "";
      if (byId("replica-fingerprint")) byId("replica-fingerprint").value = flicker.binding_fingerprint || "";
      if (byId("replica-history-fingerprint")) byId("replica-history-fingerprint").value = flicker.history_fingerprint || "";
    }
    // Detected environment values remain automatic; never persist them as overrides.
    updateConfigSummary();
  }

  function renderMaestroPair(report, acceptBindings) {
    if (!report || typeof report !== "object") return false;
    state.currentMaestroPair = report;
    var entries = [
      ["vco", "vco", "vco-bind-status", "vco-bind-detail"],
      ["flicker", "replica", "flicker-bind-status", "flicker-bind-detail"]
    ];
    var historyStates = {};
    entries.forEach(function (entry) {
      historyStates[entry[0]] = renderHistorySelect(report.roles && report.roles[entry[0]], entry[0]);
      var roleReport = report.roles && report.roles[entry[0]];
      var candidates = byId(entry[0] + "-test-candidates");
      if (candidates) candidates.innerHTML = (roleReport && roleReport.test_names || []).map(function (name) { return '<option value="' + escapeHtml(name) + '"></option>'; }).join("");
    });
    var historiesReady = entries.every(function (entry) { return historyStates[entry[0]].bindingReady; });
    var bindingAccepted = Boolean(acceptBindings && report.ok && historiesReady);
    var exchangeFailed = Boolean(report.waveform_exchange && !report.waveform_exchange.ok);
    if (!report.ok) entries.forEach(function (entry) { clearMaestroRoleFingerprints(entry[0]); });
    if (bindingAccepted) applyMaestroBindings(report);
    entries.forEach(function (entry) {
      var role = entry[0];
      var errors = pairRoleErrors(report, entry[1]);
      var roleReport = report.roles && report.roles[role];
      var contractReady = Boolean(roleReport && roleReport.selected_test && !errors.length);
      var historyState = historyStates[role];
      var fields = maestroRoleFields(role);
      var sealed = Boolean(byId(fields.fingerprint) && byId(fields.fingerprint).value && byId(fields.historyFingerprint) && byId(fields.historyFingerprint).value);
      var label = exchangeFailed ? "波形传递待修正" : !contractReady ? "需处理" : historyState.requiresManual ? "选择 history" : !historyState.selected ? "无完整 history" : !historyState.bindingReady ? "需重新检测" : (bindingAccepted || sealed ? "已绑定" : "已识别");
      var status = byId(entry[2]);
      if (status) {
        status.textContent = label;
        status.className = !exchangeFailed && contractReady && historyState.bindingReady ? "tag" : "tag orange";
      }
      var detail = byId(entry[3]);
      if (detail) detail.innerHTML = roleDetailHtml(roleReport, role, historyState);
    });
    var panel = byId("maestro-pair-issues");
    if (panel) {
      var issues = report.issues || [];
      var historyNotices = entries.map(function (entry) {
        var historyState = historyStates[entry[0]];
        var label = maestroRoleFields(entry[0]).label;
        if (historyState.requiresManual) {
          return '<div class="pair-issue warning"><i aria-hidden="true"></i><span><strong>' + escapeHtml(label + " history 需手动选择") + '</strong><small>' + escapeHtml("发现 " + historyState.completeCount + " 个完整 RDB；请选择一个后重新检测绑定。") + '</small></span><span class="tag orange">HISTORY</span></div>';
        }
        return "";
      }).filter(Boolean);
      panel.hidden = false;
      var issueHtml = issues.map(function (issue) {
        var severity = issue.severity || "info";
        var action = issue.action ? " · " + issue.action : "";
        return '<div class="pair-issue ' + escapeHtml(severity) + '"><i aria-hidden="true"></i><span><strong>' + escapeHtml(issue.title || issue.code) + '</strong><small>' + escapeHtml((issue.detail || issue.message || "") + action) + '</small></span><a href="errors.html?code=' + encodeURIComponent(issue.code || "") + '">' + escapeHtml(issue.code || "INFO") + '</a></div>';
      });
      panel.innerHTML = issueHtml.concat(historyNotices).join("") || '<div class="empty-state compact">双 TB 合同与所选 history 完整，未发现缺失或改名。</div>';
    }
    var bindings = report.bindings || {};
    if (bindingAccepted || !acceptBindings) {
      if (bindings.vco && byId("summary-main")) byId("summary-main").textContent = bindings.vco.test || bindings.vco.maestro_dir;
      if (bindings.flicker && byId("summary-replica")) byId("summary-replica").textContent = bindings.flicker.test || bindings.flicker.maestro_dir;
    }
    renderPortableMappings(report);
    if (state.offsetPreview) renderOffsetPreview();
    var advanced = byId("signal-settings");
    if (advanced && acceptBindings && (!report.ok || !historiesReady)) advanced.open = true;
    if (bindingAccepted && byId("mapping-change-notice")) byId("mapping-change-notice").hidden = true;
    return bindingAccepted;
  }

  async function inspectMaestroPair(config, acceptBindings, feedback) {
    if (!state.apiOnline) {
      if (feedback) reportError({ code: "ISF-E9000", message: "API 不可达，无法读取 Maestro" });
      return null;
    }
    try {
      var revision = state.setupRevision;
      if (config.offsetMode && config.offsetMode !== "single" && !previewFirstOffset(config)) {
        if (!await requestOffsetPreview(config) || revision !== state.setupRevision) return null;
      }
      var report = await apiRequest("/api/maestro/pair", {
        method: "POST",
        body: maestroPairPayload(config, !acceptBindings),
        timeout: 30000
      });
      if (revision !== state.setupRevision) {
        if (feedback) toast("检测期间设置已变化，请用当前设置重新检测", "error");
        return null;
      }
      report.ui_binding_ready = renderMaestroPair(report, acceptBindings);
      if (feedback) {
        var message = report.ui_binding_ready ? "双 Maestro 与精确 history 已检测并绑定" : report.ok ? "请选择各卡片的完整 history，再重新检测绑定" : "双 Maestro 仍有必须处理的问题";
        if (!report.ui_binding_ready && report.roles) {
          var testChoices = ["vco", "flicker"].filter(function (role) { var item = report.roles[role] || {}; return (item.test_names || []).length > 1 && !item.selected_test; });
          var missingNames = ["vco", "flicker"].reduce(function (all, role) {
            return all.concat(((report.roles[role] || {}).mapping_fields || []).filter(function (field) { return field.required !== false && ["missing", "ambiguous", "invalid", "stale"].indexOf(field.status) >= 0; }).map(function (field) { return (role === "vco" ? "VCO " : "噪声 TB ") + (field.label || field.key); }));
          }, []);
          if (testChoices.length) message = testChoices.map(function (role) { return role === "vco" ? "VCO" : "噪声 TB"; }).join("、") + " 有多个 Maestro Test；请在 Test 下拉中选择。器件和电流检测源在高级对应表单独选择。";
          else if (missingNames.length) message = "需手动对应：" + missingNames.slice(0, 4).join("、") + "；请在高级设置逐行选择或输入。";
        }
        toast(message, report.ui_binding_ready ? undefined : "error", 7000);
      }
      return report;
    } catch (error) {
      if (feedback) reportError(error, "Maestro 双 TB 检测失败");
      return null;
    }
  }

  function invalidateMaestroHistory(role) {
    var fields = maestroRoleFields(role);
    var select = byId(fields.history);
    var selected = select && select.options && select.selectedIndex >= 0 ? select.options[select.selectedIndex] : null;
    clearPortableBinding(role, false);
    var status = byId(fields.status);
    if (status) {
      status.textContent = "需重新检测";
      status.className = "tag orange";
    }
    var detail = byId(fields.detail);
    if (detail) {
      detail.innerHTML = select && select.value
        ? '<strong>history 已更改</strong><span>history · ' + escapeHtml(selected && selected.dataset.historyName || "?") + '</span><span>RDB · ' + escapeHtml(select.value) + '</span><span>complete/checked · ' + escapeHtml(selected && selected.dataset.complete || "0") + '/' + escapeHtml(selected && selected.dataset.checked || "0") + '</span><span>history fingerprint · 待重新检测</span>'
        : '<strong>history 已清除</strong><span>请选择完整 RDB 后重新检测绑定</span>';
    }
    var preflightStatus = byId("preflight-status");
    if (preflightStatus) {
      preflightStatus.textContent = "需重跑";
      preflightStatus.className = "status-pill warning";
    }
    var required = byId("preflight-required");
    if (required) required.textContent = "history 已更改，旧信号覆盖已清除，需重新检测并绑定";
    var inspectButton = byId("inspect-maestro-pair");
    if (inspectButton) inspectButton.textContent = "重新检测并绑定当前 history";
  }

  function maestroBindingReady(config) {
    return Boolean(config && config.mainTb && config.replicaTb && config.mainHistory && config.replicaHistory &&
      config.mainFingerprint && config.replicaFingerprint && config.mainHistoryFingerprint && config.replicaHistoryFingerprint);
  }

  function uniqueRoleCandidate(findings, scoreKey) {
    var ranked = (findings || []).slice().sort(function (a, b) {
      var delta = finite(b[scoreKey], 0) - finite(a[scoreKey], 0);
      return delta || String(a.path || "").localeCompare(String(b.path || ""));
    });
    if (!ranked.length || finite(ranked[0][scoreKey], 0) <= 0) return null;
    if (ranked.length > 1 && finite(ranked[1][scoreKey], 0) === finite(ranked[0][scoreKey], 0)) return null;
    return ranked[0];
  }

  function renderMaestroCandidates(report) {
    var findings = report && report.findings || [];
    var datalist = byId("maestro-candidates");
    if (datalist) datalist.innerHTML = findings.map(function (item) { return '<option value="' + escapeHtml(item.path) + '"></option>'; }).join("");
    var vco = uniqueRoleCandidate(findings, "vco_score");
    var flicker = uniqueRoleCandidate(findings, "flicker_score");
    if (vco && flicker && vco.path !== flicker.path) {
      if (byId("main-tb") && !byId("main-tb").value) byId("main-tb").value = vco.path;
      if (byId("replica-tb") && !byId("replica-tb").value) byId("replica-tb").value = flicker.path;
      updateConfigSummary();
      return true;
    }
    return false;
  }

  function executionRequestConfig(config) {
    return Object.assign({}, config, { executionMode: "live", allowExecute: true });
  }

  async function requestPreflight(config, feedback) {
    // Only an explicit preflight/start action authorizes environment probes.
    // Loading a page, binding, importing and saving never grant authority.
    if (feedback) config = executionRequestConfig(config);
    if (!config.allowExecute) return null;
    if (!state.apiOnline) {
      if (feedback) reportError({ code: "ISF-E9000", message: "API 不可达，无法执行预检" });
      return null;
    }
    try {
      var revision = state.setupRevision;
      var report = await apiRequest("/api/preflight", {
        method: "POST",
        body: { config: configPayload(config) },
        timeout: 15000
      });
      if (revision !== state.setupRevision) {
        if (feedback) toast("预检期间设置已变化，请重新预检", "error");
        return null;
      }
      renderPreflight(report);
      if (feedback) {
        toast(report.ok ? "预检通过" : "预检未通过，请检查 FAIL 项", report.ok ? undefined : "error", 6000);
      }
      return report;
    } catch (error) {
      if (feedback) reportError(error, "预检接口失败");
      return null;
    }
  }

  function unavailableJob(id, message) {
    return {
      id: String(id || ""),
      status: "pending",
      progress: 0,
      message: message || "任务未加载",
      duration: "N/A",
      updated_at: null,
      source: "unavailable",
      stages: [],
      discovered: null
    };
  }

  function stagesFromEvents(events) {
    var order = [];
    var grouped = {};
    events.forEach(function (event, index) {
      var key = String(event.stage || event.key || event.name || "event-" + (index + 1));
      if (!grouped[key]) {
        grouped[key] = { key: key, entries: [] };
        order.push(key);
      }
      grouped[key].entries.push(event);
      grouped[key].title = event.title || event.label || key.replace(/_/g, " ");
      grouped[key].status = statusName(event.status || event.state);
      grouped[key].message = event.message || event.summary || event.detail || "未提供说明";
      grouped[key].duration = event.duration || event.elapsed || "N/A";
    });
    return order.map(function (key) {
      var stage = grouped[key];
      stage.log = stage.entries.map(function (event) {
        var timestamp = event.timestamp || event.updated_at || "N/A";
        var status = String(event.status || event.state || "unknown").toUpperCase();
        var detail = event.details && Object.keys(event.details).length ? " " + JSON.stringify(event.details) : "";
        return "[" + timestamp + "] " + status + " " + (event.message || event.summary || "") + detail;
      }).join("\n");
      delete stage.entries;
      return stage;
    });
  }

  function jobFromResponse(payload) {
    var source = payload && (payload.job || payload.data) ? (payload.job || payload.data) : (payload || {});
    var rawStages = source.stages || source.phases || source.steps;
    var stages;
    if (Array.isArray(rawStages)) {
      stages = rawStages.map(function (stage, index) {
        return {
          key: stage.key || stage.id || "stage-" + index,
          title: stage.title || stage.name || stage.label || "阶段 " + (index + 1),
          status: statusName(stage.status || stage.state),
          message: stage.message || stage.summary || stage.detail || "未提供说明",
          duration: stage.duration || stage.elapsed || "N/A",
          log: Array.isArray(stage.log || stage.logs) ? (stage.log || stage.logs).join("\n") : (stage.log || stage.logs || "N/A")
        };
      });
    } else if (rawStages && typeof rawStages === "object") {
      stages = Object.keys(rawStages).map(function (key) {
        var stage = rawStages[key] || {};
        return {
          key: key,
          title: stage.title || stage.name || key,
          status: statusName(stage.status || stage.state),
          message: stage.message || stage.summary || "未提供说明",
          duration: stage.duration || stage.elapsed || "N/A",
          log: Array.isArray(stage.log || stage.logs) ? (stage.log || stage.logs).join("\n") : (stage.log || stage.logs || "N/A")
        };
      });
    } else if (Array.isArray(source.events)) {
      stages = stagesFromEvents(source.events);
    } else {
      stages = [];
    }
    var progress = finite(source.progress || source.percent || source.percentage, NaN);
    if (!Number.isFinite(progress)) {
      var complete = stages.filter(function (stage) { return stage.status === "success" || stage.status === "failed"; }).length;
      progress = Math.round((complete / Math.max(stages.length, 1)) * 100);
    }
    if (progress <= 1 && progress > 0) progress *= 100;
    var overall = statusName(source.status || source.state);
    if (!source.status && !source.state && stages.length && stages.every(function (stage) { return stage.status === "success"; })) overall = "success";
    return {
      id: String(source.id || source.job_id || source.run_id || (payload && payload.job_id) || ""),
      status: overall,
      progress: clamp(Math.round(progress), 0, 100),
      message: source.message || source.summary || (overall === "success" ? "分析已完成" : overall === "running" ? "分析运行中" : "等待运行"),
      duration: source.duration || source.elapsed || source.elapsed_time || "N/A",
      updated_at: source.updated_at || source.updated || null,
      source: "api",
      stages: stages,
      discovered: source.discovered || source.inputs || source.testbenches || null,
      error: source.error || null,
      resumeAvailable: Boolean(source.resume_available),
      resultAvailable: Boolean(source.result_available !== undefined ? source.result_available : source.resultAvailable),
      isOffsetSweep: Boolean(source.is_offset_sweep !== undefined ? source.is_offset_sweep : source.isOffsetSweep),
      offsetSweep: source.offset_sweep || source.offsetSweep || null,
      rawStatus: String(source.rawStatus || source.status || source.state || "")
    };
  }

  function saveJob(job) {
    if (!job || !job.id) return;
    state.currentJob = job;
    writeStorage(STORAGE.job, job);
    writeStorage(STORAGE.jobId, job.id);
  }

  async function beginDiscovery(config) {
    return requestPreflight(config, true);
  }

  async function loadRemoteConfig(feedback) {
    if (!state.apiOnline) {
      if (feedback) toast("API 不可达，已读取本机配置", "error");
      state.rawConfig = persistentConfig(rawConfigFrom(readStorage(STORAGE.rawConfig, {})));
      var localConfig = persistentConfig(flatConfig(readStorage(STORAGE.config, DEFAULT_CONFIG)));
      writeStorage(STORAGE.rawConfig, state.rawConfig);
      writeStorage(STORAGE.config, localConfig);
      populateConfig(localConfig);
      return;
    }
    try {
      var revision = state.setupRevision;
      var response = await apiRequest("/api/config");
      if (revision !== state.setupRevision) {
        if (feedback) toast("读取期间设置已变化，保留当前输入", "error");
        return;
      }
      state.rawConfig = persistentConfig(rawConfigFrom(response));
      writeStorage(STORAGE.rawConfig, state.rawConfig);
      var config = persistentConfig(flatConfig(response));
      writeStorage(STORAGE.config, config);
      populateConfig(config);
      state.setupDirty = false;
      if (feedback) toast("已读取 API 配置");
    } catch (error) {
      if (feedback) reportError(error, "读取 API 配置失败，保留当前内容");
    }
  }

  function initSetup() {
    var form = byId("config-form");
    if (!form) return;
    state.rawConfig = persistentConfig(rawConfigFrom(readStorage(STORAGE.rawConfig, {})));
    var storedConfig = persistentConfig(flatConfig(readStorage(STORAGE.config, DEFAULT_CONFIG)));
    writeStorage(STORAGE.rawConfig, state.rawConfig);
    writeStorage(STORAGE.config, storedConfig);
    populateConfig(storedConfig);
    form.addEventListener("input", function (event) {
      var target = event.target;
      if (target.dataset.catalogFilter || target.dataset.catalogKind) {
        renderSignalCatalog(target.dataset.catalogFilter || target.dataset.catalogKind);
        return;
      }
      if (target.dataset.mappingScope) {
        updateMappingOverride(target.dataset.mappingScope, target.dataset.mappingKey, target.value);
        var row = target.closest("tr");
        if (row) {
          $("[data-mapping-effective]", row).textContent = target.value || "自动（待重新检测）";
          $("[data-mapping-status]", row).textContent = "待重新检测";
          refreshMappingSelection(target, row);
        }
      } else if (["mainTb", "mainTest", "replicaTb", "replicaTest"].indexOf(target.name) >= 0) {
        var changedRole = target.name.indexOf("main") === 0 ? "vco" : "flicker";
        clearPortableBinding(changedRole, true);
        if (target.name === "mainTb" && byId("main-test")) byId("main-test").value = "";
        if (target.name === "replicaTb" && byId("replica-test")) byId("replica-test").value = "";
      } else {
        invalidatePreflight();
        if (target.name === "engineConfigPath") renderPortableExecution(state.currentMaestroPair && state.currentMaestroPair.portable_setup);
        if (target.name === "mainDevice") clearMaestroRoleFingerprints("vco");
        if (target.name === "replicaDevice") clearMaestroRoleFingerprints("flicker");
      }
      state.setupDirty = true;
      if (String(target.name || "").indexOf("offset") === 0) { updateOffsetMode(collectConfig()); scheduleOffsetPreview(); }
      var saveState = byId("save-state");
      if (saveState) saveState.textContent = "有尚未保存的更改";
      updateConfigSummary();
    });
    form.addEventListener("change", function (event) {
      if (event.target.dataset.catalogFilter || event.target.dataset.catalogKind) {
        renderSignalCatalog(event.target.dataset.catalogFilter || event.target.dataset.catalogKind);
        return;
      }
      if (event.target.name === "sourceMode") toggleSourceMode(event.target.value);
      if (!event.target.dataset.catalogFilter) {
        state.setupDirty = true;
        invalidatePreflight();
      }
      updateConfigSummary();
      if (String(event.target.name || "").indexOf("offset") === 0) { updateOffsetMode(collectConfig()); scheduleOffsetPreview(); }
    });
    form.addEventListener("click", function (event) {
      var jump = event.target.closest("[data-settings-field]");
      if (jump && openSettingsTarget(jump.dataset.settingsField)) {
        event.preventDefault();
        return;
      }
      var id = event.target.dataset.mappingReset;
      var input = id && byId(id);
      if (!input) return;
      input.value = input.dataset.mappingDetected || "";
      updateMappingOverride(input.dataset.mappingScope, input.dataset.mappingKey, "");
      state.setupDirty = true;
      var row = input.closest("tr");
      if (row) {
        $("[data-mapping-effective]", row).textContent = "自动（待重新检测）";
        $("[data-mapping-status]", row).textContent = "待重新检测";
        refreshMappingSelection(input, row);
      }
      if (byId("save-state")) byId("save-state").textContent = "有尚未保存的更改";
    });
    var openSettings = byId("open-signal-settings");
    if (openSettings) openSettings.addEventListener("click", function () {
      ["vco", "flicker"].forEach(function (role) { if (byId(role + "-catalog-section")) byId(role + "-catalog-section").open = true; });
      openSettingsTarget("actual");
    });
    var reinspect = byId("reinspect-signal-mapping");
    if (reinspect) reinspect.addEventListener("click", function () {
      var button = byId("inspect-maestro-pair");
      if (button && !button.disabled) button.click();
    });
    [
      ["main-history", "vco"],
      ["replica-history", "flicker"]
    ].forEach(function (entry) {
      var select = byId(entry[0]);
      if (select) select.addEventListener("change", function () { invalidateMaestroHistory(entry[1]); });
    });

    var saveButton = byId("save-config");
    if (saveButton) saveButton.addEventListener("click", async function () {
      if (!form.reportValidity()) return;
      setButtonBusy(saveButton, true);
      await persistConfig(collectConfig(), true);
      state.setupDirty = false;
      setButtonBusy(saveButton, false);
    });

    var loadButton = byId("load-config");
    if (loadButton) loadButton.addEventListener("click", function () { loadRemoteConfig(true); });

    var inspectPairButton = byId("inspect-maestro-pair");
    if (inspectPairButton) inspectPairButton.addEventListener("click", async function () {
      if (!byId("main-tb").value || !byId("replica-tb").value) {
        toast("请先选择 VCO 与 flicker-noise 两个 Maestro", "error");
        return;
      }
      setButtonBusy(inspectPairButton, true);
      var report = await inspectMaestroPair(collectConfig(), true, true);
      if (report && report.ui_binding_ready) {
        await persistConfig(collectConfig(), false);
        state.setupDirty = false;
        inspectPairButton.textContent = "检测并绑定当前设置";
      }
      setButtonBusy(inspectPairButton, false);
    });

    var findPairButton = byId("find-maestro-pair");
    if (findPairButton) findPairButton.addEventListener("click", async function () {
      var root = String(byId("project-root") && byId("project-root").value || "").trim();
      if (!root) {
        toast("请先填写一个较小的 Maestro 搜索根目录", "error");
        return;
      }
      if (!state.apiOnline) {
        reportError({ code: "ISF-E9000", message: "API 不可达，无法查找 Maestro" });
        return;
      }
      setButtonBusy(findPairButton, true);
      try {
        var candidates = await apiRequest("/api/maestro/discover", {
          method: "POST",
          body: { roots: [root], max_depth: 6, max_results: 100 },
          timeout: 30000
        });
        var filled = renderMaestroCandidates(candidates);
        toast(filled ? "已填写唯一的 VCO / flicker 候选，请确认后检测" : "候选不唯一，请在下拉建议中手动选择", filled ? undefined : "error", 7000);
      } catch (error) {
        reportError(error, "Maestro 查找失败");
      } finally {
        setButtonBusy(findPairButton, false);
      }
    });

    var preflightButton = byId("run-preflight");
    if (preflightButton) preflightButton.addEventListener("click", async function () {
      if (!form.reportValidity()) return;
      var config = collectConfig();
      if (!maestroBindingReady(config)) {
        toast("请先为两个 Maestro 选择完整 history，并重新检测绑定", "error", 7000);
        return;
      }
      setButtonBusy(preflightButton, true);
      await persistConfig(config, false);
      await requestPreflight(config, true);
      setButtonBusy(preflightButton, false);
    });

    var importButton = byId("import-data");
    if (importButton) importButton.addEventListener("click", async function () {
      var config = collectConfig();
      var hasBundle = Boolean(config.bundlePath);
      var hasDual = Boolean(config.vcoCsvPath && config.replicaCsvPath);
      if (!hasBundle && !hasDual) {
        var firstMissing = !config.vcoCsvPath ? byId("vco-csv-path") : byId("replica-csv-path");
        if (firstMissing) firstMissing.focus();
        toast("请填写完整 bundle，或同时填写 VCO 与 replica CSV", "error");
        return;
      }
      setButtonBusy(importButton, true);
      await persistConfig(config, false);
      if (state.apiOnline) {
        try {
          var importPayload = hasBundle ? {
            path: config.bundlePath
          } : {
            format: "dual_tb",
            vco_csv: config.vcoCsvPath,
            replica_csv: config.replicaCsvPath,
            workpoint_csv: config.workpointCsvPath,
            metadata_path: config.metadataJsonPath
          };
          var response = await apiRequest("/api/import", {
            method: "POST",
            body: importPayload
          });
          if (response && (response.job || response.job_id || response.id)) {
            var importedJob = jobFromResponse(response);
            saveJob(importedJob);
            toast("导入完成，共 " + finite(response.points, 0) + " 个设计点");
            window.location.href = "results.html?id=" + encodeURIComponent(importedJob.id);
            return;
          }
          throw new Error("import response has no job id");
        } catch (error) {
          reportError(error, "导入失败，路径已保存在本机");
        }
      } else {
        toast("API 不可达；导入路径仅保存在本机", "error");
      }
      setButtonBusy(importButton, false);
    });

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (!form.reportValidity()) return;
      var submit = $('button[type="submit"]', form);
      setButtonBusy(submit, true);
      var config = collectConfig();
      await persistConfig(config, false);
      var report = await beginDiscovery(config);
      setButtonBusy(submit, false);
      if (report && report.ok) window.location.href = "discover.html";
    });
    var cachedPreflight = readStorage(STORAGE.preflight, null);
    if (cachedPreflight) renderPreflight(cachedPreflight);
    if (window.location.hash === "#portable-execution-heading") openSettingsTarget("portable_setup");
  }

  function stageIcon(status) {
    if (status === "success") return '<svg width="17" height="17" viewBox="0 0 18 18" fill="none"><path d="m4.5 9 3 3 6-6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    if (status === "failed") return '<svg width="17" height="17" viewBox="0 0 18 18" fill="none"><path d="m5.5 5.5 7 7m0-7-7 7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';
    if (status === "running") return '<svg width="17" height="17" viewBox="0 0 18 18" fill="none"><path d="M15 9a6 6 0 1 1-1.75-4.25M15 3v3.3h-3.3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    return '<svg width="17" height="17" viewBox="0 0 18 18" fill="none"><circle cx="9" cy="9" r="5.5" stroke="currentColor" stroke-width="1.3"/><path d="M9 6v3.3l2 1.2" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }

  function renderStages(stages) {
    var list = byId("stage-list");
    if (!list) return;
    var badge = byId("pipeline-status");
    if (!Array.isArray(stages) || !stages.length) {
      list.innerHTML = '<div class="empty-state">N/A · 阶段未加载</div>';
      if (badge) {
        badge.className = "status-pill";
        badge.textContent = "N/A";
      }
      return;
    }
    list.innerHTML = stages.map(function (stage) {
      var status = statusName(stage.status);
      var label = status === "success" ? "success" : status === "failed" ? "fail" : status;
      return '<article class="stage-item" data-status="' + status + '">' +
        '<span class="stage-icon" aria-hidden="true">' + stageIcon(status) + '</span>' +
        '<div class="stage-copy"><div class="stage-title-row"><strong>' + escapeHtml(stage.title) + '</strong><span class="status-pill ' + status + '">' + label + '</span></div><p>' + escapeHtml(stage.message) + '</p></div>' +
        '<span class="stage-meta">' + escapeHtml(stage.duration || "—") + '</span>' +
        '<details class="stage-log"><summary>查看日志</summary><pre class="log-output">' + escapeHtml(stage.log || "No log output.") + '</pre></details>' +
      '</article>';
    }).join("");
    var success = stages.filter(function (stage) { return statusName(stage.status) === "success"; }).length;
    var failed = stages.filter(function (stage) { return statusName(stage.status) === "failed"; }).length;
    if (badge) {
      badge.className = "status-pill " + (failed ? "failed" : success === stages.length ? "success" : "running");
      badge.textContent = failed ? failed + " fail · " + success + " success" : success + " / " + stages.length + (success === stages.length ? " success" : " complete");
    }
  }

  function formatUpdated(value) {
    if (!value) return "N/A";
    var time = new Date(value);
    if (Number.isNaN(time.getTime())) return String(value);
    var seconds = Math.max(0, Math.round((Date.now() - time.getTime()) / 1000));
    if (seconds < 10) return "刚刚";
    if (seconds < 60) return seconds + " 秒前";
    return time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function renderDiscoveryInputs(discovered, config) {
    var mainNode = byId("found-main-path");
    var replicaNode = byId("found-replica-path");
    var mainMeta = byId("found-main-meta");
    var replicaMeta = byId("found-replica-meta");
    if (!discovered) {
      if (mainNode) { mainNode.textContent = "N/A"; mainNode.title = "未加载"; }
      if (replicaNode) { replicaNode.textContent = "N/A"; replicaNode.title = "未加载"; }
      if (mainMeta) mainMeta.textContent = "未加载";
      if (replicaMeta) replicaMeta.textContent = "未加载";
      return;
    }
    var data = discovered || {};
    var selected = data.selected || (data.inputs && data.inputs.selected) || {};
    var roles = data.roles || {};
    var bindings = data.bindings || {};
    var main = roles.vco || bindings.vco || selected.vco || data.main || data.primary || (data.testbenches && (data.testbenches.vco || data.testbenches.main)) || {};
    var replica = roles.flicker || bindings.flicker || selected.replica || data.replica || data.noise || (data.testbenches && data.testbenches.replica) || {};
    var mainPath = main.maestro_dir || main.path || main.result_path || config.mainTb || "N/A";
    var replicaPath = replica.maestro_dir || replica.path || replica.result_path || config.replicaTb || "N/A";
    if (mainNode) { mainNode.textContent = mainPath; mainNode.title = mainPath; }
    if (replicaNode) { replicaNode.textContent = replicaPath; replicaNode.title = replicaPath; }
    var mainTest = main.selected_test && main.selected_test.name || main.test;
    var replicaTest = replica.selected_test && replica.selected_test.name || replica.test;
    if (mainMeta) mainMeta.textContent = mainTest ? "MAESTRO · test=" + mainTest : "等待绑定";
    if (replicaMeta) replicaMeta.textContent = replicaTest ? "MAESTRO · test=" + replicaTest : "等待绑定";
  }

  function canStopAfterOffset(job) {
    return Boolean(job && job.isOffsetSweep && ["running", "pending", "queued"].indexOf(job.status) >= 0 && !(job.offsetSweep && job.offsetSweep.stop_requested));
  }

  async function requestStopAfterOffset() {
    var job = state.currentJob;
    if (!state.apiOnline || !canStopAfterOffset(job)) return false;
    var button = byId("stop-after-offset");
    setButtonBusy(button, true);
    try {
      await apiRequest("/api/jobs/" + encodeURIComponent(job.id) + "/stop-after-offset", { method: "POST", body: {}, timeout: 10000 });
      job.offsetSweep = Object.assign({}, job.offsetSweep || {}, { stop_requested: true });
      renderJob(job);
      toast("已请求：当前频偏完整完成后停止，已完成结果保留，可继续恢复。", undefined, 7000);
      return true;
    } catch (error) { reportError(error, "停止后续频偏的请求失败"); return false; }
    finally { setButtonBusy(button, false); if (button) button.disabled = !canStopAfterOffset(state.currentJob); }
  }

  function renderJob(job) {
    if (!job) return;
    state.currentJob = job;
    var status = statusName(job.status);
    var nodes = {
      "job-id": job.id || "N/A",
      "job-status": job.source === "api" ? (job.rawStatus || status).toUpperCase() : "N/A",
      "job-duration": typeof job.duration === "number" ? job.duration.toFixed(1) + " s" : (job.duration || "N/A"),
      "job-updated": formatUpdated(job.updated_at),
      "job-message": job.message || "任务未加载",
      "job-progress-label": job.progress + "%",
      "job-source": job.source === "api" ? "Live API" : "N/A"
    };
    Object.keys(nodes).forEach(function (id) { if (byId(id)) byId(id).textContent = nodes[id]; });
    if (byId("job-id-input")) byId("job-id-input").value = job.id;
    if (byId("job-progress")) byId("job-progress").style.width = job.progress + "%";
    var icon = byId("job-state-icon");
    if (icon) {
      icon.classList.toggle("is-running", status === "running");
      icon.innerHTML = status === "success"
        ? '<svg width="22" height="22" viewBox="0 0 22 22" fill="none"><path d="m6 11 3.2 3.2L16.5 7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><circle cx="11" cy="11" r="8.5" stroke="currentColor" stroke-width="1.4"/></svg>'
        : status === "failed"
          ? '<svg width="22" height="22" viewBox="0 0 22 22" fill="none"><path d="m7.5 7.5 7 7m0-7-7 7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="11" cy="11" r="8.5" stroke="currentColor" stroke-width="1.4"/></svg>'
          : '<svg width="22" height="22" viewBox="0 0 22 22" fill="none"><path d="M18.5 11A7.5 7.5 0 1 1 16.3 5.7M18.5 3.5v4.2h-4.2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    }
    renderStages(job.stages || []);
    var cachedPreflight = readStorage(STORAGE.preflight, null);
    var discovered = job.discovered || (
      cachedPreflight && cachedPreflight.detected &&
      (cachedPreflight.detected.maestro_pair || cachedPreflight.detected.inputs)
    );
    renderDiscoveryInputs(discovered, flatConfig(readStorage(STORAGE.config, DEFAULT_CONFIG)));
    var resumeButton = byId("resume-job");
    if (resumeButton) {
      resumeButton.hidden = !job.resumeAvailable;
      resumeButton.disabled = !job.resumeAvailable;
    }
    var stopButton = byId("stop-after-offset");
    if (!stopButton && resumeButton && resumeButton.parentElement) {
      stopButton = document.createElement("button");
      stopButton.id = "stop-after-offset";
      stopButton.type = "button";
      stopButton.className = "btn warning";
      stopButton.addEventListener("click", requestStopAfterOffset);
      resumeButton.parentElement.insertBefore(stopButton, resumeButton);
    }
    if (stopButton) {
      stopButton.hidden = !job.isOffsetSweep || ["running", "pending", "queued"].indexOf(job.status) < 0;
      stopButton.disabled = !state.apiOnline || !canStopAfterOffset(job);
      stopButton.textContent = job.offsetSweep && job.offsetSweep.stop_requested ? "已请求 · 等当前频偏完成" : "当前频偏完成后停止";
      stopButton.title = "不会中断当前 Spectre；完成当前频偏后不再启动下一频偏。已完成部分可查看并恢复。";
    }
    var errorPanel = byId("job-error-panel");
    if (errorPanel) {
      if (job.error) {
        var error = job.error;
        errorPanel.hidden = false;
        errorPanel.innerHTML = '<strong>' + escapeHtml(error.code || "ISF-E9000") + '</strong><span>' + escapeHtml(error.message || job.message || "任务失败") + '</span><a href="errors.html?code=' + encodeURIComponent(error.code || "ISF-E9000") + '">排障说明</a>';
      } else {
        errorPanel.hidden = true;
        errorPanel.textContent = "";
      }
    }
    var resultLink = byId("view-results");
    if (resultLink) {
      resultLink.href = job.id && job.resultAvailable ? "results.html?id=" + encodeURIComponent(job.id) : "#";
      resultLink.setAttribute("aria-disabled", job.id && job.resultAvailable ? "false" : "true");
      resultLink.textContent = job.isOffsetSweep ? "查看已完成频偏" : "查看当前结果";
    }
  }

  async function refreshRemoteJob(feedback) {
    var input = byId("job-id-input");
    var id = input && input.value.trim() ? input.value.trim() : readStorage(STORAGE.jobId, "");
    if (!id) {
      if (feedback) toast("没有可加载的任务 ID", "error");
      return;
    }
    if (!state.apiOnline) {
      var cached = readStorage(STORAGE.job, null);
      renderJob(cached ? jobFromResponse(cached) : unavailableJob(id, "API 不可达，任务未加载"));
      if (feedback) toast("API 不可达，无法刷新任务", "error");
      return;
    }
    try {
      var response = await apiRequest("/api/jobs/" + encodeURIComponent(id));
      var job = jobFromResponse(response);
      saveJob(job);
      renderJob(job);
      if (feedback) toast("任务状态已更新");
      if (job.status === "running" || job.status === "pending") scheduleJobPoll();
    } catch (error) {
      if (feedback) reportError(error, "无法读取任务 " + id);
    }
  }

  function scheduleJobPoll() {
    if (state.pollTimer) window.clearTimeout(state.pollTimer);
    state.pollTimer = window.setTimeout(function () { refreshRemoteJob(false); }, 2000);
  }

  async function startRemoteRun(button) {
    var config = flatConfig(readStorage(STORAGE.config, DEFAULT_CONFIG));
    if (!state.apiOnline) {
      toast("API 不可达，未创建运行任务", "error");
      return;
    }
    if (state.executionPending) return;
    state.executionPending = true;
    state.workspaceStateSequence += 1;
    setButtonBusy(button, true);
    try {
      var jobs = await apiRequest("/api/jobs", { timeout: 5000 });
      var active = activeJobRows(jobs);
      if (active.length) {
        toast("已有 queued/running 任务，未启动新任务：" + active.map(function (job) { return job.id || job.job_id || "unknown"; }).join("、"), "error", 7000);
        return;
      }
      var requestConfig = executionRequestConfig(config);
      var preflight = await requestPreflight(requestConfig, false);
      if (!preflight || !preflight.ok) {
        if (preflight) renderPreflight(preflight);
        reportError({ code: "ISF-E1101", message: "预检未通过，未创建运行任务" });
        return;
      }
      var runConfig = configPayload(requestConfig);
      var response = await apiRequest("/api/run", {
        method: "POST",
        body: {
          config: runConfig,
          require_preflight: true,
          discovery_job_id: state.currentJob && state.currentJob.id,
          run_root: runConfig.run_root || undefined,
          bundle_path: runConfig.bundle_path || (runConfig.auto && runConfig.auto.bundle_path) || undefined
        },
        timeout: 15000
      });
      var job = jobFromResponse(response);
      saveJob(job);
      renderJob(job);
      toast("分析任务已提交：" + job.id);
      scheduleJobPoll();
    } catch (error) {
      reportError(error, "运行接口失败，未创建任务");
    } finally {
      state.executionPending = false;
      setButtonBusy(button, false);
    }
  }

  function initDiscover() {
    var config = flatConfig(readStorage(STORAGE.config, DEFAULT_CONFIG));
    var storedJob = readStorage(STORAGE.job, null);
    renderJob(storedJob ? jobFromResponse(storedJob) : unavailableJob(readStorage(STORAGE.jobId, ""), "任务未加载"));
    var cachedPreflight = readStorage(STORAGE.preflight, null);
    if (cachedPreflight) {
      renderPreflight(cachedPreflight);
      renderDiscoveryInputs(cachedPreflight.detected && (cachedPreflight.detected.maestro_pair || cachedPreflight.detected.inputs), config);
      var readiness = byId("run-readiness");
      if (readiness) readiness.textContent = cachedPreflight.ok ? "预检已通过，可开始运行。" : "预检未通过，请返回配置页补充 FAIL 项。";
    }
    if (byId("run-project-name")) byId("run-project-name").textContent = config.projectName;
    if (byId("run-profile-copy")) byId("run-profile-copy").textContent = (config.offsetMode === "single" ? config.offsetValue + " " + config.offsetUnit : "多频偏 · " + config.offsetMode) + " · k = 0…" + config.harmonics + " · " + config.phaseSamples + " samples";
    if (byId("run-command")) byId("run-command").textContent = config.vcoCommand || config.submitCommand || "Bundled runner · stage commands resolved at runtime";

    var refresh = byId("refresh-job");
    if (refresh) refresh.addEventListener("click", function () { refreshRemoteJob(true); });
    var input = byId("job-id-input");
    if (input) input.addEventListener("change", function () { refreshRemoteJob(true); });
    var discoverButton = byId("start-discovery");
    if (discoverButton) discoverButton.addEventListener("click", async function () {
      setButtonBusy(discoverButton, true);
      var report = await beginDiscovery(config);
      setButtonBusy(discoverButton, false);
      if (!report) return;
      renderDiscoveryInputs(report.detected && (report.detected.maestro_pair || report.detected.inputs), config);
      var readiness = byId("run-readiness");
      if (readiness) readiness.textContent = report.ok ? "预检已通过，可开始运行。" : "发现存在 FAIL，请返回配置页处理。";
    });
    var runButton = byId("start-run");
    if (runButton) runButton.addEventListener("click", function () { startRemoteRun(runButton); });
    var resumeButton = byId("resume-job");
    if (resumeButton) resumeButton.addEventListener("click", async function () {
      var id = state.currentJob && state.currentJob.id;
      if (!id) return reportError({ code: "ISF-E5001", message: "没有可恢复的任务 ID" });
      if (state.executionPending) return;
      state.executionPending = true;
      state.workspaceStateSequence += 1;
      setButtonBusy(resumeButton, true);
      try {
        var jobs = await apiRequest("/api/jobs", { timeout: 5000 });
        var active = activeJobRows(jobs);
        if (active.length) {
          toast("已有 queued/running 任务，未恢复另一任务：" + active.map(function (job) { return job.id || job.job_id || "unknown"; }).join("、"), "error", 7000);
          return;
        }
        // This explicit click resumes only the selected job; the backend still
        // validates its frozen execution contract and authority, not this form.
        await apiRequest("/api/jobs/" + encodeURIComponent(id) + "/resume", {
          method: "POST",
          body: { background: true },
          timeout: 10000
        });
        toast("恢复任务已提交：" + id);
        await refreshRemoteJob(false);
        scheduleJobPoll();
      } catch (error) {
        reportError(error, "任务恢复失败");
      } finally {
        state.executionPending = false;
        setButtonBusy(resumeButton, false);
      }
    });
    if (state.apiOnline && !readStorage(STORAGE.workspaceState, null)) requestPreflight(config, false).then(function (report) {
      if (report) {
        renderDiscoveryInputs(report.detected && (report.detected.maestro_pair || report.detected.inputs), config);
        var readiness = byId("run-readiness");
        if (readiness) readiness.textContent = report.ok ? "预检已通过，可开始运行。" : "发现存在 FAIL，请返回配置页处理。";
      }
    });
  }

  function unavailableResults(id, message) {
    return {
      id: String(id || ""),
      source: "error",
      error: String(message || "结果不可用"),
      runKind: "unavailable",
      signoff: false,
      metadata: {},
      provenance: null,
      summaryRows: [],
      dualTb: {},
      metrics: { frequency: null, period: null, offsetHz: null, mdc: null, mdyn: null, qsum: "N/A", errorDb: null, trendDb: null, pairErrorDb: null, pairPredictedAsd: null, pairReferenceAsd: null },
      charts: { comparison: null, voltage: [], current: [], isf: [], modulation: [], product: [], phasor: [] },
      errors: [],
      files: []
    };
  }

  function toPointArray(values) {
    if (!Array.isArray(values)) return [];
    return values.map(function (value, index) {
      if (typeof value === "number") return { x: values.length > 1 ? index / (values.length - 1) : 0, y: value };
      if (Array.isArray(value)) return { x: finite(value[0], index), y: finite(value[1], 0) };
      var x = value.x;
      if (x === undefined) x = value.phase;
      if (x === undefined) x = value.theta;
      if (x === undefined) x = value.t;
      if (x === undefined) x = values.length > 1 ? index / (values.length - 1) : 0;
      var y = value.y;
      if (y === undefined) y = value.value;
      if (y === undefined) y = value.v;
      return { x: finite(x, index), y: finite(y, 0) };
    });
  }

  function normalizeLineGroup(source, fallback, names) {
    if (!source) return fallback;
    var output = [];
    if (Array.isArray(source) && source.length && source[0] && (source[0].values || source[0].data || source[0].points)) {
      source.forEach(function (series, index) {
        output.push({
          name: series.name || series.label || (names[index] && names[index].name) || "Series " + (index + 1),
          color: series.color || (names[index] && names[index].color) || fallback[index % fallback.length].color,
          values: toPointArray(series.values || series.data || series.points),
          dash: Boolean(series.dash || series.dashed)
        });
      });
    } else if (typeof source === "object" && !Array.isArray(source)) {
      (names || []).forEach(function (descriptor, index) {
        var candidates = descriptor.keys || [descriptor.name, descriptor.name.toLowerCase()];
        var values = null;
        candidates.some(function (key) {
          if (source[key] !== undefined) { values = source[key]; return true; }
          return false;
        });
        if (values) output.push({ name: descriptor.name, color: descriptor.color, values: toPointArray(values), dash: Boolean(descriptor.dash) });
      });
    }
    return output.length ? output : fallback;
  }

  function median(values) {
    if (!values.length) return null;
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    var middle = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[middle] : 0.5 * (sorted[middle - 1] + sorted[middle]);
  }

  function rowSeries(rows, key) {
    return (rows || []).reduce(function (values, row, index) {
      var y = Number(row[key]);
      if (!Number.isFinite(y)) return values;
      var wt = Number(row.wt_rad);
      var x = Number.isFinite(wt) ? wt / (Math.PI * 2) : ((rows.length > 1) ? index / (rows.length - 1) : 0);
      values.push({ x: x, y: y });
      return values;
    }, []);
  }

  function firstSeries(rows, keys) {
    for (var index = 0; index < keys.length; index += 1) {
      var values = rowSeries(rows, keys[index]);
      if (values.length) return values;
    }
    return [];
  }

  function wrappedDegrees(value) {
    var result = (finite(value, 0) + 180) % 360;
    if (result < 0) result += 360;
    return result - 180;
  }

  function hasOwnedSignoff(source, jobId) {
    var provenance = source && source.provenance;
    var dualTb = source && source.dual_tb;
    return Boolean(
      source &&
      String(source.run_kind || "").toLowerCase() === "signoff" &&
      dualTb && dualTb.signoff === true &&
      provenance &&
      String(provenance.job_id || "") === String(jobId || source.job_id || source.id || "") &&
      provenance.manifest_sha256 &&
      Number(provenance.psf_runs) > 0
    );
  }

  function ownedOffsetHz(source, row) {
    var value = finiteOrNull(row && (row.offset_Hz !== undefined ? row.offset_Hz : row.offset_hz));
    var metadata = source && source.dual_tb && source.dual_tb.metadata || source && source.metadata || {};
    if (value === null) value = finiteOrNull(source && (source.offset_Hz !== undefined ? source.offset_Hz : source.offset_hz));
    if (value === null) value = finiteOrNull(metadata.offset_Hz !== undefined ? metadata.offset_Hz : metadata.offset_hz);
    return value !== null && value > 0 ? value : null;
  }

  function resultOperatingPoints(source) {
    var groups = [];
    (source.summary || []).forEach(function (row) {
      var identity = String(row.operating_point_id || row.point || "");
      var group = groups.find(function (item) { return item.id === identity; });
      if (!group) { group = { id: identity, corner: row.corner || "", parameters: row.operating_parameters || {}, rows: [] }; groups.push(group); }
      group.rows.push({ row: row, offset: ownedOffsetHz(source, row), point: String(row.point || "") });
    });
    groups.forEach(function (group) { group.rows.sort(function (a, b) { return (a.offset === null ? Infinity : a.offset) - (b.offset === null ? Infinity : b.offset); }); });
    return groups.sort(function (a, b) { return a.id.localeCompare(b.id); });
  }

  function selectResultSample(source, requested, offset) {
    var groups = resultOperatingPoints(source);
    var exact = (source.summary || []).find(function (row) { return String(row.point) === String(requested); });
    var identity = exact ? String(exact.operating_point_id || exact.point) : String(requested || "");
    var group = groups.find(function (item) { return item.id === identity; }) || groups[0];
    if (!group || !group.rows.length) return null;
    var sample = group.rows.find(function (item) { return String(item.offset) === String(offset); });
    if (!sample && exact) sample = group.rows.find(function (item) { return item.point === String(exact.point); });
    return { group: group, groups: groups, sample: sample || group.rows[0] };
  }

  function hasTotalNoiseReference(summary) {
    var reference = summary && summary.spectre_noise_reference;
    return !!reference && reference.scope === "whole_vco" && reference.schema_version === 1;
  }

  function noiseSummaryForDisplay(summary) {
    var row = Object.assign({}, summary || {});
    if (!hasTotalNoiseReference(row)) {
      row.spectre_total_pm_asd_V_rtHz = null;
      row.spectre_total_dBc_Hz = null;
      row.spectre_noise_contributions = [];
    }
    // History remains immutable; only its display copy follows the current
    // calculation scope. Cached scalar/JSON fields must not bypass the same
    // thermal evidence gate used by cards, curves and contribution pies.
    var thermal = thermalResult(row), recorded = row.thermal_noise || row.thermal_noise_json;
    if (recorded || Object.prototype.hasOwnProperty.call(row, "thermal_dBc_Hz")) row.thermal_dBc_Hz = predictedNoiseDbc(row, "thermal");
    if (recorded || Object.prototype.hasOwnProperty.call(row, "predicted_total_dBc_Hz")) row.predicted_total_dBc_Hz = predictedNoiseDbc(row, "both");
    if (recorded && !thermal) {
      if (typeof recorded === "string") {
        try { recorded = JSON.parse(recorded); } catch (_) { recorded = {}; }
      }
      if (!isPlainObject(recorded)) recorded = {};
      row.thermal_noise = {schema_version: 1, status: "unavailable",
        previous_scope: recorded.previous_scope || recorded.scope || "unknown",
        reason: recorded.status === "unavailable" && recorded.reason || "该历史 thermal 预算不符合当前 gm + LC 计算范围；请重新运行。"};
      if (Object.prototype.hasOwnProperty.call(row, "thermal_noise_json")) row.thermal_noise_json = JSON.stringify(row.thermal_noise);
    }
    return row;
  }

  function noiseSeries(source, operatingPoint, scope, cumulativeK) {
    var group = resultOperatingPoints(source).find(function (item) { return item.id === operatingPoint; });
    if (!group) return { phase: [], harmonics: [], orders: [] };
    var offsets = (source.offset_sweep && source.offset_sweep.offsets_hz || group.rows.map(function (item) { return item.offset; })).filter(function (value) { return typeof value === "number" && Number.isFinite(value) && value > 0; });
    offsets = offsets.filter(function (value, index) { return offsets.indexOf(value) === index; }).sort(function (a, b) { return a - b; });
    var orders = [];
    function harmonicRows(sample) { return sample && source.points && source.points[sample.point] && source.points[sample.point].harmonics || []; }
    group.rows.forEach(function (sample) { harmonicRows(sample).forEach(function (row) { var k = finiteOrNull(row.k); if (k !== null && k >= 0 && Number.isInteger(k) && orders.indexOf(k) < 0) orders.push(k); }); });
    orders.sort(function (a, b) { return a - b; });
    function values(key, order) {
      return offsets.map(function (hz) {
        var matches = group.rows.filter(function (item) { return item.offset === hz; });
        var sample = matches.length === 1 ? matches[0] : null;
        var row = sample && sample.row;
        if (key === "spectre_total_dBc_Hz" && !hasTotalNoiseReference(row)) row = null;
        if (order !== undefined) { var candidates = harmonicRows(sample).filter(function (item) { return Number(item.k) === order; }); row = candidates.length === 1 ? candidates[0] : null; }
        var value = finiteOrNull(row && row[key]);
        if (order !== undefined && scope === "pair" && value !== null) {
          var pairKey = "pair_" + key;
          value = finiteOrNull(row[pairKey]) !== null ? finiteOrNull(row[pairKey]) : value + 10 * Math.log10(2);
        }
        return { x: hz, y: value };
      });
    }
    var pair = scope === "pair";
    var phase = [
      { name: "ISF 计算 · " + (pair ? "双器件 flicker" : "单器件 flicker"), color: COLORS.teal, values: values(pair ? "predicted_pair_dBc_Hz" : "dynamic_dBc_Hz") },
      { name: "VCO 总仿真相噪 · PM-SSB（全部噪声源）", color: COLORS.orange, values: values("spectre_total_dBc_Hz"), dash: true }
    ];
    var palette = [COLORS.blue, COLORS.purple, COLORS.amber, COLORS.orange, COLORS.gray];
    var harmonics = orders.map(function (k, index) { return { id: "flicker-k-" + k, name: "Flicker k=" + k + " 单阶等效 · " + (pair ? "双器件" : "单器件"), color: palette[index % palette.length], values: values("harmonic_equivalent_dBc_Hz", k) }; });
    harmonics.push({ id: "flicker-total", name: "Flicker 全部阶相干总和 · " + (pair ? "双器件" : "单器件"), color: COLORS.teal, values: values(pair ? "predicted_pair_dBc_Hz" : "dynamic_dBc_Hz"), total: true });
    if (cumulativeK !== null && cumulativeK !== undefined && orders.indexOf(Number(cumulativeK)) >= 0) harmonics.push({ name: "累计 k=0…" + cumulativeK, color: "#222c35", dash: true, values: values("cumulative_dBc_Hz", Number(cumulativeK)) });
    return { phase: phase, harmonics: harmonics, orders: orders };
  }

  function thermalResult(summary) {
    var result = summary && summary.thermal_noise;
    if (result && Array.isArray(result.sources) && result.sources.some(function (source) { return source && (source.object_id || source.source_basis === "paired_noise_TB_transient_state_white_PSD"); })) return null;
    return result && result.schema_version === 1 && result.status === "available" && result.scope === "gm_pair_and_lc_network" && result.passive_scope === "passive_only_lc_roots_v1" ? result : null;
  }

  function addNoiseDbc(values) {
    // Independent sources add in linear power; missing branches stay missing.
    if (!values.length || values.some(function (value) { return finiteOrNull(value) === null; })) return null;
    var maximum = Math.max.apply(null, values);
    return maximum + 10 * Math.log10(values.reduce(function (sum, value) { return sum + Math.pow(10, (value - maximum) / 10); }, 0));
  }

  function predictedNoiseDbc(summary, kind) {
    summary = summary || {};
    var flicker = finiteOrNull(summary.predicted_pair_dBc_Hz);
    var thermal = thermalResult(summary);
    var thermalDbc = finiteOrNull(thermal && thermal.phase_noise_dBc_Hz);
    return kind === "flicker" ? flicker : kind === "thermal" ? thermalDbc : addNoiseDbc([flicker, thermalDbc]);
  }

  function thermalBandPsd(summary, band, absoluteBand) {
    var thermal = thermalResult(summary);
    if (!thermal || !Array.isArray(thermal.sources) || !thermal.sources.length) return null;
    var power = 0, found = false;
    // Each exported input-band entry is already a power contribution. No
    // complex amplitudes from different independent input bands are combined.
    for (var i = 0; i < thermal.sources.length; i += 1) {
      var contributions = thermal.sources[i].input_band_contributions;
      if (!Array.isArray(contributions)) return null;
      for (var j = 0; j < contributions.length; j += 1) {
        var inputBand = finiteOrNull(contributions[j].input_band);
        if (inputBand === null || (absoluteBand ? Math.abs(inputBand) : inputBand) !== band) continue;
        var psd = finiteOrNull(contributions[j].psd_V2_Hz);
        if (psd === null || psd < 0) return null;
        found = true; power += psd;
      }
    }
    return found ? power : null;
  }

  function outputPsdDbc(power, summary) {
    var carrier = finiteOrNull(summary && summary.carrier_peak_V);
    return power !== null && power > 0 && carrier !== null && carrier > 0 ? 10 * Math.log10(2 * power / (carrier * carrier)) : null;
  }

  function thermalBandDbc(summary, band) {
    return outputPsdDbc(thermalBandPsd(summary, band, false), summary);
  }

  function flickerHarmonicPairPsd(source, summary, order) {
    var point = source.points && source.points[summary.point];
    var matches = (point && point.harmonics || []).filter(function (row) { return finiteOrNull(row.k) === order; });
    var carrier = finiteOrNull(summary.carrier_peak_V);
    if (matches.length !== 1 || carrier === null || carrier <= 0) return null;
    var row = matches[0], dbc = finiteOrNull(row.pair_harmonic_equivalent_dBc_Hz);
    if (dbc === null && finiteOrNull(row.harmonic_equivalent_dBc_Hz) !== null) dbc = Number(row.harmonic_equivalent_dBc_Hz) + 10 * Math.log10(2);
    if (dbc !== null) return Math.pow(10, dbc / 10) * carrier * carrier / 2;
    // A recorded zero is distinguishable from a missing dB value (-infinity).
    if (finiteOrNull(row.Q_mag_V_rtHz) === 0 || (finiteOrNull(row.Q_real_V_rtHz) === 0 && finiteOrNull(row.Q_imag_V_rtHz) === 0)) return 0;
    return null;
  }

  function resultNoiseSeries(source, operatingPoint, kind, cumulativeK) {
    var existing = noiseSeries(source, operatingPoint, "pair", cumulativeK);
    var group = resultOperatingPoints(source).find(function (item) { return item.id === operatingPoint; });
    if (!group || !existing.phase.length) return existing;
    var offsets = existing.phase[0].values.map(function (point) { return point.x; });
    function values(calculate) { return offsets.map(function (hz) {
      var rows = group.rows.filter(function (sample) { return sample.offset === hz; });
      return { x: hz, y: rows.length === 1 ? calculate(rows[0].row) : null };
    }); }
    var flicker = { id: "flicker", name: "ISF 双器件 flicker", color: "#555", dashPattern: "8 4", values: values(function (row) { return predictedNoiseDbc(row, "flicker"); }) };
    var thermal = { id: "thermal", name: "ISF thermal · gm 双器件及 LC", color: "#222", dashPattern: "3 4", values: values(function (row) { return predictedNoiseDbc(row, "thermal"); }) };
    var total = { id: "predicted-total", name: "ISF flicker + thermal · 功率和", color: "#000", total: true, values: values(function (row) { return predictedNoiseDbc(row, "both"); }) };
    var reference = Object.assign({}, existing.phase[1], { id: "spectre-total", color: "#777", dash: false, dashPattern: "12 3 2 3", total: true });
    var phase = kind === "flicker" ? [flicker, reference] : kind === "thermal" ? [thermal, reference] : [flicker, thermal, total, reference];
    var harmonics = kind === "flicker" ? existing.harmonics.map(function (item, index) { return Object.assign({}, item, { color: item.total ? "#000" : "#555", dashPattern: ["", "8 4", "2 3", "8 3 2 3"][index % 4] }); }) : [];
    if (kind !== "flicker") {
      var bands = [];
      group.rows.forEach(function (sample) {
        var result = thermalResult(sample.row);
        (result && result.sources || []).forEach(function (source) { (source.input_band_contributions || []).forEach(function (part) {
          var band = finiteOrNull(part.input_band);
          if (band !== null && Number.isInteger(band) && bands.indexOf(Math.abs(band)) < 0) bands.push(Math.abs(band));
        }); });
      });
      bands.sort(function (a, b) { return a - b; }).filter(function (band) { return kind === "thermal" || existing.orders.indexOf(band) >= 0; }).forEach(function (band, index) {
        harmonics.push({ id: (kind === "both" ? "combined-k-" : "thermal-band-") + band, name: kind === "both" ? "k=" + band + " · Flicker 等效 + thermal |m| 功率" : "Thermal 独立输入频带 |m|=" + band + " · 功率和", color: "#777", dashPattern: ["3 3", "9 3", "9 3 2 3"][index % 3], values: values(function (row) {
          var thermalPower = thermalBandPsd(row, band, true);
          if (kind === "thermal") return outputPsdDbc(thermalPower, row);
          var flickerPower = flickerHarmonicPairPsd(source, row, band);
          return outputPsdDbc(thermalPower === null || flickerPower === null ? null : thermalPower + flickerPower, row);
        }) });
      });
      harmonics.push(kind === "both" ? Object.assign({}, total) : Object.assign({}, thermal, { total: true }));
    }
    return { phase: phase, harmonics: harmonics, orders: existing.orders };
  }

  function normalizeCanonicalResult(source, result, id, pointOverride) {
    if (!Array.isArray(source.summary) || !source.points || typeof source.points !== "object") return false;
    var pointNames = Object.keys(source.points).sort();
    if (!pointNames.length) return false;
    var queryPoint = new URLSearchParams(window.location.search).get("point");
    var offsetSelector = byId("result-offset-select");
    var choice = selectResultSample(source, pointOverride || queryPoint, offsetSelector && offsetSelector.value);
    if (!choice) return false;
    var selected = choice.sample.point;
    var pointSelector = byId("result-point-select");
    if (pointSelector) {
      pointSelector.innerHTML = choice.groups.map(function (group) {
        return '<option value="' + escapeHtml(group.id) + '"' + (group.id === choice.group.id ? " selected" : "") + ">" + escapeHtml(group.id + (group.corner ? " · " + group.corner : "")) + "</option>";
      }).join("");
    }
    if (offsetSelector) offsetSelector.innerHTML = choice.group.rows.map(function (sample) { return '<option value="' + escapeHtml(sample.offset) + '"' + (sample.point === selected ? ' selected' : '') + '>' + escapeHtml(sample.offset === null ? "频偏未提供" : formatOffsetHz(sample.offset)) + '</option>'; }).join("");
    var summary = noiseSummaryForDisplay(choice.sample.row);
    var point = source.points[selected] || {};
    var workpoint = Array.isArray(point.workpoint) ? point.workpoint : [];
    var harmonics = Array.isArray(point.harmonics) ? point.harmonics : [];
    var h0 = harmonics.filter(function (row) { return finite(row.k, -1) === 0; })[0] || {};
    var finalHarmonic = harmonics.length ? harmonics[harmonics.length - 1] : {};
    var fixedOffsetRows = source.summary.filter(function (row) { return ownedOffsetHz(source, row) === choice.sample.offset; });
    var errors = dynamicErrorValues(fixedOffsetRows);
    var center = median(errors);
    var trend = center === null || errors.length !== fixedOffsetRows.length
      ? null
      : Math.max.apply(null, errors.map(function (value) { return Math.abs(value - center); }));
    var qre = finiteOrNull(finalHarmonic.cum_real_V_rtHz !== undefined ? finalHarmonic.cum_real_V_rtHz : finalHarmonic.cum_real);
    var qim = finiteOrNull(finalHarmonic.cum_imag_V_rtHz !== undefined ? finalHarmonic.cum_imag_V_rtHz : finalHarmonic.cum_imag);
    result.id = String(id || source.id || "imported");
    result.point = selected;
    result.operatingPointId = choice.group.id;
    result.sampleSummary = summary;
    result.sampleHarmonics = harmonics;
    result.offsetSweep = source.offset_sweep || null;
    result.rawCanonical = source;
    result.source = "api";
    result.metrics.frequency = finiteOrNull(summary.f0_Hz);
    result.metrics.period = result.metrics.frequency !== null && result.metrics.frequency > 0 ? 1 / result.metrics.frequency : null;
    result.metrics.offsetHz = resultOffsetHz(source, summary);
    result.metrics.mdc = finiteOrNull(
      summary.mDC_cycle_mean_A_rtHz !== undefined ? summary.mDC_cycle_mean_A_rtHz
        : (h0.Mdc_mag_A_rtHz !== undefined ? h0.Mdc_mag_A_rtHz : h0.mdc_mag)
    );
    result.metrics.mdyn = finiteOrNull(
      summary.mDyn_rms_A_rtHz !== undefined ? summary.mDyn_rms_A_rtHz
        : (h0.Mdyn_mag_A_rtHz !== undefined ? h0.Mdyn_mag_A_rtHz : h0.mdyn_mag)
    );
    result.metrics.qsum = qre === null || qim === null
      ? "N/A"
      : qre.toExponential(3) + (qim >= 0 ? "+j" : "−j") + Math.abs(qim).toExponential(3);
    result.metrics.errorDb = finiteOrNull(summary.dynamic_error_dB);
    result.metrics.trendDb = trend;
    result.metrics.pairErrorDb = finiteOrNull(summary.pair_error_dB);
    result.metrics.pairPredictedAsd = finiteOrNull(summary.predicted_pair_pm_asd_V_rtHz);
    result.metrics.pairReferenceAsd = finiteOrNull(summary.spectre_pair_pm_asd_V_rtHz);
    result.metrics.calculatedDbc = finiteOrNull(summary.dynamic_dBc_Hz);
    result.metrics.spectreDbc = finiteOrNull(summary.spectre_fn_dBc_Hz);
    result.metrics.totalSpectreDbc = hasTotalNoiseReference(summary) ? finiteOrNull(summary.spectre_total_dBc_Hz) : null;
    result.metrics.pairCalculatedDbc = finiteOrNull(summary.predicted_pair_dBc_Hz);
    result.metrics.pairSpectreDbc = finiteOrNull(summary.spectre_pair_dBc_Hz);
    result.signoff = hasOwnedSignoff(source, result.id);
    result.runKind = result.signoff ? "signoff" : (source.run_kind === "plan" ? "plan" : "replay");
    result.provenance = source.provenance || null;
    result.metadata = (source.dual_tb && source.dual_tb.metadata) || {};
    result.summaryRows = source.summary;
    result.dualTb = source.dual_tb || {};
    result.files = [];
    var rowCounts = {
      "vco_results.csv": source.dual_tb && source.dual_tb.vco_rows,
      "replica_results.csv": source.dual_tb && source.dual_tb.replica_rows,
      "workpoint.csv": source.dual_tb && source.dual_tb.workpoint_rows,
      "metadata.json": 1,
      "manifest.json": 1
    };
    var provenanceFiles = source.provenance && source.provenance.files;
    if (Array.isArray(provenanceFiles)) {
      result.files = provenanceFiles.filter(function (file) {
        return file && file.status === "verified";
      }).map(function (file) {
        return {
          name: file.name,
          path: file.path,
          type: String(file.name || "data").split(".").pop(),
          rows: rowCounts[file.name] || "—",
          size: file.bytes,
          hash: String(file.sha256 || "").slice(0, 12),
          status: "verified"
        };
      });
    }
    var comparisonSeries = [];
    var pairValues = fixedOffsetRows.map(function (row) {
      var value = finiteOrNull(row.pair_error_dB);
      return value === null ? NaN : value;
    });
    if (pairValues.some(Number.isFinite)) {
      comparisonSeries.push({ name: "双器件 flicker", color: "#333", values: pairValues });
    }
    result.charts.comparison = {
      labels: fixedOffsetRows.map(function (row) { return String(row.operating_point_id || row.point || "point"); }),
      series: comparisonSeries
    };

    result.charts.voltage = [
      { name: "VG", color: COLORS.teal, values: firstSeries(workpoint, ["VG_V", "VG"]) },
      { name: "VD", color: COLORS.orange, values: firstSeries(workpoint, ["VD_V", "VD"]) },
      { name: "VS", color: COLORS.blue, values: firstSeries(workpoint, ["VS_V", "VS"]) },
      { name: "VB", color: COLORS.purple, values: firstSeries(workpoint, ["VB_V", "VB"]), dash: true }
    ].filter(function (series) { return series.values.length; });
    result.charts.current = [
      { name: "Main ID", color: COLORS.teal, values: firstSeries(workpoint, ["ID_main_A", "ID_drain_to_source_A"]) },
      { name: "Replica ID", color: COLORS.orange, values: firstSeries(workpoint, ["ID_replica_A"]), dash: true }
    ].filter(function (series) { return series.values.length; });
    var isfIValues = firstSeries(workpoint, ["isf_i", "isf", "ISF", "gamma"]);
    var isfQValues = firstSeries(workpoint, ["isf_q"]);
    result.charts.isf = [
      { name: "Carrier-I signed-H", color: COLORS.blue, values: isfIValues },
      { name: "Carrier-Q signed-H", color: COLORS.purple, values: isfQValues, dash: true }
    ].filter(function (series) { return series.values.length; });
    result.charts.modulation = [
      { name: "mDyn", color: COLORS.purple, values: firstSeries(workpoint, ["m_dyn_A_rtHz", "mDyn", "m_dyn"]) },
      { name: "mDC", color: COLORS.amber, values: firstSeries(workpoint, ["m_DC_A_rtHz", "mDC", "m_dc"]), dash: true }
    ].filter(function (series) { return series.values.length; });
    var productIValues = firstSeries(workpoint, ["product_i", "product", "isf_product"]);
    var productQValues = firstSeries(workpoint, ["product_q"]);
    result.charts.product = [
      { name: "ISF-I × mDyn", color: COLORS.orange, values: productIValues },
      { name: "ISF-Q × mDyn", color: COLORS.purple, values: productQValues, dash: true }
    ].filter(function (series) { return series.values.length; });
    result.charts.phasor = harmonics.reduce(function (values, row, index) {
      var re = finiteOrNull(row.cum_real_V_rtHz !== undefined ? row.cum_real_V_rtHz : row.cum_real);
      var im = finiteOrNull(row.cum_imag_V_rtHz !== undefined ? row.cum_imag_V_rtHz : row.cum_imag);
      if (re !== null && im !== null) values.push({ k: finite(row.k, index), re: re, im: im });
      return values;
    }, []);
    var referenceAsd = finiteOrNull(summary.spectre_pair_pm_asd_V_rtHz);
    result.errors = harmonics.map(function (row, index) {
      var refMag = finiteOrNull(row.Mdc_mag_A_rtHz);
      var dynMag = finiteOrNull(row.Mdyn_mag_A_rtHz);
      var refPhase = finiteOrNull(row.Mdc_phase_deg);
      var dynPhase = finiteOrNull(row.Mdyn_phase_deg);
      var amplitude = finiteOrNull(row.A_k);
      if (amplitude === null && refMag !== null && refMag > 0 && dynMag !== null) amplitude = dynMag / refMag;
      var delta = finiteOrNull(row.delta_k_deg);
      if (delta === null && dynPhase !== null && refPhase !== null) delta = wrappedDegrees(dynPhase - refPhase);
      var ckReal = finiteOrNull(row.Ck_real);
      var ckImag = finiteOrNull(row.Ck_imag);
      if (ckReal === null && amplitude !== null && delta !== null) ckReal = amplitude * Math.cos(delta * Math.PI / 180);
      if (ckImag === null && amplitude !== null && delta !== null) ckImag = amplitude * Math.sin(delta * Math.PI / 180);
      var cumulativeRe = finiteOrNull(row.cum_real_V_rtHz !== undefined ? row.cum_real_V_rtHz : row.cum_real);
      var cumulativeIm = finiteOrNull(row.cum_imag_V_rtHz !== undefined ? row.cum_imag_V_rtHz : row.cum_imag);
      var cumulative = cumulativeRe === null || cumulativeIm === null ? null : Math.hypot(cumulativeRe, cumulativeIm);
      var qReal = finiteOrNull(row.Q_real_V_rtHz !== undefined ? row.Q_real_V_rtHz : row.Qk_real_V_rtHz);
      var qImag = finiteOrNull(row.Q_imag_V_rtHz !== undefined ? row.Q_imag_V_rtHz : row.Qk_imag_V_rtHz);
      var qMag = finiteOrNull(row.Q_mag_V_rtHz);
      var qPhase = finiteOrNull(row.Q_phase_deg);
      if (qMag === null && qReal !== null && qImag !== null) qMag = Math.hypot(qReal, qImag);
      if (qPhase === null && qReal !== null && qImag !== null) qPhase = Math.atan2(qImag, qReal) * 180 / Math.PI;
      var equivalentDbc = finiteOrNull(row.pair_harmonic_equivalent_dBc_Hz);
      if (equivalentDbc === null && finiteOrNull(row.harmonic_equivalent_dBc_Hz) !== null) equivalentDbc = Number(row.harmonic_equivalent_dBc_Hz) + 10 * Math.log10(2);
      var cumulativeDbc = finiteOrNull(row.pair_cumulative_dBc_Hz);
      if (cumulativeDbc === null && finiteOrNull(row.cumulative_dBc_Hz) !== null) cumulativeDbc = Number(row.cumulative_dBc_Hz) + 10 * Math.log10(2);
      var cumulativeError = finiteOrNull(row.pair_cumulative_error_dB);
      if (cumulativeError === null && cumulativeDbc !== null && finiteOrNull(summary.spectre_pair_dBc_Hz) !== null) cumulativeError = cumulativeDbc - Number(summary.spectre_pair_dBc_Hz);
      if (cumulativeError === null && cumulative !== null && cumulative > 0 && referenceAsd !== null && referenceAsd > 0) cumulativeError = 20 * Math.log10(Math.sqrt(2) * cumulative / referenceAsd);
      return {
        k: finite(row.k, index),
        refMag: refMag,
        refPhase: refPhase,
        dynMag: dynMag,
        dynPhase: dynPhase,
        amplitude: amplitude,
        deltaDeg: delta,
        ckReal: ckReal,
        ckImag: ckImag,
        qMag: qMag,
        qPhase: qPhase,
        equivalentDbc: equivalentDbc,
        cumulativeDbc: cumulativeDbc,
        magErrorDb: amplitude !== null && amplitude > 0 ? 20 * Math.log10(amplitude) : null,
        phaseError: delta === null ? null : Math.abs(delta),
        cumulativeErrorDb: cumulativeError
      };
    });
    return true;
  }

  function normalizeResults(payload, id, pointOverride) {
    var source = payload && (payload.result || payload.data) ? (payload.result || payload.data) : (payload || {});
    if (!source || typeof source !== "object") return unavailableResults(id, "结果 payload 无效");
    var result = unavailableResults(id, "");
    delete result.error;
    result.source = "api";
    if (normalizeCanonicalResult(source, result, id, pointOverride)) return result;
    result.id = String(source.id || source.job_id || source.run_id || id || result.id);
    result.source = "api";
    result.signoff = hasOwnedSignoff(source, result.id);
    result.runKind = result.signoff ? "signoff" : ((source.run_kind || source.runKind) === "plan" ? "plan" : "replay");
    result.provenance = source.provenance || null;
    result.metadata = source.metadata || (source.dual_tb && source.dual_tb.metadata) || {};
    result.summaryRows = Array.isArray(source.summary) ? source.summary : [];
    result.dualTb = source.dual_tb || {};
    result.files = [];
    var metrics = source.metrics || source.summary || source;
    result.metrics.frequency = metrics.frequency || metrics.frequency_hz || metrics.f0 || result.metrics.frequency;
    result.metrics.period = metrics.period || metrics.period_s || result.metrics.period;
    result.metrics.offsetHz = resultOffsetHz(source, metrics);
    result.metrics.mdc = finiteOrNull(metrics.mdc !== undefined ? metrics.mdc : metrics.m_dc);
    result.metrics.mdyn = finiteOrNull(metrics.mdyn !== undefined ? metrics.mdyn : metrics.m_dyn_rms);
    result.metrics.qsum = metrics.qsum || metrics.q_sum || metrics.cumulative_q || result.metrics.qsum;
    result.metrics.errorDb = finiteOrNull(metrics.errorDb !== undefined ? metrics.errorDb : (metrics.closure_error_db !== undefined ? metrics.closure_error_db : (metrics.error_db !== undefined ? metrics.error_db : metrics.closure_error)));
    var trendValue = metrics.trendDb !== undefined ? metrics.trendDb : (metrics.trend_delta_db !== undefined ? metrics.trend_delta_db : metrics.golden_delta_db);
    result.metrics.trendDb = trendValue === undefined || trendValue === null || trendValue === "" ? null : finite(trendValue, null);
    var pairError = metrics.pairErrorDb !== undefined ? metrics.pairErrorDb : metrics.pair_error_dB;
    result.metrics.pairErrorDb = pairError === undefined || pairError === null || pairError === "" ? null : finite(pairError, null);
    result.metrics.pairPredictedAsd = finiteOrNull(metrics.predicted_pair_pm_asd_V_rtHz);
    result.metrics.pairReferenceAsd = finiteOrNull(metrics.spectre_pair_pm_asd_V_rtHz);

    var waveforms = source.waveforms || source.series || {};
    result.charts.voltage = normalizeLineGroup(source.voltages || waveforms.voltage || waveforms.voltages, result.charts.voltage, [
      { name: "VG", color: COLORS.teal, keys: ["VG", "vg", "v_g"] }, { name: "VD", color: COLORS.orange, keys: ["VD", "vd", "v_d"] },
      { name: "VS", color: COLORS.blue, keys: ["VS", "vs", "v_s"] }, { name: "VB", color: COLORS.purple, keys: ["VB", "vb", "v_b"], dash: true }
    ]);
    result.charts.current = normalizeLineGroup(source.currents || waveforms.current || waveforms.currents, result.charts.current, [
      { name: "Main ID", color: COLORS.teal, keys: ["main", "main_id", "id_main"] }, { name: "Replica ID", color: COLORS.orange, keys: ["replica", "replica_id", "id_replica"], dash: true }
    ]);
    result.charts.isf = normalizeLineGroup(source.isf || waveforms.isf, result.charts.isf, [
      { name: "Main ISF", color: COLORS.blue, keys: ["main", "main_isf", "isf_main"] }, { name: "Replica aligned", color: COLORS.gray, keys: ["replica", "replica_isf", "isf_replica"], dash: true }
    ]);
    result.charts.modulation = normalizeLineGroup(source.modulation || waveforms.modulation, result.charts.modulation, [
      { name: "mDyn", color: COLORS.purple, keys: ["mDyn", "mdyn", "m_dyn"] }, { name: "mDC", color: COLORS.amber, keys: ["mDC", "mdc", "m_dc"], dash: true }
    ]);
    result.charts.product = normalizeLineGroup(source.product || source.isf_product || waveforms.product, result.charts.product, [
      { name: "ISF × mDyn", color: COLORS.orange, keys: ["product", "isf_x_mdyn", "values"] }
    ]);

    var phasor = source.qk_cumulative || source.phasor || (source.qk && source.qk.cumulative);
    if (Array.isArray(phasor) && phasor.length) {
      result.charts.phasor = phasor.reduce(function (values, point, index) {
        var re = finiteOrNull(Array.isArray(point) ? point[0] : (point.re !== undefined ? point.re : (point.real !== undefined ? point.real : point.x)));
        var im = finiteOrNull(Array.isArray(point) ? point[1] : (point.im !== undefined ? point.im : (point.imag !== undefined ? point.imag : point.y)));
        if (re !== null && im !== null) {
          values.push({
            k: Array.isArray(point) ? index : finite(point.k !== undefined ? point.k : point.harmonic, index),
            re: re,
            im: im
          });
        }
        return values;
      }, []);
    }

    var errors = source.errors || source.harmonic_errors || source.harmonic_closure;
    if (Array.isArray(errors) && errors.length) {
      result.errors = errors.map(function (row, index) {
        if (Array.isArray(row)) return { k: finite(row[0], index), refMag: finiteOrNull(row[1]), refPhase: finiteOrNull(row[2]), dynMag: finiteOrNull(row[3]), dynPhase: finiteOrNull(row[4]), magErrorDb: finiteOrNull(row[5]), phaseError: finiteOrNull(row[6]), cumulativeErrorDb: finiteOrNull(row[7]) };
        return {
          k: finite(row.k !== undefined ? row.k : row.harmonic, index),
          refMag: finiteOrNull(row.refMag !== undefined ? row.refMag : row.q_ref_mag),
          refPhase: finiteOrNull(row.refPhase !== undefined ? row.refPhase : row.q_ref_phase_deg),
          dynMag: finiteOrNull(row.dynMag !== undefined ? row.dynMag : row.q_dyn_mag),
          dynPhase: finiteOrNull(row.dynPhase !== undefined ? row.dynPhase : row.q_dyn_phase_deg),
          magErrorDb: finiteOrNull(row.magErrorDb !== undefined ? row.magErrorDb : row.mag_error_db),
          phaseError: finiteOrNull(row.phaseError !== undefined ? row.phaseError : row.phase_error_deg),
          cumulativeErrorDb: finiteOrNull(row.cumulativeErrorDb !== undefined ? row.cumulativeErrorDb : row.cumulative_error_db)
        };
      });
    }

    if (Array.isArray(source.files) && source.files.length) {
      result.files = source.files.filter(function (file) {
        return file && file.status === "verified";
      }).map(function (file, index) {
        return {
          name: file.name || String(file.path || "file-" + index).split("/").pop(),
          path: file.path || file.url || file.name,
          type: file.type || String(file.name || file.path || "data").split(".").pop(),
          rows: file.rows !== undefined ? file.rows : "—",
          size: file.size || file.bytes || "—",
          hash: file.hash || file.sha256 || "—",
          status: "verified",
          content: file.content || ""
        };
      });
    }
    return result;
  }

  function axisFormat(value, span) {
    var absolute = Math.abs(value);
    if ((absolute > 0 && absolute < 0.001) || absolute >= 10000) return value.toExponential(1);
    if (span < 0.1) return value.toFixed(3);
    if (span < 1) return value.toFixed(2);
    return value.toFixed(1);
  }

  function pathFor(values, xScale, yScale) {
    return values.map(function (point, index) {
      return (index ? "L" : "M") + xScale(point.x).toFixed(2) + " " + yScale(point.y).toFixed(2);
    }).join(" ");
  }

  function clearPhaseInteraction() {
    state.phaseCharts.forEach(function (chart) {
      if (chart && chart._updatePhaseCursor) chart._updatePhaseCursor(null, null);
    });
    var tooltip = byId("chart-tooltip");
    if (tooltip) tooltip.classList.remove("is-visible");
  }

  function frequencyChartMarkup(series, logX) {
    var allX = [], finiteValues = [];
    (series || []).forEach(function (item) { item.values.forEach(function (point) { if (Number.isFinite(point.x) && point.x > 0) { allX.push(point.x); if (typeof point.y === "number" && Number.isFinite(point.y)) finiteValues.push(point.y); } }); });
    if (!allX.length || !finiteValues.length) return '<div class="empty-state">当前没有可用的真实频偏相噪数据；缺失值不会补零或外推。</div>';
    var width = 820, height = 300, left = 80, right = 25, top = 20, bottom = 50;
    var transform = logX ? Math.log10 : function (x) { return x; };
    var xmin = transform(Math.min.apply(null, allX)), xmax = transform(Math.max.apply(null, allX));
    if (xmin === xmax) { xmin -= logX ? 0.5 : Math.max(1, xmax * 0.1); xmax += logX ? 0.5 : Math.max(1, xmax * 0.1); if (!logX) xmin = Math.max(0, xmin); }
    var ymin = Math.min.apply(null, finiteValues), ymax = Math.max.apply(null, finiteValues);
    var padding = Math.max(1, (ymax - ymin) * 0.12); ymin -= padding; ymax += padding;
    var xscale = function (x) { return left + (transform(x) - xmin) / (xmax - xmin) * (width - left - right); };
    var yscale = function (y) { return top + (ymax - y) / (ymax - ymin) * (height - top - bottom); };
    var html = '<svg class="chart-svg frequency-svg" viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="真实频偏与相噪，dBc/Hz；点击曲线突出，悬停采样点查看真实值"><text class="chart-label" x="12" y="14">dBc/Hz</text>';
    for (var i = 0; i <= 4; i += 1) {
      var y = top + (height - top - bottom) * i / 4;
      html += '<line class="chart-gridline" x1="' + left + '" y1="' + y + '" x2="' + (width - right) + '" y2="' + y + '"/><text class="chart-label" x="' + (left - 9) + '" y="' + (y + 4) + '" text-anchor="end">' + (ymax - (ymax - ymin) * i / 4).toFixed(1) + '</text>';
      var x = left + (width - left - right) * i / 4;
      var coordinate = xmin + (xmax - xmin) * i / 4;
      var hz = logX ? Math.pow(10, coordinate) : coordinate;
      html += '<line class="chart-gridline" x1="' + x + '" y1="' + top + '" x2="' + x + '" y2="' + (height - bottom) + '"/><text class="chart-label" x="' + x + '" y="' + (height - 28) + '" text-anchor="middle">' + escapeHtml(formatOffsetHz(hz)) + '</text>';
    }
    html += '<text class="chart-label" x="' + (width / 2) + '" y="' + (height - 6) + '" text-anchor="middle">频偏 / Hz · ' + (logX ? '对数轴' : '线性轴') + '</text>';
    series.forEach(function (item, index) {
      var connected = false, path = "";
      item.values.forEach(function (point) {
        if (!Number.isFinite(point.x) || point.x <= 0 || typeof point.y !== "number" || !Number.isFinite(point.y)) { connected = false; return; }
        path += (connected ? " L" : " M") + xscale(point.x).toFixed(2) + " " + yscale(point.y).toFixed(2);
        connected = true;
      });
      var dash = item.dashPattern || (item.dash ? "6 5" : "");
      html += '<g class="frequency-series" data-series-index="' + index + '"><path class="chart-line" d="' + path + '" stroke="' + escapeHtml(item.color || "#222") + '"' + (dash ? ' stroke-dasharray="' + escapeHtml(dash) + '"' : '') + (item.total ? ' style="stroke-width:3"' : '') + '/><path class="frequency-hit-line" d="' + path + '"/>';
      item.values.forEach(function (point) {
        if (!Number.isFinite(point.x) || point.x <= 0 || typeof point.y !== "number" || !Number.isFinite(point.y)) return;
        var label = item.name + ' · ' + point.x + ' Hz · ' + point.y.toFixed(4) + ' dBc/Hz';
        html += '<circle class="frequency-sample" data-hz="' + point.x + '" data-dbc="' + point.y + '" cx="' + xscale(point.x).toFixed(2) + '" cy="' + yscale(point.y).toFixed(2) + '" r="3.5" fill="' + escapeHtml(item.color || "#222") + '" tabindex="0" role="img" aria-label="' + escapeHtml(label) + '"><title>' + escapeHtml(label) + '</title></circle>';
      });
      html += '</g>';
    });
    return html + '</svg><div class="chart-legend frequency-legend">' + series.map(function (item, index) { return '<button type="button" class="legend-item frequency-legend-button" data-series-index="' + index + '" aria-pressed="false"><svg width="30" height="12" aria-hidden="true"><path d="M0 6 L30 6" stroke="' + escapeHtml(item.color || "#222") + '" stroke-width="' + (item.total ? 3 : 2) + '" stroke-dasharray="' + escapeHtml(item.dashPattern || (item.dash ? "6 5" : "")) + '"/></svg>' + escapeHtml(item.name) + '</button>'; }).join('') + '</div>';
  }

  function attachFrequencyInteraction(element) {
    if (!element) return;
    var tooltip = byId("chart-tooltip"), selected = null;
    if (tooltip) tooltip.classList.remove("is-visible");
    function highlight(index) {
      selected = selected === index ? null : index;
      $$("[data-series-index]", element).forEach(function (item) {
        var active = item.dataset.seriesIndex === selected;
        item.classList.toggle("is-highlighted", active);
        item.classList.toggle("is-muted", selected !== null && !active);
        if (item.tagName && item.tagName.toLowerCase() === "button") item.setAttribute("aria-pressed", String(active));
        // Overlapping markers must not hide the selected curve's hover target.
        if (active && item.tagName && item.tagName.toLowerCase() === "g" && item.parentNode) item.parentNode.appendChild(item);
      });
    }
    element.onclick = function (event) {
      var target = event.target.closest && event.target.closest("[data-series-index]");
      if (target) highlight(target.dataset.seriesIndex);
    };
    function show(point, event) {
      if (!tooltip || !point) return;
      tooltip.textContent = point.getAttribute("aria-label");
      tooltip.style.left = Math.max(8, Math.min(event.clientX + 14, (window.innerWidth || 1000) - 330)) + "px";
      tooltip.style.top = Math.max(8, event.clientY - 50) + "px";
      tooltip.classList.add("is-visible");
    }
    element.onpointermove = function (event) {
      var svg = $(".frequency-svg", element), nearest = null, distance = 18;
      if (!svg) return;
      // Use actual rendered marker positions, never interpolation or axis ticks.
      $$(".frequency-sample", svg).forEach(function (point) {
        var group = point.closest("[data-series-index]");
        if (selected !== null && group.dataset.seriesIndex !== selected) return;
        var rect = point.getBoundingClientRect();
        var d = Math.hypot(event.clientX - rect.left - rect.width / 2, event.clientY - rect.top - rect.height / 2);
        if (d < distance) { nearest = point; distance = d; }
      });
      if (nearest) show(nearest, event);
      else if (tooltip) tooltip.classList.remove("is-visible");
    };
    element.onfocusin = function (event) {
      var point = event.target;
      if (!point.classList || !point.classList.contains("frequency-sample")) return;
      var rect = point.getBoundingClientRect(); show(point, { clientX: rect.left, clientY: rect.top });
    };
    element.onpointerleave = element.onfocusout = function () { if (tooltip) tooltip.classList.remove("is-visible"); };
  }

  function metricDefinition(key) {
    var metrics = state.metricCatalog && state.metricCatalog.metrics || {};
    if (Object.prototype.hasOwnProperty.call(metrics, key)) return metrics[key];
    var name = Object.keys(metrics).find(function (name) { return Array.isArray(metrics[name].aliases) && metrics[name].aliases.indexOf(key) >= 0; });
    return name ? metrics[name] : { label: key, meaning: "此字段来自结果记录；共享词典未提供解释。", purpose: "保留原值供核对，不推断其口径。", unit: "未提供" };
  }

  function metricHelpHtml(key) {
    var definition = metricDefinition(key);
    return '<h3>' + escapeHtml(definition.label || key) + '</h3><code>' + escapeHtml(key) + '</code><dl class="metric-definition">' +
      [["unit", "单位"], ["meaning", "含义"], ["purpose", "用途"], ["scope", "比较口径"], ["formula", "公式"], ["unavailable", "缺失值"]].filter(function (entry) { return definition[entry[0]]; }).map(function (entry) { return '<dt>' + entry[1] + '</dt><dd>' + escapeHtml(definition[entry[0]]) + '</dd>'; }).join('') +
      '</dl><p>未提供、非有限或无法定义的数值显示 N/A，不当作零。零分母下的比值和 dB 不补算。</p>';
  }

  function metricButton(key, label) {
    return '<button type="button" class="metric-help-button" data-metric-key="' + escapeHtml(key) + '">' + escapeHtml(label || metricDefinition(key).label || key) + ' <span aria-hidden="true">ⓘ</span></button>';
  }

  function visibleResultMetric(key) {
    // Pair-only presentation; retain these original fields in the result and
    // exports, including their use in explicitly matched per-source evidence.
    return ["dc_dBc_Hz", "delay_dBc_Hz", "dynamic_dBc_Hz", "spectre_fn_dBc_Hz",
      "dc_pm_asd_V_rtHz", "delay_pm_asd_V_rtHz", "dynamic_pm_asd_V_rtHz",
      "spectre_fn_pm_asd_V_rtHz", "spectre_pm_asd_V_rtHz", "second_device_predicted_pm_asd_V_rtHz",
      "dc_error_dB", "delay_error_dB", "dynamic_error_dB", "dynamic_error_db", "errorDb",
      "harmonic_equivalent_dBc_Hz", "cumulative_dBc_Hz", "cumulative_error_dB",
      "cumulative_error_db", "cumulativeErrorDb", "noise_scope"].indexOf(key) < 0;
  }

  function renderMetricTables(summary, harmonics) {
    var panel = byId("result-metric-tables");
    if (!panel) return;
    summary = noiseSummaryForDisplay(summary);
    function display(value) { if (value === null || value === undefined || typeof value === "number" && !Number.isFinite(value)) return "N/A"; return typeof value === "object" ? JSON.stringify(value) : String(value); }
    var keys = Object.keys(summary || {}).filter(visibleResultMetric);
    var harmonicKeys = [];
    (harmonics || []).forEach(function (row) { Object.keys(row).forEach(function (key) { if (visibleResultMetric(key) && harmonicKeys.indexOf(key) < 0) harmonicKeys.push(key); }); });
    panel.innerHTML = '<h3>当前工况 / 频偏摘要</h3><div class="table-scroll"><table class="data-table"><tbody>' + keys.map(function (key) { return '<tr><th>' + metricButton(key) + '</th><td>' + escapeHtml(display(summary[key])) + '</td><td>' + escapeHtml(metricDefinition(key).unit || "") + '</td></tr>'; }).join('') + '</tbody></table></div><h3>当前频偏全部谐波字段</h3><div class="table-scroll"><table class="data-table"><thead><tr>' + harmonicKeys.map(function (key) { return '<th>' + metricButton(key) + '</th>'; }).join('') + '</tr></thead><tbody>' + (harmonics || []).map(function (row) { return '<tr>' + harmonicKeys.map(function (key) { return '<td>' + escapeHtml(display(row[key])) + '</td>'; }).join('') + '</tr>'; }).join('') + '</tbody></table></div>';
  }

  async function loadMetricCatalog() {
    if (state.metricCatalog) return state.metricCatalog;
    try { state.metricCatalog = await apiRequest("/metric_catalog.json", { timeout: 5000 }); }
    catch (error) { state.metricCatalog = null; }
    return state.metricCatalog;
  }

  function initMetricHelp() {
    var dialog = byId("metric-help-dialog");
    if (!dialog) return;
    document.addEventListener("click", async function (event) {
      var button = event.target.closest && event.target.closest("[data-metric-key]");
      if (!button) return;
      await loadMetricCatalog();
      byId("metric-help-content").innerHTML = metricHelpHtml(button.dataset.metricKey);
      if (typeof dialog.showModal === "function") { if (!dialog.open) dialog.showModal(); }
      else { dialog.hidden = false; dialog.setAttribute("open", ""); }
    });
    byId("close-metric-help").addEventListener("click", function () { if (typeof dialog.close === "function") dialog.close(); else { dialog.removeAttribute("open"); dialog.hidden = true; } });
    loadMetricCatalog().then(function () { if (state.currentResults && !state.currentResults.error) renderMetricTables(state.currentResults.sampleSummary, state.currentResults.sampleHarmonics); });
  }

  function noiseContributionRows(summary, noiseKind) {
    summary = summary || {};
    var carrier = finiteOrNull(summary.carrier_peak_V);
    var owned = hasTotalNoiseReference(summary);
    var totalAsd = owned ? finiteOrNull(summary.spectre_total_pm_asd_V_rtHz) : null;
    var totalPsd = totalAsd !== null && totalAsd > 0 ? totalAsd * totalAsd : null;
    function row(label, device, mechanism, asd, simulated, calculated, kind, psd) {
      asd = finiteOrNull(asd); psd = finiteOrNull(psd);
      if (psd === null && asd !== null && asd >= 0) psd = asd * asd;
      return {label: label, device: device, mechanism: mechanism, simulated_dbc: finiteOrNull(simulated), calculated_dbc: finiteOrNull(calculated), fraction_pct: totalPsd !== null && psd !== null ? 100 * psd / totalPsd : null, psd_V2_Hz: psd, kind: kind};
    }
    var thermal = thermalResult(summary), sources = thermal && Array.isArray(thermal.sources) ? thermal.sources : [];
    function matchedSources(item) { return sources.filter(function (source) { return source.device === item.device && source.mechanism === item.mechanism; }); }
    function includedSource(item) {
      if (noiseKind === "flicker") return item.mechanism === "fn";
      // "id" alone is ambiguous: MOS channel thermal and diode shot share it.
      if (noiseKind === "thermal") return item.mechanism === "rn" || matchedSources(item).length > 0 || ["channel_thermal", "resistive_thermal"].indexOf(item.noise_family) >= 0;
      return true;
    }
    var referenceSources = owned && Array.isArray(summary.spectre_noise_contributions) ? summary.spectre_noise_contributions.filter(function (item) { return item && typeof item === "object"; }) : [];
    function matchedReferences(item) { return referenceSources.filter(function (reference) { return reference.device === item.device && reference.mechanism === item.mechanism; }); }
    var selectedReference = referenceSources.filter(function (item) { return matchedSources(item).length; });
    var covered = sources.length && sources.every(function (source) {
      var matches = matchedReferences(source);
      return matchedSources(source).length === 1 && matches.length === 1 && finiteOrNull(matches[0].psd_V2_Hz) !== null && matches[0].psd_V2_Hz >= 0;
    });
    var thermalReferencePsd = covered ? selectedReference.reduce(function (sum, item) { return sum + Number(item.psd_V2_Hz); }, 0) : null;
    var thermalReferenceDbc = thermalReferencePsd > 0 && carrier > 0 ? 10 * Math.log10(2 * thermalReferencePsd / (carrier * carrier)) : null;
    var rows = [row("VCO 总仿真 / 已覆盖计算总量", "全部器件 / 已计算源", "全部机制 · PM-SSB / flicker + thermal", totalAsd, owned ? summary.spectre_total_dBc_Hz : null, predictedNoiseDbc(summary, "both"), "aggregate")];
    if (noiseKind !== "thermal") rows.push(row("独立双器件 flicker 对比", "目标 + 配对器件", "fn", summary.spectre_pair_pm_asd_V_rtHz, summary.spectre_pair_dBc_Hz, summary.predicted_pair_dBc_Hz, "aggregate"));
    if (noiseKind !== "flicker") rows.push(row("Thermal 同源对比" + (sources.length && !covered ? "（参考源不完整）" : ""), "已计算的 gm 双器件及 LC", "thermal（仅精确匹配源）", null, thermalReferenceDbc, predictedNoiseDbc(summary, "thermal"), "aggregate", thermalReferencePsd));
    referenceSources.filter(function (item) { return finiteOrNull(item.psd_V2_Hz) !== null && item.psd_V2_Hz >= 0 && includedSource(item); }).slice().sort(function (a, b) { return b.psd_V2_Hz - a.psd_V2_Hz; }).forEach(function (item) {
      var psd = Number(item.psd_V2_Hz), asd = Math.sqrt(psd);
      var dbc = psd > 0 && carrier !== null && carrier > 0 ? 10 * Math.log10(2) + 20 * (Math.log10(asd) - Math.log10(carrier)) : null;
      var matches = matchedSources(item);
      var uniqueReference = matchedReferences(item).length === 1;
      var calculated = matches.length === 1 && uniqueReference ? finiteOrNull(matches[0].phase_noise_dBc_Hz) : null;
      if (item.mechanism === "fn" && thermal && uniqueReference) {
        var target = thermal.target_noise_trace, peer = thermal.peer_noise_trace;
        var isTarget = typeof target === "string" && target.trim().length > 0 && item.device === target;
        var isSymmetricPeer = typeof target === "string" && target.trim().length > 0 && typeof peer === "string" && peer.trim().length > 0 && peer !== target && item.device === peer &&
          thermal.pair_model === "symmetric_independent" && summary.pair_model === "symmetric_independent" &&
          finiteOrNull(summary.predicted_pair_dBc_Hz) !== null;
        // The peer reuses the explicitly declared symmetric-pair prediction;
        // never infer a partner from an M0/M1 spelling or from table position.
        calculated = isTarget || isSymmetricPeer ? finiteOrNull(summary.dynamic_dBc_Hz) : null;
      }
      rows.push(row(String(item.device || "未知器件") + " · " + String(item.mechanism || "未知机制"), item.device || "未知器件", item.mechanism || "未知机制", asd, dbc, calculated, "source", psd));
    });
    sources.filter(function (source) {
      var references = matchedReferences(source);
      return includedSource(source) && (!references.length || references.length === 1 && (finiteOrNull(references[0].psd_V2_Hz) === null || references[0].psd_V2_Hz < 0));
    }).forEach(function (source) {
      var invalidReference = matchedReferences(source).length === 1;
      rows.push(row(String(source.device || "未提供器件") + " · " + String(source.mechanism || "thermal") + (invalidReference ? "（仿真参考无效）" : ""), source.device, source.mechanism, null, null, source.phase_noise_dBc_Hz, "source", null));
    });
    return rows;
  }

  function noisePieData(summary, noiseKind) {
    summary = summary || {};
    var thermal = thermalResult(summary), predicted = [], measured = [], calculatedMissing = [], simulatedMissing = [];
    var sources = thermal && Array.isArray(thermal.sources) ? thermal.sources : [];
    function key(item) { return JSON.stringify([item.device, item.mechanism]); }
    function label(item) { return String(item.device || "未知器件") + " · " + String(item.mechanism || "未知机制"); }
    function independent(item) { return item && typeof item === "object" && item.kind !== "aggregate" && item.noise_family !== "aggregate" && ["total", "totalsrc", "all"].indexOf(item.mechanism) < 0; }
    function selected(item) {
      if (noiseKind === "flicker") return item.mechanism === "fn";
      return noiseKind !== "thermal" || item.mechanism === "rn" || ["channel_thermal", "resistive_thermal"].indexOf(item.noise_family) >= 0 || sources.some(function (source) { return key(source) === key(item); });
    }
    if (noiseKind !== "flicker") {
      if (!thermal || !sources.length) calculatedMissing.push("thermal 独立源未提供");
      else predicted = sources.filter(independent).map(function (source) { return {device: source.device, mechanism: source.mechanism, psd_V2_Hz: finiteOrNull(source.psd_V2_Hz)}; });
    }
    if (noiseKind !== "thermal") {
      var target = thermal && thermal.target_noise_trace, peer = thermal && thermal.peer_noise_trace;
      var amplitude = finiteOrNull(summary.dynamic_pm_asd_V_rtHz), dbc = finiteOrNull(summary.dynamic_dBc_Hz), carrier = finiteOrNull(summary.carrier_peak_V);
      var power = amplitude !== null && amplitude >= 0 ? amplitude * amplitude : dbc !== null && carrier !== null && carrier > 0 ? Math.pow(10, dbc / 10) * carrier * carrier / 2 : null;
      var targetKnown = typeof target === "string" && target.trim().length > 0;
      if (targetKnown) predicted.push({device: target, mechanism: "fn", psd_V2_Hz: power});
      else calculatedMissing.push("flicker 目标源身份未提供");
      var pairEvidence = finiteOrNull(summary.predicted_pair_dBc_Hz) !== null || (finiteOrNull(summary.predicted_pair_pm_asd_V_rtHz) !== null && Number(summary.predicted_pair_pm_asd_V_rtHz) >= 0);
      if (targetKnown && typeof peer === "string" && peer.trim().length > 0 && peer !== target && thermal.pair_model === "symmetric_independent" && summary.pair_model === "symmetric_independent" && pairEvidence) predicted.push({device: peer, mechanism: "fn", psd_V2_Hz: power});
      else calculatedMissing.push("flicker 配对源缺少明确对称配对证据");
    }
    if (hasTotalNoiseReference(summary) && Array.isArray(summary.spectre_noise_contributions)) measured = summary.spectre_noise_contributions.filter(independent).filter(selected);
    else simulatedMissing.push("同一 VCO 结果的独立源参考未提供");
    function dataset(records, missing) {
      var counts = {};
      records.forEach(function (item) { var identity = key(item); counts[identity] = (counts[identity] || 0) + 1; });
      var slices = [];
      records.forEach(function (item) {
        var psd = finiteOrNull(item.psd_V2_Hz), description = label(item);
        if (counts[key(item)] !== 1 || psd === null || psd < 0 || !item.device || !item.mechanism) {
          var reason = description + (counts[key(item)] !== 1 ? "（来源重复）" : "（PSD 缺失或无效）");
          if (missing.indexOf(reason) < 0) missing.push(reason);
        } else slices.push({key: key(item), label: description, psd: psd});
      });
      slices.sort(function (a, b) { return b.psd - a.psd || a.key.localeCompare(b.key); });
      var total = slices.reduce(function (sum, item) { return sum + item.psd; }, 0);
      return {slices: slices, missing: missing, total: Number.isFinite(total) ? total : null};
    }
    var data = {calculated: dataset(predicted, calculatedMissing), simulated: dataset(measured, simulatedMissing)};
    var allSlices = data.calculated.slices.concat(data.simulated.slices);
    var sourceKeys = allSlices.map(function (slice) { return slice.key; }).filter(function (key, index, keys) { return keys.indexOf(key) === index; }).sort();
    allSlices.forEach(function (slice) { slice.color_index = sourceKeys.indexOf(slice.key); });
    return data;
  }

  function noisePieMarkup(data, title, scope) {
    var positive = data.total !== null && data.total > 0, angle = -Math.PI / 2;
    var svg = positive ? '<svg class="noise-pie" viewBox="0 0 220 220" role="img" aria-label="' + escapeHtml(title + '：已列独立源 PSD 份额') + '">' : '';
    var legend = data.slices.map(function (slice) {
      var fraction = positive ? slice.psd / data.total : 0, next = angle + fraction * Math.PI * 2;
      var fill = ["#222", "#666", "#aaa", "#ddd", "#444", "#888", "#bbb"][slice.color_index % 7];
      var text = slice.label + ' · ' + slice.psd.toExponential(5) + ' V²/Hz · ' + (positive ? (100 * fraction).toFixed(2) + '%' : '份额 N/A');
      if (fraction > 0) {
        var shape = fraction >= 1 - 1e-12 ? '<circle cx="110" cy="110" r="94"' : '<path d="M110 110 L' + (110 + 94 * Math.cos(angle)).toFixed(3) + ' ' + (110 + 94 * Math.sin(angle)).toFixed(3) + ' A94 94 0 ' + (fraction > 0.5 ? 1 : 0) + ' 1 ' + (110 + 94 * Math.cos(next)).toFixed(3) + ' ' + (110 + 94 * Math.sin(next)).toFixed(3) + ' Z"';
        svg += shape + ' fill="' + fill + '" stroke="#fff" stroke-width="1" tabindex="0"><title>' + escapeHtml(text) + '</title></' + (fraction >= 1 - 1e-12 ? 'circle' : 'path') + '>';
      }
      angle = next;
      return '<li><i style="background:' + fill + '" aria-hidden="true"></i><span>' + escapeHtml(text) + '</span></li>';
    }).join('');
    if (positive) svg += '</svg>';
    else svg = '<p class="empty-state">' + (data.slices.length && data.total === 0 ? '已列源 PSD 均明确为 0，无可绘制份额。' : 'N/A · 没有可用的独立源 PSD。') + '</p>';
    return '<figure class="noise-pie-card"><figcaption><strong>' + escapeHtml(title) + '</strong><p>' + escapeHtml(scope) + '</p></figcaption>' + svg + '<p>占比仅以本图已列来源为分母，不等同于全 VCO 覆盖率。' + (data.missing.length ? '部分来源缺失，不能作为完整预算。' : '') + '</p><ul class="noise-pie-legend">' + legend + data.missing.map(function (label) { return '<li class="noise-pie-missing">' + escapeHtml(label) + '：N/A</li>'; }).join('') + '</ul></figure>';
  }

  function renderNoiseContributions(results) {
    var panel = byId("noise-contribution-table");
    if (!panel) return;
    if (!results.rawCanonical || results.error) {
      panel.textContent = "等待真实 VCO 仿真结果";
      if (byId("noise-contribution-pies")) byId("noise-contribution-pies").textContent = "等待真实 VCO 仿真结果";
      return;
    }
    var summary = results.sampleSummary || {};
    function cell(value, digits) { var number = finiteOrNull(value); return number === null ? "N/A" : number.toFixed(digits); }
    var allRows = noiseContributionRows(summary);
    var thermalComparison = allRows[2];
    var rows = state.noiseKind === "both" ? allRows : noiseContributionRows(summary, state.noiseKind);
    if (byId("noise-contribution-pies")) {
      var pies = noisePieData(summary, state.noiseKind);
      byId("noise-contribution-pies").innerHTML = noisePieMarkup(pies.calculated, "ISF 计算 · 独立源", "仅 gm 的 flicker / thermal 与 LC 的 thermal；未建模源不补零。") + noisePieMarkup(pies.simulated, "VCO 仿真 · 独立源", "仅同一工况、频偏已采集的器件与机制，按当前噪声类型筛选；不混入总量或汇总行。");
    }
    if (byId("metric-thermal-error")) byId("metric-thermal-error").textContent = thermalComparison.calculated_dbc !== null && thermalComparison.simulated_dbc !== null ? signedDb(thermalComparison.calculated_dbc - thermalComparison.simulated_dbc) : "N/A";
    var missing = !hasTotalNoiseReference(summary) || finiteOrNull(summary.spectre_total_dBc_Hz) === null ? '<p class="notice-box">该结果未采集 VCO 总相噪；请用新版运行后查看。未用器件 flicker 替代总噪声。</p>' : '';
    var detailMissing = !rows.some(function (item) { return item.kind === "source"; }) ? '<p>当前筛选下没有可用的器件噪声机制明细。</p>' : '';
    var filterNote = state.noiseKind === "thermal" ? ' Thermal 明细仅列明确的沟道/电阻热噪声或已计算的同名热噪声源；散粒噪声和未分类来源可切换到两类合并视图查看。' : '';
    panel.innerHTML = missing + '<p>工况：' + escapeHtml(results.operatingPointId) + ' · ' + escapeHtml(formatOffsetHz(results.metrics.offsetHz)) + '。汇总/对比行与源明细有重叠，不能再次相加。占比 = 仿真源 PSD / VCO 总 PM-SSB PSD；仿真和计算仅按完整器件名及机制精确对应，不按行序猜测。N/A 表示未计算或没有同源参考。' + filterNote + '</p>' + detailMissing + '<div class="table-scroll"><table class="data-table"><thead><tr><th>项目 / 器件</th><th>噪声机制</th><th>仿真 dBc/Hz</th><th>ISF 计算 dBc/Hz</th><th>占总噪声 %</th></tr></thead><tbody>' + rows.map(function (item) { return '<tr class="noise-row-' + item.kind + '"><td>' + escapeHtml(item.label) + '</td><td>' + escapeHtml(item.mechanism) + '</td><td>' + cell(item.simulated_dbc, 4) + '</td><td>' + cell(item.calculated_dbc, 4) + '</td><td>' + cell(item.fraction_pct, 4) + (item.kind === "source" && item.fraction_pct !== null ? '<span class="noise-power-bar" aria-hidden="true"><i style="width:' + clamp(item.fraction_pct, 0, 100) + '%"></i></span>' : '') + '</td></tr>'; }).join('') + '</tbody></table></div>';
  }

  function renderFlickerResults(results) {
    var kind = byId("noise-kind");
    state.noiseKind = kind ? kind.value : state.noiseKind;
    renderNoiseContributions(results);
    var raw = results.rawCanonical;
    var phase = byId("phase-noise-chart"), harmonic = byId("harmonic-noise-chart");
    if (!raw || results.error) {
      if (phase) phase.innerHTML = '<div class="empty-state">等待已完成的相噪结果</div>';
      if (harmonic) harmonic.innerHTML = '<div class="empty-state">等待谐波结果</div>';
      ["metric-calculated-dbc", "metric-spectre-dbc", "metric-thermal-error"].forEach(function (id) { if (byId(id)) byId(id).textContent = "N/A"; });
      ["noise-scope-note", "sample-quality-note", "result-operating-parameters", "result-metric-tables"].forEach(function (id) { if (byId(id)) byId(id).textContent = ""; });
      if (byId("sweep-result-note")) byId("sweep-result-note").hidden = true;
      return;
    }
    state.noiseScope = "pair";
    var cumulative = byId("cumulative-k-select");
    var oldK = cumulative && cumulative.value;
    var series = resultNoiseSeries(raw, results.operatingPointId, state.noiseKind, oldK === "" || oldK === null || oldK === undefined ? null : Number(oldK));
    if (cumulative) cumulative.innerHTML = '<option value="">不显示累计曲线</option>' + series.orders.map(function (k) { return '<option value="' + k + '"' + (String(k) === oldK ? ' selected' : '') + '>累计 k=0…' + k + '</option>'; }).join('');
    var logX = !byId("noise-x-scale") || byId("noise-x-scale").value !== "linear";
    if (phase) phase.innerHTML = frequencyChartMarkup(series.phase, logX);
    if (harmonic) harmonic.innerHTML = frequencyChartMarkup(series.harmonics, logX);
    attachFrequencyInteraction(phase);
    attachFrequencyInteraction(harmonic);
    if (cumulative) cumulative.disabled = state.noiseKind !== "flicker";
    var calculated = predictedNoiseDbc(results.sampleSummary, state.noiseKind);
    var reference = results.metrics.totalSpectreDbc;
    if (byId("metric-calculated-dbc")) byId("metric-calculated-dbc").textContent = finiteOrNull(calculated) === null ? "N/A" : calculated.toFixed(3) + " dBc/Hz";
    if (byId("metric-spectre-dbc")) byId("metric-spectre-dbc").textContent = finiteOrNull(reference) === null ? "N/A" : reference.toFixed(3) + " dBc/Hz";
    if (byId("calculated-dbc-help")) byId("calculated-dbc-help").dataset.metricKey = state.noiseKind === "flicker" ? "predicted_pair_dBc_Hz" : "thermal_noise";
    if (byId("spectre-dbc-help")) byId("spectre-dbc-help").dataset.metricKey = "spectre_total_dBc_Hz";
    var summary = results.sampleSummary || {};
    if (byId("noise-scope-note")) byId("noise-scope-note").textContent = "计算范围：gm 的 flicker / thermal 与 LC 的 thermal。仿真曲线始终为 VCO 总 PM-SSB 相噪。Flicker 为独立双器件的对称预测，各器件内部先相干合成；thermal 按独立输入频带及独立源的功率相加，再与 flicker 加功率。计算只覆盖列出的源，不能把与全 VCO 的差值当作同源误差。" + (!thermalResult(summary) ? " 当前结果未提供可用 thermal 数据，thermal 和两类计算总和显示 N/A，不以零代替。" : "") + (finiteOrNull(reference) === null ? " 当前结果未采集总相噪，不用器件 flicker 顶替。" : "");
    if (byId("result-operating-parameters")) byId("result-operating-parameters").textContent = "工况：" + results.operatingPointId + " · 实际参数：" + (summary.operating_parameters ? JSON.stringify(summary.operating_parameters) : "未提供") + " · 当前表格频偏：" + formatOffsetHz(results.metrics.offsetHz);
    var thermal = thermalResult(summary), thermalQuality = thermal && thermal.numerical_quality;
    if (summary.thermal_noise && !thermal && byId("noise-scope-note")) byId("noise-scope-note").textContent += " " + (summary.thermal_noise.reason || "该历史结果的计算范围不符合当前 gm + LC 范围；thermal 和总计算值显示 N/A，请重新运行。");
    if (state.noiseKind !== "flicker" && byId("noise-scope-note")) {
      var inputLimit = finiteOrNull(thermal && thermal.input_max_harmonic);
      byId("noise-scope-note").textContent += inputLimit !== null && Number.isInteger(inputLimit) && inputLimit >= 0
        ? " Thermal 有限带预算：独立输入频带 |m|≤" + inputLimit + "；更高频带不计。"
        : " Thermal 实际输入频带范围未记录，不能把它解释为无限频带预算。";
    }
    var quality = [[summary.pm_magnitude_projection_valid, "PM 近似"], [summary.numerical_quality_pass, "Flicker 数值质量"], [thermalQuality ? thermalQuality.pass : undefined, "Thermal 数值质量"], [summary.hbac_secondary_validation_pass, "HBAC 辅助检查"]].map(function (entry) { return entry[1] + "：" + (entry[0] === true ? "通过" : entry[0] === false ? "未通过" : "未提供"); });
    [[summary.numerical_quality_issues, "Flicker 问题"], [thermalQuality && thermalQuality.issues, "Thermal 问题"]].forEach(function (entry) {
      if (entry[0] && (!Array.isArray(entry[0]) || entry[0].length)) quality.push(entry[1] + "：" + (typeof entry[0] === "object" ? JSON.stringify(entry[0]) : entry[0]));
    });
    if (byId("sample-quality-note")) byId("sample-quality-note").textContent = quality.join(" · ") + "。运行完成不代表验证通过；曲线接近不能单独证明计算可信。";
    var sweep = results.offsetSweep;
    if (byId("sweep-result-note")) { byId("sweep-result-note").hidden = !sweep; byId("sweep-result-note").textContent = sweep ? (sweep.complete ? "全部频偏已完成" : "部分结果：仅展示已完成的频偏") + " · 已完成 " + (sweep.completed_offsets_hz || []).length + "/" + (sweep.offsets_hz || []).length + " 个频偏；批任务状态不替代各点质量检查。" : ""; }
    renderMetricTables(summary, results.sampleHarmonics);
  }

  function renderLineChart(element, series) {
    if (!element) return;
    if (!series || !series.length) {
      element.innerHTML = '<div class="empty-state">当前导入格式未包含这组逐点曲线；请导入 canonical workpoint.csv 或自动提取 PSF。</div>';
      return;
    }
    var width = 760;
    var height = 270;
    var margin = { top: 17, right: 20, bottom: 35, left: 50 };
    var plotWidth = width - margin.left - margin.right;
    var plotHeight = height - margin.top - margin.bottom;
    var all = [];
    series.forEach(function (item) { item.values.forEach(function (point) { if (Number.isFinite(point.x) && Number.isFinite(point.y)) all.push(point); }); });
    if (!all.length) return;
    var xMin = Math.min.apply(null, all.map(function (point) { return point.x; }));
    var xMax = Math.max.apply(null, all.map(function (point) { return point.x; }));
    var yMin = Math.min.apply(null, all.map(function (point) { return point.y; }).concat([0]));
    var yMax = Math.max.apply(null, all.map(function (point) { return point.y; }).concat([0]));
    if (xMax === xMin) xMax = xMin + 1;
    if (yMax === yMin) { yMax += 1; yMin -= 1; }
    var yPadding = (yMax - yMin) * 0.09;
    yMin -= yPadding;
    yMax += yPadding;
    var xScale = function (x) { return margin.left + ((x - xMin) / (xMax - xMin)) * plotWidth; };
    var yScale = function (y) { return margin.top + (1 - (y - yMin) / (yMax - yMin)) * plotHeight; };
    var markup = '<svg class="chart-svg" viewBox="0 0 ' + width + " " + height + '" role="img">';
    for (var yi = 0; yi <= 4; yi += 1) {
      var y = margin.top + (plotHeight / 4) * yi;
      var value = yMax - ((yMax - yMin) / 4) * yi;
      markup += '<line class="chart-gridline" x1="' + margin.left + '" y1="' + y + '" x2="' + (width - margin.right) + '" y2="' + y + '"/>';
      markup += '<text class="chart-label" x="' + (margin.left - 9) + '" y="' + (y + 3) + '" text-anchor="end">' + axisFormat(value, yMax - yMin) + '</text>';
    }
    var phaseLabels = ["0", "½π", "π", "3⁄2π", "2π"];
    for (var xi = 0; xi <= 4; xi += 1) {
      var x = margin.left + (plotWidth / 4) * xi;
      markup += '<line class="chart-gridline" x1="' + x + '" y1="' + margin.top + '" x2="' + x + '" y2="' + (height - margin.bottom) + '"/>';
      markup += '<text class="chart-label" x="' + x + '" y="' + (height - 13) + '" text-anchor="middle">' + phaseLabels[xi] + '</text>';
    }
    if (yMin < 0 && yMax > 0) markup += '<line class="chart-zero" x1="' + margin.left + '" y1="' + yScale(0) + '" x2="' + (width - margin.right) + '" y2="' + yScale(0) + '"/>';
    series.forEach(function (item) {
      markup += '<path class="chart-line" d="' + pathFor(item.values, xScale, yScale) + '" stroke="' + escapeHtml(item.color) + '"' + (item.dash ? ' stroke-dasharray="6 5"' : "") + '/>';
    });
    markup += '<line class="chart-zero chart-hover-line" x1="0" y1="' + margin.top + '" x2="0" y2="' + (height - margin.bottom) + '" opacity="0"/>';
    series.forEach(function (item, index) { markup += '<circle class="chart-point chart-hover-point" data-index="' + index + '" cx="0" cy="0" r="3.5" fill="' + escapeHtml(item.color) + '" opacity="0"/>'; });
    markup += '</svg>';
    element.innerHTML = markup;
    var svg = $("svg", element);
    var hoverLine = $(".chart-hover-line", svg);
    var hoverPoints = $$(".chart-hover-point", svg);
    var tooltip = byId("chart-tooltip");
    element._updatePhaseCursor = function (ratio, event) {
      if (ratio === null) {
        hoverLine.setAttribute("opacity", "0");
        hoverPoints.forEach(function (point) { point.setAttribute("opacity", "0"); });
        return;
      }
      var chartX = margin.left + ratio * plotWidth;
      hoverLine.setAttribute("x1", chartX);
      hoverLine.setAttribute("x2", chartX);
      hoverLine.setAttribute("opacity", "1");
      var lines = ['<strong>θ = ' + (ratio * 360).toFixed(1) + '°</strong>'];
      series.forEach(function (item, index) {
        var pointIndex = Math.round(ratio * (item.values.length - 1));
        var point = item.values[pointIndex];
        if (!point) return;
        hoverPoints[index].setAttribute("cx", xScale(point.x));
        hoverPoints[index].setAttribute("cy", yScale(point.y));
        hoverPoints[index].setAttribute("opacity", "1");
        lines.push('<span style="color:' + escapeHtml(item.color) + '">●</span> ' + escapeHtml(item.name) + ': ' + axisFormat(point.y, yMax - yMin));
      });
      if (tooltip && event) {
        tooltip.innerHTML = lines.join("<br>");
        tooltip.classList.add("is-visible");
        var tooltipLeft = event.clientX + 13;
        if (tooltipLeft + 180 > window.innerWidth) tooltipLeft = event.clientX - 175;
        tooltip.style.left = tooltipLeft + "px";
        tooltip.style.top = Math.max(8, event.clientY - 22) + "px";
      }
    };
    state.phaseCharts.push(element);
    svg.addEventListener("pointermove", function (event) {
      var rect = svg.getBoundingClientRect();
      var viewX = ((event.clientX - rect.left) / rect.width) * width;
      var ratio = clamp((viewX - margin.left) / plotWidth, 0, 1);
      state.phaseCharts.forEach(function (chart) {
        if (chart && chart._updatePhaseCursor) chart._updatePhaseCursor(ratio, chart === element ? event : null);
      });
    });
    svg.addEventListener("pointerleave", function () {
      clearPhaseInteraction();
    });
  }

  function renderPhasorChart(element, values) {
    if (!element) return;
    if (!values || !values.length) {
      element.innerHTML = '<div class="empty-state">当前结果没有 Qk 累计复数数据。</div>';
      return;
    }
    var width = 980;
    var height = 330;
    var margin = { top: 20, right: 30, bottom: 35, left: 50 };
    var plotWidth = width - margin.left - margin.right;
    var plotHeight = height - margin.top - margin.bottom;
    var maxAbs = Math.max.apply(null, values.reduce(function (output, point) { return output.concat([Math.abs(point.re), Math.abs(point.im)]); }, [0]));
    maxAbs = (maxAbs > 0 ? maxAbs : 1) * 1.18;
    var scale = Math.min(plotWidth, plotHeight) / (2 * maxAbs);
    var cx = margin.left + plotWidth / 2;
    var cy = margin.top + plotHeight / 2;
    var xScale = function (x) { return cx + x * scale; };
    var yScale = function (y) { return cy - y * scale; };
    var markup = '<svg class="chart-svg" viewBox="0 0 ' + width + " " + height + '" role="img">';
    [0.25, 0.5, 0.75, 1].forEach(function (ratio) {
      markup += '<circle cx="' + cx + '" cy="' + cy + '" r="' + (maxAbs * ratio * scale) + '" fill="none" class="chart-gridline"/>';
    });
    markup += '<line class="chart-axis" x1="' + margin.left + '" y1="' + cy + '" x2="' + (width - margin.right) + '" y2="' + cy + '"/>';
    markup += '<line class="chart-axis" x1="' + cx + '" y1="' + margin.top + '" x2="' + cx + '" y2="' + (height - margin.bottom) + '"/>';
    markup += '<text class="chart-label" x="' + (width - margin.right) + '" y="' + (cy - 8) + '" text-anchor="end">Re{ΣQk}</text>';
    markup += '<text class="chart-label" x="' + (cx + 8) + '" y="' + (margin.top + 9) + '">Im{ΣQk}</text>';
    markup += '<path class="chart-line" d="' + values.map(function (point, index) { return (index ? "L" : "M") + xScale(point.re).toFixed(2) + " " + yScale(point.im).toFixed(2); }).join(" ") + '" stroke="' + COLORS.teal + '"/>';
    values.forEach(function (point, index) {
      var finalPoint = index === values.length - 1;
      markup += '<circle class="chart-point phasor-point" data-k="' + point.k + '" data-re="' + point.re + '" data-im="' + point.im + '" cx="' + xScale(point.re) + '" cy="' + yScale(point.im) + '" r="' + (finalPoint ? 5.5 : 3.5) + '" fill="' + (finalPoint ? COLORS.orange : COLORS.teal) + '"/>';
      if (index === 0 || finalPoint || index % 2 === 0) markup += '<text class="chart-label" x="' + (xScale(point.re) + 7) + '" y="' + (yScale(point.im) - 7) + '">k' + point.k + '</text>';
    });
    markup += '</svg>';
    element.innerHTML = markup;
    var tooltip = byId("chart-tooltip");
    $$(".phasor-point", element).forEach(function (point) {
      point.addEventListener("pointerenter", function (event) {
        if (!tooltip) return;
        tooltip.innerHTML = '<strong>ΣQ₀…' + escapeHtml(point.dataset.k) + '</strong><br>Re: ' + scientificText(point.dataset.re, 4) + '<br>Im: ' + scientificText(point.dataset.im, 4);
        tooltip.classList.add("is-visible");
        tooltip.style.left = (event.clientX + 12) + "px";
        tooltip.style.top = Math.max(8, event.clientY - 22) + "px";
      });
      point.addEventListener("pointerleave", function () { if (tooltip) tooltip.classList.remove("is-visible"); });
      point.addEventListener("click", function () { highlightHarmonic(point.dataset.k); });
    });
  }

  function highlightHarmonic(k) {
    var key = String(k);
    $$(".phasor-point").forEach(function (point) {
      point.classList.toggle("is-highlighted", String(point.dataset.k) === key);
    });
    $$("#error-table-body tr").forEach(function (row) {
      var active = String(row.dataset.k) === key;
      row.classList.toggle("is-highlighted", active);
      if (active) row.scrollIntoView({ block: "nearest" });
    });
  }

  function renderComparisonChart(element, data) {
    if (!element) return;
    if (!data || !Array.isArray(data.labels) || !data.labels.length || !Array.isArray(data.series)) {
      element.innerHTML = '<div class="empty-state">需要至少一个设计点的 DC、统一延迟和动态误差数据。</div>';
      return;
    }
    var width = 980;
    var height = 310;
    var margin = { top: 24, right: 28, bottom: 62, left: 58 };
    var plotWidth = width - margin.left - margin.right;
    var plotHeight = height - margin.top - margin.bottom;
    var finiteValues = [];
    data.series.forEach(function (series) {
      (series.values || []).forEach(function (value) { if (Number.isFinite(Number(value))) finiteValues.push(Number(value)); });
    });
    if (!finiteValues.length) {
      element.innerHTML = '<div class="empty-state">当前结果没有可绘制的方法误差。</div>';
      return;
    }
    var yMin = Math.min.apply(null, finiteValues.concat([-3, 0]));
    var yMax = Math.max.apply(null, finiteValues.concat([3, 0]));
    var padding = Math.max(1, (yMax - yMin) * 0.08);
    yMin -= padding;
    yMax += padding;
    var xScale = function (index) {
      return data.labels.length === 1 ? margin.left + plotWidth / 2 : margin.left + index * plotWidth / (data.labels.length - 1);
    };
    var yScale = function (value) { return margin.top + (yMax - value) * plotHeight / (yMax - yMin); };
    var markup = '<svg class="chart-svg" viewBox="0 0 ' + width + ' ' + height + '" role="img">';
    var gateTop = yScale(3);
    var gateBottom = yScale(-3);
    markup += '<rect x="' + margin.left + '" y="' + gateTop + '" width="' + plotWidth + '" height="' + (gateBottom - gateTop) + '" fill="rgba(37,130,93,.08)"/>';
    for (var tick = 0; tick <= 5; tick += 1) {
      var value = yMax - tick * (yMax - yMin) / 5;
      var y = yScale(value);
      markup += '<line class="chart-gridline" x1="' + margin.left + '" y1="' + y + '" x2="' + (width - margin.right) + '" y2="' + y + '"/>';
      markup += '<text class="chart-label" x="' + (margin.left - 9) + '" y="' + (y + 3) + '" text-anchor="end">' + value.toFixed(1) + '</text>';
    }
    markup += '<line class="chart-zero" x1="' + margin.left + '" y1="' + yScale(0) + '" x2="' + (width - margin.right) + '" y2="' + yScale(0) + '"/>';
    data.labels.forEach(function (label, index) {
      var x = xScale(index);
      markup += '<text class="chart-label" x="' + x + '" y="' + (height - 22) + '" text-anchor="middle">' + escapeHtml(label.replace(/^k_/, "")) + '</text>';
    });
    data.series.forEach(function (series) {
      var valid = [];
      (series.values || []).forEach(function (raw, index) {
        var value = Number(raw);
        if (Number.isFinite(value)) valid.push({ index: index, value: value });
      });
      markup += '<path class="chart-line" d="' + valid.map(function (point, index) { return (index ? "L" : "M") + xScale(point.index).toFixed(2) + ' ' + yScale(point.value).toFixed(2); }).join(' ') + '" stroke="' + escapeHtml(series.color) + '"/>';
      valid.forEach(function (point) {
        markup += '<circle class="chart-point" cx="' + xScale(point.index) + '" cy="' + yScale(point.value) + '" r="4" fill="' + escapeHtml(series.color) + '"/>';
        if (series.name === "动态") markup += '<text class="chart-label" x="' + xScale(point.index) + '" y="' + (yScale(point.value) - 9) + '" text-anchor="middle">' + (point.value > 0 ? "+" : "") + point.value.toFixed(2) + '</text>';
      });
    });
    markup += '<text class="chart-label" x="' + (width - margin.right) + '" y="' + (gateTop - 5) + '" text-anchor="end">±3 dB absolute gate</text>';
    markup += '</svg>';
    element.innerHTML = markup;
  }

  function metricText(value, kind) {
    if (finiteOrNull(value) === null && typeof value !== "string") return "N/A";
    if (typeof value === "string") return value;
    if (kind === "frequency") {
      if (value >= 1e9) return (value / 1e9).toFixed(3) + " GHz";
      if (value >= 1e6) return (value / 1e6).toFixed(3) + " MHz";
    }
    return finite(value, 0).toFixed(3);
  }

  function scientificText(value, digits) {
    var number = finiteOrNull(value);
    if (number === null) return "N/A";
    if (number === 0) return "0";
    var absolute = Math.abs(number);
    if (absolute < 1e-3 || absolute >= 1e4) return number.toExponential(digits === undefined ? 3 : digits);
    return number.toPrecision(digits === undefined ? 4 : digits + 1);
  }

  function complexText(real, imag) {
    var re = finiteOrNull(real);
    var im = finiteOrNull(imag);
    if (re === null || im === null) return "N/A";
    return scientificText(re, 2) + (im >= 0 ? "+j" : "−j") + scientificText(Math.abs(im), 2);
  }

  function renderErrorTable(errors) {
    var body = byId("error-table-body");
    if (!body) return;
    body.innerHTML = errors.map(function (row) {
      var amplitude = finiteOrNull(row.amplitude !== undefined ? row.amplitude : (row.refMag > 0 ? row.dynMag / row.refMag : null));
      var rawDelta = row.deltaDeg !== undefined ? row.deltaDeg : (
        finiteOrNull(row.dynPhase) !== null && finiteOrNull(row.refPhase) !== null
          ? wrappedDegrees(row.dynPhase - row.refPhase)
          : null
      );
      var delta = finiteOrNull(rawDelta);
      var ckReal = finiteOrNull(row.ckReal !== undefined ? row.ckReal : (amplitude !== null && delta !== null ? amplitude * Math.cos(delta * Math.PI / 180) : null));
      var ckImag = finiteOrNull(row.ckImag !== undefined ? row.ckImag : (amplitude !== null && delta !== null ? amplitude * Math.sin(delta * Math.PI / 180) : null));
      var qMag = row.qMag !== undefined ? row.qMag : row.dynMag;
      var qPhase = row.qPhase !== undefined ? row.qPhase : row.dynPhase;
      return '<tr data-k="' + escapeHtml(row.k) + '" tabindex="0">' +
        '<td class="mono">' + escapeHtml(row.k) + "</td>" +
        '<td class="numeric mono">' + scientificText(row.refMag, 3) + "</td>" +
        '<td class="numeric mono">' + scientificText(row.dynMag, 3) + "</td>" +
        '<td class="numeric">' + (amplitude === null ? "N/A" : amplitude.toFixed(4)) + "</td>" +
        '<td class="numeric">' + (delta === null ? "N/A" : (delta > 0 ? "+" : delta < 0 ? "−" : "") + Math.abs(delta).toFixed(2) + "°") + "</td>" +
        '<td class="numeric mono">' + complexText(ckReal, ckImag) + "</td>" +
        '<td class="numeric mono">' + scientificText(qMag, 3) + "</td>" +
        '<td class="numeric">' + (finiteOrNull(qPhase) === null ? "N/A" : Number(qPhase).toFixed(2) + "°") + "</td>" +
        '<td class="numeric">' + (finiteOrNull(row.equivalentDbc) === null ? "N/A" : Number(row.equivalentDbc).toFixed(3)) + "</td>" +
        '<td class="numeric">' + (finiteOrNull(row.cumulativeDbc) === null ? "N/A" : Number(row.cumulativeDbc).toFixed(3)) + "</td></tr>";
    }).join("");
    $$("#error-table-body tr").forEach(function (row) {
      row.addEventListener("click", function () { highlightHarmonic(row.dataset.k); });
      row.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") highlightHarmonic(row.dataset.k);
      });
    });
  }

  function signedDb(value) {
    var number = finiteOrNull(value);
    if (number === null) return "N/A";
    return (number > 0 ? "+" : number < 0 ? "−" : "") + Math.abs(number).toFixed(3) + " dB";
  }

  function renderResults(results) {
    state.currentResults = results;
    clearPhaseInteraction();
    state.phaseCharts = [];
    if (byId("comparison-offset")) byId("comparison-offset").textContent = formatOffsetHz(results.metrics && results.metrics.offsetHz);
    if (results.error) {
      ["metric-frequency", "metric-mdc", "metric-mdyn", "metric-qsum", "metric-error", "metric-pair-error", "metric-calculated-dbc", "metric-spectre-dbc"].forEach(function (id) { if (byId(id)) byId(id).textContent = "N/A"; });
      ["result-operating-parameters", "sample-quality-note", "result-metric-tables"].forEach(function (id) { if (byId(id)) byId(id).textContent = ""; });
      if (byId("sweep-result-note")) byId("sweep-result-note").hidden = true;
      renderFlickerResults(results);
      if (byId("metric-period")) byId("metric-period").textContent = "无可验证结果";
      if (byId("metric-error-gates")) byId("metric-error-gates").textContent = "N/A · 未加载";
      if (byId("metric-pair-detail")) byId("metric-pair-detail").textContent = "无可验证双管结果";
      var failedClosure = byId("closure-status");
      if (failedClosure) { failedClosure.className = "status-pill"; failedClosure.textContent = "N/A · 未加载"; }
      var failedNote = byId("result-source-note");
      if (failedNote) $("span", failedNote).innerHTML = '<strong>结果未加载：</strong>' + escapeHtml(results.error) + (results.id ? '；任务 <code>' + escapeHtml(results.id) + '</code> 未通过来源验证。' : "。");
      renderComparisonChart(byId("comparison-chart"), null);
      renderLineChart(byId("voltage-chart"), []);
      renderLineChart(byId("current-chart"), []);
      renderLineChart(byId("isf-chart"), []);
      renderLineChart(byId("modulation-chart"), []);
      renderLineChart(byId("product-chart"), []);
      renderPhasorChart(byId("qk-chart"), []);
      renderErrorTable([]);
      return;
    }
    var metrics = results.metrics;
    if (byId("metric-frequency")) byId("metric-frequency").textContent = metricText(metrics.frequency, "frequency");
    if (byId("metric-period")) byId("metric-period").textContent = metrics.period === null
      ? "T = N/A"
      : "T = " + (Number(metrics.period) * 1e12).toFixed(2) + " ps";
    if (byId("metric-mdc")) byId("metric-mdc").textContent = scientificText(metrics.mdc, 3) + " A/√Hz";
    if (byId("metric-mdyn")) byId("metric-mdyn").textContent = scientificText(metrics.mdyn, 3) + " A/√Hz";
    if (byId("metric-qsum")) byId("metric-qsum").textContent = typeof metrics.qsum === "string" ? metrics.qsum : JSON.stringify(metrics.qsum);
    var errorValue = finiteOrNull(metrics.errorDb);
    var absoluteAvailable = errorValue !== null;
    var absolutePass = absoluteAvailable && Math.abs(errorValue) <= 3;
    var trendValue = finiteOrNull(metrics.trendDb);
    var trendAvailable = trendValue !== null;
    var trendPass = trendAvailable && Math.abs(trendValue) <= 1.5;
    if (byId("metric-error")) {
      byId("metric-error").textContent = absoluteAvailable ? signedDb(errorValue) : "N/A";
      byId("metric-error").className = "metric-value " + (absoluteAvailable ? (absolutePass ? "delta-good" : "delta-bad") : "");
    }
    if (byId("metric-error-gates")) byId("metric-error-gates").textContent = "absolute " + (absoluteAvailable ? (absolutePass ? "PASS" : "FAIL") : "N/A") + " · trend " + (trendAvailable ? signedDb(trendValue) + " " + (trendPass ? "PASS" : "WARN") : "N/A");
    var pairAvailable = metrics.pairErrorDb !== null && Number.isFinite(Number(metrics.pairErrorDb));
    var pairPass = pairAvailable && Math.abs(Number(metrics.pairErrorDb)) <= 3.0;
    if (byId("metric-pair-error")) {
      byId("metric-pair-error").textContent = pairAvailable ? signedDb(metrics.pairErrorDb) : "N/A";
      byId("metric-pair-error").className = "metric-value " + (pairAvailable ? (pairPass ? "delta-good" : "delta-bad") : "");
    }
    if (byId("metric-pair-detail")) byId("metric-pair-detail").textContent = pairAvailable
      ? ("power sum " + (pairPass ? "PASS" : "FAIL") + " · pred " + scientificText(metrics.pairPredictedAsd, 2) + " / ref " + scientificText(metrics.pairReferenceAsd, 2) + " V/√Hz")
      : "未提供第二只器件与 pair reference";
    var closure = byId("closure-status");
    if (closure) {
      closure.className = "status-pill" + (!absoluteAvailable ? "" : " " + (!absolutePass ? "failed" : !trendAvailable || trendPass ? "success" : "warning"));
      closure.textContent = "absolute " + (absoluteAvailable ? (absolutePass ? "PASS" : "FAIL") : "N/A") + " · trend " + (trendAvailable ? (trendPass ? "PASS" : "WARN") : "N/A");
    }
    var note = byId("result-source-note");
    if (note) $("span", note).innerHTML = results.source === "api"
      ? '显示任务 <code>' + escapeHtml(results.id) + '</code>' + (results.point ? '、设计点 <code>' + escapeHtml(results.point) + '</code>' : '') + ' 的 API 结果 · <strong>' + (results.signoff ? "SIGNOFF" : escapeHtml((results.runKind || "replay").toUpperCase()) + " NON-SIGNOFF") + '</strong>。'
      : '当前没有经过后端 provenance 验证的结果。';
    renderComparisonChart(byId("comparison-chart"), results.charts.comparison);
    renderLineChart(byId("voltage-chart"), results.charts.voltage);
    renderLineChart(byId("current-chart"), results.charts.current);
    renderLineChart(byId("isf-chart"), results.charts.isf);
    renderLineChart(byId("modulation-chart"), results.charts.modulation);
    renderLineChart(byId("product-chart"), results.charts.product);
    renderPhasorChart(byId("qk-chart"), results.charts.phasor);
    renderErrorTable(results.errors);
    renderFlickerResults(results);
    writeStorage(STORAGE.resultView, workspaceStateView());
  }

  function currentJobId() {
    var params = new URLSearchParams(window.location.search);
    return params.get("id") || readStorage(STORAGE.jobId, "");
  }

  async function loadRemoteResults(feedback) {
    var select = byId("result-job-select");
    var restored = readStorage(STORAGE.workspaceState, null);
    var view = restored && restored.view && restored.view.job_id ? restored.view : null;
    var id = view ? view.job_id : select && select.value ? select.value : currentJobId();
    if (!state.apiOnline) {
      var offline = unavailableResults(id, "API 不可达");
      renderResults(offline);
      if (feedback) toast("API 不可达，结果未加载", "error");
      return offline;
    }
    try {
      var payload = await apiRequest("/api/results/" + encodeURIComponent(id), { timeout: 8000 });
      state.currentRawResults = payload;
      var pointSelector = byId("result-point-select");
      var results = view ? exactWorkspaceResult(payload, view) : normalizeResults(payload, id, pointSelector && pointSelector.value);
      if (view) {
        applyWorkspaceResultView(view, results);
        writeStorage(STORAGE.workspaceState, {loaded: true, view: {}});
      }
      renderResults(results);
      if (feedback) toast("结果已从 API 更新");
      return results;
    } catch (error) {
      if (view) writeStorage(STORAGE.workspaceState, {loaded: true, view: {}});
      state.currentRawResults = null;
      var unavailable = unavailableResults(id, "结果接口不可用：" + errorMessage(error));
      renderResults(unavailable);
      if (feedback) toast("结果接口不可用，结果未加载", "error");
      return unavailable;
    }
  }

  async function populateResultJobs(selectedId) {
    var select = byId("result-job-select");
    if (!select || !state.apiOnline) return;
    try {
      var payload = await apiRequest("/api/jobs", { timeout: 5000 });
      var jobs = (payload.jobs || []).filter(function (job) { return job.result_available; });
      if (selectedId && !jobs.some(function (job) { return String(job.id) === String(selectedId); })) jobs.unshift({ id: selectedId, status: "requested" });
      select.innerHTML = jobs.map(function (job) { return '<option value="' + escapeHtml(job.id) + '"' + (String(job.id) === String(selectedId) ? ' selected' : '') + '>' + escapeHtml(job.id + " · " + job.status) + '</option>'; }).join("");
    } catch (error) {
      select.innerHTML = '<option value="' + escapeHtml(selectedId) + '">' + escapeHtml(selectedId) + '</option>';
    }
  }

  function initResults() {
    initMetricHelp();
    var id = currentJobId();
    var select = byId("result-job-select");
    if (select) {
      select.innerHTML = '<option value="' + escapeHtml(id) + '">' + escapeHtml(id) + "</option>";
      select.addEventListener("change", function () {
        state.workspaceStateSequence += 1;
        var restored = readStorage(STORAGE.workspaceState, null);
        if (restored) writeStorage(STORAGE.workspaceState, {loaded: true, view: {}});
        loadRemoteResults(false);
      });
    }
    var pointSelect = byId("result-point-select");
    if (pointSelect) pointSelect.addEventListener("change", function () {
      state.workspaceStateSequence += 1;
      var jobSelect = byId("result-job-select");
      var selectedJob = jobSelect && jobSelect.value ? jobSelect.value : currentJobId();
      if (state.currentRawResults) renderResults(normalizeResults(state.currentRawResults, selectedJob, pointSelect.value));
    });
    var offsetSelect = byId("result-offset-select");
    if (offsetSelect) offsetSelect.addEventListener("change", function () {
      state.workspaceStateSequence += 1;
      if (state.currentRawResults) renderResults(normalizeResults(state.currentRawResults, byId("result-job-select") && byId("result-job-select").value || currentJobId(), pointSelect && pointSelect.value));
    });
    ["noise-kind", "noise-x-scale", "cumulative-k-select"].forEach(function (id) { if (byId(id)) byId(id).addEventListener("change", function () {
      state.workspaceStateSequence += 1;
      if (state.currentResults) { renderFlickerResults(state.currentResults); writeStorage(STORAGE.resultView, workspaceStateView()); }
    }); });
    renderResults(unavailableResults(id, "等待 API 结果"));
    var reload = byId("reload-results");
    if (reload) reload.addEventListener("click", async function () {
      setButtonBusy(reload, true);
      await loadRemoteResults(true);
      setButtonBusy(reload, false);
    });
    var print = byId("print-report");
    if (print) print.addEventListener("click", function () { window.print(); });
  }

  function renderFiles(files) {
    var body = byId("file-table-body");
    if (!body) return;
    if (!files || !files.length) {
      body.innerHTML = '<tr><td colspan="7"><div class="empty-state">没有经过后端 provenance 验证的可导出文件。</div></td></tr>';
      return;
    }
    body.innerHTML = files.map(function (file, index) {
      var status = statusName(file.status);
      return "<tr>" +
        '<td><input class="file-select" type="checkbox" data-index="' + index + '" checked aria-label="选择 ' + escapeHtml(file.name) + '"></td>' +
        '<td><button class="btn ghost small file-preview" type="button" data-index="' + index + '" style="padding-left:0;text-align:left"><span><span class="file-name">' + escapeHtml(file.name) + '</span><span class="file-path" title="' + escapeHtml(file.path) + '">' + escapeHtml(file.path) + "</span></span></button></td>" +
        '<td><span class="file-type">' + escapeHtml(file.type) + "</span></td>" +
        '<td class="numeric">' + escapeHtml(file.rows) + "</td>" +
        '<td class="numeric">' + escapeHtml(file.size) + "</td>" +
        '<td><span class="mono caption">' + escapeHtml(file.hash) + "</span></td>" +
        '<td><span class="status-pill ' + (status === "success" ? "success" : status) + '">' + escapeHtml(file.status) + "</span></td></tr>";
    }).join("");
    $$(".file-preview", body).forEach(function (button) {
      button.addEventListener("click", function () { previewFile(files[finite(button.dataset.index, 0)]); });
    });
  }

  async function fetchFileText(path) {
    var controller = new AbortController();
    var timer = window.setTimeout(function () { controller.abort(); }, 7000);
    try {
      var headers = { Accept: "text/plain, application/json" };
      if (API_TOKEN) headers["X-ISF-Token"] = API_TOKEN;
      var response = await window.fetch(
        "/api/file?path=" + encodeURIComponent(path),
        { headers: headers, signal: controller.signal }
      );
      if (!response.ok) throw new Error("HTTP " + response.status);
      var text = await response.text();
      var contentType = response.headers.get("content-type") || "";
      if (contentType.indexOf("application/json") >= 0) {
        try {
          var json = JSON.parse(text);
          if (typeof json.content === "string") return json.content;
          if (typeof json.data === "string") return json.data;
          return JSON.stringify(json, null, 2);
        } catch (error) {
          return text;
        }
      }
      return text;
    } finally {
      window.clearTimeout(timer);
    }
  }

  async function previewFile(file) {
    if (!file) return;
    var shell = byId("preview-shell");
    var pathNode = byId("preview-path");
    if (pathNode) { pathNode.textContent = file.path; pathNode.title = file.path; }
    if (shell) shell.innerHTML = '<div class="preview-empty">Loading ' + escapeHtml(file.name) + "…</div>";
    var content = file.content || "";
    if (state.apiOnline) {
      try {
        content = await fetchFileText(file.path);
      } catch (error) {
        content = "Unable to load provenance file from API.\n" + error.message;
        toast("文件接口不可用，内容未加载", "error");
      }
    }
    state.currentPreview = { file: file, content: content };
    if (shell) {
      shell.innerHTML = "";
      var pre = document.createElement("pre");
      pre.textContent = content || "(empty file)";
      shell.appendChild(pre);
    }
    if (byId("copy-preview")) byId("copy-preview").disabled = false;
    if (byId("download-preview")) byId("download-preview").disabled = false;
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy") ? resolve() : reject(new Error("Copy failed"));
      } catch (error) {
        reject(error);
      } finally {
        document.body.removeChild(area);
      }
    });
  }

  function safeFilename(value) {
    return String(value || "isf-export").trim().replace(/[^a-zA-Z0-9._-]+/g, "_").replace(/^_+|_+$/g, "") || "isf-export";
  }

  function downloadBlob(content, filename, type) {
    var blob = content instanceof Blob ? content : new Blob([content], { type: type || "application/octet-stream" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function selectedFiles() {
    var files = (state.currentResults || { files: [] }).files;
    return $$(".file-select:checked").map(function (checkbox) { return files[finite(checkbox.dataset.index, -1)]; }).filter(Boolean);
  }

  async function downloadFiles(files) {
    if (!files.length) {
      toast("请至少选择一个文件", "error");
      return;
    }
    var generated = 0;
    for (var index = 0; index < files.length; index += 1) {
      var file = files[index];
      var content = file.content || "";
      if (state.apiOnline) {
        try {
          content = await fetchFileText(file.path);
        } catch (error) {
          toast("无法读取 provenance 文件：" + file.name, "error");
          continue;
        }
      } else if (!content) {
        toast("API 不可达，无法读取：" + file.name, "error");
        continue;
      }
      downloadBlob(content, safeFilename(file.name), file.type === "json" ? "application/json" : "text/plain;charset=utf-8");
      generated += 1;
    }
    toast("已生成 " + generated + " 个下载文件", generated ? undefined : "error");
  }

  function csvEscape(value) {
    var text = String(value == null ? "" : value);
    return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  }

  function errorsCsv(errors) {
    var rows = [["k", "q_ref_mag", "q_ref_phase_deg", "q_dyn_mag", "q_dyn_phase_deg", "mag_error_db", "phase_error_deg", "cumulative_error_db"]];
    errors.forEach(function (row) { rows.push([row.k, row.refMag, row.refPhase, row.dynMag, row.dynPhase, row.magErrorDb, row.phaseError, row.cumulativeErrorDb]); });
    return rows.map(function (row) { return row.map(csvEscape).join(","); }).join("\n") + "\n";
  }

  function exportData() {
    var results = state.currentResults || unavailableResults(currentJobId(), "没有可导出的后端结果");
    if (results.source !== "api" || results.error) {
      toast(results.error || "没有可导出的后端结果", "error");
      return;
    }
    var formats = $$('input[name="export-format"]:checked').map(function (input) { return input.value; });
    if (!formats.length) {
      toast("请至少选择一种导出格式", "error");
      return;
    }
    var prefixInput = byId("export-prefix");
    var prefix = safeFilename(prefixInput ? prefixInput.value : "isf-" + results.id);
    var count = 0;
    if (formats.indexOf("json") >= 0) {
      downloadBlob(JSON.stringify({ config: flatConfig(readStorage(STORAGE.config, DEFAULT_CONFIG)), job: state.currentJob || readStorage(STORAGE.job, null), results: results }, null, 2), prefix + ".json", "application/json");
      count += 1;
    }
    if (formats.indexOf("csv") >= 0) {
      downloadBlob(errorsCsv(results.errors), prefix + "_harmonic_errors.csv", "text/csv;charset=utf-8");
      count += 1;
    }
    if (formats.indexOf("manifest") >= 0) {
      var manifest = {
        job_id: results.id,
        generated_at: new Date().toISOString(),
        source: results.source,
        files: results.files.map(function (file) { return { name: file.name, path: file.path, type: file.type, rows: file.rows, size: file.size, hash: file.hash, status: file.status }; })
      };
      downloadBlob(JSON.stringify(manifest, null, 2), prefix + "_manifest.json", "application/json");
      count += 1;
    }
    toast("已生成 " + count + " 个导出文件");
  }

  function setInspectPill(id, label, stateName) {
    var element = byId(id);
    if (!element) return;
    element.className = "status-pill" + (stateName ? " " + stateName : "");
    element.textContent = label;
  }

  function setInspectText(id, value) {
    var element = byId(id);
    if (element) element.textContent = value;
  }

  function inspectFiniteStats(results) {
    var total = 0;
    var invalid = 0;
    Object.keys(results.charts || {}).forEach(function (key) {
      var group = results.charts[key];
      if (!Array.isArray(group)) return;
      group.forEach(function (series) {
        var values = series && Array.isArray(series.values) ? series.values : [];
        values.forEach(function (point) {
          [point.x, point.y].forEach(function (value) {
            total += 1;
            if (finiteOrNull(value) === null) invalid += 1;
          });
        });
      });
    });
    (results.errors || []).forEach(function (row) {
      Object.keys(row || {}).forEach(function (key) {
        if (typeof row[key] !== "number") return;
        total += 1;
        if (!Number.isFinite(row[key])) invalid += 1;
      });
    });
    return { total: total, invalid: invalid };
  }

  function validationItem(title, detail, stateName, label) {
    var glyph = stateName === "success" ? "✓" : (stateName === "failed" ? "×" : "!");
    return '<li class="check-item"><span class="check-glyph' + (stateName === "success" ? "" : " warning") + '" aria-hidden="true">' + glyph + '</span><span class="check-copy"><strong>' + escapeHtml(title) + '</strong><span>' + escapeHtml(detail) + '</span></span><span class="status-pill ' + escapeHtml(stateName) + '">' + escapeHtml(label) + '</span></li>';
  }

  function renderInspectQuality(results) {
    var available = results.source === "api" && !results.error;
    if (!available) {
      ["quality-complete", "quality-phase", "quality-nan", "quality-unit"].forEach(function (id) { setInspectText(id, "N/A"); });
      ["quality-complete-status", "quality-phase-status", "quality-nan-status", "quality-unit-status"].forEach(function (id) { setInspectPill(id, "N/A", ""); });
      setInspectText("quality-complete-copy", results.error || "未加载");
      setInspectText("quality-phase-copy", "未加载");
      setInspectText("quality-nan-copy", "未加载");
      setInspectText("quality-unit-copy", "未加载");
      var pendingList = byId("validation-list");
      if (pendingList) pendingList.innerHTML = '<li class="check-item"><span class="check-copy"><strong>N/A</strong><span>验证结果未加载</span></span><span class="status-pill">N/A</span></li>';
      setInspectPill("validation-summary", "N/A", "");
      return;
    }
    var metadata = results.metadata || {};
    var dualTb = results.dualTb || {};
    var files = results.files || [];
    var requiredNames = ["vco_results.csv", "replica_results.csv", "workpoint.csv", "metadata.json", "manifest.json"];
    var presentNames = files.map(function (file) { return file.name; });
    var present = requiredNames.filter(function (name) { return presentNames.indexOf(name) >= 0; }).length;
    var completenessOk = available && present === requiredNames.length;
    setInspectText("quality-complete", available ? ((100 * present / requiredNames.length).toFixed(1) + "%") : "N/A");
    setInspectText("quality-complete-copy", available ? (present + " / " + requiredNames.length + " provenance 文件") : (results.error || "等待后端结果"));
    setInspectPill("quality-complete-status", completenessOk ? "pass" : (available ? "fail" : "N/A"), completenessOk ? "success" : (available ? "failed" : ""));

    var spread = finiteOrNull(metadata.raw_t0_spread_s);
    var noShift = metadata.manual_waveform_shift === false;
    var originOk = available && spread !== null && Math.abs(spread) <= 1e-15 && noShift;
    setInspectText("quality-phase", spread !== null ? ((spread * 1e15).toFixed(3) + " fs") : "N/A");
    setInspectText("quality-phase-copy", originOk ? "共同原点；manual_waveform_shift=false" : "共同原点或无平移证据不完整");
    setInspectPill("quality-phase-status", originOk ? "pass" : (available ? "fail" : "N/A"), originOk ? "success" : (available ? "failed" : ""));

    var finiteStats = inspectFiniteStats(results);
    var finiteOk = available && finiteStats.total > 0 && finiteStats.invalid === 0;
    setInspectText("quality-nan", available ? (finiteStats.invalid + " / " + finiteStats.total) : "N/A");
    setInspectText("quality-nan-copy", finiteOk ? "当前渲染数值均为有限值" : "数值结果不可用或包含非有限值");
    setInspectPill("quality-nan-status", finiteOk ? "clean" : (available ? "fail" : "N/A"), finiteOk ? "success" : (available ? "failed" : ""));

    var unitOk = metadata.canonical_noise_quantity === "ASD_A_per_sqrt_Hz" && metadata.source_noise_quantity === "PSD_A2_per_Hz" && metadata.current_direction === "drain_to_source" && metadata.carrier_amplitude === "differential_peak" && metadata.output_noise === "PM_SSB";
    setInspectText("quality-unit", unitOk ? "ASD · PM-SSB" : "N/A");
    setInspectText("quality-unit-copy", unitOk ? "PSD 开方一次 · D→S · differential peak" : "单位/方向/载波合同不完整");
    setInspectPill("quality-unit-status", unitOk ? "pass" : (available ? "fail" : "N/A"), unitOk ? "success" : (available ? "failed" : ""));

    var checks = [];
    var rowCountOk = available && Number(dualTb.vco_rows) > 0 && Number(dualTb.replica_rows) > 0 && Number(dualTb.workpoint_rows) > 0 && (results.summaryRows || []).length > 0;
    checks.push({ title: "Canonical 数据完整", detail: rowCountOk ? ((results.summaryRows || []).length + " points · " + dualTb.vco_rows + "/" + dualTb.replica_rows + "/" + dualTb.workpoint_rows + " rows") : "缺少 summary 或双 TB 行数", state: rowCountOk ? "success" : "failed", label: rowCountOk ? "pass" : "fail" });
    checks.push({ title: "共同时间原点", detail: originOk ? ("raw t0 spread " + (spread * 1e15).toFixed(3) + " fs；无人工平移") : "原始 t0/replica 驱动原点证据不足", state: originOk ? "success" : "failed", label: originOk ? "pass" : "fail" });
    var sharedOk = metadata.common_source_normalization === "Mdyn[k]=Dk*sqrt_Sxi";
    checks.push({ title: "共享 flicker 归一化", detail: sharedOk ? "所有谐波共享同一个 sqrt(Sxi)" : "未证明 Mdyn[k]=Dk·sqrt(Sxi)", state: sharedOk ? "success" : "failed", label: sharedOk ? "pass" : "fail" });
    var phaseOk = String(metadata.phase_selection || "").indexOf("HBNOISE_AM_PM") >= 0;
    var hbacSecondary = metadata.hbac_role === "secondary_validation_only";
    checks.push({ title: "动态相位路径", detail: phaseOk ? ("HBNOISE AM/PM 工程选角；HBAC " + (hbacSecondary ? "仅二次验证" : "角色未锁定")) : "未记录 AM/PM 默认选角", state: phaseOk && hbacSecondary ? "success" : "warning", label: phaseOk && hbacSecondary ? "pass" : "warn" });
    var closureEvidence = closureGateEvidence(results.summaryRows, results.metrics && results.metrics.trendDb);
    checks.push({
      title: "Spectre 闭合双 gate",
      detail: "max |error| " + (closureEvidence.maxAbs === null ? "N/A" : closureEvidence.maxAbs.toFixed(3) + " dB") + " · trend " + (closureEvidence.trend === null ? "N/A" : closureEvidence.trend.toFixed(3) + " dB"),
      state: closureEvidence.absPass ? (closureEvidence.trendPass ? "success" : "warning") : "failed",
      label: closureEvidence.absPass ? (closureEvidence.trendPass ? "pass" : "warn") : "fail"
    });
    var provenance = results.provenance || {};
    var provenanceOk = available && provenance.job_id === results.id && Boolean(provenance.manifest_sha256);
    var isSignoff = results.signoff && results.runKind === "signoff";
    checks.push({ title: "任务与 PSF provenance", detail: provenanceOk ? ((results.runKind || "import").toUpperCase() + " · " + Number(provenance.psf_runs || 0) + " PSF · manifest " + String(provenance.manifest_sha256).slice(0, 12)) : "缺少 job/manifest 归属证明", state: provenanceOk ? (isSignoff ? "success" : "warning") : "failed", label: provenanceOk ? (isSignoff ? "signoff" : "replay") : "fail" });

    var list = byId("validation-list");
    if (list) list.innerHTML = checks.map(function (item) { return validationItem(item.title, item.detail, item.state, item.label); }).join("");
    var failed = checks.filter(function (item) { return item.state === "failed"; }).length;
    var warnings = checks.filter(function (item) { return item.state === "warning"; }).length;
    var passed = checks.filter(function (item) { return item.state === "success"; }).length;
    setInspectPill("validation-summary", failed ? (failed + " fail") : (warnings ? (passed + " pass · " + warnings + " warn") : (passed + " pass")), failed ? "failed" : (warnings ? "warning" : "success"));
  }

  function renderInspect(results) {
    state.currentResults = results;
    renderFiles(results.files || []);
    renderInspectQuality(results);
    var prefix = byId("export-prefix");
    if (prefix && !prefix.dataset.edited) prefix.value = results.source === "api" && !results.error && results.id ? "isf_" + safeFilename(results.id) : "";
  }

  async function loadInspectResults(feedback) {
    var id = currentJobId();
    if (!state.apiOnline) {
      renderInspect(unavailableResults(id, "API 不可达"));
      if (feedback) toast("API 不可达，结果未加载", "error");
      return;
    }
    try {
      var payload = await apiRequest("/api/results/" + encodeURIComponent(id), { timeout: 8000 });
      renderInspect(normalizeResults(payload, id));
      if (feedback) toast("数据清单已从 API 更新");
    } catch (error) {
      renderInspect(unavailableResults(id, "结果接口不可用：" + errorMessage(error)));
      if (feedback) toast("结果接口不可用，结果未加载", "error");
    }
  }

  function initInspect() {
    renderInspect(unavailableResults(currentJobId(), "等待 API 结果"));
    var selectAll = byId("select-all-files");
    if (selectAll) selectAll.addEventListener("change", function () {
      $$(".file-select").forEach(function (checkbox) { checkbox.checked = selectAll.checked; });
    });
    var refresh = byId("refresh-data");
    if (refresh) refresh.addEventListener("click", async function () {
      setButtonBusy(refresh, true);
      await loadInspectResults(true);
      setButtonBusy(refresh, false);
    });
    var downloadSelected = byId("download-selected");
    if (downloadSelected) downloadSelected.addEventListener("click", function () { downloadFiles(selectedFiles()); });
    var exportAll = byId("export-all");
    if (exportAll) exportAll.addEventListener("click", exportData);
    var buildExport = byId("build-export");
    if (buildExport) buildExport.addEventListener("click", exportData);
    var prefix = byId("export-prefix");
    if (prefix) prefix.addEventListener("input", function () { prefix.dataset.edited = "true"; });
    var copy = byId("copy-preview");
    if (copy) copy.addEventListener("click", function () {
      if (!state.currentPreview) return;
      copyText(state.currentPreview.content).then(function () { toast("预览内容已复制"); }).catch(function () { toast("复制失败，请手动选择文本", "error"); });
    });
    var downloadPreview = byId("download-preview");
    if (downloadPreview) downloadPreview.addEventListener("click", function () {
      if (!state.currentPreview) return;
      downloadBlob(state.currentPreview.content, safeFilename(state.currentPreview.file.name), "text/plain;charset=utf-8");
      toast("文件已生成");
    });
  }

  function renderErrorCatalog(catalog, query) {
    var list = byId("error-catalog-list");
    if (!list) return;
    var needle = String(query || "").trim().toLowerCase();
    var entries = (catalog && catalog.entries || []).filter(function (entry) {
      if (!needle) return true;
      return JSON.stringify(entry).toLowerCase().indexOf(needle) >= 0;
    });
    list.innerHTML = entries.map(function (entry) {
      var causes = (entry.causes || []).map(function (item) { return "<li>" + escapeHtml(item) + "</li>"; }).join("");
      var actions = (entry.actions || []).map(function (item) { return "<li>" + escapeHtml(item) + "</li>"; }).join("");
      return '<article class="error-entry" id="' + escapeHtml(entry.code) + '" data-code="' + escapeHtml(entry.code) + '">' +
        '<header><code>' + escapeHtml(entry.code) + '</code><span class="status-pill ' + (entry.severity === "warning" ? "warning" : "failed") + '">' + escapeHtml(entry.severity || "error") + '</span></header>' +
        '<h2>' + escapeHtml(entry.title || "") + '</h2><p>' + escapeHtml(entry.summary || "") + '</p>' +
        '<div class="error-entry-grid"><div><strong>常见原因</strong><ul>' + causes + '</ul></div><div><strong>建议动作</strong><ol>' + actions + '</ol></div></div>' +
      '</article>';
    }).join("") || '<div class="empty-state">没有匹配的错误码。</div>';
    var meta = byId("error-catalog-meta");
    if (meta) meta.textContent = entries.length + " / " + (catalog && catalog.entries ? catalog.entries.length : 0) + " 项 · schema " + finite(catalog && catalog.schema_version, 1);
  }

  async function initErrors() {
    var catalog;
    try {
      catalog = await apiRequest("/api/errors", { timeout: 5000 });
    } catch (_apiError) {
      try {
        var response = await window.fetch("error_catalog.json", { cache: "no-store" });
        if (!response.ok) throw new Error("catalog unavailable");
        catalog = await response.json();
      } catch (error) {
        reportError(error, "本地错误码目录无法读取");
        return;
      }
    }
    var params = new URLSearchParams(window.location.search);
    var initial = params.get("code") || "";
    var search = byId("error-search");
    if (search) {
      search.value = initial;
      search.addEventListener("input", function () { renderErrorCatalog(catalog, search.value); });
    }
    renderErrorCatalog(catalog, initial);
    if (initial) {
      window.setTimeout(function () {
        var target = byId(initial);
        if (target) target.scrollIntoView({ block: "center" });
      }, 50);
    }
  }

  async function boot() {
    setupNavigation();
    bindGlobalActions();
    window.addEventListener("scroll", clearPhaseInteraction, true);
    window.addEventListener("blur", clearPhaseInteraction);
    var page = document.body.dataset.page;
    if (page === "setup") initSetup();
    if (page === "discover") initDiscover();
    if (page === "results") initResults();
    if (page === "inspect") initInspect();
    if (page === "errors") initErrors();
    initWorkspaceStates();

    var online = await checkHealth(false);
    if (!online) return;
    var restored = readStorage(STORAGE.workspaceState, null);
    if (byId("workspace-state-list")) refreshWorkspaceStates().catch(function (error) { workspaceStateStatus(errorMessage(error)); });
    if (page === "setup" && !state.setupDirty && !restored) loadRemoteConfig(false);
    if (page === "discover") {
      refreshRemoteJob(false);
      var discoverConfig = flatConfig(readStorage(STORAGE.config, DEFAULT_CONFIG));
      if (!restored) requestPreflight(discoverConfig, false).then(function (report) {
        if (report) renderDiscoveryInputs(
          report.detected && (report.detected.maestro_pair || report.detected.inputs),
          discoverConfig
        );
      });
    }
    if (page === "results") {
      await populateResultJobs(restored && restored.view && restored.view.job_id || currentJobId());
      loadRemoteResults(false);
    }
    if (page === "inspect") loadInspectResults(false);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
