#!/usr/bin/env python3
"""
POC - Pipeline step 5: voiceover (TTS) + timing (ADR-08).

Input  : the filled scenes (output of step 4).
Output : the same scenes as JSON on stdout, each enriched with a `timing` block
         (audio file path + real duration). The .wav files are written to out/audio/.

TIMING is driven by the voiceover (ADR-08): we first synthesize the audio of each
scene's script, then its REAL DURATION drives the scene duration (and later the
animation timing). We only know the timing after the audio.

WAV chosen on purpose: its duration is measured with the pure stdlib (`wave` module),
without ffprobe.

Usage: python3 tts.py scene_full.json [out/audio] > scene_audio.json
"""

import contextlib
import os
import sys
import wave
from typing import Any

from layout_store import CUED_LAYOUTS
from utils import TTS_MODEL, TTS_VOICE, print_json, read_json, synthesize

# TTS provider: "mistral" (Voxtral API) or "f5" (local F5-TTS clone, MPS).
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "mistral")

# Must match render-motor/src/PolymniaVideo.tsx.
FPS = 60
LEAD = 14


def _synthesize(text: str) -> bytes:
    """Route to the chosen TTS provider. Returns WAV bytes."""
    if TTS_PROVIDER == "f5":
        from tts_f5 import synthesize_f5

        return synthesize_f5(text)
    return synthesize(text, response_format="wav")


def _voice_info() -> dict[str, Any]:
    """Voice metadata depending on the provider (for the timing block)."""
    if TTS_PROVIDER == "f5":
        from tts_f5 import F5_MODEL, F5_REF_AUDIO

        return {
            "provider": "f5",
            "voice_id": f"clone:{os.path.basename(F5_REF_AUDIO)}",
            "model": F5_MODEL,
        }
    return {"provider": "mistral", "voice_id": TTS_VOICE, "model": TTS_MODEL}


def wav_duration(path: str) -> float:
    """WAV file duration in seconds (frames / framerate)."""
    with contextlib.closing(wave.open(path, "rb")) as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return round(frames / float(rate), 3) if rate else 0.0


def voiceover_scene(scene: dict[str, Any], audio_dir: str) -> dict[str, Any]:
    """Step 5 on ONE scene: synthesize the narration, measure duration, sync cues.

    Reads the script from props.narration (tool-call shape); for cued layouts
    (steps/bullets), forced-aligns the audio and injects startFrame per element.
    """
    props = scene.get("props", {})
    script = (props.get("narration") or scene.get("voiceover") or "").strip()
    if not script:
        sys.exit(f"Error: scene order={scene.get('order')} without a narration.")

    audio = _synthesize(script)

    path = os.path.join(audio_dir, f"scene-{scene.get('order')}.wav")
    with open(path, "wb") as f:
        f.write(audio)

    duration = wav_duration(path)

    # Narration-synced reveal for list-like components (startFrame per element).
    if scene.get("type") in CUED_LAYOUTS:
        from align import word_timestamps
        from timing import attach_start_frames

        words = word_timestamps(path, props.get("narration", ""))
        attach_start_frames(props, duration, FPS, LEAD, words=words)

    return {
        **scene,
        "timing": {
            "audio_path": path,
            "duration_s": duration,
            **_voice_info(),
        },
    }


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python3 tts.py scene_full.json [out/audio] > scene_audio.json")

    scenes = read_json(sys.argv[1])
    audio_dir = sys.argv[2] if len(sys.argv) > 2 else "out/audio"
    os.makedirs(audio_dir, exist_ok=True)

    scene_list = scenes.get("scenes", [])
    if not scene_list:
        sys.exit("Error: no scene found in the input (key 'scenes').")

    audio_scenes = [voiceover_scene(scene, audio_dir) for scene in scene_list]
    total = round(sum(s["timing"]["duration_s"] for s in audio_scenes), 3)

    print_json(
        {
            **{k: v for k, v in scenes.items() if k != "scenes"},
            "total_duration_s": total,
            "scenes": audio_scenes,
        }
    )


if __name__ == "__main__":
    main()
