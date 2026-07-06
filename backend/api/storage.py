#!/usr/bin/env python3
"""Storage abstraction (issue #12): a single interface for every binary artefact the
app persists (scene audio WAV, rendered MP4) so business logic never touches the
filesystem directly for these — `LocalStorage` (dev, under `out/storage/`) and
`S3Storage` (prod, private bucket) implement the same interface, selected by the
`STORAGE_BACKEND` env var. Dev behaviour is unchanged: `STORAGE_BACKEND` defaults to
`local`, and this ticket does not migrate any existing data to S3.

Keys are POSIX-style relative paths (e.g. "projects/<pid>/render.mp4") — backends
map them to their own addressing (a local file path, an S3 object key).

`S3Storage.signed_url()` serves through **CloudFront**, not an S3 presigned URL
(issue #14 / architecture §12): the bucket stays private behind the distribution
(OAC/OAI), so a presigned S3 URL would still expose the bucket's own endpoint. See
`STORAGE_CLOUDFRONT_*` env vars below.
"""

import os
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from functools import cache

# backend/api/storage.py -> backend/
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_LOCAL_ROOT = os.path.join(_BACKEND_ROOT, "out", "storage")


class StorageError(Exception):
    """Base class for all Storage errors."""


class StorageConfigError(StorageError):
    """`STORAGE_BACKEND` (or a backend-specific env var it requires) is invalid."""


class StorageBackendUnavailableError(StorageError):
    """The selected backend's runtime dependency isn't installed (e.g. boto3 for S3)."""


class StorageKeyNotFoundError(StorageError):
    """No data stored at the requested key."""

    def __init__(self, key: str) -> None:
        super().__init__(f"no data stored at key {key!r}")
        self.key = key


class StorageInvalidKeyError(StorageError):
    """`key` resolves outside the backend's storage root (e.g. via `../` path
    segments) — refused rather than silently clamped, since a caller-controlled
    key that escapes the root is always a bug or an attack, never legitimate."""

    def __init__(self, key: str) -> None:
        super().__init__(f"key {key!r} resolves outside the storage root")
        self.key = key


class Storage(ABC):
    """Backend-agnostic binary store. All methods use the same POSIX-style `key` for
    a given artefact regardless of backend, so call-sites never branch on backend.
    """

    @abstractmethod
    def put(self, key: str, data: bytes) -> None:
        """Write `data` at `key`, creating it or overwriting an existing value."""

    @abstractmethod
    def get(self, key: str) -> bytes:
        """Read the bytes stored at `key`. Raises `StorageKeyNotFoundError` if absent."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Whether `key` currently has data stored."""

    @abstractmethod
    def url(self, key: str) -> str:
        """A durable reference to `key` (not necessarily directly fetchable — e.g. the
        local backend returns a `file://` URI, not an HTTP URL)."""

    @abstractmethod
    def signed_url(self, key: str, ttl_seconds: int) -> str:
        """A time-limited, directly-fetchable URL for `key` (CDN/S3 delivery)."""

    @abstractmethod
    def local_path(self, key: str) -> str | None:
        """The on-disk path backing `key`, for backends that hold bytes as local
        files — lets a caller stream large payloads directly (e.g. via
        `starlette.responses.FileResponse`, which adds HTTP Range support for
        free) instead of loading them fully into memory via `get()`.

        Returns `None` for backends with no local filesystem representation
        (e.g. S3): callers must fall back to `signed_url()` there instead of
        proxying bytes through this process (architecture §12 — private
        bucket, CDN/signed-URL delivery). Raises `StorageKeyNotFoundError` if
        `key` is absent *and* that can be checked without a network round trip
        (local only — `S3Storage.local_path` always returns `None` without
        checking existence, callers proxy through `get()`/`signed_url()`)."""


