#!/usr/bin/env python3
"""
POC - Pipeline step 1: generate a structured educational plan.

Input  : a text file (input.txt) passed as an argument.
Output : an educational plan as JSON on stdout (key message, audience, idea progression).

This plan is the reusable artifact of the pipeline: it is then split into scenes (step 2).
The model does NOT generate scenes here, only the narrative structure.

Usage: python3 generate_plan.py input.txt > plan.json
"""

import sys
from typing import Any

from utils import call_mistral, print_json, read_text

# --- System prompt: force structured JSON output ---------------------------

SYSTEM_PROMPT = """You are an instructional designer. From raw content, you produce the PLAN
of a short educational motion-design video.

You do NOT write the script, you do NOT split into scenes, you do NOT choose visuals.
You produce only the narrative structure: what it is about, for whom,
in what order, with a single idea per section.

You reorganize, prune and prioritize the content if needed so it is clear and progressive.

You reply EXCLUSIVELY with a valid JSON object, with no text before or after,
and no Markdown fences.
Expected schema:
{
  "key_message": "string - the single idea the video must convey, in one sentence",
  "audience": "string - the target audience and its level (e.g. 'beginners, general public')",
  "angle": "string - the angle of attack (e.g. 'starts from a concrete problem')",
  "sections": [
    {
      "title": "string - short section title",
      "key_idea": "string - the single idea carried by this section, in one sentence"
    }
  ]
}
Aim for 3 to 7 sections. A single idea per section."""


def generate_plan(source_text: str) -> dict[str, Any]:
    """Step 1: turn raw text into an educational plan."""
    return call_mistral(SYSTEM_PROMPT, source_text, temperature=0.3)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python3 generate_plan.py input.txt > plan.json")

    source_text = read_text(sys.argv[1])
    plan = generate_plan(source_text)
    print_json(plan)


if __name__ == "__main__":
    main()
