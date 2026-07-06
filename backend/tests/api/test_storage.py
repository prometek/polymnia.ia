"""Storage abstraction (issue #12 / PRO-11).

`Storage` (put/get/exists/url/signed_url) is the single interface every binary
artefact (scene audio WAV, rendered MP4) goes through, backed by `LocalStorage`
(dev) or `S3Storage` (prod), selected by `STORAGE_BACKEND`. Acceptance criteria:
 1. Dev runs 100% local via LocalStorage, no regression.
 2. The same calling code works against S3 (below, via moto).
 3. No binary filesystem access left outside the abstraction in migrated call-sites
    (covered end-to-end in tests/api/test_jobs.py's step-progression tests).
"""

import os
from pathlib import Path

import pytest
from api.storage import (
    LocalStorage,
    S3Storage,
    Storage,
    StorageBackendUnavailableError,
    StorageConfigError,
    StorageInvalidKeyError,
    StorageKeyNotFoundError,
    get_storage,
)

# --- LocalStorage (unit) -----------------------------------------------------


def test_local_storage_put_get_round_trip(tmp_path: object) -> None:
    storage = LocalStorage(str(tmp_path))
    storage.put("projects/p1/audio/scene-0.wav", b"some-wav-bytes")
    assert storage.get("projects/p1/audio/scene-0.wav") == b"some-wav-bytes"


def test_local_storage_put_creates_nested_directories(tmp_path: object) -> None:
    """`key` is a POSIX-style relative path with slashes — put() must create every
    intermediate directory, callers never mkdir themselves."""
    storage = LocalStorage(str(tmp_path))
    storage.put("a/b/c/file.bin", b"x")
    assert os.path.isfile(os.path.join(str(tmp_path), "a", "b", "c", "file.bin"))


def test_local_storage_put_overwrites_existing_key(tmp_path: object) -> None:
    storage = LocalStorage(str(tmp_path))
    storage.put("k", b"first")
    storage.put("k", b"second")
    assert storage.get("k") == b"second"


def test_local_storage_exists_true_after_put(tmp_path: object) -> None:
    storage = LocalStorage(str(tmp_path))
    assert storage.exists("missing") is False
    storage.put("present", b"data")
    assert storage.exists("present") is True


def test_local_storage_get_missing_key_raises_not_found(tmp_path: object) -> None:
    storage = LocalStorage(str(tmp_path))
    with pytest.raises(StorageKeyNotFoundError) as exc_info:
        storage.get("nope")
    assert exc_info.value.key == "nope"


def test_local_storage_put_rejects_key_escaping_root_via_parent_segments(
    tmp_path: Path,
) -> None:
    """A `../` key must never write outside the storage root — refused with a typed
    error rather than silently escaping (path traversal)."""
    storage = LocalStorage(str(tmp_path))
    with pytest.raises(StorageInvalidKeyError) as exc_info:
        storage.put("../escape.bin", b"malicious")
    assert exc_info.value.key == "../escape.bin"
    assert not (tmp_path.parent / "escape.bin").exists()


def test_local_storage_get_rejects_key_escaping_root_via_parent_segments(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(str(tmp_path))
    with pytest.raises(StorageInvalidKeyError):
        storage.get("../../etc/passwd")


def test_local_storage_url_is_a_file_uri(tmp_path: object) -> None:
    storage = LocalStorage(str(tmp_path))
    storage.put("k", b"data")
    url = storage.url("k")
    assert url.startswith("file://")
    assert os.path.join(str(tmp_path), "k") in url


def test_local_storage_signed_url_is_directly_fetchable_locally(tmp_path: object) -> None:
    """No auth boundary to enforce on local disk — signed_url() still returns
    something a caller can resolve back to the same bytes (interface parity)."""
    storage = LocalStorage(str(tmp_path))
    storage.put("k", b"payload")
    signed = storage.signed_url("k", ttl_seconds=60)
    assert signed == storage.url("k")


def test_local_storage_local_path_returns_the_backing_file(tmp_path: Path) -> None:
    """Lets a caller (the MP4 download route) stream the file directly, e.g. via
    `FileResponse`, instead of loading it whole into memory via `get()`."""
    storage = LocalStorage(str(tmp_path))
    storage.put("k", b"payload")
    assert storage.local_path("k") == os.path.join(str(tmp_path), "k")


def test_local_storage_local_path_missing_key_raises_not_found(tmp_path: Path) -> None:
    storage = LocalStorage(str(tmp_path))
    with pytest.raises(StorageKeyNotFoundError):
        storage.local_path("nope")


# --- get_storage() backend selection -----------------------------------------


def test_get_storage_defaults_to_local(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path))
    storage = get_storage()
    assert isinstance(storage, LocalStorage)


