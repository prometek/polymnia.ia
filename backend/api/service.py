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
from pathlib import Path
from typing import Any

from . import db
from .storage import get_storage

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


def _audio_key(pid: str, order: int) -> str:
    return f"projects/{pid}/audio/scene-{order}.wav"


def _video_key(pid: str) -> str:
    return f"projects/{pid}/render.mp4"


def _total(scenes: list[Scene]) -> float:
    return round(sum(float(s["timing"]["duration_s"]) for s in scenes), 3)


def _voiceover_and_store(pid: str, scene: Scene) -> Scene:
    """Run TTS for one scene (pipeline/tts.py) then persist its WAV via Storage
    (issue #12), replacing `timing.audio_path` with the storage key.

    tts.py still writes the WAV to a local scratch dir first: it measures the WAV
    duration with the stdlib `wave` module and, for cued layouts, force-aligns the
    audio, both of which need a real file on disk. That scratch file is not the
    artefact of record — this is the one place that promotes it into Storage, so
    every other reader (render packing, future replay) goes through the interface.
    """
    voiced = tts.voiceover_scene(scene, _audio_dir(pid))
    staged_path = voiced["timing"]["audio_path"]
    key = _audio_key(pid, voiced["order"])
    try:
        with open(staged_path, "rb") as f:
            data = f.read()
        get_storage().put(key, data)
    finally:
        # The scratch WAV is not the artefact of record once promotion is attempted
        # (issue #13) — remove it so it doesn't accumulate on whichever worker ran
        # TTS, whether or not the put above succeeded (a retry re-runs TTS anyway).
        Path(staged_path).unlink(missing_ok=True)
    voiced["timing"]["audio_path"] = key
    return voiced


def generate(pid: str, input_text: str, kit: Kit, job_id: str) -> tuple[list[Scene], float]:
    """Full generation for a project: plan -> outline -> fill -> TTS + align.

    Reports progress on `job_id` at each stage boundary (issue #9): plan/outline/fill/
    tts. Step writes are plain `db` calls — this stays free of the Celery/queue layer.
    """
    db.set_job_step(job_id, "plan")
    plan = generate_plan.generate_plan(input_text)
    db.set_job_step(job_id, "outline")
    scenes_outline = outline.build_outline(plan, kit.get("kicker_style", "thematic"))["scenes"]
    db.set_job_step(job_id, "fill")
    filled = [fill.fill_scene(s, kit) for s in scenes_outline]
    db.set_job_step(job_id, "tts")
    scenes = [_voiceover_and_store(pid, s) for s in filled]
    return scenes, _total(scenes)


def run_generation(pid: str, input_text: str, kit: Kit, job_id: str) -> None:
    """Queue job: generate the whole project and persist it with a status transition.

    Owns the *video* status (generating -> ready/error). Re-raises after marking the
    video errored so the caller (Celery task) can fail the job and the broker retry.
    """
    try:
        scenes, total = generate(pid, input_text, kit, job_id)
        db.replace_scenes(pid, scenes)
        db.set_total(pid, total)
        db.set_status(pid, "ready")
    except Exception:
        db.set_status(pid, "error")
        logger.exception("generate failed for project %s", pid)
        raise


def run_render(pid: str, scenes: list[Scene], kit: Kit, job_id: str) -> None:
    """Queue job: render the MP4, persist it via Storage and its key, with a status
    transition.

    Owns the *video* status (rendering -> ready/error). Re-raises after marking the
    video errored so the caller (Celery task) can fail the job and the broker retry.
    """
    try:
        mp4 = render_project(pid, scenes, kit, job_id)
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
    edited = _voiceover_and_store(pid, edited)
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
        scenes[idx] = _voiceover_and_store(pid, scenes[idx])
    db.upsert_scene(pid, scenes[idx])
    db.set_total(pid, _total(scenes))
    return scenes[idx]


def _cleanup_render_scratch(pub: str, props_path: str, rendered_path: str) -> None:
    """Remove this render's local scratch (issue #13). None of these are the artefact
    of record: the durable MP4 already lives in Storage by the time this runs (or the
    render/promotion failed, in which case there's nothing worth keeping either way).
    Left on disk, they'd accumulate unboundedly on the render worker across renders —
    the audio copies materialized under `public/proj-{pid}/` for Remotion's
    `staticFile()` reads, the packed `render-input-{pid}.json`, and Remotion's own
    local MP4 output.

    Guarded per-path (`ignore_errors`/`missing_ok`) rather than one try/except around
    the lot, so a partial render (e.g. the subprocess failed before ever writing the
    MP4) can't turn cleanup itself into a crash.
    """
    shutil.rmtree(pub, ignore_errors=True)
    Path(props_path).unlink(missing_ok=True)
    Path(rendered_path).unlink(missing_ok=True)


def render_project(pid: str, scenes: list[Scene], kit: Kit, job_id: str) -> str:
    """Build a project-scoped render-input and render the full MP4. Returns its
    Storage key (issue #12) — not a filesystem path.

    Reports progress on `job_id` (issue #9): 'packing' while building render-input and
    copying audio, 'render' for the Remotion subprocess itself.

    All local scratch this creates (materialized audio, render-input JSON, Remotion's
    own MP4 output) is removed in `finally` once promotion into Storage is attempted
    (issue #13) — the render worker must not depend on its local disk for output.
    """
    from pack_render import (
        PUBLIC,
        RENDER_DIR,
        copy_logo,
        emoji_map,
        render_background,
        render_cosmetic,
        resolve_content,
    )

    storage = get_storage()
    db.set_job_step(job_id, "packing")
    pub = os.path.join(PUBLIC, f"proj-{pid}")
    os.makedirs(pub, exist_ok=True)
    emojis = emoji_map(kit)
    props_path = os.path.join(RENDER_DIR, f"render-input-{pid}.json")
    rendered_path = os.path.join(RENDER_DIR, "out", f"{pid}.mp4")

    try:
        out_scenes = []
        for s in scenes:
            key = s["timing"]["audio_path"]  # storage key, set by _voiceover_and_store
            name = os.path.basename(key)
            # Remotion is a headless subprocess reading from render-motor/public/ on local
            # disk (its `staticFile()` mechanism) — this materialization stays local
            # regardless of backend; the audio itself is only ever read via Storage.
            with open(os.path.join(pub, name), "wb") as f:
                f.write(storage.get(key))
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
        with open(props_path, "w", encoding="utf-8") as f:
            json.dump(render_input, f, ensure_ascii=False, indent=2)

        db.set_job_step(job_id, "render")
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
        # Remotion writes the MP4 to its own local out/ dir (subprocess, can't target S3
        # directly) — this is the one place that promotes that output into Storage.
        with open(rendered_path, "rb") as f:
            mp4_data = f.read()
        video_key = _video_key(pid)
        storage.put(video_key, mp4_data)
        return video_key
    finally:
        _cleanup_render_scratch(pub, props_path, rendered_path)
