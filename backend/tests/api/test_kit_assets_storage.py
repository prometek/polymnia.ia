"""Baked brand-kit assets (logo + image background) live in Storage, not copied into
`render-motor/public/` (issue #15 / PRO-15).

Intent (ticket #15): a kit's logo and image background are baked into the Storage
abstraction under a scoped key at kit creation, `Asset.file` / `cosmetic.background`
reference that key, and the render worker resolves the remote asset into its own
sandbox at pack time — no systematic local copy into `public/`, and the source stays
durable / re-uploadable (§14). This file drives `bake_kit_assets` (ingestion) and the
worker-side resolvers (`_resolve_logo` / `_resolve_background`) directly, plus one
end-to-end pass through the `POST /brand-kits` endpoint.
"""

import json
import os
import subprocess
from typing import Any

import pytest
from api import db, service
from api.storage import get_storage
from starlette.testclient import TestClient

import pack_render


def _write_local(path: str, data: bytes) -> str:
    with open(path, "wb") as f:
        f.write(data)
    return path


# --- 1. Ingestion: bake_kit_assets promotes local files into Storage --------


def test_bake_kit_assets_promotes_logo_and_background_to_storage(tmp_path: Any) -> None:
    """A kit posted with local logo + image-background files: after baking, each `file`
    is a Storage key (never a local path), and the exact bytes come back via
    `Storage.get`."""
    logo_bytes = b"<svg>logo</svg>"
    bg_bytes = b"<svg>bg</svg>"
    logo_path = _write_local(str(tmp_path / "logo.svg"), logo_bytes)
    bg_path = _write_local(str(tmp_path / "waves.svg"), bg_bytes)

    kit: dict[str, Any] = {
        "id": "kit-bake",
        "assets": [{"id": "logo-dark", "type": "logo", "primary": True, "file": logo_path}],
        "cosmetic": {"background": {"type": "image", "file": bg_path, "overlayDecor": True}},
    }
    service.bake_kit_assets(kit)

    logo_key = kit["assets"][0]["file"]
    bg_key = kit["cosmetic"]["background"]["file"]
    assert logo_key.startswith("brand-kits/kit-bake/assets/")
    assert not os.path.isabs(logo_key)
    assert bg_key.startswith("brand-kits/kit-bake/assets/")
    assert get_storage().get(logo_key) == logo_bytes
    assert get_storage().get(bg_key) == bg_bytes
    # Non-file parts of the background survive baking unchanged.
    assert kit["cosmetic"]["background"]["overlayDecor"] is True


def test_bake_kit_assets_is_content_addressed_and_idempotent(tmp_path: Any) -> None:
    """Re-baking the same bytes yields the same key (content-addressed) — this is what
    keeps `db.upsert_brand_kit`'s change-detection stable across re-posts."""
    data = b"stable-bytes"
    p1 = _write_local(str(tmp_path / "a.png"), data)
    p2 = _write_local(str(tmp_path / "b.png"), data)  # same bytes, different path

    k1 = {"id": "kit-ca", "assets": [{"id": "l", "type": "logo", "file": p1}]}
    k2 = {"id": "kit-ca", "assets": [{"id": "l", "type": "logo", "file": p2}]}
    service.bake_kit_assets(k1)
    service.bake_kit_assets(k2)
    assert k1["assets"][0]["file"] == k2["assets"][0]["file"]

    # A value already in Storage is left as-is (re-post of an already-baked kit).
    already = dict(k1)
    baked_key = k1["assets"][0]["file"]
    k3 = {"id": "kit-ca", "assets": [{"id": "l", "type": "logo", "file": baked_key}]}
    service.bake_kit_assets(k3)
    assert k3["assets"][0]["file"] == baked_key
    assert already["assets"][0]["file"] == baked_key


