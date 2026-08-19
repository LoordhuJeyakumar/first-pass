/**
 * First Pass — Operator Console polling, EBU Tech 3341 metering, and QC slate logic.
 *
 * Responsibilities:
 *   - POST /api/run when the operator clicks RUN.
 *   - Drive 24 FPS frame-accurate timecode on the QC slate via requestAnimationFrame.
 *   - Poll GET /api/run/{run_id} every 1.5 s until status is done/failed.
 *   - Render shared-axis relative LU (−4…+4) and True Peak (−6…0 dBTP) meters.
 *   - Append ledger rows incrementally (only newly-received entries).
 *   - Trigger single-impact verdict stamp animation on run completion.
 */

"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let activeRunId = null;
let pollTimer = null;
let renderedLedgerCount = 0;
let tcStartTime = null;
let tcAnimationFrame = null;

// ---------------------------------------------------------------------------
// DOM references (resolved after DOMContentLoaded)
// ---------------------------------------------------------------------------

let elVerdict, elVerdictText, elBlockerCount, elMasterId, elVerdictLive, elVerdictPhase;
let elFixList, elFixBody, elFixEmpty;
let elLedgerBody, elLedgerEmpty;
let elRunBtn, elMasterSelect, elSpecSelect, elStatus;
let elReadinessBody;
let elSlateMaster, elSlateSpec, elSlateLangs, elSlateDate, elSlateTc;
let elMeterGrid, elMeterEmpty, elMeterSectionTitle, elMeterSpecSubline;
let elDurationStrip, elAgentPassNote;

const FIX_EMPTY_IDLE =
  "No findings listed. Click RUN to check this master against the spec.";
const READINESS_IDLE = "No language scores yet. Click RUN to check.";
const READINESS_POST_RUN =
  "Per-language readiness applies only to destinations with regional certification gating.";

const DURATION_OPS = {
  "Evaluate spec": "EVALUATE",
  "Publish telemetry": "TELEMETRY",
  "Publish dashboard": "DASHBOARD",
  "Start agent": "AGENT",
};

const METER_SUBLINE_GIST =
  "how far each language sits from this destination's loudness target";

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  elVerdict       = document.getElementById("verdict-banner");
  elVerdictText   = document.getElementById("verdict-text");
  elBlockerCount  = document.getElementById("verdict-detail");
  elMasterId      = document.getElementById("verdict-master");
  elVerdictLive   = document.getElementById("verdict-live");
  elVerdictPhase  = document.getElementById("verdict-phase");
  elFixList       = document.getElementById("fix-list");
  elFixBody       = document.getElementById("fix-body");
  elFixEmpty      = document.getElementById("fix-empty");
  elLedgerBody    = document.getElementById("ledger-body");
  elLedgerEmpty   = document.getElementById("ledger-empty");
  elRunBtn        = document.getElementById("run-btn");
  elMasterSelect  = document.getElementById("master-select");
  elSpecSelect    = document.getElementById("spec-select");
  elStatus        = document.getElementById("run-status");
  elReadinessBody = document.getElementById("readiness-body");

  elSlateMaster   = document.getElementById("slate-master");
  elSlateSpec     = document.getElementById("slate-spec");
  elSlateLangs    = document.getElementById("slate-langs");
  elSlateDate     = document.getElementById("slate-date");
  elSlateTc       = document.getElementById("slate-tc");

  elMeterGrid     = document.getElementById("meter-grid");
  elMeterEmpty    = document.getElementById("meter-empty");
  elMeterSectionTitle = document.getElementById("meter-section-title");
  elMeterSpecSubline = document.getElementById("meter-spec-subline");
  elDurationStrip = document.getElementById("duration-strip");
  elAgentPassNote = document.getElementById("agent-pass-note");

  if (elRunBtn) elRunBtn.addEventListener("click", handleRunClick);
  wireDashboardLink();
  wireGlossary();
  wireHowItWorks();

  const fixtureRun = new URLSearchParams(window.location.search).get("run");
  if (fixtureRun) applyRemoteRun(fixtureRun);
});

// ---------------------------------------------------------------------------
// Timecode Generator (24 FPS, requestAnimationFrame)
// ---------------------------------------------------------------------------

function prefersReducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function announceVerdict(sentence) {
  if (!elVerdictLive) return;
  elVerdictLive.textContent = "";
  // Force a mutation so polite AT re-announces the same phrase on a re-run.
  window.requestAnimationFrame(() => {
    if (elVerdictLive) elVerdictLive.textContent = sentence;
  });
}

function formatTimecode(elapsedMs) {
  const elapsedSec = elapsedMs / 1000.0;
  const totalFrames = Math.floor(elapsedSec * 24.0);
  const ff = String(totalFrames % 24).padStart(2, "0");
  const ss = String(Math.floor(elapsedSec) % 60).padStart(2, "0");
  const mm = String(Math.floor(elapsedSec / 60) % 60).padStart(2, "0");
  const hh = String(Math.floor(elapsedSec / 3600)).padStart(2, "0");
  return `${hh}:${mm}:${ss}:${ff}`;
}

