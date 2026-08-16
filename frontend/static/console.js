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

let elVerdict, elVerdictText, elBlockerCount, elMasterId, elVerdictLive;
let elFixList, elFixBody, elFixEmpty;
let elLedgerBody, elLedgerEmpty;
let elRunBtn, elMasterSelect, elSpecSelect, elStatus;
let elReadinessBody;
let elSlateMaster, elSlateSpec, elSlateLangs, elSlateDate, elSlateTc;
let elMeterGrid, elMeterEmpty, elMeterSectionTitle;

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  elVerdict       = document.getElementById("verdict-banner");
  elVerdictText   = document.getElementById("verdict-text");
  elBlockerCount  = document.getElementById("verdict-detail");
  elMasterId      = document.getElementById("verdict-master");
  elVerdictLive   = document.getElementById("verdict-live");
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

  if (elRunBtn) elRunBtn.addEventListener("click", handleRunClick);

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

function startTC() {
  stopTC();
  tcStartTime = Date.now();
  if (prefersReducedMotion()) {
    if (elSlateTc) elSlateTc.textContent = "00:00:00:00";
    return;
  }
  function tick() {
    if (!tcStartTime) return;
    if (elSlateTc) elSlateTc.textContent = formatTimecode(Date.now() - tcStartTime);
    tcAnimationFrame = requestAnimationFrame(tick);
  }
  tick();
}

