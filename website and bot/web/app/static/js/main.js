import { translate } from "./i18n.js";
import { getAll, getSetting, openDatabase, purgeLegacySensitiveStorage, putMany, putOne, setSetting } from "./storage.js";

const $ = (id) => document.getElementById(id);
const uid = () => crypto.randomUUID?.() ?? `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
const progressClasses = ["p0", "p10", "p15", "p20", "p30", "p40", "p50", "p60", "p70", "p80", "p90", "p100"];
const history = { pulse: [], spo2: [], temperature: [], respiration: [] };
const ECG_BUFFER_LEN = 160;
const ecgWaveBuffer = [];
let ecgSamplePhase = 0;
let lastPacketCount = -1;
let lastHardwareButtonCount = null;
let lastHardwareButtonDown = false;
let lastDemoSample = 0;
let isFetchingSensors = false;

const DEMO_SCENARIOS = {
  normal: {
    pulse: 78, spo2: 98, temperature: 36.6, respiration: 16, tremor: 0.1,
    ecg_mv: 1640, ecg_leads_ok: true, air_co2_ppm: 435, finger_on_sensor: true, temp_ok: true, currentZone: "GREEN"
  },
  review: {
    pulse: 98, spo2: 94, temperature: 37.7, respiration: 21, tremor: 0.35,
    ecg_mv: 1790, ecg_leads_ok: true, air_co2_ppm: 860, finger_on_sensor: true, temp_ok: true, currentZone: "YELLOW"
  },
  urgent: {
    pulse: 118, spo2: 91, temperature: 38.4, respiration: 25, tremor: 0.72,
    ecg_mv: 2140, ecg_leads_ok: true, air_co2_ppm: 1290, finger_on_sensor: true, temp_ok: true, air_ok: true, connected: true, currentZone: "RED"
  },
  disconnected: {
    pulse: 0, spo2: 0, temperature: 0, respiration: 0, tremor: 0,
    ecg_mv: 0, ecg_leads_ok: false, air_co2_ppm: 420, finger_on_sensor: false, temp_ok: false, air_ok: false, connected: false, currentZone: "GREEN"
  }
};
const state = {
  lang: "uz",
  online: false,
  api: false,
  patients: [],
  replies: [],
  lastReplyId: 0,
  vitals: {
    pulse: null, spo2: null, temperature: null, respiration: null, tremor: null,
    ecg_mv: null, ecg_leads_ok: true, air_co2_ppm: null, finger_on_sensor: true, temp_ok: true,
    button_start: false, currentZone: "GREEN", ecg_wave: []
  },
  examRunning: false,
  examCompleted: false,
  scenario: "urgent",
  sensorMode: "auto",
  hardwareSeen: false,
  assessment: null,
  sensorSource: "waiting",
  db: null,
  deviceId: "",
  nurse: {
    name: "Salimova Nilufar",
    id: "PN-204",
    facility: "Bo‘stonliq tumani 4-sonli OSHP"
  }
};

function generateEcgBatch(bpm = 76, leadsOk = true, count = 16) {
  const out = [];
  if (!leadsOk) {
    for (let i = 0; i < count; i += 1) out.push(2048);
    return out;
  }
  const beatsPerSec = Math.max(45, Math.min(160, Number(bpm) || 76)) / 60;
  const stepPerSample = beatsPerSec / 50; // 50 Hz AD8232 sampling rate
  for (let i = 0; i < count; i += 1) {
    ecgSamplePhase = (ecgSamplePhase + stepPerSample) % 1;
    const c = ecgSamplePhase;
    let delta = Math.sin(c * Math.PI * 12) * 10;
    if (c > 0.12 && c < 0.22) delta += Math.sin(((c - 0.12) / 0.1) * Math.PI) * 145; // P wave
    else if (c >= 0.27 && c < 0.30) delta -= ((c - 0.27) / 0.03) * 190; // Q dip
    else if (c >= 0.30 && c < 0.34) delta += Math.sin(((c - 0.30) / 0.04) * Math.PI) * 890; // R peak (~2938)
    else if (c >= 0.34 && c < 0.38) delta -= Math.sin(((c - 0.34) / 0.04) * Math.PI) * 260; // S dip
    else if (c >= 0.49 && c < 0.65) delta += Math.sin(((c - 0.49) / 0.16) * Math.PI) * 210; // T wave
    out.push(clamp(Math.round(2048 + delta), 0, 4095));
  }
  return out;
}

function appendEcgSamples(samples) {
  if (!Array.isArray(samples) || samples.length === 0) return;
  for (const val of samples) {
    const num = Number(val);
    if (Number.isFinite(num)) ecgWaveBuffer.push(clamp(Math.round(num), 0, 4095));
  }
  while (ecgWaveBuffer.length > ECG_BUFFER_LEN) ecgWaveBuffer.shift();
}

function setBadge(elementId, variant, text) {
  const el = $(elementId);
  if (!el) return;
  el.classList.remove("badge-ok", "badge-warn", "badge-err");
  el.classList.add(variant);
  el.textContent = text;
}

const t = (key) => translate(state.lang, key);

function cleanText(value, maxLength) {
  return String(value ?? "")
    .normalize("NFKC")
    .replace(/[\u0000-\u001F\u007F]/g, " ")
    .replace(/[<>]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, maxLength);
}

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

function showToast(message) {
  const element = $("toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => element.classList.remove("show"), 3200);
}

function setInlineError(elementId, message = "") {
  const element = $(elementId);
  element.textContent = message;
  element.classList.toggle("hidden", !message);
}

function setProgress(element, percentage) {
  element.classList.remove(...progressClasses);
  element.classList.add(`p${percentage}`);
}

function applyLanguage() {
  document.documentElement.lang = state.lang;
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
    element.placeholder = t(element.dataset.i18nPlaceholder);
  });
  document.querySelectorAll(".lang").forEach((button) => {
    const active = button.dataset.lang === state.lang;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  $("sensorStatus").textContent = t(state.sensorSource);
  updateSensorLabels();
  if (state.sensorSource === "hardwareMissing") setInlineError("telemetryError", t("hardwareWaiting"));
  setInlineError("formError");
  updateModeControl();
  if (state.assessment) state.assessment = buildDemoAssessment();
  updateReviewCard();
  renderRecords();
}

function updateModeControl() {
  const button = $("modeToggle");
  button.classList.toggle("online", state.online);
  button.setAttribute("aria-pressed", String(state.online));
  button.querySelector("span").textContent = t(state.online ? "online" : "offline");
  $("modeIcon").src = `/static/icons/${state.online ? "wifi" : "wifi-off"}.svg`;
  const connection = !state.online ? "connectionOffline" : state.api ? "connectionServer" : "connectionDemo";
  $("connectionSummary").textContent = t(connection);
  $("syncButton").querySelector("span").textContent = t(state.api ? "sync" : "syncDemo");
}

function demoOnly(record) {
  return typeof record.server_id === "string" && record.server_id.startsWith("demo-");
}

function awaitsSync(record) {
  return record.status !== "synced" || (state.api && demoOnly(record));
}

function showSyncFeedback(key, isError = false) {
  const feedback = $("syncFeedback");
  feedback.textContent = t(key);
  feedback.dataset.i18n = key;
  feedback.classList.toggle("is-error", isError);
  feedback.classList.remove("hidden");
  showToast(t(key));
}

function renderRecords() {
  const pending = state.patients.filter(awaitsSync).length;
  $("queueCount").textContent = String(pending);
  document.querySelector(".queue-pill")?.classList.toggle("is-synced", pending === 0);
  $("replyBadge").textContent = String(state.replies.length);
  $("patientTotal").textContent = String(state.patients.length);
  const hasPatients = state.patients.length > 0;
  $("patientEmpty").classList.toggle("hidden", hasPatients);
  $("patientTableWrap").classList.toggle("hidden", !hasPatients);
  $("patientTableWrap").setAttribute("aria-busy", "false");
  $("loadDemoButton").disabled = state.patients.some((record) => record.demo_data);
  $("patientTableBody").innerHTML = state.patients.slice().reverse().map((record) => {
    const vitals = record.vitals || {};
    const assessment = record.ai_assessment || { level: "unvalidated", score: null };
    const levelKey = assessment.level === "demo_high" ? "zoneHigh" : assessment.level === "demo_review" ? "zoneReview" : assessment.level === "demo_low" ? "zoneLow" : "zonePending";
    const time = new Date(record.captured_at || Date.now()).toLocaleTimeString(state.lang === "uz" ? "uz-UZ" : "ru-RU", { hour: "2-digit", minute: "2-digit" });
    const statusLabel = awaitsSync(record) ? "pending" : demoOnly(record) ? "demoSyncedStatus" : "syncedStatus";
    const snapshot = `${t("opticalPulse")} ${vitals.pulse ?? "—"} · SpO₂ ${vitals.spo2 ?? "—"}% · ${t("temperature")} ${vitals.temperature ?? "—"}°C · ${t("respiration")} ${vitals.respiration ?? "—"}${t("perMinute")}`;
    return `<tr>
      <td data-label="${escapeHtml(t("time"))}"><time datetime="${escapeHtml(record.captured_at || "")}">${escapeHtml(time)}</time></td>
      <td data-label="${escapeHtml(t("patient"))}"><div><strong>${escapeHtml(record.patient.name)}</strong><small>${escapeHtml(record.patient.age)} · ${escapeHtml(record.patient.village)}${record.demo_data ? ` · ${escapeHtml(t("testData"))}` : ""}</small><small>${escapeHtml(t("complaintText"))}: ${escapeHtml(record.complaint || "—")}</small><small>${escapeHtml(t("nurseDiagnosis"))}: ${escapeHtml(record.nurse_diagnosis || "—")}</small></div></td>
      <td data-label="${escapeHtml(t("shortSensors"))}" class="vitals-cell"><span>${escapeHtml(snapshot)}</span></td>
      <td data-label="${escapeHtml(t("demoZone"))}"><span class="zone-badge ${escapeHtml(assessment.level)}">${escapeHtml(t(levelKey))}${assessment.score === null || assessment.score === undefined ? "" : ` · ${escapeHtml(assessment.score)}/100`}</span></td>
      <td data-label="${escapeHtml(t("shortSync"))}"><span class="record-status ${statusLabel === "syncedStatus" ? "synced" : ""}">${escapeHtml(t(statusLabel))}</span></td>
    </tr>`;
  }).join("");

  const replyList = $("replyList");
  replyList.setAttribute("aria-busy", "false");
  replyList.innerHTML = state.replies.length
    ? state.replies.slice().reverse().map((reply) => `
      <article class="reply-card">
        <div class="reply-meta"><b>${escapeHtml(reply.action || "doctor_reply")}</b><time datetime="${escapeHtml(reply.created_at || "")}">${new Date(reply.created_at || Date.now()).toLocaleString(state.lang === "uz" ? "uz-UZ" : "ru-RU", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" })}</time></div>
        <p>${escapeHtml(reply.message)}</p>
      </article>`).join("")
    : `<div class="inline-state empty-state"><span>${t("noReplies")}</span></div>`;
}

function normalizeSensorPayload(raw) {
  const n = raw?.sensors ?? raw?.data ?? raw ?? {};
  const number = (value) => typeof value === "number" && Number.isFinite(value) ? value : null;
  const pulse = number(n.heart_rate_bpm ?? n.hr ?? raw?.pulse);
  const spo2 = number(n.spo2_percent ?? n.spo2 ?? raw?.spo2);
  const temperature = number(n.temperature_c ?? n.temp ?? raw?.temperature);
  const co2 = number(n.air_co2_ppm ?? n.co2);
  const mqRaw = number(n.mq_raw);
  const wave = Array.isArray(n.ecg_wave) ? n.ecg_wave.filter(Number.isFinite) : [];
  const finger = n.finger_on_sensor ?? n.finger;
  return {
    pulse: finger !== false && pulse > 0 ? pulse : null,
    spo2: finger !== false && spo2 > 0 ? spo2 : null,
    temperature: n.temp_ok !== false && temperature !== 0 && temperature !== -127 ? temperature : null,
    respiration: number(n.respiration_rate ?? raw?.respiration),
    tremor: number(n.tremor_hz ?? raw?.tremor),
    ecg_mv: number(n.ecg_mv),
    ecg_leads_ok: n.ecg_leads_ok ?? wave.length > 0,
    ecg_wave: wave,
    air_co2_ppm: co2,
    mq_raw: mqRaw,
    finger_on_sensor: finger ?? (pulse > 0 || spo2 > 0),
    temp_ok: n.temp_ok ?? temperature !== null,
    air_ok: co2 !== null || mqRaw !== null,
    button_start: n.button_start === true,
    currentZone: null,
    connected: true,
  };
}

function simulateVitals() {
  const current = state.vitals;
  const baseline = DEMO_SCENARIOS[state.scenario];
  const fingerOk = baseline.finger_on_sensor !== false;
  const tempOk = baseline.temp_ok !== false;
  const airOk = baseline.air_ok !== false;

  current.pulse = fingerOk ? clamp(Math.round(baseline.pulse + (Math.random() - 0.5) * 4), 45, 150) : 0;
  current.spo2 = fingerOk ? clamp(Math.round(baseline.spo2 + (Math.random() - 0.5) * 2), 80, 100) : 0;
  current.temperature = tempOk ? +clamp(baseline.temperature + (Math.random() - 0.5) * 0.16, 34, 42).toFixed(1) : 0;
  current.respiration = airOk ? clamp(Math.round(baseline.respiration + (Math.random() - 0.5) * 2), 8, 40) : 0;
  current.tremor = +clamp(baseline.tremor + (Math.random() - 0.5) * 0.08, 0, 1).toFixed(2);
  current.ecg_mv = baseline.ecg_leads_ok ? clamp(Math.round(baseline.ecg_mv + (Math.random() - 0.5) * 90), 900, 3100) : 0;
  current.ecg_leads_ok = baseline.ecg_leads_ok;
  current.air_co2_ppm = airOk ? clamp(Math.round(baseline.air_co2_ppm + (Math.random() - 0.5) * 28), 400, 2200) : 0;
  current.finger_on_sensor = fingerOk;
  current.temp_ok = tempOk;
  current.air_ok = airOk;
  current.connected = baseline.connected !== false;
  current.currentZone = baseline.currentZone;
  current.button_start = false;
  current.ecg_wave = generateEcgBatch(current.pulse || 76, current.ecg_leads_ok, 16);
  appendEcgSamples(current.ecg_wave);
  updateVitals("simulated");
}

function clearSensorHistory() {
  ecgWaveBuffer.length = 0;
  Object.values(history).forEach((series) => { series.length = 0; });
  lastPacketCount = -1;
}

function usesHardware() {
  return state.sensorMode === "live" || (state.sensorMode === "auto" && state.hardwareSeen);
}

function updateSensorLabels() {
  const live = usesHardware();
  $("sensorScenario").disabled = live;
  $("airCardTitle").textContent = t(live ? "mqLiveTitle" : "mq135Title");
  $("airCardLabel").textContent = t(live ? "mqRawLabel" : "airLabel");
  $("airCardUnit").textContent = t(live ? "mqEstimateUnit" : "perMinute");
  $("espZoneBadge").classList.toggle("hidden", live);
}

function showNoHardware() {
  if (state.sensorSource !== "hardwareMissing") {
    clearSensorHistory();
    state.vitals = normalizeSensorPayload({});
    state.vitals.connected = false;
    state.assessment = null;
    updateVitals("hardwareMissing");
  }
  if ($("telemetryError").classList.contains("hidden")) {
    setInlineError("telemetryError", t("hardwareWaiting"));
  }
}

function handleHardwareButton(raw, pressed) {
  const count = raw.buttonPressCount;
  const newPress = Number.isInteger(count) && lastHardwareButtonCount !== null
    ? count > lastHardwareButtonCount
    : pressed && !lastHardwareButtonDown;
  lastHardwareButtonCount = Number.isInteger(count) ? count : null;
  lastHardwareButtonDown = pressed;
  if (newPress && !state.examRunning) startExam();
}

async function refreshVitals() {
  if (state.sensorMode !== "demo") {
    if (isFetchingSensors) return;
    isFetchingSensors = true;
    try {
      const response = await fetch("/api/sensors/live", { signal: AbortSignal.timeout(1250), cache: "no-store" });
      if (!response.ok) throw new Error("Sensor connection unavailable");
      const raw = await response.json();
      if (state.sensorMode !== "demo" && raw.connected === true && !/^simulated/i.test(raw.source || "")) {
        if (state.sensorSource !== "liveHardware") {
          clearSensorHistory();
          state.assessment = null;
        }
        state.hardwareSeen = true;
        setInlineError("telemetryError");
        if (Number.isInteger(raw.packetCount) && raw.packetCount === lastPacketCount) return;
        lastPacketCount = raw.packetCount ?? -1;
        state.vitals = normalizeSensorPayload(raw);
        if (!state.vitals.ecg_leads_ok) ecgWaveBuffer.length = 0;
        else appendEcgSamples(state.vitals.ecg_wave);
        handleHardwareButton(raw, state.vitals.button_start);
        updateVitals("liveHardware");
        return;
      }
    } catch {
      // A missing server or packet is handled by the selected sensor mode below.
    } finally {
      isFetchingSensors = false;
    }
  }
  if (usesHardware()) {
    showNoHardware();
  } else if (Date.now() - lastDemoSample >= 900) {
    lastDemoSample = Date.now();
    setInlineError("telemetryError");
    simulateVitals();
  }
}

function updateVitals(sourceKey = "simulated") {
  const v = state.vitals;
  const live = usesHardware();
  const fingerOk = v.finger_on_sensor === true;
  const tempOk = v.temp_ok === true && Number.isFinite(v.temperature);
  const leadsOk = v.ecg_leads_ok !== false;
  const airOk = v.air_ok !== false && v.connected !== false && (live
    ? v.air_co2_ppm != null || v.mq_raw != null
    : Number(v.respiration) > 0);

  $("pulseValue").textContent = fingerOk && v.pulse > 0 ? String(v.pulse) : "—";
  if ($("maxHrValue")) $("maxHrValue").textContent = fingerOk && v.pulse > 0 ? `${v.pulse}${t("perMinute")}` : "—";
  if ($("ecgMvValue")) $("ecgMvValue").textContent = leadsOk ? String(v.ecg_mv ?? "—") : "—";
  $("spo2Value").textContent = fingerOk && Number(v.spo2) > 0 ? String(v.spo2) : "—";
  $("temperatureValue").textContent = tempOk ? String(v.temperature) : "—";
  if ($("co2Value")) $("co2Value").textContent = airOk ? String(v.air_co2_ppm ?? "—") : "—";
  $("respirationValue").textContent = airOk ? String((live ? v.air_co2_ppm : v.respiration) ?? "—") : "—";
  $("tremorValue").textContent = v.tremor ?? "—";

  setBadge("ecgLeadStatus", leadsOk ? "badge-ok" : "badge-err", t(leadsOk ? "ecgLeadsOk" : "ecgLeadsOff"));
  setBadge("fingerStatus", fingerOk ? "badge-ok" : "badge-warn", t(fingerOk ? "fingerOn" : "fingerOff"));
  setBadge("tempStatus", tempOk ? "badge-ok" : "badge-warn", t(tempOk ? "tempProbeOk" : "tempProbeOff"));

  const co2High = Number(v.air_co2_ppm) >= 1000;
  if (!airOk) {
    setBadge("co2Status", "badge-warn", t("airNoData"));
    if ($("airQualityText")) $("airQualityText").textContent = t("airNoData");
  } else if (live) {
    setBadge("co2Status", "badge-ok", t("liveHardware"));
    $("airQualityText").textContent = v.mq_raw ?? "—";
  } else {
    setBadge("co2Status", co2High ? "badge-warn" : "badge-ok", t(co2High ? "co2Elevated" : "co2Normal"));
    if ($("airQualityText")) $("airQualityText").textContent = t(co2High ? "airStuff" : "airFresh");
  }

  if (!leadsOk && !fingerOk && !tempOk && !airOk) {
    setBadge("espZoneBadge", "badge-warn", t("deviceOfflineBadge"));
  } else {
    const zone = ["RED", "YELLOW", "GREEN"].includes(v.currentZone) ? v.currentZone : "GREEN";
    const zoneVariant = zone === "RED" ? "badge-err" : zone === "YELLOW" ? "badge-warn" : "badge-ok";
    setBadge("espZoneBadge", zoneVariant, t(`hwZone_${zone}`));
  }

  const seriesValues = {
    pulse: live ? (leadsOk && ecgWaveBuffer.length ? v.ecg_mv ?? 0 : null) : Number(v.pulse) || 0,
    spo2: fingerOk && v.spo2 > 0 ? v.spo2 : null,
    temperature: tempOk ? v.temperature : null,
    respiration: airOk ? (live ? v.air_co2_ppm ?? v.mq_raw : v.respiration) : null
  };
  Object.entries(seriesValues).forEach(([key, numVal]) => {
    if (numVal === null) history[key].length = 0;
    else {
      history[key].push(numVal);
      if (history[key].length > 28) history[key].shift();
    }
    drawChart(`${key}Chart`, history[key]);
  });

  if (state.sensorSource !== sourceKey) $("sensorStatus").textContent = t(sourceKey);
  state.sensorSource = sourceKey;
  updateSensorLabels();
}

const wavePhase = { pulseChart: 0, spo2Chart: 0, temperatureChart: 0, respirationChart: 0 };

function buildOscilloscopePoints(id, values, width, height) {
  if (id === "pulseChart") {
    const leadsOk = state.vitals.ecg_leads_ok !== false;
    const samples = usesHardware() || (leadsOk && ecgWaveBuffer.length > 4)
      ? ecgWaveBuffer
      : generateEcgBatch(76, leadsOk, 96);
    const maxDev = Math.max(360, ...samples.map((v) => Math.abs(v - 2048)));
    const scaleSpan = maxDev * 1.18;
    const pts = [];
    for (let i = 0; i < samples.length; i += 1) {
      const x = (i / Math.max(1, samples.length - 1)) * width;
      const normY = 0.5 - ((samples[i] - 2048) / (scaleSpan * 2));
      const y = clamp(normY * height, 8, height - 8);
      pts.push({ x, y });
    }
    return pts;
  }

  if (usesHardware()) {
    const min = Math.min(...values);
    const max = Math.max(...values);
    return values.map((value, index) => ({
      x: values.length === 1 ? width / 2 : index * width / (values.length - 1),
      y: max === min ? height / 2 : height - 8 - (value - min) / (max - min) * (height - 16),
    }));
  }

  wavePhase[id] = (wavePhase[id] || 0) + 0.45;
  const phase = wavePhase[id];
  const sampleCount = 72;
  const pts = [];

  for (let i = 0; i < sampleCount; i++) {
    const x = (i / (sampleCount - 1)) * width;
    const u = (i / sampleCount) * 4.5 + phase * 0.16;
    let normY = 0.52;

    if (id === "spo2Chart") {
      const tWave = u * Math.PI * 2;
      normY = state.vitals.finger_on_sensor === false ? 0.5 : 0.54 - Math.sin(tWave) * 0.25 - Math.sin(tWave * 2 - 0.7) * 0.09;
    } else if (id === "temperatureChart") {
      const min = Math.min(...values) - 0.6;
      const max = Math.max(...values) + 0.6;
      const idx = Math.min(values.length - 1, Math.floor((i / sampleCount) * values.length));
      const base = (values[idx] - min) / (max - min || 1);
      normY = state.vitals.temp_ok === false ? 0.5 : 0.76 - base * 0.44 + Math.sin(u * 1.5) * 0.03;
    } else {
      const env = 0.2 + Math.abs(Math.sin(u * 1.35)) * 0.64;
      normY = state.vitals.air_ok === false || state.vitals.connected === false ? 0.5 : 0.5 + Math.sin(u * 14) * 0.38 * env;
    }

    const y = Math.max(7, Math.min(height - 7, normY * height));
    pts.push({ x, y });
  }
  return pts;
}

function drawChart(id, values) {
  const canvas = $(id);
  if (!canvas) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 180;
  const height = canvas.clientHeight || (id === "pulseChart" ? 152 : 76);
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);

  const isEcg = id === "pulseChart";
  context.fillStyle = isEcg ? "#112e28" : "#f2f7f4";
  context.fillRect(0, 0, width, height);

  // Crisp 20px millimeter gridlines
  context.strokeStyle = isEcg ? "#284941" : "#dbe8e1";
  context.lineWidth = 1;
  context.beginPath();
  for (let gx = 20; gx < width; gx += 20) {
    const xPos = Math.floor(gx) + 0.5;
    context.moveTo(xPos, 0);
    context.lineTo(xPos, height);
  }
  for (let gy = 20; gy < height; gy += 20) {
    const yPos = Math.floor(gy) + 0.5;
    context.moveTo(0, yPos);
    context.lineTo(width, yPos);
  }
  context.stroke();

  if (values.length < 1) return;

  const strokeColor = isEcg ? "#83e4b9" : "#25765f";
  const points = buildOscilloscopePoints(id, values, width, height);
  if (!points.length) return;

  // Crisp un-blurred clinical oscilloscope trace
  context.save();
  context.beginPath();
  points.forEach((pt, index) => (index === 0 ? context.moveTo(pt.x, pt.y) : context.lineTo(pt.x, pt.y)));
  context.strokeStyle = strokeColor;
  context.lineWidth = id === "pulseChart" ? 2.0 : 1.75;
  context.lineJoin = "round";
  context.lineCap = "round";
  context.shadowColor = "transparent";
  context.shadowBlur = 0;
  context.stroke();
  context.restore();

  if (id === "pulseChart" && state.vitals.ecg_leads_ok === false) {
    context.fillStyle = "rgba(254, 226, 226, 0.92)";
    context.font = "700 13px 'Geist Sans', 'Inter', sans-serif";
    context.textAlign = "center";
    context.fillText(t("ecgLeadsOff"), width / 2, height / 2 - 14);
    context.textAlign = "start";
  }

  // Clean 2px white leading-edge point without halo fill
  const last = points[points.length - 1];
  context.beginPath();
  context.arc(last.x - 3, last.y, 2, 0, Math.PI * 2);
  context.fillStyle = strokeColor;
  context.fill();
}

function buildDemoAssessment() {
  const values = state.vitals;
  const fingerOk = values.finger_on_sensor !== false && Number(values.pulse) > 0;
  const tempOk = values.temp_ok !== false && Number(values.temperature) > 20;
  const complaint = cleanText($("complaint").value, 1000).toLocaleLowerCase(state.lang === "uz" ? "uz-UZ" : "ru-RU");
  let score = 0;
  const factors = [];
  if (fingerOk && Number(values.pulse) >= 105) { score += 20; factors.push(`${t("pulseFactor")}: ${values.pulse} BPM`); }
  if (fingerOk && Number(values.spo2) > 0 && Number(values.spo2) <= 93) { score += 30; factors.push(`${t("spo2Factor")}: ${values.spo2}%`); }
  if (tempOk && Number(values.temperature) >= 38) { score += 20; factors.push(`${t("temperatureFactor")}: ${values.temperature} °C`); }
  if (Number(values.air_co2_ppm) >= 1100) { score += 20; factors.push(`${t("co2Factor")}: ${t("airStuff")}`); }
  else if (Number(values.respiration) >= 24) { score += 20; factors.push(`${t("respirationFactor")}: ${values.respiration} ${t("perMinute")}`); }
  if (/(nafas|дых|hush|созн|og‘riq|og'riq|боль)/iu.test(complaint)) { score += 10; factors.push(t("complaintFactor")); }
  score = clamp(score, 0, 100);
  const band = score >= 60 ? "high" : score >= 25 ? "review" : "low";
  if (!factors.length) factors.push(t("noDemoFactors"));
  return {
    level: `demo_${band}`,
    score,
    band,
    title: t(`assessmentTitle_${band}`),
    summary: t(`assessmentSummary_${band}`),
    factors
  };
}

function updateReviewCard() {
  const card = $("aiCard");
  const assessment = state.assessment;
  const preview = assessment ? `${t(`zone_${assessment.band}`)} · ${assessment.score}/100` : t("reviewPending");
  if ($("reviewPeek").textContent !== preview) $("reviewPeek").textContent = preview;
  card.classList.remove("review-pending", "review-low", "review-review", "review-high");
  $("riskScoreBar").className = "";
  if (!assessment) {
    card.classList.add("review-pending");
    $("review-title").textContent = t("reviewPending");
    $("riskBand").textContent = t("notAnalysed");
    $("riskSummary").textContent = t("reviewSummary");
    $("riskScore").textContent = "—";
    $("riskScoreBar").classList.add("score-pending");
    $("riskScoreBar").parentElement.setAttribute("aria-valuenow", "0");
    $("riskReasons").innerHTML = `<li>${escapeHtml(t("runAnalysisHint"))}</li>`;
    return;
  }
  card.classList.add(`review-${assessment.band}`);
  $("review-title").textContent = assessment.title;
  $("riskBand").textContent = t(`zone_${assessment.band}`);
  $("riskSummary").textContent = assessment.summary;
  $("riskScore").textContent = `${assessment.score}/100`;
  $("riskScoreBar").classList.add(`score-${assessment.band}`);
  setProgress($("riskScoreBar"), Math.round(assessment.score / 10) * 10);
  $("riskScoreBar").parentElement.setAttribute("aria-valuenow", String(assessment.score));
  $("riskReasons").innerHTML = assessment.factors.map((factor) => `<li>${escapeHtml(factor)}</li>`).join("");
}

async function runDemoAssessment(showMessage = true) {
  if (usesHardware()) {
    if (showMessage) showToast(t("hardwareDemoOnly"));
    return;
  }
  state.assessment = buildDemoAssessment();
  updateReviewCard();
  if (showMessage) showToast(t("analysisUpdated"));
  if (state.online && state.api) {
    try {
      const v = state.vitals;
      const response = await fetch("/api/ai/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          patient_name: cleanText($("patientName").value, 120) || "Bemor",
          patient_age: Number($("patientAge").value) || 40,
          mahalla: cleanText($("patientVillage").value, 120) || "Qishloq MFY",
          voice_text: cleanText($("complaint").value, 1000),
          sensors: {
            heart_rate_bpm: v.pulse || 0,
            spo2_percent: v.spo2 || 0,
            temperature_c: v.temperature || 0,
            temp_ok: v.temp_ok !== false,
            ecg_mv: v.ecg_mv || 0,
            ecg_leads_ok: v.ecg_leads_ok !== false,
            air_co2_ppm: v.air_co2_ppm || 420,
            finger_on_sensor: v.finger_on_sensor !== false,
            respiration_rate: v.respiration || 16
          }
        }),
        signal: AbortSignal.timeout(4000)
      });
      if (response.ok) {
        const ai = await response.json();
        const zone = String(ai.risk_zone || "GREEN").toUpperCase();
        const band = zone === "RED" ? "high" : zone === "YELLOW" ? "review" : "low";
        state.assessment = {
          level: `demo_${band}`,
          score: clamp(Number(ai.risk_score ?? state.assessment.score), 0, 100),
          band,
          title: t(`assessmentTitle_${band}`),
          summary: cleanText(ai.preliminary_diagnosis_uz || state.assessment.summary, 480),
          factors: Array.isArray(ai.nurse_instructions_uz) && ai.nurse_instructions_uz.length
            ? ai.nurse_instructions_uz.map((item) => cleanText(item, 200))
            : state.assessment.factors
        };
        if (ai.hardware_feedback?.zone) {
          state.vitals.currentZone = ai.hardware_feedback.zone;
          const z = ["RED", "YELLOW", "GREEN"].includes(state.vitals.currentZone) ? state.vitals.currentZone : "GREEN";
          setBadge("espZoneBadge", z === "RED" ? "badge-err" : z === "YELLOW" ? "badge-warn" : "badge-ok", t(`hwZone_${z}`));
        }
        updateReviewCard();
      }
    } catch {
      // Offline / unreachable backend falls back cleanly to local calculation.
    }
  }
}

async function startExam() {
  if (state.examRunning) return;
  state.examRunning = true;
  state.examCompleted = false;
  $("examButton").disabled = true;
  $("examProgress").classList.remove("hidden");
  $("examCountdown").classList.remove("hidden");
  $("examComplete").classList.add("hidden");
  const progress = $("examProgress").querySelector("[role=progressbar]");
  let remaining = 10;
  $("examSeconds").textContent = String(remaining);
  setProgress($("examProgressBar"), 0);
  progress.setAttribute("aria-valuenow", "0");
  const timer = setInterval(() => {
    remaining -= 1;
    $("examSeconds").textContent = String(remaining);
    setProgress($("examProgressBar"), (10 - remaining) * 10);
    progress.setAttribute("aria-valuenow", String(10 - remaining));
    if (remaining <= 0) {
      clearInterval(timer);
      state.examRunning = false;
      state.examCompleted = true;
      $("examCountdown").classList.add("hidden");
      $("examComplete").classList.remove("hidden");
      $("examButton").disabled = false;
      runDemoAssessment(false);
      showToast(t("examDone"));
    }
  }, 1000);
}

function validateForm() {
  const fields = [$("patientName"), $("patientAge"), $("patientVillage"), $("complaint")];
  fields.forEach((field) => field.setAttribute("aria-invalid", "false"));
  const name = cleanText($("patientName").value, 120);
  const village = cleanText($("patientVillage").value, 120);
  const complaint = cleanText($("complaint").value, 1000);
  const age = Number($("patientAge").value);
  const invalid = [];
  if (name.length < 2) invalid.push($("patientName"));
  if ($("patientAge").value === "" || !Number.isInteger(age) || age < 0 || age > 120) invalid.push($("patientAge"));
  if (village.length < 2) invalid.push($("patientVillage"));
  if (complaint.length < 2) invalid.push($("complaint"));
  invalid.forEach((field) => field.setAttribute("aria-invalid", "true"));
  if (invalid.length) return { error: t("invalidForm"), focus: invalid[0] };
  if (!$("patientConsent").checked) return { error: t("consentRequired"), focus: $("patientConsent") };
  const nurseDiagnosis = cleanText($("nurseDiagnosis").value, 1000);
  return { name, village, complaint, age, nurseDiagnosis };
}

async function savePatient(event) {
  event.preventDefault();
  setInlineError("formError");
  const result = validateForm();
  if (result.error) {
    setInlineError("formError", result.error);
    $("formError").focus();
    result.focus.focus();
    return;
  }
  const assessment = usesHardware()
    ? { level: "unvalidated", score: null, summary: t("hardwareRecordSummary") }
    : state.assessment ?? buildDemoAssessment();
  const record = {
    local_id: uid(),
    device_id: state.deviceId,
    patient: { name: result.name, age: result.age, village: result.village },
    vitals: {
      pulse: state.vitals.pulse,
      spo2: state.vitals.spo2,
      temperature: state.vitals.temperature,
      respiration: state.vitals.respiration,
      tremor: state.vitals.tremor,
      ecg_mv: state.vitals.ecg_mv,
      ecg_leads_ok: state.vitals.ecg_leads_ok,
      air_co2_ppm: state.vitals.air_co2_ppm,
      finger_on_sensor: state.vitals.finger_on_sensor,
      temp_ok: state.vitals.temp_ok
    },
    complaint: result.complaint,
    nurse_diagnosis: result.nurseDiagnosis,
    ai_assessment: { level: assessment.level, score: assessment.score, summary: assessment.summary },
    consent: { granted: true, version: "prototype-notice-v1", captured_at: new Date().toISOString() },
    captured_at: new Date().toISOString(),
    status: "pending"
  };
  try {
    await putOne(state.db, "patients", record);
    state.patients.push(record);
    if (state.online && state.api) {
      try {
        await fetch("/api/patients/save_offline", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: record.local_id,
            patient_name: result.name,
            patient_age: result.age,
            mahalla: result.village,
            voice_text: result.complaint,
            nurse_diagnosis: result.nurseDiagnosis,
            sensors: {
              heart_rate_bpm: state.vitals.pulse || 0,
              spo2_percent: state.vitals.spo2 || 0,
              temperature_c: state.vitals.temperature || 0,
              temp_ok: state.vitals.temp_ok !== false,
              ecg_mv: state.vitals.ecg_mv || 0,
              ecg_leads_ok: state.vitals.ecg_leads_ok !== false,
              air_co2_ppm: state.vitals.air_co2_ppm || 420,
              finger_on_sensor: state.vitals.finger_on_sensor !== false,
              respiration_rate: state.vitals.respiration || 16
            }
          }),
          signal: AbortSignal.timeout(2000)
        });
      } catch {
        // IndexedDB holds the record even when local backend is stopped.
      }
    }
    renderRecords();
    $("patientForm").reset();
    state.examCompleted = false;
    state.assessment = null;
    updateReviewCard();
    showToast(t("savedQueue"));
  } catch {
    setInlineError("formError", t("storageError"));
  }
}

async function retryFetch(url, options, attempts = 4) {
  let lastError;
  for (let index = 0; index < attempts; index += 1) {
    try {
      const response = await fetch(url, options);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response;
    } catch (error) {
      lastError = error;
      if (index < attempts - 1) await new Promise((resolve) => setTimeout(resolve, [600, 1200, 2400][index]));
    }
  }
  throw lastError;
}

async function syncPatients() {
  if ($("syncButton").disabled) return;
  const pending = state.patients.filter(awaitsSync);
  if (!state.online) return showToast(t("needOnline"));
  if (!pending.length) return showToast(t("nothingToSync"));
  $("syncOverlay").classList.remove("hidden");
  setProgress($("syncTrack"), 15);
  $("syncButton").disabled = true;
  const useServer = state.api;
  try {
    if (useServer) {
      const payload = pending.map(({ status, server_id: serverId, ...patient }) => patient);
      let syncedViaPrimary = false;
      try {
        const response = await retryFetch("/api/patients/sync", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ patients: payload })
        }, 2);
        const data = await response.json();
        setProgress($("syncTrack"), 65);
        data.acknowledgements.forEach((acknowledgement) => {
          const record = state.patients.find((patient) => patient.local_id === acknowledgement.local_id);
          if (record) {
            record.status = "synced";
            record.server_id = acknowledgement.server_id;
          }
        });
        syncedViaPrimary = true;
      } catch {
        // Fallback for teammate backend (live_server.py): save_offline + /api/dmed/sync
      }
      if (!syncedViaPrimary) {
        for (const p of pending) {
          await fetch("/api/patients/save_offline", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              id: p.local_id,
              patient_name: p.patient.name,
              patient_age: p.patient.age,
              mahalla: p.patient.village,
              voice_text: p.complaint,
              sensors: {
                heart_rate_bpm: p.vitals?.pulse || 0,
                spo2_percent: p.vitals?.spo2 || 0,
                temperature_c: p.vitals?.temperature || 0,
                temp_ok: p.vitals?.temp_ok !== false,
                ecg_mv: p.vitals?.ecg_mv || 0,
                ecg_leads_ok: p.vitals?.ecg_leads_ok !== false,
                air_co2_ppm: p.vitals?.air_co2_ppm || 420,
                finger_on_sensor: p.vitals?.finger_on_sensor !== false,
                respiration_rate: p.vitals?.respiration || 16
              }
            })
          });
        }
        const dmedResp = await retryFetch("/api/dmed/sync", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({})
        }, 2);
        const dmedData = await dmedResp.json();
        setProgress($("syncTrack"), 75);
        pending.forEach((patient, index) => {
          patient.status = "synced";
          patient.server_id = dmedData?.dmed_receipts?.[index]?.dmed_encounter_id || `DMED-${Date.now()}-${index}`;
        });
      }
    } else {
      await new Promise((resolve) => setTimeout(resolve, 800));
      pending.forEach((patient, index) => {
        patient.status = "synced";
        patient.server_id = `demo-${Date.now()}-${index}`;
      });
      const reply = { id: uid(), patient_id: "demo", action: t("demoSystem"), message: t("demoReply"), created_at: new Date().toISOString() };
      state.replies.push(reply);
      await putOne(state.db, "replies", reply);
    }
    await putMany(state.db, "patients", state.patients);
    setProgress($("syncTrack"), 100);
    renderRecords();
    showSyncFeedback(useServer ? "synced" : "demoSyncDone");
    await new Promise((resolve) => setTimeout(resolve, 450));
  } catch {
    showSyncFeedback("syncFailed", true);
  } finally {
    $("syncOverlay").classList.add("hidden");
    $("syncButton").disabled = false;
  }
}

async function pollReplies() {
  if (!state.online || !state.api) return;
  try {
    const response = await fetch(`/api/doctor-replies?device_id=${encodeURIComponent(state.deviceId)}&after_id=${state.lastReplyId}`, { signal: AbortSignal.timeout(2500) });
    if (!response.ok) return;
    const data = await response.json();
    if (!data.replies.length) return;
    state.replies.push(...data.replies);
    state.lastReplyId = Math.max(...data.replies.map((reply) => Number(reply.id) || 0));
    await putMany(state.db, "replies", data.replies);
    renderRecords();
    showToast(t("newDoctorReply"));
  } catch {
    // Polling failures are intentionally quiet; the next interval retries.
  }
}

function startVoiceInput() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) return showToast(t("voiceUnavailable"));
  const recognition = new Recognition();
  recognition.lang = state.lang === "uz" ? "uz-UZ" : "ru-RU";
  recognition.interimResults = true;
  $("voiceButton").classList.add("listening");
  $("voiceStatus").textContent = t("listening");
  recognition.onresult = (event) => {
    $("complaint").value = cleanText(Array.from(event.results).map((result) => result[0].transcript).join(" "), 1000);
  };
  recognition.onend = () => {
    $("voiceButton").classList.remove("listening");
    $("voiceStatus").textContent = t("voiceHint");
  };
  recognition.onerror = recognition.onend;
  recognition.start();
}

async function loadDemoRecords(showNotice = true) {
  if (state.patients.some((record) => record.demo_data)) {
    if (showNotice !== false) showToast(t("demoAlreadyLoaded"));
    return;
  }
  const now = Date.now();
  const samples = [
    { id: "001", age: 64, village: "DEMO", complaint: "TEST: nafas bo‘yicha qayd", vitals: DEMO_SCENARIOS.urgent, level: "demo_high", score: 90, summary: t("assessmentSummary_high") },
    { id: "002", age: 42, village: "DEMO", complaint: "TEST: kuzatuv qaydi", vitals: DEMO_SCENARIOS.review, level: "demo_review", score: 40, summary: t("assessmentSummary_review") },
    { id: "003", age: 29, village: "DEMO", complaint: "TEST: odatiy demo qaydi", vitals: DEMO_SCENARIOS.normal, level: "demo_low", score: 0, summary: t("assessmentSummary_low") }
  ].map((sample, index) => ({
    local_id: `demo-queue-${sample.id}-${now}`,
    device_id: state.deviceId,
    patient: { name: `TEST BEMOR ${sample.id}`, age: sample.age, village: sample.village },
    vitals: { ...sample.vitals },
    complaint: sample.complaint,
    ai_assessment: { level: sample.level, score: sample.score, summary: sample.summary },
    consent: { granted: true, version: "prototype-notice-v1", captured_at: new Date(now - index * 25 * 60 * 1000).toISOString() },
    captured_at: new Date(now - index * 25 * 60 * 1000).toISOString(),
    status: "pending",
    demo_data: true
  }));
  if (state.db) await putMany(state.db, "patients", samples);
  state.patients.push(...samples);
  renderRecords();
  if (showNotice !== false) showToast(t("demoLoaded"));
}

function getNurseInitials(name) {
  return String(name || "SN")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0].toUpperCase())
    .join("") || "SN";
}

function updateSessionBar() {
  $("nurseAvatar").textContent = getNurseInitials(state.nurse.name);
  $("activeNurseName").textContent = state.nurse.name;
  $("activeNurseId").textContent = state.nurse.id;
  $("activeNurseFacility").textContent = state.nurse.facility;
}

function openLoginModal() {
  setInlineError("loginError");
  $("loginNurseName").value = state.nurse.name;
  $("loginNurseId").value = state.nurse.id;
  $("loginNurseFacility").value = state.nurse.facility;
  $("loginOverlay").classList.remove("hidden");
  $("workspace").inert = true;
  $("loginNursePin").focus();
}

async function handleNurseLogin(event) {
  event.preventDefault();
  setInlineError("loginError");
  const name = cleanText($("loginNurseName").value, 100);
  const id = cleanText($("loginNurseId").value, 32);
  const facility = cleanText($("loginNurseFacility").value, 120);
  const pin = cleanText($("loginNursePin").value, 8);

  if (name.length < 2 || id.length < 2 || facility.length < 2 || !/^\d{4,8}$/.test(pin)) {
    setInlineError("loginError", t("invalidLogin"));
    return;
  }

  state.nurse = { name, id, facility };
  if (state.db) await setSetting(state.db, "nurse-profile", state.nurse);
  sessionStorage.setItem("db_session_active", "1");
  updateSessionBar();
  $("loginOverlay").classList.add("hidden");
  $("workspace").inert = false;
  $("examButton").focus();
  showToast(t("loginSuccess"));
}

function handleNurseLogout() {
  sessionStorage.removeItem("db_session_active");
  openLoginModal();
  showToast(t("loggedOut"));
}

async function initialize() {
  purgeLegacySensitiveStorage();
  try {
    state.db = await openDatabase();
    state.lang = await getSetting(state.db, "language", "uz");
    state.api = await getSetting(state.db, "api-enabled", false);
    const savedScenario = await getSetting(state.db, "sensor-scenario", "normal");
    state.scenario = DEMO_SCENARIOS[savedScenario] ? savedScenario : "normal";
    const mode = await getSetting(state.db, "sensor-mode", "auto");
    if (["auto", "live", "demo"].includes(mode)) state.sensorMode = mode;
    const savedNurse = await getSetting(state.db, "nurse-profile", null);
    if (savedNurse?.name && savedNurse?.id) {
      state.nurse = {
        name: savedNurse.name,
        id: savedNurse.id,
        facility: savedNurse.facility || state.nurse.facility
      };
    }
    state.deviceId = await getSetting(state.db, "device-id", "");
    if (!state.deviceId) {
      state.deviceId = `tablet-${uid()}`;
      await setSetting(state.db, "device-id", state.deviceId);
    }
    state.patients = await getAll(state.db, "patients");
    state.replies = await getAll(state.db, "replies");
    state.lastReplyId = Math.max(0, ...state.replies.map((reply) => Number(reply.id) || 0));
  } catch {
    setInlineError("formError", t("storageError"));
  }
  $("apiEnabled").checked = state.api;
  $("sensorScenario").value = state.scenario;
  $("sensorMode").value = state.sensorMode;
  updateSessionBar();
  applyLanguage();
  renderRecords();
  updateReviewCard();
  if (sessionStorage.getItem("db_session_active") !== "1") {
    openLoginModal();
  }
}

document.querySelectorAll(".preset-nurse-btn").forEach((button) => {
  button.setAttribute("aria-pressed", "false");
  button.addEventListener("click", () => {
    document.querySelectorAll(".preset-nurse-btn").forEach((preset) => {
      preset.setAttribute("aria-pressed", String(preset === button));
    });
    $("loginNurseName").value = button.dataset.name || "";
    $("loginNurseId").value = button.dataset.id || "";
    $("loginNurseFacility").value = button.dataset.facility || "";
    $("loginNursePin").value = button.dataset.pin || "2026";
    setInlineError("loginError");
  });
});

$("loginForm").addEventListener("submit", handleNurseLogin);
$("logoutButton").addEventListener("click", handleNurseLogout);

document.querySelectorAll(".lang").forEach((button) => {
  button.addEventListener("click", async () => {
    state.lang = button.dataset.lang;
    if (state.db) await setSetting(state.db, "language", state.lang);
    applyLanguage();
  });
});

$("modeToggle").addEventListener("click", () => {
  state.online = !state.online;
  updateModeControl();
  if (state.online && state.api) setTimeout(syncPatients, 500);
});

$("apiEnabled").addEventListener("change", async (event) => {
  state.api = event.target.checked;
  updateModeControl();
  renderRecords();
  if (state.db) await setSetting(state.db, "api-enabled", state.api);
  showToast(t(state.api ? "apiOn" : "apiOff"));
});

$("examButton").addEventListener("click", startExam);
$("runAiButton").addEventListener("click", () => runDemoAssessment(true));
$("patientForm").addEventListener("submit", savePatient);
$("syncButton").addEventListener("click", syncPatients);
$("voiceButton").addEventListener("click", startVoiceInput);
$("loadDemoButton").addEventListener("click", loadDemoRecords);
$("sensorMode").addEventListener("change", (event) => {
  state.sensorMode = event.target.value;
  state.hardwareSeen = false;
  state.assessment = null;
  lastDemoSample = 0;
  lastHardwareButtonCount = null;
  lastHardwareButtonDown = false;
  clearSensorHistory();
  updateReviewCard();
  updateSensorLabels();
  if (state.sensorMode === "live") showNoHardware();
  if (state.db) void setSetting(state.db, "sensor-mode", state.sensorMode).catch(() => {});
  void refreshVitals();
});
$("sensorScenario").addEventListener("change", async (event) => {
  state.scenario = event.target.value;
  state.assessment = null;
  Object.assign(state.vitals, DEMO_SCENARIOS[state.scenario]);
  appendEcgSamples(generateEcgBatch(state.vitals.pulse, state.vitals.ecg_leads_ok, 48));
  if (state.db) await setSetting(state.db, "sensor-scenario", state.scenario);
  updateVitals("simulated");
  updateReviewCard();
  showToast(t("scenarioChanged"));
});
window.addEventListener("online", () => { if (state.online && state.api) setTimeout(syncPatients, 700); });
window.addEventListener("resize", () => {
  Object.entries(history).forEach(([key, series]) => drawChart(`${key}Chart`, series));
});

await initialize();
void refreshVitals();
setInterval(refreshVitals, 300);
setInterval(pollReplies, 5000);