function paintTC() {
  if (!tcStartTime || !elSlateTc) return;
  elSlateTc.textContent = formatTimecode(Date.now() - tcStartTime);
}

function startTC() {
  if (tcAnimationFrame) {
    cancelAnimationFrame(tcAnimationFrame);
    tcAnimationFrame = null;
  }
  tcStartTime = Date.now();
  if (elSlateTc) elSlateTc.textContent = formatTimecode(0);
  if (prefersReducedMotion()) {
    return;
  }
  function tick() {
    if (!tcStartTime) return;
    paintTC();
    tcAnimationFrame = requestAnimationFrame(tick);
  }
  tick();
}

function stopTC() {
  if (tcAnimationFrame) {
    cancelAnimationFrame(tcAnimationFrame);
    tcAnimationFrame = null;
  }
  if (tcStartTime && elSlateTc) {
    elSlateTc.textContent = formatTimecode(Date.now() - tcStartTime);
  }
  tcStartTime = null;
}

// ---------------------------------------------------------------------------
// Run trigger
// ---------------------------------------------------------------------------

async function handleRunClick() {
  const master = elMasterSelect.value;
  const spec = elSpecSelect ? elSpecSelect.value : "";
  if (!master) return;

  setRunning(true);
  resetUI();
  startTC();
  if (elSlateMaster) elSlateMaster.textContent = master;

  let response;
  try {
    response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ master, spec }),
    });
  } catch (err) {
    stopTC();
    setStatus(`Network error: ${err.message}`, "error");
    setRunning(false);
    return;
  }

  if (response.status === 409) {
    stopTC();
    setStatus("A run is already in progress — wait for it to finish.", "warn");
    setRunning(false);
    return;
  }
  if (response.status === 429) {
    stopTC();
    const data = await response.json();
    setStatus(`Cooldown active. Retry in ${data.retry_after}s.`, "warn");
    setRunning(false);
    return;
  }
  if (!response.ok) {
    stopTC();
    const data = await response.json().catch(() => ({}));
    setStatus(`Error ${response.status}: ${data.detail || "unknown"}`, "error");
    setRunning(false);
    return;
  }

  const data = await response.json();
  activeRunId = data.run_id;
  renderedLedgerCount = 0;
  setStatus("Evaluating master against spec…", "info");
  pollTimer = setInterval(pollRun, 1500);
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

async function pollRun() {
  if (!activeRunId) return;

  let response;
  try {
    response = await fetch(`/api/run/${activeRunId}`);
  } catch (err) {
    // Network hiccup — keep polling
    return;
  }
  if (!response.ok) return;

  const state = await response.json();
  appendNewLedgerRows(state.ledger || []);
  updateSlate(state);
  if (tcStartTime && prefersReducedMotion()) paintTC();

  if (state.status === "done") {
    stopPolling();
    stopTC();
    clearPhase();
    renderVerdict(state);
    renderDurationStrip(state.ledger || []);
    safeRender(() => renderAudioMeters(state.evaluations || []), "audioMeters");
    safeRender(
      () => renderFixList(state.ranked_fix_plan || { jobs: [] }, state.findings || [], state),
      "fixList"
    );
    safeRender(() => renderReadiness(state.readiness || {}, state), "readiness");
    setRunning(false);
    setStatus("Evaluation complete.", "info");
  } else if (state.status === "failed") {
    stopPolling();
    stopTC();
    clearPhase();
    setStatus(`Run failed: ${state.error || "unknown error"}`, "error");
    renderVerdict({ verdict: "FAILED", blocker_count: 0, master_id: state.master_id });
    renderDurationStrip(state.ledger || []);
    setRunning(false);
  } else {
    updatePhaseFromLedger(state.ledger || []);
  }
}

function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

// ---------------------------------------------------------------------------
// QC Slate Metadata
// ---------------------------------------------------------------------------

async function applyRemoteRun(runId) {
  let response;
  try {
    response = await fetch(`/api/run/${runId}`);
  } catch (err) {
    setStatus(`Fixture fetch failed: ${err.message}`, "error");
    return;
  }
  if (!response.ok) {
    setStatus(`Fixture run not found (${response.status}).`, "error");
    return;
  }
  const state = await response.json();
  activeRunId = runId;
  renderedLedgerCount = 0;
  appendNewLedgerRows(state.ledger || []);
  updateSlate(state);
  if (state.status === "done" || state.evaluations) {
    renderVerdict(state);
    renderDurationStrip(state.ledger || []);
    safeRender(() => renderAudioMeters(state.evaluations || []), "audioMeters");
    safeRender(
      () => renderFixList(state.ranked_fix_plan || { jobs: [] }, state.findings || [], state),
      "fixList"
    );
    safeRender(() => renderReadiness(state.readiness || {}, state), "readiness");
    setStatus("Fixture loaded.", "info");
  }
}