class LocalStorage(Storage):
    """Dev backend: binaries live under a root directory on local disk (`out/storage`
    by default) — no credentials, no network. There's no HTTP server in front of this
    tree, so `url`/`signed_url` return a `file://` URI for reference only; callers that
    need the bytes (e.g. the MP4 download endpoint) must use `get()`.
    """

    def __init__(self, root: str) -> None:
        self._root = os.path.normpath(root)
        os.makedirs(self._root, exist_ok=True)

    def _path(self, key: str) -> str:
        """Resolve `key` to a path under `_root`. `key` is meant to be a POSIX-style
        relative path with no `..` segments, but nothing upstream enforces that —
        normalize and check the result stays under `_root` rather than trust it, so
        a key with `../` segments can't ever read/write outside the storage root."""
        candidate = os.path.normpath(os.path.join(self._root, *key.split("/")))
        if candidate != self._root and not candidate.startswith(self._root + os.sep):
            raise StorageInvalidKeyError(key)
        return candidate

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not os.path.isfile(path):
            raise StorageKeyNotFoundError(key)
        with open(path, "rb") as f:
            return f.read()

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._path(key))

    def url(self, key: str) -> str:
        return f"file://{self._path(key)}"

    def signed_url(self, key: str, ttl_seconds: int) -> str:
        # No auth boundary to enforce locally; ttl is meaningless here but kept for
        # interface parity with S3Storage.
        return self.url(key)

    def local_path(self, key: str) -> str:
        path = self._path(key)
        if not os.path.isfile(path):
            raise StorageKeyNotFoundError(key)
        return path


class S3Storage(Storage):
    """Prod backend: binaries live in a private S3 bucket via boto3, served to
    clients through a CloudFront distribution in front of it (issue #14).

    boto3 is an optional dependency (`uv sync --extra s3`) so local dev/CI never needs
    it installed unless `STORAGE_BACKEND=s3` is actually selected — the import happens
    here, at construction time, not at module import time.
    """

    def __init__(
        self,
        bucket: str,
        *,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        cloudfront_domain: str | None = None,
        cloudfront_key_pair_id: str | None = None,
        cloudfront_private_key_path: str | None = None,
    ) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise StorageBackendUnavailableError(
                "STORAGE_BACKEND=s3 requires boto3 — install it with `uv sync --extra s3`."
            ) from exc

        self._bucket = bucket
        # boto3 has no stubs installed by default (it's an optional dependency, see
        # pyproject.toml) — `ignore_missing_imports` makes this whole client `Any`
        # under mypy, same tolerance the project already applies to psycopg/urllib.
        self._client = boto3.client("s3", region_name=region_name, endpoint_url=endpoint_url)
        # CloudFront config is only required by signed_url() (put/get/exists never
        # need it) — kept optional here so a caller that only writes/reads objects
        # (e.g. the render worker, issue #13) doesn't need CloudFront credentials
        # configured at all; signed_url() below fails loudly if it's missing.
        self._cloudfront_domain = cloudfront_domain
        self._cloudfront_key_pair_id = cloudfront_key_pair_id
        self._cloudfront_private_key_path = cloudfront_private_key_path

    def put(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except self._client.exceptions.NoSuchKey as exc:
            raise StorageKeyNotFoundError(key) from exc
        body: bytes = response["Body"].read()
        return body

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except self._client.exceptions.ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise  # anything else (permissions, throttling, ...) must not look like "missing"
        return True

    def url(self, key: str) -> str:
        return f"s3://{self._bucket}/{key}"

    def signed_url(self, key: str, ttl_seconds: int) -> str:
        """A CloudFront signed URL for `key` (issue #14): the bucket stays private,
        the distribution is the only thing allowed to read it (OAC/OAI) — a plain
        S3 presigned URL would defeat that by pointing straight at the bucket.

        Requires `cloudfront_domain`/`cloudfront_key_pair_id`/`cloudfront_private_key_path`
        (see `STORAGE_CLOUDFRONT_*` env vars in `get_storage()`) — raises
        `StorageConfigError` naming whichever is missing rather than silently
        falling back to an S3 presigned URL.
        """
        missing = [
            env_name
            for env_name, value in (
                ("STORAGE_CLOUDFRONT_DOMAIN", self._cloudfront_domain),
                ("STORAGE_CLOUDFRONT_KEY_PAIR_ID", self._cloudfront_key_pair_id),
                ("STORAGE_CLOUDFRONT_PRIVATE_KEY_PATH", self._cloudfront_private_key_path),
            )
            if not value
        ]
        if missing:
            raise StorageConfigError(
                "STORAGE_BACKEND=s3 requires CloudFront signed-URL config; missing: "
                + ", ".join(missing)
            )
        # cryptography is declared under the `s3` extra alongside boto3 (pyproject.toml)
        # — imported lazily, at call time, for the same reason boto3 is imported lazily
        # in __init__: callers that only put()/get()/exists() (e.g. the render worker)
        # never need it.
        try:
            from botocore.signers import CloudFrontSigner
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
        except ImportError as exc:
            raise StorageBackendUnavailableError(
                "CloudFront signed URLs require 'cryptography' — install it with "
                "`uv sync --extra s3`."
            ) from exc

        assert self._cloudfront_private_key_path is not None  # narrowed by the check above
        with open(self._cloudfront_private_key_path, "rb") as f:
            private_key = load_pem_private_key(f.read(), password=None)
        if not isinstance(private_key, RSAPrivateKey):
            raise StorageConfigError(
                f"STORAGE_CLOUDFRONT_PRIVATE_KEY_PATH ({self._cloudfront_private_key_path!r}) "
                "must hold an RSA private key — CloudFront canned-policy signing requires it."
            )

        def rsa_signer(message: bytes) -> bytes:
            # CloudFront's signing protocol mandates SHA1 with PKCS1v15 — not a choice
            # made here, it's the only scheme CloudFront's viewer-request verification
            # accepts for signed URLs/cookies.
            return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())

        signer = CloudFrontSigner(self._cloudfront_key_pair_id, rsa_signer)
        resource_url = f"https://{self._cloudfront_domain}/{key}"
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        signed: str = signer.generate_presigned_url(resource_url, date_less_than=expires_at)
        return signed

    def local_path(self, key: str) -> str | None:
        # S3 objects have no local filesystem representation — callers must use
        # signed_url()/get() instead; no existence check here (that's a network
        # round trip callers should only pay for once, via get()/signed_url()).
        return None