function stopTC() {
  if (tcAnimationFrame) {
    cancelAnimationFrame(tcAnimationFrame);
    tcAnimationFrame = null;
  }
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
  setStatus("Pipeline evaluating master against spec…", "info");
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

  if (state.status === "done") {
    stopPolling();
    stopTC();
    renderVerdict(state);
    renderAudioMeters(state.evaluations || []);
    renderFixList(state.ranked_fix_plan || { jobs: [] }, state.findings || [], state);
    renderReadiness(state.readiness || {});
    setRunning(false);
    setStatus("Evaluation complete.", "info");
  } else if (state.status === "failed") {
    stopPolling();
    stopTC();
    setStatus(`Run failed: ${state.error || "unknown error"}`, "error");
    renderVerdict({ verdict: "FAILED", blocker_count: 0, master_id: state.master_id });
    setRunning(false);
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
    renderAudioMeters(state.evaluations || []);
    renderFixList(state.ranked_fix_plan || { jobs: [] }, state.findings || [], state);
    renderReadiness(state.readiness || {});
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
    ? `REJECT — ${blockers} ${blockers === 1 ? "blocker" : "blockers"}`
    : verdict;

  elBlockerCount.textContent =
    verdict === "PASS" ? "0 blockers · Delivery approved" :
    verdict === "REJECT" ? `${blockers} spec non-conformance${blockers !== 1 ? "s" : ""} must be resolved` :
    "";

  elMasterId.textContent = masterId ? `Master: ${masterId}` : "";

  const live =
    verdict === "PASS" ? "Verdict PASS" :
    verdict === "REJECT" ? `Verdict REJECT, ${blockers} blocker${blockers !== 1 ? "s" : ""}` :
    verdict === "FAILED" ? "Verdict FAILED" :
    `Verdict ${verdict}`;
  announceVerdict(live);

  let cls = "verdict-banner verdict-" +
    (verdict === "PASS" ? "pass" : verdict === "REJECT" ? "reject" : "idle");
  if (!prefersReducedMotion()) cls += " verdict-stamp-in";
  elVerdict.className = cls;
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

function collectTracks(evaluations) {
  const audioEvals = (evaluations || []).filter((e) => e.domain === "audio");
  const langsMap = {};
  audioEvals.forEach((e) => {
    const lang = e.language || "unknown";
    if (!langsMap[lang]) langsMap[lang] = {};
    if (e.clause_id === "A-2.1") langsMap[lang].loudness = e;
    if (e.clause_id === "A-2.2") langsMap[lang].true_peak = e;
  });
  const langs = Object.keys(langsMap).sort();
  const tracks = langs.map((lang) => {
    const loud = langsMap[lang].loudness || {};
    const peak = langsMap[lang].true_peak || {};
    const targetLufs = loud.target_lufs;
    const tolLu = loud.tolerance_lu;
    const deviationLu = loud.loudness_deviation_lufs;
    const loudnessLufs = loud.loudness_lufs;
    const tpDbtp = peak.true_peak_dbtp;
    const targetMaxDbtp = peak.target_max_dbtp;
    const loudPass = targetLufs !== undefined && tolLu !== undefined && deviationLu !== undefined
      ? Math.abs(deviationLu) <= tolLu
      : !!loud.passed;
    const tpPass = tpDbtp !== undefined && targetMaxDbtp !== undefined
      ? tpDbtp <= targetMaxDbtp
      : !!peak.passed;
    return {
      lang, loud, peak, targetLufs, tolLu, deviationLu, loudnessLufs,
      tpDbtp, targetMaxDbtp, loudPass, tpPass,
    };
  });
  return { tracks, langs };
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

function setMeterHeader(spec, rangeNote) {
  if (!elMeterSectionTitle) return;
  const parts = ["§ 2 · Audio Loudness & True Peak (EBU Tech 3341)"];
  if (spec.targetLufs !== undefined && spec.tolLu !== undefined) {
    parts.push(`0 LU = ${spec.targetLufs.toFixed(1)} LUFS ± ${spec.tolLu.toFixed(1)} LU`);
  }
  if (spec.targetMaxDbtp !== undefined) {
    parts.push(`ceiling ${spec.targetMaxDbtp.toFixed(1)} dBTP`);
  }
  if (rangeNote) parts.push(rangeNote);
  elMeterSectionTitle.textContent = parts.join(" · ");
}

function meterCue(pass) {
  return pass
    ? `<span class="meter-cue meter-cue-pass">PASS</span>`
    : `<span class="meter-cue meter-cue-fail">FAIL</span>`;
}

function luTrackInner(t) {
  const lu = mapToPct(t.deviationLu, LU_MIN, LU_MAX);
  const tolLeft = mapToPct(-t.tolLu, LU_MIN, LU_MAX).pct;
  const tolRight = mapToPct(t.tolLu, LU_MIN, LU_MAX).pct;
  const zero = mapToPct(0, LU_MIN, LU_MAX).pct;
  const cue = t.loudPass ? "PASS" : "FAIL";
  return `
    <div class="scale-track-container" role="img" aria-label="${esc(t.lang)} loudness ${esc(t.loudnessLufs.toFixed(1))} LUFS (${esc(fmtDev(t.deviationLu))}) ${cue}">
      <div class="tolerance-band" style="left:${tolLeft.toFixed(2)}%; width:${(tolRight - tolLeft).toFixed(2)}%;"></div>
      <div class="target-center-line" style="left:${zero.toFixed(2)}%;"></div>
      <div class="loudness-marker ${t.loudPass ? "loudness-marker-pass" : "loudness-marker-fail"}" style="left:${lu.pct.toFixed(2)}%;"></div>
      ${offScaleHtml(lu.off)}
    </div>`;
}

function tpTrackInner(t) {
  const tp = mapToPct(t.tpDbtp, TP_MIN, TP_MAX);
  const ceil = mapToPct(t.targetMaxDbtp, TP_MIN, TP_MAX);
  const cue = t.tpPass ? "PASS" : "FAIL";
  return `
    <div class="true-peak-track" role="img" aria-label="${esc(t.lang)} true peak ${esc(t.tpDbtp.toFixed(1))} dBTP ${cue}">
      <div class="ceiling-line" style="left:${ceil.pct.toFixed(2)}%;"></div>
      <div class="loudness-marker ${t.tpPass ? "loudness-marker-pass" : "loudness-marker-fail"}" style="left:${tp.pct.toFixed(2)}%;"></div>
      ${offScaleHtml(tp.off)}
    </div>`;
}

function fmtDev(dev) {
  if (dev === undefined) return "—";
  const sign = dev > 0 ? "+" : "";
  return `${sign}${dev.toFixed(1)} LU`;
}

function renderAudioMeters(evaluations) {
  if (!elMeterGrid || !elMeterEmpty) return;
  elMeterGrid.innerHTML = "";

  const { tracks } = collectTracks(evaluations);
  if (!tracks.length) {
    elMeterEmpty.style.display = "";
    if (elMeterSectionTitle) {
      elMeterSectionTitle.textContent = "§ 2 · Audio Loudness & True Peak (EBU Tech 3341)";
    }
    return;
  }
  elMeterEmpty.style.display = "none";
  const spec = specFromTracks(tracks);
  const luRange = `${LU_MIN.toFixed(0)}…+${LU_MAX.toFixed(0)} LU`;
  const tpRange = `${TP_MIN.toFixed(0)}…${TP_MAX.toFixed(0)} dBTP`;
  setMeterHeader(spec, `display ${luRange} · ${tpRange}`);

  const luTicks = ticksHtml([-4, -2, 0, 2, 4], LU_MIN, LU_MAX, "LU");
  const tpTicks = ticksHtml([-6, -4, -2, 0], TP_MIN, TP_MAX, "dBTP");
  const luRows = tracks.map((t) => `
    <div class="shared-lang-row">
      <span class="lang">${esc(t.lang)}</span>
      ${luTrackInner(t)}
      <span class="readout-val ${t.loudPass ? "readout-pass" : "readout-fail"} tabular">${meterCue(t.loudPass)} ${esc(t.loudnessLufs.toFixed(1))} LUFS (${esc(fmtDev(t.deviationLu))})</span>
    </div>`).join("");
  const tpRows = tracks.map((t) => `
    <div class="shared-lang-row">
      <span class="lang">${esc(t.lang)}</span>
      ${tpTrackInner(t)}
      <span class="readout-val ${t.tpPass ? "readout-pass" : "readout-fail"} tabular">${meterCue(t.tpPass)} ${esc(t.tpDbtp.toFixed(1))} dBTP</span>
    </div>`).join("");
  elMeterGrid.innerHTML = `
    <div class="meter-axis-caption">Relative LU (${LU_MIN.toFixed(0)} … +${LU_MAX.toFixed(0)})</div>
    <div class="scale-ticks">${luTicks}</div>
    ${luRows}
    <div class="meter-axis-caption meter-axis-caption-tp">True Peak (${TP_MIN.toFixed(0)} … ${TP_MAX.toFixed(0)} dBTP)</div>
    <div class="scale-ticks">${tpTicks}</div>
    ${tpRows}`;
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
      elFixEmpty.textContent = "No findings yet. Run the pipeline to populate.";
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

function renderReadiness(readiness) {
  if (!elReadinessBody) return;
  elReadinessBody.innerHTML = "";
  const langs = Object.keys(readiness).sort();
  if (!langs.length) return;

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

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------

function resetUI() {
  elVerdictText.textContent = "EVALUATING…";
  elBlockerCount.textContent = "";
  elMasterId.textContent = "";
  elVerdict.className = "verdict-banner verdict-idle";
  announceVerdict("Verdict EVALUATING");
  elFixBody.innerHTML = "";
  elFixEmpty.textContent = "No findings yet. Run the pipeline to populate.";
  elFixEmpty.style.display = "";
  elLedgerBody.innerHTML = "";
  if (elLedgerEmpty) elLedgerEmpty.style.display = "";
  if (elReadinessBody) elReadinessBody.innerHTML = "";
  if (elMeterGrid) elMeterGrid.innerHTML = "";
  if (elMeterEmpty) elMeterEmpty.style.display = "";
  if (elMeterSectionTitle) {
    elMeterSectionTitle.textContent = "§ 2 · Audio Loudness & True Peak (EBU Tech 3341)";
  }
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