function updateSlate(state) {
  if (elSlateMaster && state.master_id) elSlateMaster.textContent = state.master_id;
  if (elSlateSpec) {
    elSlateSpec.textContent = state.spec_id ? state.spec_id : "—";
  }
  if (elSlateDate) {
    elSlateDate.textContent = state.spec_id || state.master_id
      ? new Date().toISOString().slice(0, 10)
      : "—";
  }

  let langsList = [];
  if (state.readiness && Object.keys(state.readiness).length > 0) {
    langsList = Object.keys(state.readiness);
  } else if (state.evaluations && state.evaluations.length > 0) {
    langsList = [...new Set(state.evaluations.map((e) => e.language).filter(Boolean))];
  }
  if (elSlateLangs && langsList.length) {
    elSlateLangs.textContent = langsList.sort().join(", ");
  }
}

function parseLedgerSeconds(ts) {
  const m = String(ts || "").match(/^(\d{2}):(\d{2}):(\d{2})/);
  if (!m) return null;
  return (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]);
}

function renderDurationStrip(ledger) {
  if (!elDurationStrip) return;
  const progress = [];
  const seen = {};
  (ledger || []).forEach((entry) => {
    const label = DURATION_OPS[entry.operation];
    if (!label || seen[label]) return;
    const sec = parseLedgerSeconds(entry.timestamp);
    if (sec === null) return;
    seen[label] = true;
    progress.push({ label, sec });
  });
  if (!progress.length) {
    elDurationStrip.hidden = true;
    elDurationStrip.textContent = "";
    return;
  }
  let endSec = progress[progress.length - 1].sec;
  const last = ledger[ledger.length - 1];
  const lastSec = last ? parseLedgerSeconds(last.timestamp) : null;
  if (lastSec !== null) endSec = lastSec;

  const parts = [];
  for (let i = 0; i < progress.length; i++) {
    const start = progress[i].sec;
    const stop = i + 1 < progress.length ? progress[i + 1].sec : endSec;
    let delta = stop - start;
    if (delta < 0) delta += 86400;
    parts.push(`${progress[i].label} · ${delta.toFixed(1)} s`);
  }
  elDurationStrip.textContent = parts.join("   ");
  elDurationStrip.hidden = false;
}

// ---------------------------------------------------------------------------
// Verdict banner — single impact stamp transition
// ---------------------------------------------------------------------------

function renderVerdict(state) {
  const verdict = state.verdict || "UNKNOWN";
  const blockers = state.blocker_count || 0;
  const masterId = state.master_id || "";

  elVerdictText.textContent = verdict === "PASS"
    ? "PASS"
    : verdict === "REJECT"
    ? "REJECT"
    : verdict;

  elBlockerCount.textContent =
    verdict === "PASS" ? "0 blockers · Delivery approved" :
    verdict === "REJECT" ? `${blockers} spec non-conformance${blockers !== 1 ? "s" : ""} must be resolved` :
    "";

  elMasterId.textContent = masterId ? `Master: ${masterId}` : "";

  if (elAgentPassNote) elAgentPassNote.hidden = verdict !== "PASS";

  const live =
    verdict === "PASS" ? "Verdict PASS" :
    verdict === "REJECT" ? `Verdict REJECT, ${blockers} blocker${blockers !== 1 ? "s" : ""}` :
    verdict === "FAILED" ? "Verdict FAILED" :
    `Verdict ${verdict}`;
  announceVerdict(live);

  elVerdict.className = "burn-in verdict-" +
    (verdict === "PASS" ? "pass" : verdict === "REJECT" ? "reject" : "idle");
  elVerdictText.classList.remove("verdict-stamp-in");
  if (!prefersReducedMotion() && (verdict === "PASS" || verdict === "REJECT")) {
    void elVerdictText.offsetWidth;
    elVerdictText.classList.add("verdict-stamp-in");
  }
  clearPhase();
}

// ---------------------------------------------------------------------------
// EBU Tech 3341 Audio Meters (Relative LU & True Peak dBTP)
// ---------------------------------------------------------------------------

const LU_MIN = -4.0;
const LU_MAX = 4.0;
const TP_MIN = -6.0;
const TP_MAX = 0.0;

function mapToPct(value, min, max) {
  const span = max - min;
  const raw = ((value - min) / span) * 100;
  const off = raw < 0 ? "low" : raw > 100 ? "high" : null;
  return { pct: Math.max(0, Math.min(100, raw)), off: off };
}

function offScaleHtml(off) {
  if (!off) return "";
  return `<div class="meter-offscale meter-offscale-${off}" title="off-scale"></div>`;
}

function ticksHtml(values, min, max, unit) {
  return values.map((v) => {
    const pct = ((v - min) / (max - min)) * 100;
    const label = (v > 0 ? "+" : "") + v + " " + unit;
    return `<span class="tick-mark" style="left:${pct.toFixed(2)}%">${esc(label)}</span>`;
  }).join("");
}

