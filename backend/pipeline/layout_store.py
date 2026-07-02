#!/usr/bin/env python3
"""
Layout catalog (style space) — backend POC version.

Source of truth for RENDERING = render-motor/src/PolymniaVideo.tsx (SceneByLayout +
PLayoutId type). This module is its MIRROR on the AI-pipeline side: it serves step 3
(layout choice, ADR-09) where the AI SELECTS one layout per scene from a closed list
— it never invents one (ADR-04/09).

The `id`s must stay STRICTLY identical to PLayoutId on the render-motor side
(title, bullets, diagram, definition, comparison, steps, stat, chart, section,
statement, image, outro), otherwise the render worker cannot resolve the scene.

For each layout we add decision-helper metadata (description, when_to_use,
content_types) absent from the TS side: these only guide the LLM, not the render.
"""

import copy
from typing import Any, TypedDict


class Layout(TypedDict):
    id: str
    name: str
    slots: list[str]  # content fields to fill (step 4)
    description: str  # what the layout shows
    when_to_use: str  # selection heuristic for the LLM
    content_types: list[str]  # `content_type` values (step 2) that match


# Catalog — mirror of the components in render-motor/src/PolymniaVideo.tsx.
LAYOUTS: list[Layout] = [
    {
        "id": "title",
        "name": "Intro title",
        "slots": ["title", "subtitle"],
        "description": "Hook screen: a big title and a subtitle. Sets the topic.",
        "when_to_use": "First scene, or any scene opening a part. A single strong idea, little text.",  # noqa: E501
        "content_types": ["title"],
    },
    {
        "id": "bullets",
        "name": "Bullet list + icons",
        "slots": ["items[]"],
        "description": "A list of short points, each with an icon. An idea broken into sub-points.",
        "when_to_use": "Simple enumeration without strong ordering. For ordered steps -> 'steps', for A vs B -> 'comparison'.",  # noqa: E501
        "content_types": ["list"],
    },
    {
        "id": "diagram",
        "name": "Box diagram",
        "slots": ["nodes[]"],
        "description": "Connected boxes (flow, relations, schema). Shows a structure or a process.",
        "when_to_use": "Relation/flow between elements (connected boxes). For numbers -> 'stat' or 'chart', for steps -> 'steps'.",  # noqa: E501
        "content_types": ["schema"],
    },
    {
        "id": "definition",
        "name": "Term + definition",
        "slots": ["term", "definition"],
        "description": "A highlighted term and its definition in one or two sentences.",
        "when_to_use": "Introduce/explain a precise word or concept.",
        "content_types": ["definition"],
    },
    {
        "id": "comparison",
        "name": "Two-column comparison",
        "slots": ["left", "right"],
        "description": "Two opposing columns (A vs B), each with a title and points.",
        "when_to_use": "Contrast two options, before/after, pros/cons.",
        "content_types": ["comparison"],
    },
    {
        "id": "steps",
        "name": "Numbered steps",
        "slots": ["steps"],
        "description": "An ordered sequence of numbered steps with progression.",
        "when_to_use": "Process, how-to, chronology.",
        "content_types": ["steps"],
    },
    {
        "id": "stat",
        "name": "Key figure",
        "slots": ["stats"],
        "description": "One to three big key figures with their label. Strong visual impact.",
        "when_to_use": "Highlight a striking number, a proportion, a measure.",
        "content_types": ["figure"],
    },
    {
        "id": "chart",
        "name": "Chart",
        "slots": ["chart"],
        "description": "A data chart: bars, line or pie.",
        "when_to_use": "Compare numeric values, show an evolution or a distribution.",
        "content_types": ["figure", "comparison"],
    },
    {
        "id": "section",
        "name": "Section card",
        "slots": ["kicker", "title"],
        "description": "Chapter/section card: a small label + a big part title.",
        "when_to_use": "Mark the move to a new part, break the rhythm between blocks.",
        "content_types": ["title"],
    },
    {
        "id": "statement",
        "name": "Statement",
        "slots": ["text"],
        "description": "A single strong sentence, full-bleed, oversized type. A keyword can be emphasized.",  # noqa: E501
        "when_to_use": "Drive home a strong idea, a punchline, a realization.",
        "content_types": ["definition", "conclusion"],
    },
    {
        "id": "image",
        "name": "Focal visual",
        "slots": ["glyph", "caption"],
        "description": "A large centered visual (emoji/icon) with a caption. Illustrative scene.",
        "when_to_use": "Illustrate an idea with a simple symbol, breathe between dense scenes.",
        "content_types": ["title", "definition"],
    },
    {
        "id": "outro",
        "name": "Outro / CTA",
        "slots": ["cta"],
        "description": "Closing screen: summary or call to action. Always full-bleed.",
        "when_to_use": "Last scene, summary or conclusion scene.",
        "content_types": ["conclusion"],
    },
]