def test_bake_kit_assets_different_bytes_different_key(tmp_path: Any) -> None:
    p1 = _write_local(str(tmp_path / "a.svg"), b"one")
    p2 = _write_local(str(tmp_path / "b.svg"), b"two")
    k1 = {"id": "kit-d", "assets": [{"id": "l", "type": "logo", "file": p1}]}
    k2 = {"id": "kit-d", "assets": [{"id": "l", "type": "logo", "file": p2}]}
    service.bake_kit_assets(k1)
    service.bake_kit_assets(k2)
    assert k1["assets"][0]["file"] != k2["assets"][0]["file"]


def test_bake_kit_assets_missing_file_fails_loudly() -> None:
    """A `file` that resolves neither locally nor in Storage is a bad kit — surface it,
    don't silently render without the asset."""
    kit = {"id": "kit-x", "assets": [{"id": "l", "type": "logo", "file": "nope/missing.svg"}]}
    with pytest.raises(FileNotFoundError, match="not found on local disk or in Storage"):
        service.bake_kit_assets(kit)


def test_bake_kit_assets_noop_without_files() -> None:
    """Assets with no `file` (emoji icons, non-image backgrounds) are untouched."""
    kit: dict[str, Any] = {
        "id": "kit-e",
        "assets": [{"id": "icon-anchor", "type": "icon", "emoji": "⚓"}],
        "cosmetic": {"background": {"type": "gradient"}},
    }
    service.bake_kit_assets(kit)
    assert kit["assets"][0] == {"id": "icon-anchor", "type": "icon", "emoji": "⚓"}
    assert kit["cosmetic"]["background"] == {"type": "gradient"}


# --- 2. Resolution: worker materializes keys into its sandbox ---------------


def test_resolve_logo_materializes_from_storage_into_project_sandbox(tmp_path: Any) -> None:
    key = "brand-kits/kit-r/assets/deadbeef.svg"
    get_storage().put(key, b"<svg>the-logo</svg>")
    pub = str(tmp_path / "proj-pub")
    os.makedirs(pub)

    kit = {"assets": [{"id": "logo-dark", "type": "logo", "primary": True, "file": key}]}
    static_path = service._resolve_logo(kit, get_storage(), pub, "p1")

    assert static_path == "proj-p1/deadbeef.svg"  # staticFile-relative, project-scoped
    with open(os.path.join(pub, "deadbeef.svg"), "rb") as f:
        assert f.read() == b"<svg>the-logo</svg>"


def test_resolve_logo_none_when_no_logo(tmp_path: Any) -> None:
    pub = str(tmp_path / "pub")
    os.makedirs(pub)
    assert service._resolve_logo({"assets": []}, get_storage(), pub, "p1") is None


def test_resolve_background_image_materializes_from_storage(tmp_path: Any) -> None:
    key = "brand-kits/kit-r/assets/cafe1234.svg"
    get_storage().put(key, b"<svg>bg</svg>")
    pub = str(tmp_path / "pub")
    os.makedirs(pub)

    kit = {"cosmetic": {"background": {"type": "image", "file": key, "overlayDecor": True}}}
    out = service._resolve_background(kit, get_storage(), pub, "p1")

    assert out == {"type": "image", "overlayDecor": True, "value": "proj-p1/cafe1234.svg"}
    with open(os.path.join(pub, "cafe1234.svg"), "rb") as f:
        assert f.read() == b"<svg>bg</svg>"


def test_resolve_background_non_image_carries_no_file(tmp_path: Any) -> None:
    pub = str(tmp_path / "pub")
    os.makedirs(pub)
    kit = {"cosmetic": {"background": {"type": "gradient", "overlayDecor": False}}}
    out = service._resolve_background(kit, get_storage(), pub, "p1")
    assert out == {"type": "gradient", "overlayDecor": False}
    assert "value" not in out


# --- 3. End-to-end: render-input references the resolved remote assets -------