const MC = typeof MeterCollect !== "undefined" ? MeterCollect : null;

function collectTracks(evaluations) {
  return MC ? MC.collectTracks(evaluations) : { tracks: [], langs: [] };
}

function fmtFixed(value, decimals) {
  return MC ? MC.fmtFixed(value, decimals) : "—";
}

function specFromTracks(tracks) {
  const withLoud = tracks.find((t) => t.targetLufs !== undefined);
  const withTp = tracks.find((t) => t.targetMaxDbtp !== undefined);
  return {
    targetLufs: withLoud ? withLoud.targetLufs : undefined,
    tolLu: withLoud ? withLoud.tolLu : undefined,
    targetMaxDbtp: withTp ? withTp.targetMaxDbtp : undefined,
  };
}

function setMeterSpecSubline(text) {
  if (elMeterSpecSubline) elMeterSpecSubline.textContent = text || "";
}

function setMeterHeader(spec, rangeNote) {
  const parts = [METER_SUBLINE_GIST];
  if (spec.targetLufs !== undefined && spec.tolLu !== undefined) {
    parts.push(`0 LU = ${fmtFixed(spec.targetLufs)} LUFS ± ${fmtFixed(spec.tolLu)} LU`);
  }
  if (spec.targetMaxDbtp !== undefined) {
    parts.push(`ceiling ${fmtFixed(spec.targetMaxDbtp)} dBTP`);
  }
  if (rangeNote) parts.push(rangeNote);
  setMeterSpecSubline(parts.join(" · "));
}

function meterCue(pass) {
  return pass
    ? `<span class="meter-cue meter-cue-pass">PASS</span>`
    : `<span class="meter-cue meter-cue-fail">FAIL</span>`;
}

function markerDelayMs(langIndex) {
  if (prefersReducedMotion()) return 0;
  return Math.min(langIndex * 40, 200);
}

function luTrackInner(t, langIndex) {
  const hasDev = t.deviationLu !== undefined && t.deviationLu !== null;
  const hasTol = t.tolLu !== undefined && t.tolLu !== null;
  const lu = hasDev ? mapToPct(t.deviationLu, LU_MIN, LU_MAX) : { pct: 50, off: null };
  const tolLeft = hasTol ? mapToPct(-t.tolLu, LU_MIN, LU_MAX).pct : 0;
  const tolRight = hasTol ? mapToPct(t.tolLu, LU_MIN, LU_MAX).pct : 0;
  const zero = mapToPct(0, LU_MIN, LU_MAX).pct;
  const cue = t.loudPass ? "PASS" : "FAIL";
  const delay = markerDelayMs(langIndex);
  const tolBand = hasTol
    ? `<div class="tolerance-band" style="left:${tolLeft.toFixed(2)}%; width:${(tolRight - tolLeft).toFixed(2)}%;"></div>`
    : "";
  const marker = hasDev
    ? `<div class="loudness-marker ${t.loudPass ? "loudness-marker-pass" : "loudness-marker-fail"}" data-target-left="${lu.pct.toFixed(2)}%" style="left:${zero.toFixed(2)}%; transition-delay:${delay}ms;"></div>${offScaleHtml(lu.off)}`
    : "";
  return `
    <div class="scale-track-container" role="img" aria-label="${esc(t.lang)} loudness ${esc(fmtFixed(t.loudnessLufs))} LUFS (${esc(fmtDev(t.deviationLu))}) ${cue}">
      ${tolBand}
      <div class="target-center-line" style="left:${zero.toFixed(2)}%;"></div>
      ${marker}
    </div>`;
}

function tpTrackInner(t, langIndex) {
  const hasTp = t.tpDbtp !== undefined && t.tpDbtp !== null;
  const hasCeil = t.targetMaxDbtp !== undefined && t.targetMaxDbtp !== null;
  const tp = hasTp ? mapToPct(t.tpDbtp, TP_MIN, TP_MAX) : { pct: 50, off: null };
  const ceil = hasCeil ? mapToPct(t.targetMaxDbtp, TP_MIN, TP_MAX) : { pct: 100, off: null };
  const cue = t.tpPass ? "PASS" : "FAIL";
  const delay = markerDelayMs(langIndex);
  const ceilLine = hasCeil
    ? `<div class="ceiling-line" style="left:${ceil.pct.toFixed(2)}%;"></div>`
    : "";
  const marker = hasTp
    ? `<div class="loudness-marker ${t.tpPass ? "loudness-marker-pass" : "loudness-marker-fail"}" data-target-left="${tp.pct.toFixed(2)}%" style="left:0%; transition-delay:${delay}ms;"></div>${offScaleHtml(tp.off)}`
    : "";
  return `
    <div class="true-peak-track" role="img" aria-label="${esc(t.lang)} true peak ${esc(fmtFixed(t.tpDbtp))} dBTP ${cue}">
      ${ceilLine}
      ${marker}
    </div>`;
}