# Index by id + set of valid ids (validates the LLM choice).
LAYOUTS_BY_ID: dict[str, Layout] = {ly["id"]: ly for ly in LAYOUTS}
LAYOUT_IDS: list[str] = [ly["id"] for ly in LAYOUTS]


def catalog_for_prompt() -> str:
    """Render the catalog as compact text to inject into the system prompt."""
    lines = []
    for ly in LAYOUTS:
        lines.append(
            f'- "{ly["id"]}" ({ly["name"]}): {ly["description"]} '
            f"Use when: {ly['when_to_use']} "
            f"Slots to fill next: {', '.join(ly['slots'])}."
        )
    return "\n".join(lines)


def is_valid_layout(layout_id: str) -> bool:
    """True if the id belongs to the catalog (ADR-09 guardrail)."""
    return layout_id in LAYOUTS_BY_ID


# --- COMPOSITION axis (placement) ------------------------------------------
# Composition (content placement) varies to break monotony: two scenes of the
# same layout no longer look alike. Chosen on the code side (deterministic +
# anti-repetition), not by the LLM. Each layout declares its valid compositions
# (some have only one natural placement).

COMPOSITIONS = ["centered", "left", "right", "full"]

COMPO_BY_LAYOUT: dict[str, list[str]] = {
    "title": ["centered", "left", "full"],
    "bullets": ["left", "right"],
    "diagram": ["centered"],
    "definition": ["centered", "left"],
    "comparison": ["centered"],
    "steps": ["left", "right"],
    "stat": ["centered", "full"],
    "chart": ["centered"],
    "section": ["centered", "full", "left"],
    "statement": ["centered", "full", "left"],
    "image": ["centered", "left", "right"],
    "outro": ["centered", "full"],
}


def compositions_for(layout_id: str) -> list[str]:
    """Valid compositions for a layout (default: centered)."""
    return COMPO_BY_LAYOUT.get(layout_id, ["centered"])


def choose_composition(layout_id: str, previous: str | None) -> str:
    """Pick a composition for the layout, avoiding repeating the previous one."""
    options = compositions_for(layout_id)
    for opt in options:
        if opt != previous:
            return opt
    return options[0]


# --- Tools (function calling) ----------------------------------------------
# Each layout is a TOOL whose parameters (JSON Schema) are the content slots. The
# LLM fills the args -> props (tool-call shape {type, props}). Asset references
# (icon_ref) are enums of the kit ids -> the model cannot invent an asset
# (provider-side validation, ADR-04/05).
#
# Every tool also gets a `narration` (the scene's spoken script). List-like layouts
# (steps, bullets) get a per-element `narration_cue` -> the backend maps it to a
# real `startFrame` via forced alignment (ADR-08), so elements pop in sync.

# Layouts whose elements appear progressively, synced to the narration.
# Maps layout id -> the array property whose items carry a narration_cue.
CUED_LAYOUTS = {"steps": "steps", "bullets": "items", "stat": "stats", "diagram": "nodes"}

