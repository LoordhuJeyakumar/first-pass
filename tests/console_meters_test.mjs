/**
 * Node harness: exercises meter_collect.collectTracks against stdin JSON evaluations.
 * Emits JSON summary to stdout; exit 1 on failure.
 */
import { createRequire } from "module";
import { fileURLToPath } from "url";
import path from "path";

const require = createRequire(import.meta.url);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const { collectTracks, isLoudnessEval, isTruePeakEval } = require(
  path.join(root, "frontend/static/meter_collect.js")
);

async function main() {
  const raw = await new Promise((resolve, reject) => {
    let buf = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { buf += chunk; });
    process.stdin.on("end", () => resolve(buf));
    process.stdin.on("error", reject);
  });

  const evaluations = JSON.parse(raw || "[]");
  const loudnessHits = evaluations.filter(isLoudnessEval);
  const tpHits = evaluations.filter(isTruePeakEval);

  if (!loudnessHits.length) {
    console.error("no loudness-shaped audio evaluations");
    process.exit(1);
  }

  const { tracks } = collectTracks(evaluations);
  if (!tracks.length) {
    console.error("collectTracks returned zero tracks");
    process.exit(1);
  }

  const missingLufs = tracks.filter(
    (t) => t.targetLufs !== undefined && t.loudnessLufs === undefined
  );
  if (missingLufs.length) {
    console.error("tracks missing loudnessLufs despite loudness eval:", missingLufs.map((t) => t.lang));
    process.exit(1);
  }

  process.stdout.write(JSON.stringify({
    track_count: tracks.length,
    langs: tracks.map((t) => t.lang),
    loudness_eval_count: loudnessHits.length,
    tp_eval_count: tpHits.length,
    sample: tracks[0],
  }));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