function settleMeterMarkers() {
  if (!elMeterGrid) return;
  const markers = elMeterGrid.querySelectorAll(".loudness-marker[data-target-left]");
  const apply = () => {
    markers.forEach((m) => {
      if (prefersReducedMotion()) m.style.transitionDelay = "0s";
      m.style.left = m.getAttribute("data-target-left");
    });
  };
  if (prefersReducedMotion()) {
    apply();
    return;
  }
  requestAnimationFrame(() => {
    requestAnimationFrame(apply);
  });
}

function fmtDev(dev) {
  if (dev === undefined || dev === null || Number.isNaN(Number(dev))) return "—";
  const sign = dev > 0 ? "+" : "";
  return `${sign}${fmtFixed(dev)} LU`;
}

function renderAudioMeters(evaluations) {
  if (!elMeterGrid || !elMeterEmpty) return;
  elMeterGrid.innerHTML = "";

  const { tracks } = collectTracks(evaluations);
  if (!tracks.length) {
    elMeterEmpty.style.display = "";
    setMeterSpecSubline(METER_SUBLINE_GIST);
    return;
  }
  elMeterEmpty.style.display = "none";
  const spec = specFromTracks(tracks);
  const luRange = `${LU_MIN.toFixed(0)}…+${LU_MAX.toFixed(0)} LU`;
  const tpRange = `${TP_MIN.toFixed(0)}…${TP_MAX.toFixed(0)} dBTP`;
  setMeterHeader(spec, `display ${luRange} · ${tpRange}`);

  const luTicks = ticksHtml([-4, -2, 0, 2, 4], LU_MIN, LU_MAX, "LU");
  const tpTicks = ticksHtml([-6, -4, -2, 0], TP_MIN, TP_MAX, "dBTP");
  const luRows = tracks.map((t, i) => `
    <div class="shared-lang-row">
      <span class="lang">${esc(t.lang)}</span>
      ${luTrackInner(t, i)}
      <span class="readout-val ${t.loudPass ? "readout-pass" : "readout-fail"} tabular">${meterCue(t.loudPass)} ${esc(fmtFixed(t.loudnessLufs))} LUFS (${esc(fmtDev(t.deviationLu))})</span>
    </div>`).join("");
  const tpRows = tracks.map((t, i) => `
    <div class="shared-lang-row">
      <span class="lang">${esc(t.lang)}</span>
      ${tpTrackInner(t, i)}
      <span class="readout-val ${t.tpPass ? "readout-pass" : "readout-fail"} tabular">${meterCue(t.tpPass)} ${esc(fmtFixed(t.tpDbtp))} dBTP</span>
    </div>`).join("");
  elMeterGrid.innerHTML = `
    <div class="meter-axis-caption">Relative LU (${LU_MIN.toFixed(0)} … +${LU_MAX.toFixed(0)})</div>
    <div class="scale-ticks">${luTicks}</div>
    ${luRows}
    <div class="meter-axis-caption meter-axis-caption-tp">True Peak (${TP_MIN.toFixed(0)} … ${TP_MAX.toFixed(0)} dBTP)</div>
    <div class="scale-ticks">${tpTicks}</div>
    ${tpRows}`;
  settleMeterMarkers();
}

// ---------------------------------------------------------------------------
// Fix list
// ---------------------------------------------------------------------------

