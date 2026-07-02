#!/usr/bin/env python3
"""Shared helpers for the POC pipeline (Mistral API calls + IO)."""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---------------------------------------------------------

API_URL = "https://api.mistral.ai/v1/chat/completions"

# Structuring/reasoning step: Medium or Large.
# Switch to "mistral-large-latest" if the output is too poor.
MODEL = "mistral-medium-latest"

# API key: export it before running -> export MISTRAL_API_KEY="..."
API_KEY = os.getenv("MISTRAL_API_KEY")

# --- TTS configuration (Voxtral) -------------------------------------------

TTS_URL = "https://api.mistral.ai/v1/audio/speech"
TTS_MODEL = "voxtral-mini-tts-2603"
# Mistral preset voice (slug). Overridable via env MISTRAL_TTS_VOICE.
# NB: presets are English only (en_us/en_gb). Voxtral reads FR despite an English
# timbre; for real FR -> clone a voice (POST /v1/audio/voices).
TTS_VOICE = os.getenv("MISTRAL_TTS_VOICE", "en_paul_neutral")


def call_mistral(system_prompt: str, user_content: str, temperature: float = 0.3) -> dict:
    """Generic Mistral API call with forced JSON output.

    temperature: low = stable, reproducible structure.
    """
    if not API_KEY:
        sys.exit("Error: environment variable MISTRAL_API_KEY is not set.")

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        # Force a valid JSON response on the API side (equivalent to 'JSON mode')
        "response_format": {"type": "json_object"},
        "temperature": temperature,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        sys.exit(f"HTTP error {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Network error: {e.reason}")

    # The model response is a JSON string -> parse it
    content = body["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        sys.exit(f"The model did not return valid JSON:\n{content}")


def call_tool(system_prompt: str, user_content: str, tool: dict, temperature: float = 0.4) -> dict:
    """Mistral function calling: force a call to `tool` and return its arguments.

    We pass a single tool + tool_choice='any' -> the model MUST call it.
    Arguments come back as a JSON string (standard) -> we parse them.
    """
    if not API_KEY:
        sys.exit("Error: environment variable MISTRAL_API_KEY is not set.")

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "tools": [tool],
        "tool_choice": "any",  # force a tool call (here the only one provided)
        "temperature": temperature,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        sys.exit(f"HTTP error {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Network error: {e.reason}")

    message = body["choices"][0]["message"]
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        sys.exit(f"The model did not call the tool:\n{json.dumps(message)[:500]}")

    # arguments = JSON string (standard function calling) -> parse
    arguments = tool_calls[0]["function"]["arguments"]
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        sys.exit(f"Tool arguments are not parsable:\n{arguments}")


def available_assets(brand_kit: dict) -> list[dict]:
    """Compact list of referenceable assets (id + usage hint) for prompt context."""
    return [
        {"id": a.get("id"), "type": a.get("type"), "usage": a.get("usage", "")}
        for a in brand_kit.get("assets", [])
    ]


def collect_asset_refs(content: dict, brand_kit: dict) -> list[str]:
    """Extract and validate asset ids referenced in a content/props tree.

    Convention: any key ending with "_ref" carries an asset id; it must exist in
    the kit (ADR-04/05 guardrail).
    """
    valid_ids = {a.get("id") for a in brand_kit.get("assets", [])}
    refs: list[str] = []

    def visit(node):
        if isinstance(node, dict):
            for key, val in node.items():
                if key.endswith("_ref") and isinstance(val, str):
                    if val not in valid_ids:
                        sys.exit(
                            f"Error: asset_ref '{val}' absent from the brand kit. "
                            f"Valid ids: {', '.join(sorted(valid_ids))}."
                        )
                    if val not in refs:
                        refs.append(val)
                else:
                    visit(val)
        elif isinstance(node, list):
            for el in node:
                visit(el)

    visit(content)
    return refs


def read_text(path: str) -> str:
    """Read a UTF-8 text file."""
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_json(path: str) -> dict:
    """Read a UTF-8 JSON file."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def print_json(obj: dict) -> None:
    """Print an object as indented JSON (chainable via stdout)."""
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def synthesize(text: str, voice_id: str = TTS_VOICE, response_format: str = "wav") -> bytes:
    """Call the Mistral TTS API (Voxtral) and return the decoded audio bytes.

    The API response is JSON {"audio_data": base64} -> we decode to raw bytes.
    """
    if not API_KEY:
        sys.exit("Error: environment variable MISTRAL_API_KEY is not set.")

    payload = {
        "model": TTS_MODEL,
        "input": text,
        "voice_id": voice_id,
        "response_format": response_format,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        TTS_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        sys.exit(f"HTTP error {e.code} (TTS): {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Network error (TTS): {e.reason}")

    audio_b64 = body.get("audio_data")
    if not audio_b64:
        sys.exit(f"TTS response without 'audio_data':\n{json.dumps(body)[:500]}")

    return base64.b64decode(audio_b64)
