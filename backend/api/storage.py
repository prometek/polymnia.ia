#!/usr/bin/env python3
"""Storage abstraction (issue #12): a single interface for every binary artefact the
app persists (scene audio WAV, rendered MP4) so business logic never touches the
filesystem directly for these — `LocalStorage` (dev, under `out/storage/`) and
`S3Storage` (prod, private bucket) implement the same interface, selected by the
`STORAGE_BACKEND` env var. Dev behaviour is unchanged: `STORAGE_BACKEND` defaults to
`local`, and this ticket does not migrate any existing data to S3.

Keys are POSIX-style relative paths (e.g. "projects/<pid>/render.mp4") — backends
map them to their own addressing (a local file path, an S3 object key).
"""

import os
from abc import ABC, abstractmethod

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


class LocalStorage(Storage):
    """Dev backend: binaries live under a root directory on local disk (`out/storage`
    by default) — no credentials, no network. There's no HTTP server in front of this
    tree, so `url`/`signed_url` return a `file://` URI for reference only; callers that
    need the bytes (e.g. the MP4 download endpoint) must use `get()`.
    """

    def __init__(self, root: str) -> None:
        self._root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self._root, *key.split("/"))

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


class S3Storage(Storage):
    """Prod backend: binaries live in a private S3 bucket via boto3.

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
        signed: str = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )
        return signed


def get_storage() -> Storage:
    """Build the Storage backend selected by `STORAGE_BACKEND` (default: `local`).

    Not cached: construction is cheap for both backends (a `mkdir` for local, a boto3
    client for S3), and re-reading the env each call keeps this easy to override in
    tests without process-wide state to reset.
    """
    backend = os.environ.get("STORAGE_BACKEND", "local")
    if backend == "local":
        root = os.environ.get("STORAGE_LOCAL_ROOT", _DEFAULT_LOCAL_ROOT)
        return LocalStorage(root)
    if backend == "s3":
        try:
            bucket = os.environ["STORAGE_S3_BUCKET"]
        except KeyError as exc:
            raise StorageConfigError(
                "STORAGE_BACKEND=s3 requires STORAGE_S3_BUCKET to be set."
            ) from exc
        return S3Storage(
            bucket,
            region_name=os.environ.get("STORAGE_S3_REGION"),
            endpoint_url=os.environ.get("STORAGE_S3_ENDPOINT_URL"),  # e.g. moto/LocalStack in tests
        )
    raise StorageConfigError(f"unknown STORAGE_BACKEND={backend!r}; expected 'local' or 's3'")
