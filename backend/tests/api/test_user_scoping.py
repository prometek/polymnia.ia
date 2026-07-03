"""Multi-tenant read isolation (issue #2 / PRO-02).

Every video/kit read must be scoped to the current user. User A must never see
or read user B's resources; a cross-user GET returns 404 (no existence leak).
"""

from collections.abc import Callable

from api import db
from starlette.testclient import TestClient


def _seed_project(user_id: str, kit_id: str, name: str) -> str:
    """Create a brand kit (+version) and a video owned by `user_id`; return video id."""
    version_id = db.upsert_brand_kit({"id": kit_id, "name": kit_id}, user_id)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, user_id, version_id, name)
    return vid


def test_get_other_users_project_returns_404(
    client: TestClient, as_user: Callable[[str], None]
) -> None:
    uid_a = db.ensure_user("a@test.local")
    uid_b = db.ensure_user("b@test.local")
    vid_a = _seed_project(uid_a, "kit-a", "A's video")

    as_user(uid_a)
    assert client.get(f"/projects/{vid_a}").status_code == 200  # owner reads it

    as_user(uid_b)
    assert client.get(f"/projects/{vid_a}").status_code == 404  # stranger → 404


def test_list_projects_only_returns_own(client: TestClient, as_user: Callable[[str], None]) -> None:
    uid_a = db.ensure_user("a@test.local")
    uid_b = db.ensure_user("b@test.local")
    vid_a = _seed_project(uid_a, "kit-a", "A's video")
    vid_b = _seed_project(uid_b, "kit-b", "B's video")

    as_user(uid_a)
    ids = {p["id"] for p in client.get("/projects").json()}
    assert ids == {vid_a}
    assert vid_b not in ids


def test_list_brand_kits_only_returns_own(
    client: TestClient, as_user: Callable[[str], None]
) -> None:
    uid_a = db.ensure_user("a@test.local")
    uid_b = db.ensure_user("b@test.local")
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid_a)
    db.upsert_brand_kit({"id": "kit-b", "name": "B"}, uid_b)

    as_user(uid_a)
    ids = {k["id"] for k in client.get("/brand-kits").json()}
    assert ids == {"kit-a"}


def test_brand_kit_assets_scoped_by_user(
    client: TestClient, as_user: Callable[[str], None]
) -> None:
    uid_a = db.ensure_user("a@test.local")
    uid_b = db.ensure_user("b@test.local")
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid_a)

    as_user(uid_b)
    # B cannot resolve A's kit → 404 (same as a non-existent kit, no existence leak)
    assert client.get("/brand-kits/kit-a/assets").status_code == 404


def test_new_project_rejects_other_users_kit(
    client: TestClient, as_user: Callable[[str], None]
) -> None:
    uid_a = db.ensure_user("a@test.local")
    uid_b = db.ensure_user("b@test.local")
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid_a)

    as_user(uid_b)
    resp = client.post("/projects", json={"input_text": "hello", "brand_kit_id": "kit-a"})
    assert resp.status_code == 404  # B can't build on A's kit
