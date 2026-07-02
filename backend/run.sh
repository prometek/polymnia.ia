#!/usr/bin/env bash
# Full POC pipeline: input.txt -> MP4.
# Chains the 5 AI steps + packing + Remotion render.
#
# Usage: ./run.sh [input.txt] [styleId] [brand_kit.json]
#   input.txt      source text          (default: inputs/input.txt)
#   styleId        render visual style  (default: <kit visualStyle> ; whiteboard|kawaii|aquarelle|retro|tech)
#   brand_kit.json brand kit to apply   (default: inputs/brand_kit.json)
#
# TTS variants: TTS_PROVIDER=f5 ./run.sh   (local clone) otherwise Voxtral API.

set -euo pipefail

cd "$(dirname "$0")"

INPUT="${1:-inputs/input.txt}"
STYLE="${2:-}"                    # empty => the brand kit visualStyle decides
KIT="${3:-inputs/brand_kit.json}"

PY=".venv/bin/python"
OUT="out"
RENDER_DIR="../render-motor"
MP4="$RENDER_DIR/out/polymnia.mp4"

[ -f "$PY" ] || { echo "Error: venv not found ($PY)."; exit 1; }
[ -f "$INPUT" ] || { echo "Error: input file not found ($INPUT)."; exit 1; }
mkdir -p "$OUT/audio"

echo "==> 1/5  Educational plan        ($INPUT)"
"$PY" pipeline/generate_plan.py "$INPUT" > "$OUT/plan.json"

echo "==> 2/5  Outline (scenes + components, global rhythm)"
"$PY" pipeline/outline.py "$OUT/plan.json" "$KIT" > "$OUT/outline.json"

echo "==> 3/5  Fill (one tool call per scene)   (kit: $KIT)"
"$PY" pipeline/fill.py "$OUT/outline.json" "$KIT" > "$OUT/scenes_full.json"

echo "==> 4/5  Voiceover + timing (TTS + forced alignment)"
"$PY" pipeline/tts.py "$OUT/scenes_full.json" "$OUT/audio" > "$OUT/scene_audio.json"

echo "==> 5/5  Render packing          (style: ${STYLE:-<kit>})"
"$PY" pipeline/pack_render.py "$OUT/scene_audio.json" "$STYLE" "$KIT"

echo "==> Remotion render -> $MP4"
( cd "$RENDER_DIR" && npx remotion render src/index.ts Polymnia out/polymnia.mp4 --props=./render-input.json )

echo ""
echo "OK. Video: $(cd "$RENDER_DIR" && pwd)/out/polymnia.mp4"
