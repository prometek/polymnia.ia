#!/usr/bin/env python3
"""
Scoped-scene edit (US-06), no UI: re-generate ONE scene in isolation.

Re-runs the per-scene tool call with an edit instruction, then re-TTS + re-aligns
just that scene. All other scenes are untouched (this is why fill is per-scene).

Run from the backend/ directory.

List the scenes (to find the order# to edit):
       python3 pipeline/edit_scene.py --list

Edit one scene (regenerates it only) and re-render the video:
       python3 pipeline/edit_scene.py <order> "<instruction>" --render
       e.g. python3 pipeline/edit_scene.py 7 "remplace l'icone par une ancre" --render

Without --render it only updates out/scene_audio.json (pack + render yourself later).
Optional positional overrides: [scene_audio.json] [brand_kit.json].
"""

import json
import os
import subprocess
import sys
from typing import Any

from fill import fill_scene
from tts import voiceover_scene
from utils import collect_asset_refs, read_json


def _set_path(obj: Any, path: str, value: str) -> None:
    """Set a value by dotted path, e.g. 'items.0.text' (list indices supported)."""
    keys = path.split(".")
    for k in keys[:-1]:
        obj = obj[int(k)] if isinstance(obj, list) else obj[k]
    last = keys[-1]
    if isinstance(obj, list):
        obj[int(last)] = value
    else:
        obj[last] = value