PARAMS_SCHEMA: dict[str, dict[str, Any]] = {
    "title": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short, catchy title (max ~6 words)"},
            "subtitle": {"type": "string", "description": "One-line subtitle"},
        },
        "required": ["title"],
    },
    "bullets": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 3,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "A short point (one line)"},
                        "icon_ref": {"type": "string", "description": "id of a kit icon asset"},
                    },
                    "required": ["text"],
                },
            },
        },
        "required": ["items"],
    },
    "diagram": {
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "minItems": 2,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "short id (e.g. 'n1')"},
                        "label": {"type": "string", "description": "box text (short)"},
                    },
                    "required": ["id", "label"],
                },
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
                    "required": ["from", "to"],
                },
            },
        },
        "required": ["nodes"],
    },
    "definition": {
        "type": "object",
        "properties": {
            "term": {"type": "string", "description": "the defined word/concept (short)"},
            "definition": {"type": "string", "description": "its definition (1 to 2 sentences)"},
        },
        "required": ["term", "definition"],
    },
    "comparison": {
        "type": "object",
        "properties": {
            "left": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 4,
                    },
                },
                "required": ["title", "items"],
            },
            "right": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 4,
                    },
                },
                "required": ["title", "items"],
            },
        },
        "required": ["left", "right"],
    },
    "steps": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "minItems": 2,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "maxLength": 40,
                            "description": "short, punchy step title",
                        },
                        "description": {
                            "type": "string",
                            "maxLength": 120,
                            "description": "explanation shown under the title",
                        },
                        "tag": {
                            "type": "string",
                            "maxLength": 20,
                            "description": "optional short badge, e.g. 'Key step', 'Result'",
                        },
                    },
                    "required": ["label", "description"],
                },
            },
        },
        "required": ["steps"],
    },
    "stat": {
        "type": "object",
        "properties": {
            "stats": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "string",
                            "description": "the figure (e.g. '5 knots', '80%')",
                        },
                        "label": {"type": "string", "description": "what the figure represents"},
                    },
                    "required": ["value", "label"],
                },
            },
        },
        "required": ["stats"],
    },
    "chart": {
        "type": "object",
        "properties": {
            "chart_type": {"type": "string", "enum": ["bar", "line", "pie"]},
            "series": {
                "type": "array",
                "minItems": 2,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "value": {"type": "number"},
                    },
                    "required": ["label", "value"],
                },
            },
            "caption": {"type": "string", "description": "optional caption"},
        },
        "required": ["chart_type", "series"],
    },
    "section": {
        "type": "object",
        "properties": {
            "kicker": {"type": "string", "description": "small label (e.g. 'Part 2')"},
            "title": {"type": "string", "description": "section title (short)"},
        },
        "required": ["title"],
    },
    "statement": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "the punchy sentence (short)"},
            "emphasis": {
                "type": "string",
                "description": "word/group to emphasize within the sentence",
            },
        },
        "required": ["text"],
    },
    "image": {
        "type": "object",
        "properties": {
            "glyph": {"type": "string", "description": "an emoji to show large"},
            "icon_ref": {"type": "string", "description": "alternative: id of a kit icon asset"},
            "caption": {"type": "string", "description": "caption under the visual"},
        },
        "required": ["caption"],
    },
    "outro": {
        "type": "object",
        "properties": {
            "cta": {"type": "string", "description": "closing sentence / call to action"},
        },
        "required": ["cta"],
    },
}


def _inject_icon_enum(schema: dict[str, Any], asset_ids: list[str]) -> None:
    """Constrain any 'icon_ref' field of the schema to an enum of the kit asset ids."""
    if not isinstance(schema, dict):
        return
    props = schema.get("properties", {})
    for key, sub in props.items():
        if key == "icon_ref" and asset_ids:
            sub["enum"] = asset_ids
        _inject_icon_enum(sub, asset_ids)
    if "items" in schema:
        _inject_icon_enum(schema["items"], asset_ids)


def build_tool(layout_id: str, asset_ids: list[str] | None = None) -> dict[str, Any]:
    """Build the (function-calling) tool for a layout: name = layout_id,
    parameters = JSON Schema of the slots, icon_ref constrained to the kit assets."""
    layout = LAYOUTS_BY_ID.get(layout_id)
    if not layout:
        raise KeyError(f"unknown layout: {layout_id}")
    schema = copy.deepcopy(PARAMS_SCHEMA[layout_id])
    _inject_icon_enum(schema, asset_ids or [])

    # Every component is narrated: add the spoken script.
    schema["properties"]["narration"] = {
        "type": "string",
        "description": "Full spoken script for this scene. Must contain each narration_cue verbatim, in order.",  # noqa: E501
    }
    schema.setdefault("required", [])
    if "narration" not in schema["required"]:
        schema["required"].append("narration")

    # List-like layouts: per-element cue -> startFrame (narration-synced reveal).
    arr = CUED_LAYOUTS.get(layout_id)
    if arr:
        items = schema["properties"][arr]["items"]
        items["properties"]["narration_cue"] = {
            "type": "string",
            "description": "Word/phrase in narration that triggers this element. Verbatim, in order.",  # noqa: E501
        }
        items.setdefault("required", [])
        if "narration_cue" not in items["required"]:
            items["required"].append("narration_cue")

    return {
        "type": "function",
        "function": {
            "name": layout_id,
            "description": f"{layout['description']} {layout['when_to_use']}",
            "parameters": schema,
        },
    }
