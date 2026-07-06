"""Render artefacts through Storage, not the local filesystem (issue #13 / PRO-13).

Intent (ticket #13): the final MP4 must be written to Storage under a scoped key,
`videos.mp4_path` must store that key (not a disk path), voice-over WAVs must be
written to Storage under scoped keys and be resolved from there when packing a
render, and the render worker must not depend on its local disk for the *output*
artefact. This file exercises each of those points directly against
`api.service` (the orchestration layer named in the ticket), complementing the
full end-to-end coverage already in tests/api/test_jobs.py and
tests/api/test_video_download.py.
"""

import os
import subprocess
from typing import Any

import pytest
from api import db, service
from api.storage import get_storage
from starlette.testclient import TestClient

import pack_render


def _seed_project(user_id: str, kit_id: str = "kit-out") -> str:
    version_id = db.upsert_brand_kit({"id": kit_id, "name": "Out"}, user_id)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, user_id, version_id, "v")
    return vid


def _kit() -> dict[str, Any]:
    return {"visualStyle": "tech", "assets": []}


def _scene(order: int, audio_path: str) -> dict[str, Any]:
    return {
        "order": order,
        "type": "statement",
        "composition": "centered",
        "props": {},
        "asset_refs": [],
        "timing": {"duration_s": 1.0, "audio_path": audio_path},
    }


# --- 1. Voice-over WAVs land in Storage under scoped keys -------------------


def test_voiceover_and_store_writes_wav_under_scoped_storage_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """`_voiceover_and_store` is the single place tts.py's local scratch WAV gets
    promoted into Storage (service.py's own docstring) — assert the promoted key is
    scoped per-project/scene and that the exact bytes tts.py produced come back
    through `Storage.get`, not just through the scratch file."""

    def fake_voiceover_scene(scene: dict[str, Any], audio_dir: str) -> dict[str, Any]:
        os.makedirs(audio_dir, exist_ok=True)
        path = os.path.join(audio_dir, f"scene-{scene['order']}.wav")
        with open(path, "wb") as f:
            f.write(b"RIFF-fake-wav-bytes")
        return {**scene, "timing": {"duration_s": 2.5, "audio_path": path}}

    monkeypatch.setattr(service.tts, "voiceover_scene", fake_voiceover_scene)

    pid = "proj-voice-1"
    scene_in = {"order": 3, "type": "statement", "props": {}}
    voiced = service._voiceover_and_store(pid, scene_in)

    key = voiced["timing"]["audio_path"]
    assert key == f"projects/{pid}/audio/scene-3.wav"
    assert not os.path.isabs(key)  # a Storage key, never a filesystem path
    assert get_storage().get(key) == b"RIFF-fake-wav-bytes"