def test_get_storage_local_round_trips_through_the_public_interface(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path))
    storage: Storage = get_storage()
    storage.put("k", b"v")
    assert storage.get("k") == b"v"


def test_get_storage_unknown_backend_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "azure")
    with pytest.raises(StorageConfigError):
        get_storage()


def test_get_storage_s3_without_bucket_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.delenv("STORAGE_S3_BUCKET", raising=False)
    with pytest.raises(StorageConfigError):
        get_storage()


# --- S3Storage (integration, via moto) ---------------------------------------

boto3 = pytest.importorskip(
    "boto3", reason="boto3 is an optional dependency (`uv sync --extra s3`) for STORAGE_BACKEND=s3"
)
moto = pytest.importorskip(
    "moto", reason="moto (dev dependency) mocks S3 for the integration test below"
)


@pytest.fixture
def s3_bucket() -> object:
    """A moto-mocked S3 bucket — no real AWS credentials or network involved."""
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="polymnia-test-bucket")
        yield "polymnia-test-bucket"


def test_s3_storage_put_get_round_trip(s3_bucket: str) -> None:
    """Same calling code as LocalStorage (acceptance criterion 2): put() then get()
    returns the exact bytes, against a real (mocked) S3 API."""
    storage: Storage = S3Storage(s3_bucket)
    storage.put("projects/p1/render.mp4", b"mp4-bytes")
    assert storage.get("projects/p1/render.mp4") == b"mp4-bytes"


def test_s3_storage_exists(s3_bucket: str) -> None:
    storage: Storage = S3Storage(s3_bucket)
    assert storage.exists("missing") is False
    storage.put("present", b"data")
    assert storage.exists("present") is True


def test_s3_storage_get_missing_key_raises_not_found(s3_bucket: str) -> None:
    storage: Storage = S3Storage(s3_bucket)
    with pytest.raises(StorageKeyNotFoundError) as exc_info:
        storage.get("nope")
    assert exc_info.value.key == "nope"


def test_s3_storage_local_path_is_always_none(s3_bucket: str) -> None:
    """S3 objects have no local filesystem representation — callers (the MP4
    download route) must branch on this to fall back to signed_url() instead."""
    storage: Storage = S3Storage(s3_bucket)
    storage.put("present", b"data")
    assert storage.local_path("present") is None
    assert storage.local_path("missing") is None


def test_s3_storage_url_is_an_s3_uri(s3_bucket: str) -> None:
    storage = S3Storage(s3_bucket)
    assert storage.url("k") == f"s3://{s3_bucket}/k"


def test_s3_storage_signed_url_is_a_fetchable_https_url_with_ttl(s3_bucket: str) -> None:
    storage = S3Storage(s3_bucket)
    storage.put("k", b"data")
    signed = storage.signed_url("k", ttl_seconds=120)
    assert signed.startswith("https://")
    assert "k" in signed
    # A presigned GET carries its expiry in the query string (SigV4).
    assert "Expires" in signed or "X-Amz-Expires" in signed


def test_get_storage_selects_s3_backend_via_env(
    monkeypatch: pytest.MonkeyPatch, s3_bucket: str
) -> None:
    """The exact call-site path (`get_storage()` reading `STORAGE_BACKEND`) picks S3
    when configured — proves callers never branch on backend themselves."""
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("STORAGE_S3_BUCKET", s3_bucket)
    monkeypatch.setenv("STORAGE_S3_REGION", "us-east-1")
    storage = get_storage()
    assert isinstance(storage, S3Storage)
    storage.put("k", b"via-get-storage")
    assert storage.get("k") == b"via-get-storage"


def test_s3_storage_unavailable_without_boto3_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If boto3 genuinely isn't importable, construction fails with a typed,
    actionable error — not an opaque ImportError bubbling out of the constructor."""
    import api.storage as storage_module

    real_import = __import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "boto3":
            raise ImportError("simulated: boto3 not installed")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(StorageBackendUnavailableError):
        storage_module.S3Storage("some-bucket")
