// Génère un audio stub (proxy de voix off) de durée fixe.
// On mesure le coût de RENDU pur ; le TTS est un axe de coût mesuré à part.
import { execFileSync } from "node:child_process";
import { mkdirSync, existsSync } from "node:fs";

const DURATION_S = Number(process.env.DURATION_S ?? 60);
const OUT = "public/audio.wav";

mkdirSync("public", { recursive: true });

// Tonalité douce 220Hz à faible volume -> piste audio réelle à muxer, sans contenu.
execFileSync(
  "ffmpeg",
  [
    "-y",
    "-f", "lavfi",
    "-i", `sine=frequency=220:duration=${DURATION_S}`,
    "-af", "volume=0.05",
    "-ar", "48000",
    "-ac", "2",
    OUT,
  ],
  { stdio: "inherit" },
);

if (!existsSync(OUT)) throw new Error("audio gen failed");
console.log(`audio stub -> ${OUT} (${DURATION_S}s)`);
