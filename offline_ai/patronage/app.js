"use strict";

const byId = id => document.getElementById(id);
const form = byId("visit-form");
const measurements = byId("measurements");
const linkSwitch = byId("demo-link");
const receiptSection = byId("receipt-section");
const formatDate = new Intl.DateTimeFormat("uz-Latn-UZ", { dateStyle: "short", timeStyle: "short" });
const zones = { GREEN: "Yashil zona", YELLOW: "Sariq zona", RED: "Qizil zona" };
const numericSensors = ["heart_rate_bpm", "spo2_percent", "temperature_c", "respiration_rate"];
const retryDelay = 5000;
let records = [];
let sending = false;
let saving = false;
let analyzing = false;
let receiverDown = false;
let retryTimer;
let requestController;
let analysisVersion = 0;
let journalVersion = 0;
let assessment = null;

function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("patronaj-plansheti", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("visits", { keyPath: "visit.id" });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

let database;

async function withVisits(mode, run) {
  database ??= openDatabase();
  const db = await database;
  return new Promise((resolve, reject) => {
    const tx = db.transaction("visits", mode);
    const request = run(tx.objectStore("visits"));
    tx.oncomplete = () => resolve(request.result);
    tx.onabort = () => reject(tx.error);
  });
}

function updateDelivery(id, fields) {
  return withVisits("readwrite", store => {
    const request = store.get(id);
    request.onsuccess = () => {
      const record = request.result;
      if (record && !record.receipt) store.put({ ...record, ...fields });
    };
    return request;
  });
}

function textElement(tag, text, className) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}

function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}

function visitRow(visit, receivedAt, issue) {
  const row = document.createElement("li");
  row.dataset.visitId = visit.id;
  const top = document.createElement("div");
  top.className = "record-top";
  const state = textElement("span", receivedAt ? "Qabul qilindi" : issue ? "Tekshirish kerak" : "Navbatda", "state-label");
  state.dataset.state = receivedAt ? "received" : issue ? "error" : "pending";
  const time = formatDate.format(new Date(receivedAt ?? visit.captured_at));
  top.append(textElement("strong", visit.patient_code), state);
  const code = visit.id.slice(0, 8).toUpperCase();
  row.append(top, textElement("p", visit.patient_age + " yosh · " + time + " · #" + code, "record-meta"));
  row.append(textElement("p", visit.voice_text, "record-note"));
  if (issue) row.append(textElement("p", issue, "form-status"));
  return row;
}

function canSend() {
  return navigator.onLine && linkSwitch.checked;
}

function paintStatus() {
  const queued = records.filter(row => !row.receipt).length;
  byId("retry").disabled = !canSend() || sending || queued === 0;
  const active = canSend() && !receiverDown;
  byId("signal").dataset.state = active ? "active" : "paused";
  let status = "Aloqa bor";
  let detail = "Navbatdagi tashriflar avtomatik yuboriladi.";
  if (!navigator.onLine) {
    status = "Qurilma oflayn";
    detail = "Tashrif shu qurilmada saqlanadi.";
  } else if (!linkSwitch.checked) {
    status = "Sinov aloqasi uzilgan";
    detail = "Tashriflar navbatda qoladi.";
  } else if (receiverDown) {
    status = "Sinov serveri javob bermayapti";
    detail = "Navbat saqlanadi. Qayta yuborishga urinamiz.";
  }
  setText(byId("connection-status"), status);
  setText(byId("connection-detail"), detail);
  setText(byId("queue-summary"), sending ? "Yuborilmoqda…" : queued + " navbatda · " + (records.length - queued) + " qabul qilindi");
}

async function refreshRecords() {
  records = (await withVisits("readonly", store => store.getAll()))
    .sort((first, second) => second.visit.captured_at.localeCompare(first.visit.captured_at));
  const list = byId("local-records");
  list.replaceChildren(...(records.length
    ? records.map(row => visitRow(row.visit, row.receipt?.received_at, row.issue))
    : [textElement("li", "Saqlangan tashriflar shu yerda ko‘rinadi.", "empty")]));
  paintStatus();
}

function reportForm(message, error = false) {
  const status = byId("form-status");
  status.hidden = !message;
  status.dataset.tone = error ? "error" : "success";
  setText(status, message);
}

function deliveryError() {
  reportForm("Qurilmadagi tashriflar xotirasini ochib bo‘lmadi.", true);
}

function scheduleDelivery(delay = 0, includeIssues = false) {
  clearTimeout(retryTimer);
  if (canSend()) retryTimer = setTimeout(() => void deliverQueue(includeIssues), delay);
}

