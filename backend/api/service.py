#!/usr/bin/env python3
"""
Service layer: drives the existing pipeline per project (paths are project-scoped).

Reuses pipeline/* (generate_plan, outline, fill, tts, pack_render). Each project has
its own audio dir and its own render-input + MP4, so projects don't collide.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Any

from . import db

logger = logging.getLogger("polymnia.service")

Scene = dict[str, Any]
Kit = dict[str, Any]

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE = os.path.join(BACKEND, "pipeline")
sys.path.insert(0, PIPELINE)  # pipeline modules import each other as siblings

import edit_scene  # noqa: E402 (reuse _set_path / _swap_icons)
import fill  # noqa: E402
import generate_plan  # noqa: E402
import outline  # noqa: E402
import tts  # noqa: E402
from utils import collect_asset_refs  # noqa: E402


def _audio_dir(pid: str) -> str:
    d = os.path.join(BACKEND, "out", "projects", pid, "audio")
    os.makedirs(d, exist_ok=True)
    return d


def _total(scenes: list[Scene]) -> float:
    return round(sum(float(s["timing"]["duration_s"]) for s in scenes), 3)


def generate(pid: str, input_text: str, kit: Kit) -> tuple[list[Scene], float]:
    """Full generation for a project: plan -> outline -> fill -> TTS + align."""
    plan = generate_plan.generate_plan(input_text)
    scenes_outline = outline.build_outline(plan, kit.get("kicker_style", "thematic"))["scenes"]
    filled = [fill.fill_scene(s, kit) for s in scenes_outline]
    audio_dir = _audio_dir(pid)
    scenes = [tts.voiceover_scene(s, audio_dir) for s in filled]
    return scenes, _total(scenes)


def run_generation(pid: str, input_text: str, kit: Kit) -> None:
    """Queue job: generate the whole project and persist it with a status transition.

    Owns the *video* status (generating -> ready/error). Re-raises after marking the
    video errored so the caller (Celery task) can fail the job and the broker retry.
    """
    try:
        scenes, total = generate(pid, input_text, kit)
        db.replace_scenes(pid, scenes)
        db.set_total(pid, total)
        db.set_status(pid, "ready")
    except Exception:
        db.set_status(pid, "error")
        logger.exception("generate failed for project %s", pid)
        raise


def run_render(pid: str, scenes: list[Scene], kit: Kit) -> None:
    """Queue job: render the MP4 and persist its path with a status transition.

    Owns the *video* status (rendering -> ready/error). Re-raises after marking the
    video errored so the caller (Celery task) can fail the job and the broker retry.
    """
    try:
        mp4 = render_project(pid, scenes, kit)
        db.set_mp4(pid, mp4)
        db.set_status(pid, "ready")
    except Exception:
        db.set_status(pid, "error")
        logger.exception("render failed for project %s", pid)
        raise


def _find(scenes: list[Scene], order: int) -> int:
    idx = next((i for i, s in enumerate(scenes) if s.get("order") == order), None)
    if idx is None:
        raise KeyError(f"no scene with order={order}")
    return idx


def edit_ai(pid: str, scenes: list[Scene], order: int, instruction: str, kit: Kit) -> Scene:
    """Scoped AI edit: regenerate ONE scene from a prompt (re-TTS + re-align). Persists it."""
    idx = _find(scenes, order)
    edited = fill.fill_scene(scenes[idx], kit, instruction=instruction)
    edited = tts.voiceover_scene(edited, _audio_dir(pid))
    scenes[idx] = edited
    db.upsert_scene(pid, edited)
    db.set_total(pid, _total(scenes))
    return edited


def edit_direct(
    pid: str,
    scenes: list[Scene],
    order: int,
    sets: dict[str, str] | None,
    swap: str | None,
    kit: Kit,
) -> Scene:
    """Direct edit, no LLM: set props fields / swap icon. Re-TTS only if narration changed."""
    idx = _find(scenes, order)
    props = scenes[idx].setdefault("props", {})
    pairs = list((sets or {}).items())
    for path, value in pairs:
        edit_scene._set_path(props, path, value)
    if swap:
        edit_scene._swap_icons(props, swap)
    scenes[idx]["asset_refs"] = collect_asset_refs(props, kit)
    if any(p.startswith("narration") for p, _ in pairs):
        scenes[idx] = tts.voiceover_scene(scenes[idx], _audio_dir(pid))
    db.upsert_scene(pid, scenes[idx])
    db.set_total(pid, _total(scenes))
    return scenes[idx]


def render_project(pid: str, scenes: list[Scene], kit: Kit) -> str:
    """Build a project-scoped render-input and render the full MP4. Returns its path."""
    from pack_render import (
        PUBLIC,
        RENDER_DIR,
        copy_logo,
        emoji_map,
        render_background,
        render_cosmetic,
        resolve_content,
    )

    pub = os.path.join(PUBLIC, f"proj-{pid}")
    os.makedirs(pub, exist_ok=True)
    emojis = emoji_map(kit)

    out_scenes = []
    for s in scenes:
        src = s["timing"]["audio_path"]
        if not os.path.isabs(src):
            src = os.path.join(BACKEND, src)
        name = os.path.basename(src)
        shutil.copyfile(src, os.path.join(pub, name))
        out_scenes.append(
            {
                "type": s["type"],
                "composition": s.get("composition", "centered"),
                "durationS": s["timing"]["duration_s"],
                "audio": f"proj-{pid}/{name}",  # staticFile path under public/
                "props": resolve_content(s.get("props", {}), emojis),
            }
        )

    cosmetic = render_cosmetic(kit)
    cosmetic["background"] = render_background(kit)
    render_input = {
        "styleId": kit.get("visualStyle", "tech"),
        "cosmetic": cosmetic,
        "logo": copy_logo(kit),
        "scenes": out_scenes,
    }
    props_path = os.path.join(RENDER_DIR, f"render-input-{pid}.json")
    with open(props_path, "w", encoding="utf-8") as f:
        json.dump(render_input, f, ensure_ascii=False, indent=2)

    subprocess.run(
        [
            "npx",
            "remotion",
            "render",
            "src/index.ts",
            "Polymnia",
            f"out/{pid}.mp4",
            f"--props=./render-input-{pid}.json",
        ],
        cwd=RENDER_DIR,
        check=True,
    )
    return os.path.join(RENDER_DIR, "out", f"{pid}.mp4")