def _swap_icons(node: Any, new_ref: str) -> None:
    """Replace every icon_ref in the props tree with new_ref."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "icon_ref":
                node[k] = new_ref
            else:
                _swap_icons(v, new_ref)
    elif isinstance(node, list):
        for el in node:
            _swap_icons(el, new_ref)


def _preview(scene: dict[str, Any]) -> str:
    p = scene.get("props", {})
    items = p.get("items") or p.get("steps") or []
    return (
        p.get("title")
        or p.get("term")
        or p.get("text")
        or p.get("kicker")
        or p.get("cta")
        or (items[0].get("label") or items[0].get("text") if items else "")
        or ""
    )


def list_scenes(doc_path: str) -> None:
    for s in read_json(doc_path).get("scenes", []):
        print(f"#{s.get('order'):>2}  {s.get('type'):11}  {str(_preview(s))[:55]}")


def render(doc_path: str, kit_path: str) -> None:
    """Re-pack + render the whole video (single MP4) from the updated scenes."""
    here = os.path.dirname(os.path.abspath(__file__))  # backend/pipeline
    backend = os.path.dirname(here)
    subprocess.run(
        [sys.executable, os.path.join(here, "pack_render.py"), doc_path, "", kit_path],
        cwd=backend,
        check=True,
    )
    subprocess.run(
        "npx remotion render src/index.ts Polymnia out/polymnia.mp4 --props=./render-input.json",
        cwd=os.path.normpath(os.path.join(backend, "..", "render-motor")),
        shell=True,
        check=True,
    )


def preview(scene: dict[str, Any], brand_kit: dict[str, Any]) -> None:
    """Render ONLY this scene to a short clip (fast iteration, no full re-render)."""
    import shutil

    from pack_render import (
        PUBLIC_AUDIO,
        RENDER_DIR,
        copy_logo,
        emoji_map,
        render_background,
        render_cosmetic,
        resolve_content,
    )

    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    timing = scene.get("timing", {})
    src = timing.get("audio_path", "")
    if src and not os.path.isabs(src):
        src = os.path.join(backend, src)
    os.makedirs(PUBLIC_AUDIO, exist_ok=True)
    name = os.path.basename(src)
    shutil.copyfile(src, os.path.join(PUBLIC_AUDIO, name))

    cosmetic = render_cosmetic(brand_kit)
    cosmetic["background"] = render_background(brand_kit)
    one = {
        "styleId": brand_kit.get("visualStyle", "tech"),
        "cosmetic": cosmetic,
        "logo": copy_logo(brand_kit),
        "scenes": [
            {
                "type": scene["type"],
                "composition": scene.get("composition", "centered"),
                "durationS": timing.get("duration_s", 0),
                "audio": f"audio/{name}",
                "props": resolve_content(scene.get("props", {}), emoji_map(brand_kit)),
            }
        ],
    }
    props_path = os.path.join(RENDER_DIR, "preview-input.json")
    with open(props_path, "w", encoding="utf-8") as f:
        json.dump(one, f, ensure_ascii=False, indent=2)

    out = f"out/preview-{scene.get('order')}.mp4"
    subprocess.run(
        f"npx remotion render src/index.ts Polymnia {out} --props=./preview-input.json",
        cwd=RENDER_DIR,
        shell=True,
        check=True,
    )
    print(f"-> scene preview: render-motor/{out}", file=sys.stderr)


def main() -> None:
    args = sys.argv[1:]
    do_render = "--render" in args
    do_preview = "--preview" in args
    args = [a for a in args if a not in ("--render", "--preview")]

    # Pull --set / --swap (no-LLM direct edits) out of the args.
    sets, swap, rest, i = [], None, [], 0
    while i < len(args):
        if args[i] == "--set":
            sets.append(args[i + 1])
            i += 2
        elif args[i] == "--swap":
            swap = args[i + 1]
            i += 2
        else:
            rest.append(args[i])
            i += 1
    args = rest

    if not args or args[0] == "--list":
        list_scenes("out/scene_audio.json")
        return

    order = int(args[0])
    instruction = args[1] if len(args) > 1 else None
    doc_path = args[2] if len(args) > 2 else "out/scene_audio.json"
    kit_path = args[3] if len(args) > 3 else "inputs/brand_kit.json"

    doc = read_json(doc_path)
    brand_kit = read_json(kit_path)
    scenes = doc.get("scenes", [])

    idx = next((i for i, s in enumerate(scenes) if s.get("order") == order), None)
    if idx is None:
        sys.exit(f"Error: no scene with order={order}.")

    target = scenes[idx]
    audio_dir = os.path.dirname(target.get("timing", {}).get("audio_path", "")) or "out/audio"

    if sets or swap:
        # Direct edit, no LLM (US-04 text, US-05 asset swap).
        props = target.get("props", {})
        for s in sets:
            path, _, value = s.partition("=")
            _set_path(props, path.strip(), value)
        if swap:
            valid = {a.get("id") for a in brand_kit.get("assets", []) if a.get("type") == "icon"}
            if swap not in valid:
                sys.exit(f"Error: unknown icon '{swap}'. Valid: {', '.join(sorted(valid))}.")
            _swap_icons(props, swap)
        target["asset_refs"] = collect_asset_refs(props, brand_kit)
        # On-screen text/asset edits don't change audio; re-TTS only if narration changed.
        if any(s.split("=")[0].strip().startswith("narration") for s in sets):
            scenes[idx] = voiceover_scene(target, audio_dir)
            doc["total_duration_s"] = round(sum(s["timing"]["duration_s"] for s in scenes), 3)
        print(f"scene {order} edited (no LLM): set={len(sets)} swap={swap or '-'}", file=sys.stderr)
    elif instruction:
        # AI scoped edit (US-06): regenerate the scene from a prompt.
        print(f"editing scene {order} ({target.get('type')}) ...", file=sys.stderr)
        edited = fill_scene(target, brand_kit, instruction=instruction)
        edited = voiceover_scene(edited, audio_dir)  # re-TTS + re-align this scene only
        scenes[idx] = edited
        doc["total_duration_s"] = round(sum(s["timing"]["duration_s"] for s in scenes), 3)
        print(
            f"scene {order} regenerated. duration={edited['timing']['duration_s']}s",
            file=sys.stderr,
        )
    else:
        sys.exit(
            'Usage: edit_scene.py <order> "<instruction>" | <order> --set path=val | <order> --swap icon-id  [--render]'  # noqa: E501
        )

    with open(doc_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    if do_preview:
        preview(scenes[idx], brand_kit)  # fast: render only this scene
    if do_render:
        render(doc_path, kit_path)  # full video
        print("-> video re-rendered: ../render-motor/out/polymnia.mp4", file=sys.stderr)
    if not do_preview and not do_render:
        print(
            f"-> {doc_path} updated (other scenes untouched). Add --preview (1 scene) or --render (full).",  # noqa: E501
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