@cache
def _cached_local_storage(root: str) -> LocalStorage:
    return LocalStorage(root)


@cache
def _cached_s3_storage(
    bucket: str,
    region_name: str | None,
    endpoint_url: str | None,
    cloudfront_domain: str | None,
    cloudfront_key_pair_id: str | None,
    cloudfront_private_key_path: str | None,
) -> S3Storage:
    return S3Storage(
        bucket,
        region_name=region_name,
        endpoint_url=endpoint_url,
        cloudfront_domain=cloudfront_domain,
        cloudfront_key_pair_id=cloudfront_key_pair_id,
        cloudfront_private_key_path=cloudfront_private_key_path,
    )


def get_storage() -> Storage:
    """Build the Storage backend selected by `STORAGE_BACKEND` (default: `local`).

    The env is still read on every call (cheap, and keeps this easy to override per
    test via `monkeypatch`), but the backend instance itself is memoized per resolved
    config (`functools.cache`, keyed on the actual root/bucket/region/endpoint values)
    so a per-request call doesn't re-`mkdir` (local) or rebuild a boto3 client (S3)
    every time. A different config (e.g. a test's own `STORAGE_LOCAL_ROOT` under
    `tmp_path`) is simply a different cache key — no process-wide state to reset
    between tests.

    CloudFront config (`STORAGE_CLOUDFRONT_DOMAIN`/`_KEY_PAIR_ID`/`_PRIVATE_KEY_PATH`,
    issue #14) is read here too but left optional: only `S3Storage.signed_url()`
    needs it, so a process that only `put()`s/`get()`s objects (e.g. the render
    worker) can run against `STORAGE_BACKEND=s3` without it configured at all.
    """
    backend = os.environ.get("STORAGE_BACKEND", "local")
    if backend == "local":
        root = os.environ.get("STORAGE_LOCAL_ROOT", _DEFAULT_LOCAL_ROOT)
        return _cached_local_storage(root)
    if backend == "s3":
        try:
            bucket = os.environ["STORAGE_S3_BUCKET"]
        except KeyError as exc:
            raise StorageConfigError(
                "STORAGE_BACKEND=s3 requires STORAGE_S3_BUCKET to be set."
            ) from exc
        return _cached_s3_storage(
            bucket,
            os.environ.get("STORAGE_S3_REGION"),
            os.environ.get("STORAGE_S3_ENDPOINT_URL"),  # e.g. moto/LocalStack in tests
            os.environ.get("STORAGE_CLOUDFRONT_DOMAIN"),
            os.environ.get("STORAGE_CLOUDFRONT_KEY_PAIR_ID"),
            os.environ.get("STORAGE_CLOUDFRONT_PRIVATE_KEY_PATH"),
        )
    raise StorageConfigError(f"unknown STORAGE_BACKEND={backend!r}; expected 'local' or 's3'")
