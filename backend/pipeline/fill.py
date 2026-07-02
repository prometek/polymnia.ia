#!/usr/bin/env python3
"""
POC - Pipeline stage B (fill): one tool call PER scene -> {type, props}.

Input  : the outline (output of outline.py) + the brand kit.
Output : the same scenes, each enriched with `props` (the tool-call arguments:
         content + narration + per-element narration_cue) and `asset_refs`.

Per-scene call (isolated) -> a single scene can be re-generated for a scoped edit
(US-06) without touching the others. The chosen component's tool is forced
(tool_choice='any' on a single tool), so the model fills exactly that schema.

Usage: python3 fill.py outline.json [brand_kit.json] > scenes_full.json
"""

import json
import sys

from layout_store import build_tool, is_valid_layout
from utils import available_assets, call_tool, collect_asset_refs, print_json, read_json

SYSTEM_PROMPT = """You are a content designer for educational motion-design videos.
Call the provided tool (= the scene's component) and fill its parameters with real
content, faithful to the scene idea, in the brand kit's tone.

Rules:
- Write a natural spoken `narration` for the scene (1 to 4 sentences).
- On-screen text is short and COMPLEMENTS the narration (it does not duplicate it).
- For list-like components, each element's `narration_cue` MUST appear VERBATIM in the
  narration, in order (the backend syncs each element's appearance to it).
- For icon references, choose an id among those proposed (enum)."""


def fill_scene(scene: dict, brand_kit: dict, instruction: str | None = None) -> dict:
    """Stage B on ONE scene: call the component tool -> props + asset_refs.

    If `instruction` is given (scoped-scene edit, US-06), the current props and the
    edit request are added to the context so the model regenerates this scene only,
    applying the change and keeping the rest faithful.
    """
    type_id = scene.get("type")
    if not is_valid_layout(type_id):
        sys.exit(f"Error: scene order={scene.get('order')} has an invalid type '{type_id}'.")

    asset_ids = [a.get("id") for a in brand_kit.get("assets", []) if a.get("type") == "icon"]
    tool = build_tool(type_id, asset_ids)

    context = {
        "scene": {"idea": scene.get("idea"), "type": type_id},
        "brand_kit": {
            "voice": brand_kit.get("voice", {}),
            "available_assets": available_assets(brand_kit),
        },
    }
    if instruction:
        context["current_props"] = scene.get("props", {})
        context["edit_request"] = instruction
        intro = "Edit this scene by calling the tool: apply edit_request to current_props, keep the rest faithful.\n\n"  # noqa: E501
    else:
        intro = "Fill this scene by calling the tool.\n\n"

    user_content = intro + json.dumps(context, ensure_ascii=False, indent=2)
    props = call_tool(SYSTEM_PROMPT, user_content, tool, temperature=0.4)

    # The section kicker is a GLOBAL concern (consistent style across the video):
    # the outline owns it -> override whatever the isolated fill produced ("" = none).
    if type_id == "section" and "kicker" in scene:
        props["kicker"] = scene["kicker"]

    return {**scene, "props": props, "asset_refs": collect_asset_refs(props, brand_kit)}


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python3 fill.py outline.json [brand_kit.json] > scenes_full.json")

    outline = read_json(sys.argv[1])
    brand_kit_path = sys.argv[2] if len(sys.argv) > 2 else "inputs/brand_kit.json"
    brand_kit = read_json(brand_kit_path)

    scenes = outline.get("scenes", [])
    if not scenes:
        sys.exit("Error: no scene found in the outline.")

    filled = [fill_scene(scene, brand_kit) for scene in scenes]
    print_json({"brand_kit_id": brand_kit.get("id"), "scenes": filled})


if __name__ == "__main__":
    main()