def _seed_kit_with_baked_assets(user_id: str) -> tuple[str, str]:
    logo_key = "brand-kits/kit-e2e/assets/1111aaaa.svg"
    bg_key = "brand-kits/kit-e2e/assets/2222bbbb.svg"
    get_storage().put(logo_key, b"<svg>logo</svg>")
    get_storage().put(bg_key, b"<svg>bg</svg>")
    kit = {
        "id": "kit-e2e",
        "name": "E2E",
        "visualStyle": "tech",
        "assets": [{"id": "logo-dark", "type": "logo", "primary": True, "file": logo_key}],
        "cosmetic": {"background": {"type": "image", "file": bg_key, "overlayDecor": True}},
    }
    version_id = db.upsert_brand_kit(kit, user_id)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, user_id, version_id, "v")
    return vid, version_id


def test_render_project_resolves_remote_logo_and_background(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Drive the real `render_project`: the render-input handed to Remotion must point
    the logo + background at the project sandbox (`proj-{pid}/...`) materialized from
    Storage — no copy into `public/` root."""
    uid = db.ensure_user("kit-e2e@test.local")
    vid, version_id = _seed_kit_with_baked_assets(uid)
    audio_key = f"projects/{vid}/audio/scene-0.wav"
    get_storage().put(audio_key, b"wav")
    db.replace_scenes(
        vid,
        [
            {
                "order": 0,
                "type": "title",
                "composition": "centered",
                "props": {},
                "asset_refs": [],
                "timing": {"duration_s": 1.0, "audio_path": audio_key},
            }
        ],
    )

    public_dir = tmp_path / "public"
    render_dir = tmp_path / "render"
    render_dir.mkdir()
    monkeypatch.setattr(pack_render, "PUBLIC", str(public_dir))
    monkeypatch.setattr(pack_render, "RENDER_DIR", str(render_dir))

    captured: dict[str, Any] = {}

    def _fake_run(cmd: list[str], cwd: str | None = None, check: bool | None = None) -> Any:
        # Read the render-input Remotion would consume (before it's wiped in `finally`).
        with open(os.path.join(str(render_dir), f"render-input-{vid}.json")) as f:
            captured["props"] = json.load(f)
        out_dir = os.path.join(str(render_dir), "out")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, f"{vid}.mp4"), "wb") as f:
            f.write(b"mp4")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(service.subprocess, "run", _fake_run)

    kit = db.kit_from_version(version_id, uid)
    assert kit is not None
    scenes = db.get_scenes(vid)
    job_id = db.create_job(vid, "render")
    service.render_project(vid, scenes, kit, job_id)

    props = captured["props"]
    assert props["logo"] == f"proj-{vid}/1111aaaa.svg"
    assert props["cosmetic"]["background"] == {
        "type": "image",
        "overlayDecor": True,
        "value": f"proj-{vid}/2222bbbb.svg",
    }
    # No systematic copy into public/ root — only the project sandbox holds the assets.
    assert not os.path.isfile(os.path.join(str(public_dir), "1111aaaa.svg"))
    assert not os.path.isfile(os.path.join(str(public_dir), "2222bbbb.svg"))


def test_post_brand_kit_bakes_local_asset_to_storage_key(
    client: TestClient, as_user: Any, tmp_path: Any
) -> None:
    """End-to-end through the API: POST a kit whose logo is a local file → the stored
    asset's `file` is a Storage key and the bytes are durable/re-fetchable (§14)."""
    uid = db.ensure_user("kit-post@test.local")
    as_user(uid)
    logo_path = _write_local(str(tmp_path / "brand.svg"), b"<svg>brand</svg>")

    resp = client.post(
        "/brand-kits",
        json={
            "id": "kit-post",
            "name": "Posted",
            "assets": [{"id": "logo-main", "type": "logo", "primary": True, "file": logo_path}],
        },
    )
    assert resp.status_code == 201

    assets = client.get("/brand-kits/kit-post/assets").json()
    stored_file = next(a["file"] for a in assets if a["id"] == "logo-main")
    assert stored_file.startswith("brand-kits/kit-post/assets/")
    assert get_storage().get(stored_file) == b"<svg>brand</svg>"
