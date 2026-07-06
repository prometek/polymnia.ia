"""MP4 download via Storage (issue #12).

`GET /projects/{pid}/video` used to serve a raw filesystem path with
`FileResponse`; it now resolves `mp4_path` (a Storage key) through the `Storage`
abstraction so the same endpoint works unchanged against LocalStorage (dev) or
S3Storage (prod, PRO-12/14) — this file exercises the endpoint directly against
LocalStorage (dev backend), independently of the full render pipeline.
"""

from collections.abc import Callable

from api import db
from api.storage import get_storage
from starlette.testclient import TestClient


def _seed_project(user_id: str) -> str:
    version_id = db.upsert_brand_kit({"id": "kit-dl", "name": "DL"}, user_id)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, user_id, version_id, "v")
    return vid


def test_download_video_404_before_any_render(
    client: TestClient, as_user: Callable[[str], None]
) -> None:
    uid = db.ensure_user("a@test.local")
    vid = _seed_project(uid)
    as_user(uid)

    resp = client.get(f"/projects/{vid}/video")
    assert resp.status_code == 404


def test_download_video_returns_byte_identical_content_via_local_storage(
    client: TestClient, as_user: Callable[[str], None]
) -> None:
    """The rendered MP4 round-trips: bytes written to Storage under the video's key
    come back byte-for-byte identical through the download endpoint."""
    uid = db.ensure_user("a@test.local")
    vid = _seed_project(uid)
    as_user(uid)

    mp4_bytes = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4  # arbitrary binary payload
    key = f"projects/{vid}/render.mp4"
    get_storage().put(key, mp4_bytes)
    db.set_mp4(vid, key)

    resp = client.get(f"/projects/{vid}/video")
    assert resp.status_code == 200
    assert resp.content == mp4_bytes
    assert resp.headers["content-type"] == "video/mp4"
    assert f'filename="{vid}.mp4"' in resp.headers["content-disposition"]


def test_download_video_404_when_key_recorded_but_missing_from_storage(
    client: TestClient, as_user: Callable[[str], None]
) -> None:
    """A dangling `mp4_path` (recorded in the DB but absent from the backing store)
    must 404, not raise an unhandled StorageKeyNotFoundError."""
    uid = db.ensure_user("a@test.local")
    vid = _seed_project(uid)
    as_user(uid)

    db.set_mp4(vid, f"projects/{vid}/render.mp4")  # never actually put() into storage

    resp = client.get(f"/projects/{vid}/video")
    assert resp.status_code == 404


def test_download_video_404_for_other_users_project(
    client: TestClient, as_user: Callable[[str], None]
) -> None:
    owner_id = db.ensure_user("owner@test.local")
    vid = _seed_project(owner_id)
    get_storage().put(f"projects/{vid}/render.mp4", b"secret-bytes")
    db.set_mp4(vid, f"projects/{vid}/render.mp4")

    requester_id = db.ensure_user("requester@test.local")
    as_user(requester_id)

    resp = client.get(f"/projects/{vid}/video")
    assert resp.status_code == 404