async function deliverQueue(includeIssues) {
  if (!canSend() || sending) return;
  const pending = records.filter(row => !row.receipt && (includeIssues || !row.issue)).reverse();
  if (!pending.length) return;
  sending = true;
  paintStatus();
  let transportFailed = false;
  let delivered = false;
  try {
    for (const row of pending) {
      if (!canSend()) break;
      requestController = new AbortController();
      const response = await fetch("/api/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(row.visit),
        signal: AbortSignal.any([requestController.signal, AbortSignal.timeout(40000)]),
      });
      if (response.status === 409 || response.status === 422) {
        receiverDown = false;
        const failure = await response.json();
        const issue = typeof failure.detail === "string" ? failure.detail : "Server tashrif ma’lumotlarini qabul qilmadi.";
        await updateDelivery(row.visit.id, { issue });
        continue;
      }
      if (!response.ok) throw new Error("Receiver unavailable");
      const receipt = await response.json();
      if (receipt.id !== row.visit.id || typeof receipt.received_at !== "string" || Number.isNaN(Date.parse(receipt.received_at))) {
        throw new Error("Invalid receipt");
      }
      await updateDelivery(row.visit.id, { receipt, issue: null });
      delivered = true;
      receiverDown = false;
    }
  } catch {
    transportFailed = canSend();
    receiverDown = transportFailed;
  } finally {
    requestController = null;
    try {
      // Retain the send guard until the committed receipt is visible to the next attempt.
      await refreshRecords();
    } catch {
      deliveryError();
    } finally {
      sending = false;
      paintStatus();
    }
    if (delivered && receiptSection.open) void loadJournal();
    if (records.some(row => !row.receipt && !row.issue)) scheduleDelivery(transportFailed ? retryDelay : 0);
  }
}

async function loadJournal() {
  const version = ++journalVersion;
  const status = byId("journal-status");
  const list = byId("server-records");
  setText(status, "Sinov serveridan yuklanmoqda…");
  try {
    if (!canSend()) throw new Error("Offline");
    const response = await fetch("/api/journal", { cache: "no-store", signal: AbortSignal.timeout(6000) });
    if (!response.ok) throw new Error("Receiver unavailable");
    const visits = await response.json();
    if (version !== journalVersion) return;
    list.replaceChildren(...visits.map(visit => visitRow(visit, visit.received_at)));
    setText(status, visits.length ? "Sinov serverida " + visits.length + " ta tashrif" : "Sinov serveriga tashrif kelmagan.");
  } catch {
    if (version !== journalVersion) return;
    list.replaceChildren();
    setText(status, canSend() ? "Sinov serveri javob bermadi." : "Jurnal aloqa tiklanganda yuklanadi.");
  }
}

function resetAssessment() {
  analysisVersion += 1;
  assessment = null;
  byId("analysis-result").hidden = true;
}

function readForm() {
  const code = form.elements.patient_code;
  const note = form.elements.voice_text;
  code.setCustomValidity(/^[A-Za-z0-9_-]{1,32}$/.test(code.value.trim()) ? "" : "Bemor kodida lotin harfi, raqam, chiziqcha yoki pastki chiziq ishlating.");
  note.setCustomValidity((note.value.match(/\p{L}/gu) ?? []).length >= 3 ? "" : "Shikoyat yoki kuzatuvni so‘z bilan yozing.");
  if (!form.reportValidity()) return null;
  const sensors = {};
  for (const name of numericSensors) {
    const field = form.elements[name];
    if (field.value !== "") sensors[name] = field.valueAsNumber;
  }
  const tremor = form.elements.tremor_detected.value;
  if (tremor !== "") sensors.tremor_detected = tremor === "true";
  const lung = form.elements.lung_audio_status.value;
  if (lung !== "") sensors.lung_audio_status = lung;
  return { patient_code: code.value.trim(), patient_age: form.elements.patient_age.valueAsNumber, voice_text: note.value.trim(), sensors };
}

function updateFormControls() {
  byId("visit-fields").disabled = saving;
  byId("save").disabled = saving;
  byId("example-button").disabled = saving;
  byId("analyze").disabled = saving || analyzing;
  setText(byId("analyze"), analyzing ? "Tahlil qilinmoqda…" : "AI tahlili");
}

async function saveVisit(event) {
  event.preventDefault();
  if (saving) return;
  const visit = readForm();
  if (!visit) return;
  saving = true;
  updateFormControls();
  try {
    const saved = { ...visit, id: crypto.randomUUID(), captured_at: new Date().toISOString(), assessment };
    await withVisits("readwrite", store => store.add({ visit: saved, receipt: null, issue: null }));
    form.reset();
    measurements.open = false;
    resetAssessment();
    reportForm("Tashrif qurilmaga saqlandi.");
    await refreshRecords();
    scheduleDelivery();
  } catch {
    reportForm("Saqlashda xato yuz berdi. Qurilmadagi yozuvlar ro‘yxatini tekshiring.", true);
  } finally {
    saving = false;
    updateFormControls();
  }
}

