"""MP4 download via Storage (issue #12/#14).

`GET /projects/{pid}/video` resolves `mp4_path` (a Storage key) through the
`Storage` abstraction: LocalStorage (dev) streams the file straight off disk via
`FileResponse` (Range support, no full-file memory load); S3Storage (prod,
PRO-12/14) has no local filesystem representation, so the route redirects (302)
to a CloudFront signed URL instead of proxying bytes (architecture §12).
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from api import db
from api.storage import get_storage
from starlette.testclient import TestClient


def _seed_project(user_id: str) -> str:
    version_id = db.upsert_brand_kit({"id": "kit-dl", "name": "DL"}, user_id)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, user_id, version_id, "v")
    return vid


def _write_cloudfront_private_key(tmp_path: Path) -> str:
    """An ephemeral RSA key pair PEM-encoded to a file — `signed_url()` reads the
    CloudFront private key from a path (`STORAGE_CLOUDFRONT_PRIVATE_KEY_PATH`)."""
    pytest.importorskip(
        "cryptography", reason="cryptography is bundled with the s3 extra (issue #14)"
    )
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "cloudfront_private_key.pem"
    path.write_bytes(pem)
    return str(path)


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
    come back byte-for-byte identical through the download endpoint, streamed via
    `FileResponse` (accept-ranges: bytes) rather than loaded whole into memory."""
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
    assert resp.headers["accept-ranges"] == "bytes"


def test_download_video_supports_range_requests_via_local_storage(
    client: TestClient, as_user: Callable[[str], None]
) -> None:
    """In-browser seeking needs Range support: `FileResponse` (not the old whole-body
    `Response`) must serve a byte range as 206 Partial Content with the right slice."""
    uid = db.ensure_user("range@test.local")
    vid = _seed_project(uid)
    as_user(uid)

    mp4_bytes = bytes(range(256)) * 4  # 1024 arbitrary bytes
    key = f"projects/{vid}/render.mp4"
    get_storage().put(key, mp4_bytes)
    db.set_mp4(vid, key)

    resp = client.get(f"/projects/{vid}/video", headers={"Range": "bytes=10-19"})
    assert resp.status_code == 206
    assert resp.content == mp4_bytes[10:20]
    assert resp.headers["content-range"] == f"bytes 10-19/{len(mp4_bytes)}"


def test_download_video_redirects_to_signed_cloudfront_url_via_s3_storage(
    client: TestClient,
    as_user: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """S3-backed video: the API never proxies bytes for this backend — it 302s to a
    short-lived CloudFront signed URL for the same key (issue #14 / architecture §12).
    The bucket itself never appears in the redirect: only the CDN in front of it does.
    """
    boto3 = pytest.importorskip(
        "boto3", reason="boto3 is an optional dependency (`uv sync --extra s3`)"
    )
    moto = pytest.importorskip("moto", reason="moto (dev dependency) mocks S3")

    with moto.mock_aws():
        bucket = "polymnia-video-download-test"
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
        monkeypatch.setenv("STORAGE_BACKEND", "s3")
        monkeypatch.setenv("STORAGE_S3_BUCKET", bucket)
        monkeypatch.setenv("STORAGE_S3_REGION", "us-east-1")
        monkeypatch.setenv("STORAGE_CLOUDFRONT_DOMAIN", "d123abc.cloudfront.net")
        monkeypatch.setenv("STORAGE_CLOUDFRONT_KEY_PAIR_ID", "APKAEXAMPLEKEYPAIR")
        monkeypatch.setenv(
            "STORAGE_CLOUDFRONT_PRIVATE_KEY_PATH", _write_cloudfront_private_key(tmp_path)
        )

        uid = db.ensure_user("s3-dl@test.local")
        vid = _seed_project(uid)
        as_user(uid)

        key = f"projects/{vid}/render.mp4"
        get_storage().put(key, b"mp4-bytes")
        db.set_mp4(vid, key)

        resp = client.get(f"/projects/{vid}/video", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("https://d123abc.cloudfront.net/")
        assert bucket not in location  # bucket stays private, never exposed in the URL
        assert "render.mp4" in location
        assert "Key-Pair-Id=APKAEXAMPLEKEYPAIR" in location
        assert "Signature=" in location


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


def test_download_video_404_when_key_recorded_but_missing_from_s3_storage(
    client: TestClient, as_user: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same contract as the LocalStorage case above, against the S3 backend: a
    dangling `mp4_path` (recorded in the DB but never actually uploaded, or since
    deleted from the bucket) must 404 — the whole point of the Storage abstraction
    is that callers (and callers of the API) never have to care which backend is
    behind a key, so this must not regress into a 307 redirect to a signed URL for
    an object that doesn't exist."""
    boto3 = pytest.importorskip(
        "boto3", reason="boto3 is an optional dependency (`uv sync --extra s3`)"
    )
    moto = pytest.importorskip("moto", reason="moto (dev dependency) mocks S3")

    with moto.mock_aws():
        bucket = "polymnia-video-download-dangling-test"
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
        monkeypatch.setenv("STORAGE_BACKEND", "s3")
        monkeypatch.setenv("STORAGE_S3_BUCKET", bucket)
        monkeypatch.setenv("STORAGE_S3_REGION", "us-east-1")

        uid = db.ensure_user("s3-dangling@test.local")
        vid = _seed_project(uid)
        as_user(uid)

        db.set_mp4(vid, f"projects/{vid}/render.mp4")  # never actually put() into storage

        resp = client.get(f"/projects/{vid}/video", follow_redirects=False)
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
