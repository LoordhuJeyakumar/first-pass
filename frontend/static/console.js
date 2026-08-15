/**
 * First Pass — Operator Console polling and ledger logic.
 *
 * Responsibilities:
 *   - POST /api/run when the operator clicks RUN.
 *   - Poll GET /api/run/{run_id} every 1.5 s until status is done/failed.
 *   - Append ledger rows incrementally (only newly-received entries).
 *   - Update verdict banner and fix list on completion.
 *   - Enforce button disable during run (UI-layer guard; server enforces the real guard).
 */

"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let activeRunId = null;
let pollTimer = null;
let renderedLedgerCount = 0;

// ---------------------------------------------------------------------------
// DOM references (resolved after DOMContentLoaded)
// ---------------------------------------------------------------------------

let elVerdict, elVerdictText, elBlockerCount, elMasterId;
let elFixList, elFixBody, elFixEmpty;
let elLedgerBody, elLedgerEmpty;
let elRunBtn, elMasterSelect, elSpinner, elStatus;
let elReadinessBody;

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  elVerdict      = document.getElementById("verdict-banner");
  elVerdictText  = document.getElementById("verdict-text");
  elBlockerCount = document.getElementById("verdict-detail");
  elMasterId     = document.getElementById("verdict-master");
  elFixList      = document.getElementById("fix-list");
  elFixBody      = document.getElementById("fix-body");
  elFixEmpty     = document.getElementById("fix-empty");
  elLedgerBody   = document.getElementById("ledger-body");
  elLedgerEmpty  = document.getElementById("ledger-empty");
  elRunBtn       = document.getElementById("run-btn");
  elMasterSelect = document.getElementById("master-select");
  elSpinner      = document.getElementById("spinner");
  elStatus       = document.getElementById("run-status");
  elReadinessBody = document.getElementById("readiness-body");

  elRunBtn.addEventListener("click", handleRunClick);
});

// ---------------------------------------------------------------------------
// Run trigger
// ---------------------------------------------------------------------------

async function handleRunClick() {
  const master = elMasterSelect.value;
  if (!master) return;

  setRunning(true);
  resetUI();

  let response;
  try {
    response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ master }),
    });
  } catch (err) {
    setStatus(`Network error: ${err.message}`, "error");
    setRunning(false);
    return;
  }

  if (response.status === 409) {
    setStatus("A run is already in progress — wait for it to finish.", "warn");
    setRunning(false);
    return;
  }
  if (response.status === 429) {
    const data = await response.json();
    setStatus(`Cooldown active. Retry in ${data.retry_after}s.`, "warn");
    setRunning(false);
    return;
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    setStatus(`Error ${response.status}: ${data.detail || "unknown"}`, "error");
    setRunning(false);
    return;
  }

  const data = await response.json();
  activeRunId = data.run_id;
  renderedLedgerCount = 0;
  setStatus("Pipeline running…", "info");
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

  if (state.status === "done") {
    stopPolling();
    renderVerdict(state);
    renderFixList(state.findings || []);
    renderReadiness(state.readiness || {});
    setRunning(false);
    setStatus("Run complete.", "info");
  } else if (state.status === "failed") {
    stopPolling();
    setStatus(`Run failed: ${state.error || "unknown error"}`, "error");
    renderVerdict({ verdict: "FAILED", blocker_count: 0, master_id: "" });
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
// Verdict banner
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

  elVerdict.className = "verdict-banner verdict-" +
    (verdict === "PASS" ? "pass" : verdict === "REJECT" ? "reject" : "idle");
}

// ---------------------------------------------------------------------------
// Fix list
// ---------------------------------------------------------------------------

function renderFixList(findings) {
  elFixBody.innerHTML = "";

  if (!findings || findings.length === 0) {
    elFixEmpty.style.display = "";
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

    const linkCell = entry.href
      ? `<a href="${esc(entry.href)}" target="_blank" rel="noopener noreferrer" class="ledger-link">${esc(entry.link_label || "Open")}</a>`
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
  elVerdictText.textContent = "RUNNING…";
  elBlockerCount.textContent = "";
  elMasterId.textContent = "";
  elVerdict.className = "verdict-banner verdict-idle";
  elFixBody.innerHTML = "";
  elFixEmpty.style.display = "";
  elLedgerBody.innerHTML = "";
  if (elLedgerEmpty) elLedgerEmpty.style.display = "";
  if (elReadinessBody) elReadinessBody.innerHTML = "";
}

function setRunning(running) {
  elRunBtn.disabled = running;
  elRunBtn.setAttribute("aria-busy", running ? "true" : "false");
  if (elSpinner) elSpinner.style.display = running ? "inline" : "none";
}

function setStatus(msg, level) {
  if (!elStatus) return;
  elStatus.textContent = msg;
  elStatus.className = "run-status run-status-" + (level || "info");
}

/** Minimal HTML escaping — avoids XSS when rendering server data in innerHTML. */
function esc(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
