#!/usr/bin/env python3
"""
Clone a voice via the Mistral API (Voxtral) from an audio sample.

POST /v1/audio/voices  (JSON, sample_audio as base64).
Returns the id of the created voice, to be used afterwards as voice_id for TTS.

Sample advice: clean audio (low noise), a single speaker, ~10-30 s ideal
(2-3 s minimum). WAV or MP3.

Usage: python3 clone_voice.py my_sample.wav "My voice" [fr]
       -> prints the id; put it in .env:  MISTRAL_TTS_VOICE=<id>
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, cast

from utils import API_KEY

VOICES_URL = "https://api.mistral.ai/v1/audio/voices"


def clone_voice(audio_path: str, name: str, languages: list[str]) -> dict[str, Any]:
    if not API_KEY:
        sys.exit("Error: MISTRAL_API_KEY is not set.")
    if not os.path.exists(audio_path):
        sys.exit(f"Sample not found: {audio_path}")

    with open(audio_path, "rb") as f:
        sample_b64 = base64.b64encode(f.read()).decode("ascii")

    payload = {
        "name": name,
        "sample_audio": sample_b64,
        "sample_filename": os.path.basename(audio_path),
        "languages": languages,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        VOICES_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return cast(dict[str, Any], json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        sys.exit(f"HTTP error {e.code} (clone voice): {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Network error (clone voice): {e.reason}")


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit('Usage: python3 clone_voice.py sample.wav "My voice" [fr]')

    path = sys.argv[1]
    name = sys.argv[2]
    languages = [sys.argv[3]] if len(sys.argv) > 3 else ["fr"]

    voice = clone_voice(path, name, languages)
    vid = voice.get("id")
    print(json.dumps(voice, ensure_ascii=False, indent=2))
    print()
    print(f"Voice created. id = {vid}")
    print(f"-> Add to backend/.env:  MISTRAL_TTS_VOICE={vid}")


if __name__ == "__main__":
    main()
