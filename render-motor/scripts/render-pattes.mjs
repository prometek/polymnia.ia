// Preuve de diversité (décision bloquante #2) : rend le MÊME contenu sous 2 style_vectors
// distincts -> 2 vidéos visuellement différentes. Extrait des frames PNG comme preuve.
import { spawnSync, execFileSync } from "node:child_process";
import { mkdirSync, existsSync } from "node:fs";

const DURATION_S = Number(process.env.DURATION_S ?? 12); // court : preuve visuelle, pas perf
const PRESETS = (process.env.PRESETS ?? "whiteboard,kawaii,aquarelle,retro,tech").split(",");
const FPS = 60;

mkdirSync("out", { recursive: true });
if (!existsSync("public/audio.wav")) {
  execFileSync("node", ["scripts/gen-audio.mjs"], { stdio: "inherit", env: { ...process.env, DURATION_S: String(DURATION_S) } });
}

for (const preset of PRESETS) {
  const mp4 = `out/${preset}.mp4`;
  console.log(`\n=== rendu ${preset} (${DURATION_S}s) ===`);
  const r = spawnSync("npx", ["remotion", "render", "src/index.ts", preset, mp4, "--props", JSON.stringify({ durationS: DURATION_S })], {
    stdio: "inherit",
    env: { ...process.env, DURATION_S: String(DURATION_S) },
  });
  if (r.status !== 0) { console.error(`échec rendu ${preset}`); process.exit(1); }

  // Extrait une frame par scène (~12/37/62/88 % de la durée) comme preuve visuelle.
  const pts = [0.12, 0.37, 0.62, 0.88].map((p) => +(p * DURATION_S).toFixed(2));
  pts.forEach((t, i) => {
    // -ss APRÈS -i = seek précis (avant -i = snap keyframe, montrerait la mauvaise scène).
    execFileSync("ffmpeg", ["-y", "-i", mp4, "-ss", String(t), "-frames:v", "1", `out/${preset}-s${i + 1}.png`], { stdio: "ignore" });
  });
  console.log(`frames preuve -> out/${preset}-s1..s4.png`);
}

console.log(`\nOK : ${PRESETS.length} styles visuels rendus (même contenu, directions artistiques distinctes). Comparer out/<style>-s1..s4.png.`);
