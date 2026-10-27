"""S3-compatible object storage for cold archive data.

The adapter talks to MinIO through boto3's S3 API (DEC-0014). ``boto3`` is an
optional ``s3`` extra, so it is imported lazily. Credentials come from the
unified config (``.env``) and are never echoed back in errors: the wrapping
:class:`StorageError` only carries the bucket, the key and a redacted message.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from financecoffer.core.config import Config
from financecoffer.core.errors import ConfigError, StorageError
from financecoffer.core.redaction import redact_text

_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound", "NoSuchBucket"})


@dataclass(frozen=True, slots=True)
class StoredObject:
    """The parts of an object the archive needs to reason about."""

    key: str
    size: int
    sha256: str | None = None
    etag: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class ObjectStorage(Protocol):
    """Minimal surface the archive needs from an object store."""

    def head(self, key: str) -> StoredObject | None: ...

    def put(
        self, key: str, data: bytes, *, content_type: str, sha256: str
    ) -> StoredObject: ...

    def get(self, key: str) -> tuple[bytes, StoredObject]: ...

    def iter_objects(self, prefix: str) -> Iterator[StoredObject]: ...

    def delete(self, key: str) -> None: ...


def endpoint_url(endpoint: str) -> str:
    """Normalise ``host:port`` into an ``http://`` URL for boto3."""

    cleaned = endpoint.strip().rstrip("/")
    if cleaned.startswith(("http://", "https://")):
        return cleaned
    return f"http://{cleaned}"


class S3Storage:
    """boto3 client bound to the configured bucket, created on first use."""

    def __init__(self, config: Config, *, client: Any | None = None) -> None:
        self.bucket = config.minio_bucket
        self._client = client
        self._client_error: type[Exception] | None = None
        self._bucket_ready = client is not None
        if client is None:
            self._client, self._client_error = self._build_client(config)

    @staticmethod
    def _build_client(config: Config) -> tuple[Any, type[Exception]]:
        try:
            import boto3
            from botocore.config import Config as BotoConfig
            from botocore.exceptions import ClientError
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise ConfigError(
                "boto3 is required for archive commands",
                details={"hint": "install financecoffer[s3]"},
            ) from exc

        access = config.minio_access_key
        secret = config.minio_secret_key
        if access is None or secret is None:
            raise ConfigError(
                "MinIO credentials are not configured",
                details={
                    "hint": "set FC_MINIO_ACCESS_KEY / FC_MINIO_SECRET_KEY in .env",
                },
            )
        try:
            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url(config.minio_endpoint),
                aws_access_key_id=access.get_secret_value(),
                aws_secret_access_key=secret.get_secret_value(),
                region_name="us-east-1",
                config=BotoConfig(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                ),
            )
        except Exception as exc:  # pragma: no cover - depends on environment
            raise ConfigError(
                "cannot create the S3 client",
                details={"error": redact_text(str(exc))},
            ) from exc
        return client, ClientError

    # -- helpers ------------------------------------------------------------

    def _is_missing(self, exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return False
        code = str(response.get("Error", {}).get("Code", ""))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return code in _MISSING_CODES or status == 404

    def _wrap(self, action: str, key: str | None, exc: Exception) -> StorageError:
        return StorageError(
            f"MinIO {action} failed",
            details={
                "bucket": self.bucket,
                "key": key,
                "error": redact_text(str(exc).strip()) or type(exc).__name__,
            },
        )

    @staticmethod
    def _from_head(key: str, response: dict[str, Any]) -> StoredObject:
        raw = response.get("Metadata") or {}
        metadata = {str(name).lower(): str(value) for name, value in raw.items()}
        return StoredObject(
            key=key,
            size=int(response.get("ContentLength") or 0),
            sha256=metadata.get("sha256"),
            etag=str(response.get("ETag") or "").strip('"') or None,
            metadata=metadata,
        )

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        assert self._client is not None
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            if not self._is_missing(exc):
                raise self._wrap("head_bucket", None, exc) from exc
            try:
                self._client.create_bucket(Bucket=self.bucket)
            except Exception as create_exc:
                raise self._wrap("create_bucket", None, create_exc) from create_exc
        self._bucket_ready = True

    # -- ObjectStorage ------------------------------------------------------

    def head(self, key: str) -> StoredObject | None:
        assert self._client is not None
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if self._is_missing(exc):
                return None
            raise self._wrap("head_object", key, exc) from exc
        return self._from_head(key, response)

    def put(self, key: str, data: bytes, *, content_type: str, sha256: str) -> StoredObject:
        assert self._client is not None
        self._ensure_bucket()
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                Metadata={"sha256": sha256},
            )
        except Exception as exc:
            raise self._wrap("put_object", key, exc) from exc
        return StoredObject(
            key=key,
            size=len(data),
            sha256=sha256,
            metadata={"sha256": sha256},
        )

    def get(self, key: str) -> tuple[bytes, StoredObject]:
        assert self._client is not None
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"].read()
        except Exception as exc:
            if self._is_missing(exc):
                raise StorageError(
                    "archived object is missing",
                    details={"bucket": self.bucket, "key": key},
                ) from exc
            raise self._wrap("get_object", key, exc) from exc
        return body, self._from_head(key, response)

    def iter_objects(self, prefix: str) -> Iterator[StoredObject]:
        assert self._client is not None
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self.bucket, Prefix=prefix)
            for page in pages:
                for item in page.get("Contents", []):
                    yield StoredObject(
                        key=str(item["Key"]),
                        size=int(item.get("Size") or 0),
                        etag=str(item.get("ETag") or "").strip('"') or None,
                    )
        except Exception as exc:
            raise self._wrap("list_objects_v2", prefix, exc) from exc

    def delete(self, key: str) -> None:
        assert self._client is not None
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise self._wrap("delete_object", key, exc) from exc