function renderFixList(plan, findings, state) {
  elFixBody.innerHTML = "";
  const jobs = (plan && plan.jobs) || [];
  const rows = jobs.length
    ? jobs
    : (findings || []).length
      ? [{
          remediation_stage: "ungrouped",
          severity: "",
          language_fanout: "",
          items: findings,
        }]
      : [];

  if (!rows.length) {
    elFixEmpty.style.display = "";
    if (state && state.status === "done" && state.verdict === "PASS") {
      elFixEmpty.textContent = "No non-conformances — all 5 clauses passed";
    } else if (state && state.status === "done") {
      elFixEmpty.textContent = "No non-conformances — 0 findings";
    } else {
      elFixEmpty.textContent = FIX_EMPTY_IDLE;
    }
    return;
  }
  elFixEmpty.style.display = "none";

  rows.forEach((job) => {
    const header = document.createElement("tr");
    header.className = "job-stage-row";
    header.innerHTML = `<td colspan="6">${esc(job.remediation_stage || "")} · ${esc(job.severity || "")} · fanout ${esc(job.language_fanout)}</td>`;
    elFixBody.appendChild(header);
    (job.items || []).forEach((f) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="mono clause-id">${esc(f.clause_id || "")}</td>
        <td><span class="badge badge-${(f.severity || "").toLowerCase()}">${esc((f.severity || "").toUpperCase())}</span></td>
        <td class="mono tabular">${esc(f.measured || "")}</td>
        <td class="mono tabular">${esc(f.expected || "")}</td>
        <td class="lang">${esc(f.language || "—")}</td>
        <td class="finding-msg">${esc(f.message || "")}</td>
      `;
      elFixBody.appendChild(tr);
    });
  });
}

// ---------------------------------------------------------------------------
// Readiness grid
// ---------------------------------------------------------------------------

function renderReadinessEmptyRow(message) {
  const tr = document.createElement("tr");
  tr.innerHTML = `<td colspan="3" class="empty-state">${esc(message)}</td>`;
  elReadinessBody.appendChild(tr);
}

function renderReadiness(readiness, runState) {
  if (!elReadinessBody) return;
  elReadinessBody.innerHTML = "";
  const langs = Object.keys(readiness || {}).sort();
  if (!langs.length) {
    const msg =
      runState && runState.status === "done" ? READINESS_POST_RUN : READINESS_IDLE;
    renderReadinessEmptyRow(msg);
    return;
  }

  langs.forEach((lang) => {
    const ratio = readiness[lang];
    const pct = Math.round(ratio * 100);
    const cls = pct >= 100 ? "ready-full" : pct >= 67 ? "ready-partial" : "ready-low";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="lang">${esc(lang)}</td>
      <td class="mono tabular">${pct}%</td>
      <td><div class="readiness-bar ${cls}" style="width:${pct}%"></div></td>
    `;
    elReadinessBody.appendChild(tr);
  });
}

// ---------------------------------------------------------------------------
// Action ledger — incremental append with motion
// ---------------------------------------------------------------------------

function appendNewLedgerRows(ledgerEntries) {
  const newEntries = ledgerEntries.slice(renderedLedgerCount);
  if (!newEntries.length) return;

  if (elLedgerEmpty) elLedgerEmpty.style.display = "none";

  newEntries.forEach((entry) => {
    const tr = document.createElement("tr");
    tr.className = "ledger-row ledger-appear";

    const safeHref = safeUrl(entry.href);
    const linkCell = entry.href && safeHref !== "#"
      ? `<a href="${safeHref}" target="_blank" rel="noopener noreferrer" class="ledger-link">${esc(entry.link_label || "Open")}</a>`
      : `<span class="text-muted">—</span>`;

    tr.innerHTML = `
      <td class="mono ledger-ts">${esc(entry.timestamp || "")}</td>
      <td class="ledger-op">${esc(entry.operation || "")}</td>
      <td class="ledger-detail">${esc(entry.detail || "")}</td>
      <td>${linkCell}</td>
    `;
    elLedgerBody.appendChild(tr);
  });

  renderedLedgerCount = ledgerEntries.length;
}

const PHASE_BY_OPERATION = {
  "Evaluate spec": "evaluating spec",
  "Publish telemetry": "publishing telemetry",
  "Publish dashboard": "publishing dashboard",
  "Start agent": "agent: connecting",
  "Open Incident": "agent: opening incident",
  "Post Activity": "agent: posting activity",
  "Create Annotation": "agent: annotating dashboard",
  "Manage Alert Rule": "agent: managing alert rule",
  "Verify Alert Rule": "agent: verified alert rule",
};

function phaseLabelFromLedger(ledger) {
  for (let i = ledger.length - 1; i >= 0; i--) {
    const entry = ledger[i];
    if (!entry || entry.phase === "response") continue;
    const label = PHASE_BY_OPERATION[entry.operation];
    if (label) return label;
  }
  return "";
}

function updatePhaseFromLedger(ledger) {
  const label = phaseLabelFromLedger(ledger);
  if (elVerdictPhase) elVerdictPhase.textContent = label;
  if (label) setStatus(label, "info");
}

function clearPhase() {
  if (elVerdictPhase) elVerdictPhase.textContent = "";
}

function wireDashboardLink() {
  const dash = document.getElementById("grafana-dashboard-link");
  if (!dash) return;
  const href = safeUrl(dash.getAttribute("data-href"));
  if (href !== "#") {
    dash.setAttribute("href", href);
  }
}

function positionGloss(btn, pop) {
  const r = btn.getBoundingClientRect();
  pop.style.top = `${Math.round(r.bottom + 6)}px`;
  pop.style.left = `${Math.round(r.left)}px`;
}

function hideOpenGlossaries() {
  document.querySelectorAll(".gloss-pop").forEach((pop) => {
    if (typeof pop.hidePopover === "function" && pop.matches(":popover-open")) {
      pop.hidePopover();
    }
  });
}

function wireGlossary() {
  document.querySelectorAll(".gloss-trigger[popovertarget]").forEach((btn) => {
    const pop = document.getElementById(btn.getAttribute("popovertarget"));
    if (!pop || typeof pop.showPopover !== "function") return;
    pop.addEventListener("toggle", (e) => {
      btn.setAttribute("aria-expanded", e.newState === "open" ? "true" : "false");
      if (e.newState === "open") positionGloss(btn, pop);
    });
    if (!window.matchMedia("(hover: hover)").matches) return;
    const show = () => {
      if (!pop.matches(":popover-open")) pop.showPopover();
    };
    const hide = () => {
      if (pop.matches(":popover-open")) pop.hidePopover();
    };
    btn.addEventListener("mouseenter", show);
    btn.addEventListener("mouseleave", hide);
    pop.addEventListener("mouseenter", show);
    pop.addEventListener("mouseleave", hide);
  });
  window.addEventListener("scroll", hideOpenGlossaries, { passive: true });
  window.addEventListener("resize", hideOpenGlossaries);
}

// ---------------------------------------------------------------------------
// How it works — toggle + architecture diagram wires
// ---------------------------------------------------------------------------

const ARCH_CANVAS_WIDTH = 1856;

const ARCH_EDGES = [
  { from: ["arch-inputs", "right", 0.5],  to: ["arch-engine", "left", 0.5],  label: "master + spec", dashed: false, route: "h" },
  { from: ["arch-engine", "right", 0.25], to: ["arch-grafana", "left", 0.18], label: "POST /api/dashboards/db · Python REST, not MCP", dashed: false, route: "hvh", channelX: 1300 },
  { from: ["arch-engine", "right", 0.55], to: ["arch-grafana", "left", 0.45], label: "Prometheus remote-write + Loki push", dashed: false, route: "hvh", channelX: 1340 },
  { from: ["arch-engine", "right", 0.85], to: ["arch-grafana", "left", 0.72], label: "GET Ruler API · read-only pre-query", dashed: true, route: "hvh", channelX: 1380 },
  { from: ["arch-engine", "bottom", 0.5], to: ["arch-agent", "top", 0.5],    label: "structured findings", dashed: false, route: "v" },
  { from: ["arch-agent", "right", 0.5],   to: ["arch-grafana", "left", 0.90], label: "MCP streamable-http", dashed: false, route: "hv" },
  { from: ["arch-engine", "left", 0.7],   to: ["arch-console", "top", 0.22], label: "verdict + meters", dashed: false, route: "console-left" },
  { from: ["arch-agent", "bottom", 0.5],  to: ["arch-console", "top", 0.50], label: "action ledger", dashed: false, route: "v" },
];

function archPort(canvas, id, side, t) {
  const el = canvas.querySelector("#" + id);
  if (!el) return { x: 0, y: 0 };
  const x0 = el.offsetLeft;
  const y0 = el.offsetTop;
  const w = el.offsetWidth;
  const h = el.offsetHeight;
  const k = t === undefined ? 0.5 : t;
  if (side === "right")  return { x: x0 + w,     y: y0 + h * k };
  if (side === "left")   return { x: x0,         y: y0 + h * k };
  if (side === "bottom") return { x: x0 + w * k, y: y0 + h };
  if (side === "top")    return { x: x0 + w * k, y: y0 };
  return { x: 0, y: 0 };
}

function archPolyline(pts) {
  return pts.map((p, i) => (i === 0 ? "M " + p.x + " " + p.y : "L " + p.x + " " + p.y)).join(" ");
}

function archPointsFor(e, a, b) {
  if (e.route === "h") return [a, { x: b.x, y: a.y }, b];
  if (e.route === "v") return [a, b];
  if (e.route === "hv") {
    const midX = (a.x + b.x) / 2;
    return [a, { x: midX, y: a.y }, { x: midX, y: b.y }, b];
  }
  if (e.route === "hvh") {
    const x = e.channelX;
    return [a, { x: x, y: a.y }, { x: x, y: b.y }, b];
  }
  if (e.route === "console-left") {
    const gutter = 400;
    return [a, { x: gutter, y: a.y }, { x: gutter, y: b.y }, b];
  }
  return [a, b];
}

function archLabelAnchor(pts) {
  let best = 0;
  let bestLen = -1;
  for (let i = 0; i < pts.length - 1; i++) {
    const dx = pts[i + 1].x - pts[i].x;
    const dy = pts[i + 1].y - pts[i].y;
    const len = Math.abs(dx) + Math.abs(dy);
    if (len > bestLen) { bestLen = len; best = i; }
  }
  const p0 = pts[best];
  const p1 = pts[best + 1];
  return {
    x: p0.x * 0.35 + p1.x * 0.65,
    y: p0.y * 0.35 + p1.y * 0.65,
  };
}

function alignArchDiagram() {
  const canvas = document.getElementById("arch-canvas");
  if (!canvas) return;
  const engine = canvas.querySelector("#arch-engine");
  const agent = canvas.querySelector("#arch-agent");
  const inputs = canvas.querySelector("#arch-inputs");
  const grafana = canvas.querySelector("#arch-grafana");
  if (!engine || !agent || !inputs || !grafana) return;

  inputs.style.top = (engine.offsetTop + engine.offsetHeight / 2 - inputs.offsetHeight / 2) + "px";
  const mid = (engine.offsetTop + agent.offsetTop + agent.offsetHeight) / 2;
  grafana.style.top = (mid - grafana.offsetHeight / 2) + "px";

  const scroll = canvas.parentElement;
  if (!scroll) return;
  const s = Math.min(1, scroll.clientWidth / ARCH_CANVAS_WIDTH);
  canvas.style.transform = s < 1 ? "scale(" + s + ")" : "";
  scroll.style.height = (canvas.offsetHeight * s) + "px";
}

function drawArchWires() {
  const canvas = document.getElementById("arch-canvas");
  const svg = document.getElementById("arch-wires");
  if (!canvas || !svg) return;

  svg.innerHTML =
    "<defs>" +
    '<marker id="arch-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">' +
    '<path d="M 0 0 L 10 5 L 0 10 z" fill="#8d9095"/>' +
    "</marker>" +
    '<marker id="arch-arrow-warn" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">' +
    '<path d="M 0 0 L 10 5 L 0 10 z" fill="#f89b29"/>' +
    "</marker>" +
    "</defs>";

  canvas.querySelectorAll(".arch-edge").forEach((n) => n.remove());

  ARCH_EDGES.forEach((e) => {
    const a = archPort(canvas, e.from[0], e.from[1], e.from[2]);
    const b = archPort(canvas, e.to[0], e.to[1], e.to[2]);
    const pts = archPointsFor(e, a, b);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", archPolyline(pts));
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", e.dashed ? "#f89b29" : "#8d9095");
    path.setAttribute("stroke-width", "1.75");
    if (e.dashed) path.setAttribute("stroke-dasharray", "7 5");
    path.setAttribute("marker-end", e.dashed ? "url(#arch-arrow-warn)" : "url(#arch-arrow)");
    svg.appendChild(path);

    const mid = e.route === "hvh"
      ? { x: e.channelX, y: (a.y + b.y) / 2 }
      : archLabelAnchor(pts);
    const lab = document.createElement("div");
    lab.className = "arch-edge" + (e.dashed ? " dashed" : "");
    lab.textContent = e.label;
    lab.style.left = mid.x + "px";
    lab.style.top = mid.y + "px";
    if (e.route === "hvh") lab.style.transform = "translate(-100%, -50%)";
    canvas.appendChild(lab);
  });
}

function refreshArchDiagram() {
  alignArchDiagram();
  drawArchWires();
}

function wireHowItWorks() {
  const toggle = document.getElementById("howitworks-toggle");
  const panel = document.getElementById("howitworks");
  const lanes = document.querySelector(".lanes");
  if (!toggle || !panel || !lanes) return;

  function setOpen(open) {
    panel.hidden = !open;
    lanes.hidden = open;
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      hideOpenGlossaries();
      requestAnimationFrame(() => {
        refreshArchDiagram();
      });
    }
  }

  toggle.addEventListener("click", () => {
    setOpen(panel.hidden);
  });

  window.addEventListener("resize", () => {
    if (!panel.hidden) {
      requestAnimationFrame(refreshArchDiagram);
    }
  });
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------

function resetUI() {
  elVerdictText.textContent = "EVALUATING…";
  elVerdictText.classList.remove("verdict-stamp-in");
  elBlockerCount.textContent = "";
  elMasterId.textContent = "";
  elVerdict.className = "burn-in verdict-idle";
  clearPhase();
  announceVerdict("Verdict EVALUATING");
  elFixBody.innerHTML = "";
  elFixEmpty.textContent = FIX_EMPTY_IDLE;
  elFixEmpty.style.display = "";
  elLedgerBody.innerHTML = "";
  if (elLedgerEmpty) elLedgerEmpty.style.display = "";
  if (elReadinessBody) {
    elReadinessBody.innerHTML = "";
    renderReadinessEmptyRow(READINESS_IDLE);
  }
  if (elMeterGrid) elMeterGrid.innerHTML = "";
  if (elMeterEmpty) elMeterEmpty.style.display = "";
  setMeterSpecSubline(METER_SUBLINE_GIST);
  if (elDurationStrip) {
    elDurationStrip.hidden = true;
    elDurationStrip.textContent = "";
  }
  if (elAgentPassNote) elAgentPassNote.hidden = true;
  if (elSlateTc) elSlateTc.textContent = "00:00:00:00";
}

function setRunning(running) {
  elRunBtn.disabled = running;
  elRunBtn.setAttribute("aria-busy", running ? "true" : "false");
}

function setStatus(msg, level) {
  if (!elStatus) return;
  elStatus.textContent = msg;
  elStatus.className = "run-status run-status-" + (level || "info");
}

function safeRender(fn, name) {
  try {
    fn();
  } catch (err) {
    console.error(`First Pass console: ${name} render failed`, err);
  }
}

/** Validates URL scheme (http/https only) to prevent javascript: or unescaped URI injection. */
function safeUrl(url) {
  if (!url) return "#";
  const s = String(url).trim();
  return s.startsWith("http://") || s.startsWith("https://") ? esc(s) : "#";
}

/** Minimal HTML escaping — avoids XSS when rendering server data in innerHTML. */
function esc(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
