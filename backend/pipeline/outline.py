#!/usr/bin/env python3
"""
POC - Pipeline stage A (outline): plan -> ordered scenes with a chosen component.

Input  : the educational plan (output of generate_plan) passed as an argument.
Output : scenes as JSON on stdout: [{order, idea, type, composition}].

This is the GLOBAL view (single call): it both splits into scenes AND picks the
component (`type`) per scene, so overall rhythm holds (hook -> ... -> outro).
It does NOT write the narration or fill content — that's stage B (fill.py), done
per scene so a single scene can be re-generated in isolation (scoped-scene edit).

Usage: python3 outline.py plan.json > outline.json
"""

import json
import sys
from typing import Any

from layout_store import LAYOUT_IDS, catalog_for_prompt, choose_composition, is_valid_layout
from utils import call_mistral, print_json, read_json

SYSTEM_PROMPT = """You are an art director of educational motion-design videos.
From an educational PLAN, produce the ORDERED list of SCENES of the video, and for
EACH scene choose the most suitable component (`type`) FROM THE CATALOG below.

Component catalog (CLOSED list):
{catalog}

Strict rules:
- `type` MUST be an id from the catalog above. NEVER invent one.
- One scene = one single idea. A plan section may yield one or several scenes.
- Rhythm: first scene usually "title", last scene (summary/conclusion) usually "outro".
- Vary the components to keep it lively (avoid repeating the same type back to back).
{kicker_rule}
You reply EXCLUSIVELY with a valid JSON object, no text or Markdown around it.
Expected schema:
{{
  "scenes": [
    {{
      "order": 1,
      "idea": "string - the single idea of the scene",
      "type": "string - a component id from the catalog"{kicker_field}
    }}
  ]
}}"""

# Section kicker (intertitle) styles. Consistent across the WHOLE video.
KICKER_THEMATIC_RULE = (
    '- For every "section" scene, also give a short thematic `kicker` (2-3 words, e.g. "Les bases",\n'  # noqa: E501
    '  "En pratique", "A retenir"). ALL section kickers MUST share the SAME register across the\n'
    "  whole video. Do NOT number them.\n"
)
KICKER_NUMBERED_LABEL = "Partie"  # numbered style: "Partie 1", "Partie 2", ...


def build_outline(plan: dict[str, Any], kicker_style: str = "thematic") -> dict[str, Any]:
    """Stage A: split the plan into scenes and choose a component per scene.

    `kicker_style` controls section intertitles, CONSISTENT across the whole video:
      - "thematic": the LLM produces same-register thematic labels (global view),
      - "numbered": code-generated "Partie 1, 2, 3..." (deterministic),
      - "none": no kicker.
    """
    thematic = kicker_style == "thematic"
    system_prompt = SYSTEM_PROMPT.format(
        catalog=catalog_for_prompt(),
        kicker_rule=KICKER_THEMATIC_RULE if thematic else "",
        kicker_field=',\n      "kicker": "string - ONLY for type \'section\': short thematic label"'
        if thematic
        else "",
    )
    user_content = "Here is the educational plan:\n\n" + json.dumps(
        plan, ensure_ascii=False, indent=2
    )
    response = call_mistral(system_prompt, user_content, temperature=0.3)

    scenes = response.get("scenes", [])
    if not scenes:
        sys.exit("Error: the model returned no scene.")

    # Validate type + assign composition (anti-repeat) + section kicker (consistent).
    out = []
    prev_composition = None
    section_n = 0
    for i, scene in enumerate(scenes, start=1):
        layout_id = scene.get("type")
        if not is_valid_layout(layout_id):
            sys.exit(
                f"Error: type '{layout_id}' outside the catalog. Valid ids: {', '.join(LAYOUT_IDS)}."  # noqa: E501
            )
        composition = choose_composition(layout_id, prev_composition)
        prev_composition = composition
        scene_out = {
            "order": scene.get("order", i),
            "idea": scene.get("idea", ""),
            "type": layout_id,
            "composition": composition,
        }
        if layout_id == "section":
            section_n += 1
            if kicker_style == "numbered":
                scene_out["kicker"] = f"{KICKER_NUMBERED_LABEL} {section_n}"
            elif kicker_style == "none":
                scene_out["kicker"] = ""
            else:
                scene_out["kicker"] = scene.get("kicker", "")  # thematic, from LLM
        out.append(scene_out)

    return {"scenes": out}


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python3 outline.py plan.json [brand_kit.json] > outline.json")
    plan = read_json(sys.argv[1])
    brand_kit_path = sys.argv[2] if len(sys.argv) > 2 else "inputs/brand_kit.json"
    kicker_style = read_json(brand_kit_path).get("kicker_style", "thematic")
    print_json(build_outline(plan, kicker_style))


if __name__ == "__main__":
    main()
