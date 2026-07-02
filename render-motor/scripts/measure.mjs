// Mesure coût/temps de rendu Remotion (décision bloquante #1).
// Rend la composition, chronomètre, capture le pic mémoire, extrapole le coût.
import { spawnSync, execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync, existsSync } from "node:fs";
import os from "node:os";

const DURATION_S = Number(process.env.DURATION_S ?? 60);
const CONCURRENCY = process.env.CONCURRENCY ?? null; // null => défaut Remotion
const RATE = Number(process.env.RATE_USD_PER_HR ?? 0.197); // ~Fargate 4 vCPU / 8 GB
const FPS = 60;
const FRAMES = Math.round(DURATION_S * FPS);

mkdirSync("out", { recursive: true });

// 1. Audio stub à la même durée.
if (!existsSync("public/audio.wav")) {
  execFileSync("node", ["scripts/gen-audio.mjs"], { stdio: "inherit", env: process.env });
}

// 2. Construit la commande de rendu (+ GNU time si dispo pour le pic RSS).
const COMP = process.env.COMPOSITION ?? "whiteboard";
const renderArgs = ["remotion", "render", "src/index.ts", COMP, "out/video.mp4", "--props", JSON.stringify({ durationS: DURATION_S })];
if (CONCURRENCY) renderArgs.push(`--concurrency=${CONCURRENCY}`);

const hasGnuTime = spawnSync("/usr/bin/time", ["-v", "true"], { encoding: "utf8" }).status === 0;
const cmd = hasGnuTime ? "/usr/bin/time" : "npx";
const args = hasGnuTime ? ["-v", "npx", ...renderArgs] : renderArgs;

console.log(`render: ${DURATION_S}s @ ${FPS}fps (${FRAMES} frames), concurrency=${CONCURRENCY ?? "auto"}`);

// 3. Chronomètre wall-clock autour du process.
const t0 = Date.now();
const res = spawnSync(cmd, args, { encoding: "utf8", env: { ...process.env, DURATION_S: String(DURATION_S) } });
const wallS = (Date.now() - t0) / 1000;

const combined = (res.stdout ?? "") + (res.stderr ?? "");
process.stdout.write(combined);
if (res.status !== 0) {
  console.error(`render failed (exit ${res.status})`);
  process.exit(1);
}

// 4. Pic RSS depuis la sortie GNU time (kbytes -> MB).
let peakRssMB = null;
const m = combined.match(/Maximum resident set size \(kbytes\):\s*(\d+)/);
if (m) peakRssMB = Math.round(Number(m[1]) / 1024);

// 5. Métriques dérivées.
const videoMin = DURATION_S / 60;
const wallPerVideoMin = wallS / videoMin;        // secondes de rendu par minute de vidéo
const realtimeFactor = DURATION_S / wallS;        // >1 = plus rapide que le temps réel
const effFps = FRAMES / wallS;                    // frames rendues par seconde
const costPerVideoMin = (wallPerVideoMin / 3600) * RATE;

const metrics = {
  timestamp: new Date().toISOString(),
  host: { platform: os.platform(), arch: os.arch(), cpus: os.cpus().length, model: os.cpus()[0]?.model, totalMemGB: +(os.totalmem() / 1e9).toFixed(1) },
  config: { durationS: DURATION_S, fps: FPS, frames: FRAMES, resolution: "1920x1080", concurrency: CONCURRENCY ?? "auto", codec: "h264" },
  result: {
    wallSeconds: +wallS.toFixed(1),
    wallPerVideoMinuteSeconds: +wallPerVideoMin.toFixed(1),
    realtimeFactor: +realtimeFactor.toFixed(2),
    effectiveFps: +effFps.toFixed(1),
    peakRssMB,
  },
  cost: { rateUsdPerHour: RATE, usdPerVideoMinute: +costPerVideoMin.toFixed(4) },
};

writeFileSync("out/metrics.json", JSON.stringify(metrics, null, 2));

const r = metrics.result;
console.log(`
========== POC RENDU REMOTION — MESURE ==========
Host            : ${metrics.host.cpus} vCPU, ${metrics.host.totalMemGB} GB (${metrics.host.platform}/${metrics.host.arch})
Vidéo           : ${DURATION_S}s @ ${FPS}fps, 1080p, ${FRAMES} frames
Concurrency     : ${metrics.config.concurrency}
-------------------------------------------------
Wall time       : ${r.wallSeconds}s
Temps / min vidéo: ${r.wallPerVideoMinuteSeconds}s   <-- métrique clé SLO
Facteur temps-réel: ${r.realtimeFactor}x ${r.realtimeFactor >= 1 ? "(plus rapide que RT)" : "(plus lent que RT)"}
FPS effectif    : ${r.effectiveFps}
Pic RSS         : ${r.peakRssMB ?? "n/a"} MB
-------------------------------------------------
Coût @ ${RATE}$/h : ${metrics.cost.usdPerVideoMinute}$ / min de vidéo
=================================================
metrics -> out/metrics.json
`);