def test_voiceover_and_store_scopes_keys_per_project_no_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two different projects voicing the same scene order must not collide in
    Storage — each project has its own audio namespace."""

    def fake_voiceover_scene(scene: dict[str, Any], audio_dir: str) -> dict[str, Any]:
        os.makedirs(audio_dir, exist_ok=True)
        path = os.path.join(audio_dir, f"scene-{scene['order']}.wav")
        payload = f"wav-for-{audio_dir}".encode()
        with open(path, "wb") as f:
            f.write(payload)
        return {**scene, "timing": {"duration_s": 1.0, "audio_path": path}}

    monkeypatch.setattr(service.tts, "voiceover_scene", fake_voiceover_scene)

    v1 = service._voiceover_and_store("proj-a", {"order": 0, "type": "statement", "props": {}})
    v2 = service._voiceover_and_store("proj-b", {"order": 0, "type": "statement", "props": {}})

    key1, key2 = v1["timing"]["audio_path"], v2["timing"]["audio_path"]
    assert key1 != key2
    assert get_storage().get(key1) != get_storage().get(key2)


# --- 2. Final MP4 lands in Storage; mp4_path IS that key ---------------------


def _fake_subprocess_run_writing_mp4(render_dir: str, vid: str, payload: bytes) -> Any:
    def _run(
        cmd: list[str], cwd: str | None = None, check: bool | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        out_dir = os.path.join(render_dir, "out")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, f"{vid}.mp4"), "wb") as f:
            f.write(payload)
        return subprocess.CompletedProcess(cmd, 0)

    return _run


def test_render_project_returns_storage_key_and_bytes_are_resolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """`render_project` (called by `run_render`) must return a Storage KEY — the
    exact string `db.set_mp4` persists as `Video.mp4_path` — and that key must be
    resolvable via `get_storage().get(...)` back to the bytes Remotion produced."""
    uid = db.ensure_user("render-unit@test.local")
    vid = _seed_project(uid)
    audio_key = f"projects/{vid}/audio/scene-0.wav"
    get_storage().put(audio_key, b"not-a-real-wav")
    db.replace_scenes(vid, [_scene(0, audio_key)])

    public_dir = tmp_path / "public"
    render_dir = tmp_path / "render"
    render_dir.mkdir()
    monkeypatch.setattr(pack_render, "PUBLIC", str(public_dir))
    monkeypatch.setattr(pack_render, "RENDER_DIR", str(render_dir))

    mp4_bytes = b"fake-mp4-payload"
    monkeypatch.setattr(
        service.subprocess, "run", _fake_subprocess_run_writing_mp4(str(render_dir), vid, mp4_bytes)
    )

    scenes = db.get_scenes(vid)
    job_id = db.create_job(vid, "render")
    key = service.render_project(vid, scenes, _kit(), job_id)

    assert key == f"projects/{vid}/render.mp4"
    assert not os.path.isabs(key)
    assert get_storage().get(key) == mp4_bytes


def test_run_render_persists_mp4_path_equal_to_the_returned_storage_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """acceptance criterion (b): `Video.mp4_path` (via `db.set_mp4`, driven by
    `run_render`) must equal exactly the Storage key `render_project` returned —
    no divergence between what's persisted and what's actually in Storage."""
    uid = db.ensure_user("render-unit-2@test.local")
    vid = _seed_project(uid, kit_id="kit-out-2")
    audio_key = f"projects/{vid}/audio/scene-0.wav"
    get_storage().put(audio_key, b"not-a-real-wav")
    db.replace_scenes(vid, [_scene(0, audio_key)])

    public_dir = tmp_path / "public"
    render_dir = tmp_path / "render"
    render_dir.mkdir()
    monkeypatch.setattr(pack_render, "PUBLIC", str(public_dir))
    monkeypatch.setattr(pack_render, "RENDER_DIR", str(render_dir))

    mp4_bytes = b"another-fake-mp4"
    monkeypatch.setattr(
        service.subprocess, "run", _fake_subprocess_run_writing_mp4(str(render_dir), vid, mp4_bytes)
    )

    scenes = db.get_scenes(vid)
    job_id = db.create_job(vid, "render")
    service.run_render(vid, scenes, _kit(), job_id)

    video = db.get_video(vid, uid)
    assert video is not None
    assert video["status"] == "ready"
    assert video["mp4_path"] == f"projects/{vid}/render.mp4"
    assert get_storage().get(video["mp4_path"]) == mp4_bytes


# --- 3. GET /projects/{pid}/video: e2e through Storage for local + S3 -------
#
# The full local-backend byte-identical / Range / dangling-key / other-user's-project
# cases, and the S3 signed-URL-redirect + dangling-key cases, are already covered
# end-to-end in tests/api/test_video_download.py (moto-backed for S3). Re-asserting
# them here would just re-derive the same test cases from the same intent — no new
# coverage — so this file adds only the one video-download angle not covered there:
# the render pipeline's OWN key format is what the download endpoint resolves.


def test_video_download_resolves_the_exact_key_render_project_produced(
    client: TestClient,
    as_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """End-to-end: drive the real render task, then download — the bytes served
    must be exactly what `render_project` promoted into Storage, fetched through
    the key persisted on the video row (not a hardcoded/parallel path)."""
    from api.celery_app import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        uid = db.ensure_user("render-e2e@test.local")
        vid = _seed_project(uid, kit_id="kit-out-3")
        audio_key = f"projects/{vid}/audio/scene-0.wav"
        get_storage().put(audio_key, b"not-a-real-wav")
        db.replace_scenes(vid, [_scene(0, audio_key)])
        as_user(uid)

        public_dir = tmp_path / "public"
        render_dir = tmp_path / "render"
        render_dir.mkdir()
        monkeypatch.setattr(pack_render, "PUBLIC", str(public_dir))
        monkeypatch.setattr(pack_render, "RENDER_DIR", str(render_dir))

        mp4_bytes = b"e2e-fake-mp4-bytes"
        monkeypatch.setattr(
            service.subprocess,
            "run",
            _fake_subprocess_run_writing_mp4(str(render_dir), vid, mp4_bytes),
        )

        resp = client.post(f"/projects/{vid}/render")
        assert resp.status_code == 202

        video = db.get_video(vid, uid)
        assert video is not None
        assert video["status"] == "ready"

        download = client.get(f"/projects/{vid}/video")
        assert download.status_code == 200
        assert download.content == mp4_bytes
    finally:
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False


# --- 4. Acceptance point (a): does a local MP4 blob persist after promotion? -


def test_render_project_local_intermediate_mp4_is_not_left_on_disk_after_promotion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Acceptance criterion (a): "a render produces an MP4 object IN Storage and NO
    PERSISTENT LOCAL MP4 BLOB REMAINS". Drives the real `render_project` path and
    asserts the intermediate local MP4 Remotion wrote is gone once promotion into
    Storage succeeds."""
    uid = db.ensure_user("render-cleanup@test.local")
    vid = _seed_project(uid, kit_id="kit-cleanup")
    audio_key = f"projects/{vid}/audio/scene-0.wav"
    get_storage().put(audio_key, b"not-a-real-wav")
    db.replace_scenes(vid, [_scene(0, audio_key)])

    public_dir = tmp_path / "public"
    render_dir = tmp_path / "render"
    render_dir.mkdir()
    monkeypatch.setattr(pack_render, "PUBLIC", str(public_dir))
    monkeypatch.setattr(pack_render, "RENDER_DIR", str(render_dir))

    mp4_bytes = b"leftover-fake-mp4"
    monkeypatch.setattr(
        service.subprocess, "run", _fake_subprocess_run_writing_mp4(str(render_dir), vid, mp4_bytes)
    )

    scenes = db.get_scenes(vid)
    job_id = db.create_job(vid, "render")
    key = service.render_project(vid, scenes, _kit(), job_id)

    # The promotion itself must have succeeded regardless of the finding below.
    assert get_storage().get(key) == mp4_bytes

    local_rendered_path = os.path.join(str(render_dir), "out", f"{vid}.mp4")
    assert not os.path.isfile(local_rendered_path), (
        f"intermediate local MP4 still present at {local_rendered_path!r} after "
        "promotion into Storage — acceptance criterion (a) is not met"
    )
