/**
 * First Pass — Operator Console polling, EBU Tech 3341 metering, and QC slate logic.
 *
 * Responsibilities:
 *   - POST /api/run when the operator clicks RUN.
 *   - Drive 24 FPS frame-accurate timecode on the QC slate via requestAnimationFrame.
 *   - Poll GET /api/run/{run_id} every 1.5 s until status is done/failed.
 *   - Render relative LU audio loudness meters (-18 LU to +9 LU) and True Peak bars per EBU Tech 3341.
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

let elVerdict, elVerdictText, elBlockerCount, elMasterId;
let elFixList, elFixBody, elFixEmpty;
let elLedgerBody, elLedgerEmpty;
let elRunBtn, elMasterSelect, elStatus;
let elReadinessBody;
let elSlateMaster, elSlateSpec, elSlateLangs, elSlateDate, elSlateTc;
let elMeterGrid, elMeterEmpty;

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  elVerdict       = document.getElementById("verdict-banner");
  elVerdictText   = document.getElementById("verdict-text");
  elBlockerCount  = document.getElementById("verdict-detail");
  elMasterId      = document.getElementById("verdict-master");
  elFixList       = document.getElementById("fix-list");
  elFixBody       = document.getElementById("fix-body");
  elFixEmpty      = document.getElementById("fix-empty");
  elLedgerBody    = document.getElementById("ledger-body");
  elLedgerEmpty   = document.getElementById("ledger-empty");
  elRunBtn        = document.getElementById("run-btn");
  elMasterSelect  = document.getElementById("master-select");
  elStatus        = document.getElementById("run-status");
  elReadinessBody = document.getElementById("readiness-body");

  elSlateMaster   = document.getElementById("slate-master");
  elSlateSpec     = document.getElementById("slate-spec");
  elSlateLangs    = document.getElementById("slate-langs");
  elSlateDate     = document.getElementById("slate-date");
  elSlateTc       = document.getElementById("slate-tc");

  elMeterGrid     = document.getElementById("meter-grid");
  elMeterEmpty    = document.getElementById("meter-empty");

  if (elRunBtn) elRunBtn.addEventListener("click", handleRunClick);
  if (elSlateDate) elSlateDate.textContent = new Date().toISOString().slice(0, 10);
});

// ---------------------------------------------------------------------------
// Timecode Generator (24 FPS, requestAnimationFrame)
// ---------------------------------------------------------------------------

function startTC() {
  stopTC();
  tcStartTime = Date.now();
  function tick() {
    if (!tcStartTime) return;
    const elapsedMs = Date.now() - tcStartTime;
    const elapsedSec = elapsedMs / 1000.0;
    const totalFrames = Math.floor(elapsedSec * 24.0);
    const ff = String(totalFrames % 24).padStart(2, "0");
    const ss = String(Math.floor(elapsedSec) % 60).padStart(2, "0");
    const mm = String(Math.floor(elapsedSec / 60) % 60).padStart(2, "0");
    const hh = String(Math.floor(elapsedSec / 3600)).padStart(2, "0");
    if (elSlateTc) elSlateTc.textContent = `${hh}:${mm}:${ss}:${ff}`;
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
      body: JSON.stringify({ master }),
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
    renderFixList(state.findings || [], state);
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

function updateSlate(state) {
  if (elSlateMaster && state.master_id) elSlateMaster.textContent = state.master_id;
  if (elSlateSpec && state.spec_id) elSlateSpec.textContent = state.spec_id;

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

  // Reset class to force single-shot stamp animation keyframe execution
  elVerdict.className = "verdict-banner verdict-" +
    (verdict === "PASS" ? "pass" : verdict === "REJECT" ? "reject" : "idle") +
    " verdict-stamp-in";
}

// ---------------------------------------------------------------------------
// EBU Tech 3341 Audio Meters (Relative LU & True Peak dBTP)
// ---------------------------------------------------------------------------

function renderAudioMeters(evaluations) {
  if (!elMeterGrid || !elMeterEmpty) return;
  elMeterGrid.innerHTML = "";

  const audioEvals = (evaluations || []).filter((e) => e.domain === "audio");
  if (!audioEvals.length) {
    elMeterEmpty.style.display = "";
    return;
  }
  elMeterEmpty.style.display = "none";

  // Group by language
  const langsMap = {};
  audioEvals.forEach((e) => {
    const lang = e.language || "unknown";
    if (!langsMap[lang]) langsMap[lang] = {};
    if (e.clause_id === "A-2.1") langsMap[lang].loudness = e;
    if (e.clause_id === "A-2.2") langsMap[lang].true_peak = e;
  });

  const langs = Object.keys(langsMap).sort();
  langs.forEach((lang) => {
    const data = langsMap[lang];
    const loud = data.loudness || {};
    const peak = data.true_peak || {};

    const targetLufs = loud.target_lufs !== undefined ? loud.target_lufs : -27.0;
    const tolLu = loud.tolerance_lu !== undefined ? loud.tolerance_lu : 2.0;
    const deviationLu = loud.loudness_deviation_lufs !== undefined ? loud.loudness_deviation_lufs : 0.0;
    const loudnessLufs = loud.loudness_lufs !== undefined ? loud.loudness_lufs : targetLufs;

    const tpDbtp = peak.true_peak_dbtp !== undefined ? peak.true_peak_dbtp : -2.0;
    const targetMaxDbtp = peak.target_max_dbtp !== undefined ? peak.target_max_dbtp : -2.0;

    // Loudness check: passes if |deviation| <= tolerance_lu
    const loudPass = Math.abs(deviationLu) <= tolLu;
    // True Peak check: passes if tpDbtp <= targetMaxDbtp (Boundary: -2.0 <= -2.0 is PASS)
    const tpPass = tpDbtp <= targetMaxDbtp;

    const devSign = deviationLu > 0 ? "+" : "";
    const loudReadout = `${loudnessLufs.toFixed(1)} LUFS (${devSign}${deviationLu.toFixed(1)} LU)`;
    const tpReadout = `${tpDbtp.toFixed(1)} dBTP`;

    // Proportions for Relative LU scale (-18.0 LU to +9.0 LU, span = 27.0 LU)
    const luMarkerPct = Math.max(0, Math.min(100, ((deviationLu - (-18.0)) / 27.0) * 100));
    const tolLeftPct = Math.max(0, (((0 - tolLu) - (-18.0)) / 27.0) * 100);
    const tolRightPct = Math.min(100, (((0 + tolLu) - (-18.0)) / 27.0) * 100);
    const tolWidthPct = tolRightPct - tolLeftPct;
    const zeroPct = ((0 - (-18.0)) / 27.0) * 100;

    // Proportions for True Peak scale (-18.0 dBTP to +3.0 dBTP, span = 21.0 dBTP)
    const tpFillPct = Math.max(0, Math.min(100, ((tpDbtp - (-18.0)) / 21.0) * 100));
    const ceilingPct = Math.max(0, Math.min(100, ((targetMaxDbtp - (-18.0)) / 21.0) * 100));

    const row = document.createElement("div");
    row.className = "meter-track-row";
    row.innerHTML = `
      <div class="meter-track-header">
        <span class="meter-track-title">${esc(lang)}</span>
        <div class="meter-track-readouts">
          <div class="readout-item">
            <span class="readout-label">LOUDNESS:</span>
            <span class="readout-val ${loudPass ? 'readout-pass' : 'readout-fail'}">${esc(loudReadout)}</span>
          </div>
          <div class="readout-item">
            <span class="readout-label">TRUE PEAK:</span>
            <span class="readout-val ${tpPass ? 'readout-pass' : 'readout-fail'}">${esc(tpReadout)}</span>
          </div>
        </div>
      </div>

      <!-- Relative LU Scale Block -->
      <div class="meter-bar-block">
        <div class="meter-bar-header">
          <span>Integrated Loudness (Relative LU, target ${targetLufs.toFixed(1)} LUFS ± ${tolLu.toFixed(1)} LU)</span>
          <span>0 LU = ${targetLufs.toFixed(1)} LUFS</span>
        </div>
        <div class="scale-track-container" aria-label="${esc(lang)} loudness meter">
          <div class="tolerance-band" style="left:${tolLeftPct.toFixed(2)}%; width:${tolWidthPct.toFixed(2)}%;"></div>
          <div class="target-center-line" style="left:${zeroPct.toFixed(2)}%;"></div>
          <div class="loudness-marker ${loudPass ? 'loudness-marker-pass' : 'loudness-marker-fail'}" style="left:${luMarkerPct.toFixed(2)}%;"></div>
        </div>
        <div class="scale-ticks">
          <span class="tick-mark" style="left:0%">-18 LU</span>
          <span class="tick-mark" style="left:22.22%">-12 LU</span>
          <span class="tick-mark" style="left:44.44%">-6 LU</span>
          <span class="tick-mark" style="left:59.26%">-2 LU</span>
          <span class="tick-mark" style="left:66.67%">0 LU</span>
          <span class="tick-mark" style="left:74.07%">+2 LU</span>
          <span class="tick-mark" style="left:88.89%">+6 LU</span>
          <span class="tick-mark" style="left:100%">+9 LU</span>
        </div>
      </div>

      <!-- True Peak dBTP Bar Block -->
      <div class="meter-bar-block" style="margin-top:4px;">
        <div class="meter-bar-header">
          <span>True Peak (Max ${targetMaxDbtp.toFixed(1)} dBTP limit)</span>
          <span>Ceiling: ${targetMaxDbtp.toFixed(1)} dBTP</span>
        </div>
        <div class="true-peak-track" aria-label="${esc(lang)} true peak meter">
          <div class="ceiling-line" style="left:${ceilingPct.toFixed(2)}%;"></div>
          <div class="true-peak-fill ${tpPass ? 'true-peak-pass' : 'true-peak-fail'}" style="width:${tpFillPct.toFixed(2)}%;"></div>
        </div>
        <div class="scale-ticks">
          <span class="tick-mark" style="left:0%">-18 dBTP</span>
          <span class="tick-mark" style="left:28.57%">-12 dBTP</span>
          <span class="tick-mark" style="left:57.14%">-6 dBTP</span>
          <span class="tick-mark" style="left:76.19%">-2 dBTP</span>
          <span class="tick-mark" style="left:85.71%">0 dBTP</span>
          <span class="tick-mark" style="left:100%">+3 dBTP</span>
        </div>
      </div>
    `;
    elMeterGrid.appendChild(row);
  });
}

// ---------------------------------------------------------------------------
// Fix list
// ---------------------------------------------------------------------------

function renderFixList(findings, state) {
  elFixBody.innerHTML = "";

  if (!findings || findings.length === 0) {
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

  findings.forEach((f) => {
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
  elFixBody.innerHTML = "";
  elFixEmpty.textContent = "No findings yet. Run the pipeline to populate.";
  elFixEmpty.style.display = "";
  elLedgerBody.innerHTML = "";
  if (elLedgerEmpty) elLedgerEmpty.style.display = "";
  if (elReadinessBody) elReadinessBody.innerHTML = "";
  if (elMeterGrid) elMeterGrid.innerHTML = "";
  if (elMeterEmpty) elMeterEmpty.style.display = "";
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
