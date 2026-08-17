/**
 * Pure audio-meter track collection — spec-agnostic, keyed by evaluation payload shape.
 * Loaded before console.js; also require()-able from Node tests.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.MeterCollect = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function isLoudnessEval(e) {
    return !!(
      e &&
      e.domain === "audio" &&
      e.loudness_lufs !== undefined &&
      e.loudness_deviation_lufs !== undefined &&
      e.target_lufs !== undefined &&
      e.tolerance_lu !== undefined
    );
  }

  function isTruePeakEval(e) {
    return !!(
      e &&
      e.domain === "audio" &&
      e.true_peak_dbtp !== undefined &&
      e.target_max_dbtp !== undefined
    );
  }

  function fmtFixed(value, decimals) {
    const d = decimals === undefined ? 1 : decimals;
    if (value === undefined || value === null || Number.isNaN(Number(value))) return "—";
    return Number(value).toFixed(d);
  }

  function collectTracks(evaluations) {
    const audioEvals = (evaluations || []).filter((e) => e.domain === "audio");
    const langsMap = {};
    audioEvals.forEach((e) => {
      const lang = e.language || "unknown";
      if (!langsMap[lang]) langsMap[lang] = {};
      if (isLoudnessEval(e)) langsMap[lang].loudness = e;
      if (isTruePeakEval(e)) langsMap[lang].true_peak = e;
    });
    const langs = Object.keys(langsMap)
      .filter((lang) => langsMap[lang].loudness || langsMap[lang].true_peak)
      .sort();
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

  return { isLoudnessEval, isTruePeakEval, collectTracks, fmtFixed };
});