async function fillExample() {
  try {
    const response = await fetch("/example.json", { signal: AbortSignal.timeout(4000) });
    if (!response.ok) throw new Error("Example unavailable");
    const { patient_age, voice_text, sensors } = await response.json();
    for (const [key, value] of Object.entries({ patient_code: "P-104", patient_age, voice_text, ...sensors })) {
      form.elements[key].value = String(value);
    }
    measurements.open = true;
    resetAssessment();
    form.elements.patient_code.setCustomValidity("");
    form.elements.voice_text.setCustomValidity("");
    reportForm("");
  } catch {
    reportForm("Namunani yuklab bo‘lmadi. Tashrifni qo‘lda kiriting.", true);
  }
}

function showAssessment(reply, sourceLinks) {
  const panel = byId("analysis-result");
  panel.dataset.zone = reply.risk_zone;
  panel.replaceChildren(textElement("strong", zones[reply.risk_zone]), textElement("p", reply.preliminary_diagnosis_uz));
  for (const match of sourceLinks.matchAll(/<(https:\/\/[^<>]+)>; rel="cite-as"/g)) {
    const url = new URL(match[1]);
    const label = { "cdn.who.int": "WHO IITT", "api-portal.gov.uz": "SSV pnevmoniya protokoli" }[url.hostname];
    if (!label) continue;
    const link = textElement("a", label);
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    const line = textElement("p", "Manba · ");
    line.append(link);
    panel.append(line);
  }
  panel.hidden = false;
}

async function analyzeVisit() {
  if (analyzing || saving) return;
  const visit = readForm();
  if (!visit) return;
  if (Object.keys(visit.sensors).length !== 6) {
    measurements.open = true;
    reportForm("AI tahlili uchun barcha 6 ta datchik qiymatini kiriting.", true);
    return;
  }
  analyzing = true;
  updateFormControls();
  reportForm("");
  const version = analysisVersion;
  const { patient_age, voice_text, sensors } = visit;
  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patient_age, voice_text, sensors }),
      signal: AbortSignal.timeout(105000),
    });
    const reply = await response.json();
    if (version !== analysisVersion) return;
    if (!response.ok || !Object.hasOwn(zones, reply.risk_zone)) {
      reportForm(typeof reply.detail === "string" ? reply.detail : "AI tahlili bajarilmadi. Tashrifni saqlashingiz mumkin.", true);
      return;
    }
    assessment = reply;
    showAssessment(reply, response.headers.get("Link") ?? "");
  } catch {
    if (version === analysisVersion) reportForm("Lokal AI bilan aloqa uzildi. Tashrifni saqlashingiz mumkin.", true);
  } finally {
    analyzing = false;
    updateFormControls();
  }
}

function connectionChanged() {
  receiverDown = false;
  if (!canSend()) requestController?.abort();
  paintStatus();
  scheduleDelivery();
  if (receiptSection.open) void loadJournal();
}

async function prepareOfflineShell() {
  const status = byId("offline-readiness");
  if (!("serviceWorker" in navigator)) {
    setText(status, "Bu brauzerda sahifani oflayn qayta ochish mavjud emas.");
    return;
  }
  try {
    await navigator.serviceWorker.register("/sw.js");
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) {
      await new Promise(resolve => navigator.serviceWorker.addEventListener("controllerchange", resolve, { once: true }));
    }
    setText(status, "Planshet oflayn ochishga tayyor.");
  } catch {
    setText(status, "Oflayn ochish tayyorlanmadi. Aloqa borida sahifani yangilang.");
  }
}

try {
  linkSwitch.checked = localStorage.getItem("patronaj-link-paused") !== "1";
} catch { /* The demo switch still works for the current page. */ }

linkSwitch.addEventListener("change", () => {
  try {
    localStorage.setItem("patronaj-link-paused", linkSwitch.checked ? "0" : "1");
  } catch { /* Keep the in-page state when browser storage is restricted. */ }
  connectionChanged();
});
window.addEventListener("online", connectionChanged);
window.addEventListener("offline", connectionChanged);
byId("retry").addEventListener("click", () => scheduleDelivery(0, true));
byId("example-button").addEventListener("click", fillExample);
byId("analyze").addEventListener("click", analyzeVisit);
receiptSection.addEventListener("toggle", () => { if (receiptSection.open) void loadJournal(); });
form.addEventListener("submit", saveVisit);
form.addEventListener("input", event => {
  event.target.setCustomValidity("");
  resetAssessment();
  reportForm("");
});
form.addEventListener("invalid", event => { if (measurements.contains(event.target)) measurements.open = true; }, true);
paintStatus();
void prepareOfflineShell();
void refreshRecords().then(() => scheduleDelivery()).catch(deliveryError);
