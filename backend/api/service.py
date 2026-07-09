#!/usr/bin/env python3
"""
Service layer: drives the existing pipeline per project (paths are project-scoped).

Reuses pipeline/* (generate_plan, outline, fill, tts, pack_render). Each project has
its own audio dir and its own render-input + MP4, so projects don't collide.
"""

import hashlib
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


# --- Brand kit assets: bake to Storage (issue #15) -------------------------


def _asset_key(kit_id: str, file: str, data: bytes) -> str:
    """Storage key for a baked kit asset (logo/background image).

    Content-addressed under the brand kit (`sha256` of the bytes): the same file
    re-posted always maps to the same key. That stability is what keeps
    `db.upsert_brand_kit`'s "new version only if changed" idempotency intact — the
    baked `file` value has to be reproducible from the input, so a key can't embed
    the (not-yet-assigned) `brand_kit_version_id`. Version scoping still holds at the
    DB level: each `Asset` row is FK-scoped to its `brand_kit_version_id` (ADR-06).
    """
    ext = os.path.splitext(file)[1]
    digest = hashlib.sha256(data).hexdigest()[:16]
    return f"brand-kits/{kit_id}/assets/{digest}{ext}"


def _bake_asset_file(storage: Any, kit_id: str, obj: dict[str, Any]) -> None:
    """Promote one asset's `file` (logo asset or `cosmetic.background`) into Storage,
    rewriting it in place to the resulting key. No-op if the object has no `file`.

    A local path pointing at an existing file is baked; a value that is already a
    stored key is left as-is (re-posting a kit whose assets are already in Storage).
    Anything else fails loudly — a `file` that resolves neither on local disk nor in
    Storage is a bad kit, never something to silently render without.
    """
    file = obj.get("file")
    if not file:
        return
    local = os.path.join(BACKEND, file)
    if os.path.isfile(local):
        with open(local, "rb") as f:
            data = f.read()
        key = _asset_key(kit_id, file, data)
        storage.put(key, data)
        obj["file"] = key
    elif storage.exists(file):
        return
    else:
        raise FileNotFoundError(f"kit asset file not found on local disk or in Storage: {file!r}")


def bake_kit_assets(kit: dict[str, Any]) -> None:
    """Bake a kit's baked assets (logos + image background) into Storage, mutating the
    kit so each `file` becomes a Storage key (issue #15). Call before persisting the
    kit: the render worker resolves those keys from Storage instead of copying local
    files into `render-motor/public/` at pack time.
    """
    storage = get_storage()
    kit_id = kit["id"]
    for asset in kit.get("assets", []):
        _bake_asset_file(storage, kit_id, asset)
    bg = kit.get("cosmetic", {}).get("background")
    if bg and bg.get("type") == "image":
        _bake_asset_file(storage, kit_id, bg)


def _materialize_kit_asset(storage: Any, key: str, pub: str, pid: str) -> str:
    """Download a baked kit asset (Storage key) into the project's render sandbox and
    return its `staticFile()`-relative path. Mirrors how scene audio is materialized —
    project-scoped under `proj-{pid}/`, so it's wiped with the rest of the render
    scratch (issue #13) and never leaks into `public/` root across renders.
    """
    name = os.path.basename(key)
    with open(os.path.join(pub, name), "wb") as f:
        f.write(storage.get(key))
    return f"proj-{pid}/{name}"


def _resolve_logo(kit: Kit, storage: Any, pub: str, pid: str) -> str | None:
    """Materialize the kit's primary logo from Storage for the render, or None."""
    from pack_render import primary_logo

    primary = primary_logo(kit)
    if not primary or not primary.get("file"):
        return None
    return _materialize_kit_asset(storage, primary["file"], pub, pid)


def _resolve_background(kit: Kit, storage: Any, pub: str, pid: str) -> dict[str, Any] | None:
    """Build the render's background override, materializing an image background from
    Storage. Non-image backgrounds (gradient/solid/theme) carry no baked file.
    """
    bg = kit.get("cosmetic", {}).get("background")
    if not bg:
        return None
    out: dict[str, Any] = {
        "type": bg.get("type", "theme"),
        "overlayDecor": bg.get("overlayDecor", False),
    }
    if bg.get("type") == "image":
        key = bg.get("file")
        if not key:
            raise ValueError("image background has no 'file' to resolve")
        out["value"] = _materialize_kit_asset(storage, key, pub, pid)
    return out


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
        emoji_map,
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
        # Kit assets (logo + image background) are baked into Storage at kit creation
        # (issue #15); resolve them from there into the project sandbox, not from a
        # local copy in public/.
        cosmetic["background"] = _resolve_background(kit, storage, pub, pid)
        render_input = {
            "styleId": kit.get("visualStyle", "tech"),
            "cosmetic": cosmetic,
            "logo": _resolve_logo(kit, storage, pub, pid),
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
