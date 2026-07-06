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
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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


cryptography = pytest.importorskip(
    "cryptography",
    reason="cryptography is bundled with the s3 extra (CloudFront signing, issue #14)",
)


@pytest.fixture
def cloudfront_private_key_path(tmp_path: Path) -> str:
    """An ephemeral RSA key pair, PEM-encoded to a file — `signed_url()` reads the
    CloudFront private key from a path (`STORAGE_CLOUDFRONT_PRIVATE_KEY_PATH`), never
    from an inline env var value, so tests need a real file on disk."""
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


def test_s3_storage_signed_url_without_cloudfront_config_raises_config_error(
    s3_bucket: str,
) -> None:
    """No silent fallback to an S3 presigned URL (issue #14 — the bucket must stay
    private, only reachable via CloudFront): missing CloudFront config is a clear,
    actionable error instead."""
    storage = S3Storage(s3_bucket)  # no cloudfront_* kwargs
    storage.put("k", b"data")
    with pytest.raises(StorageConfigError, match="CloudFront"):
        storage.signed_url("k", ttl_seconds=120)


def test_s3_storage_signed_url_with_invalid_pem_raises_config_error_not_a_traceback(
    s3_bucket: str, tmp_path: Path
) -> None:
    """A corrupt/non-PEM private key file must fail loud with a typed, actionable
    `StorageConfigError` naming the path — never let cryptography's own ValueError
    (or a missing-file OSError) escape unhandled and surface as a 500 out of the
    download route."""
    bad_key_path = tmp_path / "not_a_key.pem"
    bad_key_path.write_bytes(b"this is not a PEM private key")
    storage = S3Storage(
        s3_bucket,
        cloudfront_domain="d123abc.cloudfront.net",
        cloudfront_key_pair_id="APKAEXAMPLEKEYPAIR",
        cloudfront_private_key_path=str(bad_key_path),
    )
    storage.put("k", b"data")
    with pytest.raises(StorageConfigError, match="PEM"):
        storage.signed_url("k", ttl_seconds=120)


@pytest.mark.parametrize(
    ("missing_kwarg", "expected_env_name"),
    [
        ("cloudfront_domain", "STORAGE_CLOUDFRONT_DOMAIN"),
        ("cloudfront_key_pair_id", "STORAGE_CLOUDFRONT_KEY_PAIR_ID"),
        ("cloudfront_private_key_path", "STORAGE_CLOUDFRONT_PRIVATE_KEY_PATH"),
    ],
)
def test_s3_storage_signed_url_with_partial_cloudfront_config_names_the_missing_var(
    s3_bucket: str,
    cloudfront_private_key_path: str,
    missing_kwarg: str,
    expected_env_name: str,
) -> None:
    """Even when only *one* of the three CloudFront settings is missing (not all
    three), signed_url() must still fail loud and name that exact setting — a
    partially-configured backend must never fall back to a plain S3 URL for the
    settings it does have."""
    kwargs: dict[str, str] = {
        "cloudfront_domain": "d123abc.cloudfront.net",
        "cloudfront_key_pair_id": "APKAEXAMPLEKEYPAIR",
        "cloudfront_private_key_path": cloudfront_private_key_path,
    }
    del kwargs[missing_kwarg]
    storage = S3Storage(s3_bucket, **kwargs)  # type: ignore[arg-type]
    storage.put("k", b"data")
    with pytest.raises(StorageConfigError, match=expected_env_name):
        storage.signed_url("k", ttl_seconds=120)


def test_s3_storage_signed_url_expires_reflects_the_configured_ttl(
    s3_bucket: str, cloudfront_private_key_path: str
) -> None:
    """Acceptance criterion 2 (issue #14): the URL must actually expire after its
    TTL — not merely carry an `Expires` param, but one whose value is derived from
    `ttl_seconds`, not a constant. Decode it and check it lines up with now + ttl."""
    storage = S3Storage(
        s3_bucket,
        cloudfront_domain="d123abc.cloudfront.net",
        cloudfront_key_pair_id="APKAEXAMPLEKEYPAIR",
        cloudfront_private_key_path=cloudfront_private_key_path,
    )
    storage.put("k", b"data")

    before = int(time.time())
    ttl_seconds = 120
    signed = storage.signed_url("k", ttl_seconds=ttl_seconds)
    after = int(time.time())

    query = parse_qs(urlparse(signed).query)
    expires = int(query["Expires"][0])
    # Loose bound: the call itself takes negligible time, but avoid flakiness.
    assert before + ttl_seconds <= expires <= after + ttl_seconds + 2
    # And the expiry is genuinely in the future relative to when it was issued —
    # a URL that were already expired on arrival would defeat the whole point.
    assert expires > before


def test_s3_storage_signed_url_ttl_is_not_hardcoded(
    s3_bucket: str, cloudfront_private_key_path: str
) -> None:
    """Two different `ttl_seconds` must produce two different `Expires` values —
    proves the TTL parameter is actually threaded through to the signature, not a
    fixed expiry baked into the signer."""
    storage = S3Storage(
        s3_bucket,
        cloudfront_domain="d123abc.cloudfront.net",
        cloudfront_key_pair_id="APKAEXAMPLEKEYPAIR",
        cloudfront_private_key_path=cloudfront_private_key_path,
    )
    storage.put("k", b"data")

    short = storage.signed_url("k", ttl_seconds=60)
    long = storage.signed_url("k", ttl_seconds=3600)

    short_expires = int(parse_qs(urlparse(short).query)["Expires"][0])
    long_expires = int(parse_qs(urlparse(long).query)["Expires"][0])
    assert long_expires > short_expires
    assert long_expires - short_expires == pytest.approx(3600 - 60, abs=2)


def test_s3_storage_signed_url_is_a_cloudfront_url_with_ttl_and_key_pair(
    s3_bucket: str, cloudfront_private_key_path: str
) -> None:
    storage = S3Storage(
        s3_bucket,
        cloudfront_domain="d123abc.cloudfront.net",
        cloudfront_key_pair_id="APKAEXAMPLEKEYPAIR",
        cloudfront_private_key_path=cloudfront_private_key_path,
    )
    storage.put("projects/p1/render.mp4", b"data")
    signed = storage.signed_url("projects/p1/render.mp4", ttl_seconds=120)
    assert signed.startswith("https://d123abc.cloudfront.net/projects/p1/render.mp4?")
    assert "Key-Pair-Id=APKAEXAMPLEKEYPAIR" in signed
    assert "Signature=" in signed
    assert "Expires=" in signed  # canned-policy expiry, derived from ttl_seconds


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
