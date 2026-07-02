#!/usr/bin/env python3
"""
POC - Backend -> render bridge: prepare the Remotion inputs.

Input  : scene_audio.json (step 5 output) + brand_kit.json.
Effects: - copy the scene .wav files into render-motor/public/audio/,
         - resolve each icon_ref (asset id) to an emoji via the brand kit,
         - write render-motor/render-input.json (props of the Polymnia composition).

This is the "packing" step: the scene stays thin in storage (asset_refs = ids),
we only resolve assets to concrete values at render time.

Usage: python3 pack_render.py scene_audio.json [styleId] [brand_kit.json]
       default styleId: "tech" (whiteboard|kawaii|aquarelle|retro|tech)
"""

import json
import os
import shutil
import sys
from typing import Any

from utils import read_json

# Backend root (the script lives in backend/pipeline/) and render repo (sibling).
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENDER_DIR = os.path.normpath(os.path.join(BACKEND, "..", "render-motor"))
PUBLIC = os.path.join(RENDER_DIR, "public")
PUBLIC_AUDIO = os.path.join(PUBLIC, "audio")
RENDER_INPUT = os.path.join(RENDER_DIR, "render-input.json")

VALID_STYLES = {"whiteboard", "kawaii", "aquarelle", "retro", "tech"}


def emoji_map(brand_kit: dict[str, Any]) -> dict[str, Any]:
    """asset id -> emoji (for assets of type icon)."""
    return {
        a["id"]: a.get("emoji", "") for a in brand_kit.get("assets", []) if a.get("type") == "icon"
    }


def render_cosmetic(brand_kit: dict[str, Any]) -> dict[str, Any]:
    """Map the brand kit cosmetic to the override expected by the render.

    The visual style keeps its treatment/decor/motion; only the kit's colors +
    fonts apply on top (ADR-10/11).
    """
    cosmo = brand_kit.get("cosmetic", {})
    pal = cosmo.get("palette", {})
    named = cosmo.get("named_palette", {})
    muted = named.get("muted", {}).get("hex", "#8AA4BE")
    fonts = cosmo.get("fonts", {})

    return {
        "palette": {
            "bg": pal.get("bg0"),
            "text": pal.get("text"),
            "accent": pal.get("accent"),
            "accent2": pal.get("bg1"),
            "muted": muted,
        },
        "fontDisplay": fonts.get("display", {}).get("family"),
        "fontBody": fonts.get("body", {}).get("family"),
        "uppercase": False,
    }


def render_background(brand_kit: dict[str, Any]) -> dict[str, Any] | None:
    """Prepare the kit background for the render. Copy the image if needed (baked asset).

    type: 'image' (file copied into public), 'gradient'/'solid' (derived from palette),
    'theme' or absent (the visual style's procedural decor).
    """
    bg = brand_kit.get("cosmetic", {}).get("background")
    if not bg:
        return None

    out = {"type": bg.get("type", "theme"), "overlayDecor": bg.get("overlayDecor", False)}

    if bg["type"] == "image":
        src = os.path.join(BACKEND, bg.get("file", ""))
        if not os.path.exists(src):
            sys.exit(f"Background not found: {src}")
        name = os.path.basename(src)
        shutil.copyfile(src, os.path.join(PUBLIC, name))
        out["value"] = name  # resolved by staticFile() on the Remotion side

    return out


def copy_logo(brand_kit: dict[str, Any]) -> str | None:
    """Copy the kit's primary logo into public/ and return its name (staticFile).

    Baked asset (ADR-11): the file is fixed, and so are its colors.
    """
    logos = [a for a in brand_kit.get("assets", []) if a.get("type") == "logo"]
    primary = next((a for a in logos if a.get("primary")), logos[0] if logos else None)
    if not primary or not primary.get("file"):
        return None

    src = os.path.join(BACKEND, primary["file"])
    if not os.path.exists(src):
        sys.exit(f"Logo not found: {src}")

    name = os.path.basename(src)
    shutil.copyfile(src, os.path.join(PUBLIC, name))
    return name


def resolve_content(content_data: dict[str, Any], emojis: dict[str, Any]) -> dict[str, Any]:
    """Resolve the items' icon_ref to an emoji (icon) for the render."""
    cd: dict[str, Any] = json.loads(json.dumps(content_data))  # deep copy
    for item in cd.get("items", []) or []:
        ref = item.get("icon_ref")
        if ref:
            item["icon"] = emojis.get(ref, "")
    # 'image' layout: top-level icon_ref -> glyph (if no emoji already provided).
    if cd.get("icon_ref") and not cd.get("glyph"):
        cd["glyph"] = emojis.get(cd["icon_ref"], "")
    return cd


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python3 pack_render.py scene_audio.json [styleId] [brand_kit.json]")

    scenes_doc = read_json(sys.argv[1])
    brand_kit_path = sys.argv[3] if len(sys.argv) > 3 else "inputs/brand_kit.json"
    brand_kit = read_json(brand_kit_path)

    # styleId: CLI argument otherwise the kit's visualStyle (default "tech").
    style_cli = sys.argv[2] if len(sys.argv) > 2 else None
    style_id = style_cli or brand_kit.get("visualStyle") or "tech"

    if style_id not in VALID_STYLES:
        sys.exit(f"Invalid styleId '{style_id}'. Valid: {', '.join(sorted(VALID_STYLES))}.")

    emojis = emoji_map(brand_kit)

    os.makedirs(PUBLIC_AUDIO, exist_ok=True)
    logo = copy_logo(brand_kit)
    cosmetic = render_cosmetic(brand_kit)
    cosmetic["background"] = render_background(brand_kit)

    out_scenes = []
    for scene in scenes_doc.get("scenes", []):
        timing = scene.get("timing", {})
        src_audio = timing.get("audio_path")
        if src_audio and not os.path.isabs(src_audio) and not os.path.exists(src_audio):
            src_audio = os.path.join(BACKEND, src_audio)  # robust outside backend CWD
        if not src_audio or not os.path.exists(src_audio):
            sys.exit(
                f"Missing audio for scene order={scene.get('order')}: {timing.get('audio_path')}"
            )

        name = os.path.basename(src_audio)
        shutil.copyfile(src_audio, os.path.join(PUBLIC_AUDIO, name))

        out_scenes.append(
            {
                "type": scene["type"],
                "composition": scene.get("composition", "centered"),
                "durationS": timing.get("duration_s", 0),
                "audio": f"audio/{name}",  # resolved by staticFile() on the Remotion side
                "props": resolve_content(scene.get("props", {}), emojis),
            }
        )

    props = {
        "styleId": style_id,
        "cosmetic": cosmetic,
        "logo": logo,
        "scenes": out_scenes,
    }
    with open(RENDER_INPUT, "w", encoding="utf-8") as f:
        json.dump(props, f, ensure_ascii=False, indent=2)

    total = round(sum(s["durationS"] for s in out_scenes), 2)
    print(f"OK: {len(out_scenes)} scenes, style='{style_id}', logo={logo or '-'}, total={total}s")
    print(f"  -> {RENDER_INPUT}")
    print(f"  -> audio copied to {PUBLIC_AUDIO}")


if __name__ == "__main__":
    main()
