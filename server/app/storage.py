from __future__ import annotations

import base64
import binascii
import hashlib
import heapq
import hmac
import logging
import os
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from mimetypes import guess_type
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote, urlencode

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

StorageProvider = Literal["cos", "local", "fake"]

logger = logging.getLogger(__name__)

_LEGACY_MEDIA_RETENTION_RULE_IDS = {
    "expire-project-media-180d",
    "expire-generation-results-180d",
}
_LEGACY_MEDIA_RETENTION_PREFIXES = {"projects/", "generation-results/"}
_LEGACY_MEDIA_RETENTION_DAYS = 180


def _is_legacy_media_retention_rule(rule: dict[str, Any]) -> bool:
    if rule.get("ID") in _LEGACY_MEDIA_RETENTION_RULE_IDS:
        return True
    lifecycle_filter = rule.get("Filter")
    expiration = rule.get("Expiration")
    return (
        isinstance(lifecycle_filter, dict)
        and lifecycle_filter.get("Prefix") in _LEGACY_MEDIA_RETENTION_PREFIXES
        and isinstance(expiration, dict)
        and expiration.get("Days") == _LEGACY_MEDIA_RETENTION_DAYS
    )


class StoragePermissionError(PermissionError):
    """Raised when the business layer denies object access."""


class SourceUrlExpired(RuntimeError):
    """Raised when a temporary provider URL is no longer safe to archive."""


class StorageBackendUnavailable(RuntimeError):
    """Raised for configured cloud operations that require a real SDK/client."""


@dataclass(frozen=True)
class StorageObjectRef:
    provider: StorageProvider
    bucket: str
    key: str


@dataclass(frozen=True)
class UploadIntent:
    method: Literal["PUT"]
    url: str
    key: str
    headers: dict[str, str]
    expires_at: datetime


@dataclass(frozen=True)
class DownloadIntent:
    method: Literal["GET"]
    url: str
    key: str
    expires_at: datetime


@dataclass(frozen=True)
class StoredObject:
    provider: str
    bucket: str
    key: str
    uri: str
    size: int
    content_type: str
    sha256: str
    updated_at: datetime


@dataclass(frozen=True)
class ArchiveSource:
    url: str
    expires_at: datetime
    content: bytes
    content_type: str


@dataclass(frozen=True)
class StorageAuditEvent:
    action: str
    status: str
    object_key: str
    actor_id: str | None
    at: datetime
    metadata: dict[str, str] = field(default_factory=dict)


class StorageAdapter(Protocol):
    provider: str
    bucket: str

    @property
    def cache_namespace(self) -> str: ...

    @property
    def audit_events(self) -> list[StorageAuditEvent]: ...

    def create_upload_intent(
        self,
        key: str,
        *,
        content_type: str,
        expires_in: timedelta,
        size_bytes: int | None = None,
    ) -> UploadIntent: ...

    def create_download_intent(
        self,
        key: str,
        *,
        expires_in: timedelta,
        can_read: bool,
    ) -> DownloadIntent: ...

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject: ...

    def put_file(self, key: str, path: Path, *, content_type: str) -> StoredObject: ...

    def get_object(self, key: str) -> bytes: ...

    def iter_object(
        self,
        key: str,
        *,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]: ...

    def copy_object(self, source_key: str, destination_key: str) -> StoredObject: ...

    def head_object(self, key: str) -> StoredObject | None: ...

    def check_readiness(self) -> None: ...

    def list_upload_keys(
        self, asset_id: str, *, after: str = "", limit: int = 100
    ) -> list[str]: ...

    def archive_result(
        self,
        source: ArchiveSource,
        *,
        destination_key: str,
        actor_id: str | None = None,
    ) -> StoredObject: ...

    def delete_object(self, key: str, *, actor_id: str | None = None) -> None: ...


@dataclass(frozen=True)
class CloudStorageConfig:
    provider: Literal["cos"]
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str | None = None
    key_prefix: str = ""
    timeout_seconds: int = 10


def create_storage_adapter(config: CloudStorageConfig) -> StorageAdapter:
    return CloudStorageAdapter(config)


STORAGE_ROOT_ENV = "VIDEO_REPLICA_STORAGE_ROOT"


def local_download_signature(
    key: str,
    expires_at: str,
    *,
    user_id: str,
    asset_id: str,
    session_epoch: str,
    secret: str,
) -> str:
    """Sign a revocable user/asset grant with a domain-separated sub-key."""
    try:
        master_key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise StorageBackendUnavailable("settings key is not valid base64") from exc
    signing_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"video-replica:url-signing:v1",
    ).derive(master_key)
    message = f"{key}|{expires_at}|{user_id}|{asset_id}|{session_epoch}".encode()
    return hmac.new(signing_key, message, hashlib.sha256).hexdigest()


def local_storage_root() -> Path:
    """Resolve the local storage root; fail fast when the env var is missing."""
    root = os.environ.get(STORAGE_ROOT_ENV)
    if not root:
        raise StorageBackendUnavailable(f"{STORAGE_ROOT_ENV} is required for local storage")
    return Path(root)


def create_local_storage_from_environment() -> StorageAdapter:
    """Build the local-filesystem storage adapter for local/dev runs.

    `active_storage_provider="local"` lets a machine without cloud storage
    credentials (e.g. a macOS dev box) run the full upload/archive flow.
    """
    return LocalStorageAdapter(root=local_storage_root())


def cloud_storage_config_from_settings(
    provider: str,
    config: dict[str, str],
) -> CloudStorageConfig:
    if provider != "cos":
        raise ValueError(f"unsupported cloud storage provider: {provider}")
    try:
        return CloudStorageConfig(
            provider="cos",
            bucket=config["bucket"],
            access_key_id=config["access_key_id"],
            secret_access_key=config["secret_access_key"],
            region=config.get("region"),
        )
    except KeyError as exc:
        raise ValueError("cloud storage settings are incomplete") from exc


def storage_object_ref_from_uri(uri: str) -> StorageObjectRef:
    provider, separator, remainder = uri.partition("://")
    if not separator:
        raise ValueError(f"invalid storage uri: {uri}")
    bucket, slash, key = remainder.partition("/")
    if provider not in {"cos", "local", "fake"} or not bucket or not slash or not key:
        raise ValueError(f"invalid storage uri: {uri}")
    return StorageObjectRef(
        provider=cast(StorageProvider, provider),
        bucket=bucket,
        key=_safe_key(key),
    )


def require_storage_match(storage: StorageAdapter, reference: StorageObjectRef) -> None:
    if storage.provider != reference.provider or storage.bucket != reference.bucket:
        raise StorageBackendUnavailable(
            f"storage adapter does not match {reference.provider}://{reference.bucket}"
        )


class _BaseStorageAdapter:
    def __init__(self, *, provider: str, bucket: str, key_prefix: str = "") -> None:
        self.provider = provider
        self.bucket = bucket
        self._key_prefix = key_prefix.strip("/")
        self._audit_events: list[StorageAuditEvent] = []

    @property
    def cache_namespace(self) -> str:
        return f"{self.provider}://{self.bucket}/{self._key_prefix}"

    @property
    def audit_events(self) -> list[StorageAuditEvent]:
        return self._audit_events

    def _upload_prefix(self, asset_id: str, after: str, limit: int) -> str:
        if not asset_id or "/" in asset_id or "\\" in asset_id or not 1 <= limit <= 1000:
            raise ValueError("invalid upload inventory bounds")
        prefix = self._object_key(f"verified-uploads/{asset_id}/entry").removesuffix("entry")
        if after and not after.startswith(prefix):
            raise ValueError("inventory cursor is outside the upload prefix")
        return prefix

    def create_upload_intent(
        self,
        key: str,
        *,
        content_type: str,
        expires_in: timedelta,
        size_bytes: int | None = None,
    ) -> UploadIntent:
        object_key = self._object_key(key)
        expires_at = _expires_at(expires_in)
        return UploadIntent(
            method="PUT",
            url=self._signed_url("PUT", object_key, expires_at),
            key=object_key,
            headers={"content-type": content_type},
            expires_at=expires_at,
        )

    def create_download_intent(
        self,
        key: str,
        *,
        expires_in: timedelta,
        can_read: bool,
    ) -> DownloadIntent:
        object_key = self._object_key(key)
        if not can_read:
            self._audit("download_intent.denied", "denied", object_key, None)
            raise StoragePermissionError(f"download denied for {object_key}")
        expires_at = _expires_at(expires_in)
        return DownloadIntent(
            method="GET",
            url=self._signed_url("GET", object_key, expires_at),
            key=object_key,
            expires_at=expires_at,
        )

    def archive_result(
        self,
        source: ArchiveSource,
        *,
        destination_key: str,
        actor_id: str | None = None,
    ) -> StoredObject:
        object_key = self._object_key(destination_key)
        if source.expires_at <= datetime.now(UTC):
            self._audit("archive_result.failed", "expired_source", object_key, actor_id)
            raise SourceUrlExpired(source.url)
        archived = self.put_object(object_key, source.content, content_type=source.content_type)
        self._audit("archive_result.succeeded", "succeeded", object_key, actor_id)
        return archived

    def delete_object(self, key: str, *, actor_id: str | None = None) -> None:
        object_key = self._object_key(key)
        self._delete_object(object_key)
        self._audit("object.deleted", "succeeded", object_key, actor_id)

    def _object_key(self, key: str) -> str:
        safe_key = _safe_key(key)
        if not self._key_prefix:
            return safe_key
        if safe_key.startswith(f"{self._key_prefix}/") or safe_key == self._key_prefix:
            return safe_key
        return f"{self._key_prefix}/{safe_key}"

    def _signed_url(self, method: str, key: str, expires_at: datetime) -> str:
        raise NotImplementedError

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        raise NotImplementedError

    def put_file(self, key: str, path: Path, *, content_type: str) -> StoredObject:
        raise NotImplementedError

    def get_object(self, key: str) -> bytes:
        raise NotImplementedError

    def iter_object(
        self,
        key: str,
        *,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        raise NotImplementedError

    def copy_object(self, source_key: str, destination_key: str) -> StoredObject:
        raise NotImplementedError

    def head_object(self, key: str) -> StoredObject | None:
        raise NotImplementedError

    def check_readiness(self) -> None:
        """Confirm that the configured storage namespace is reachable.

        Local/fake adapters can prove this with a harmless metadata lookup.
        Cloud adapters override this with a bucket-level operation so a
        missing bucket cannot be mistaken for a missing probe object.
        """
        self.head_object("health/readiness-probe")

    def _delete_object(self, key: str) -> None:
        raise NotImplementedError

    def _audit(
        self,
        action: str,
        status: str,
        object_key: str,
        actor_id: str | None,
    ) -> None:
        self._audit_events.append(
            StorageAuditEvent(
                action=action,
                status=status,
                object_key=object_key,
                actor_id=actor_id,
                at=datetime.now(UTC),
                metadata={"provider": self.provider, "bucket": self.bucket},
            )
        )


class FakeStorageAdapter(_BaseStorageAdapter):
    def put_file(self, key: str, path: Path, *, content_type: str) -> StoredObject:
        return self.put_object(key, path.read_bytes(), content_type=content_type)

    def list_upload_keys(self, asset_id: str, *, after: str = "", limit: int = 100) -> list[str]:
        prefix = self._upload_prefix(asset_id, after, limit)
        return heapq.nsmallest(
            limit, (key for key in self._objects if key.startswith(prefix) and key > after)
        )

    def __init__(
        self,
        *,
        provider: Literal["cos", "fake"],
        bucket: str,
        key_prefix: str = "",
    ) -> None:
        super().__init__(provider=provider, bucket=bucket, key_prefix=key_prefix)
        self._objects: dict[str, tuple[bytes, StoredObject]] = {}

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        object_key = self._object_key(key)
        stored = _stored_object(
            provider=self.provider,
            bucket=self.bucket,
            key=object_key,
            content=content,
            content_type=content_type,
        )
        self._objects[object_key] = (content, stored)
        return stored

    def get_object(self, key: str) -> bytes:
        object_key = self._object_key(key)
        return self._objects[object_key][0]

    def iter_object(
        self,
        key: str,
        *,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        content = self.get_object(key)
        stop = len(content) if end is None else min(len(content), end + 1)
        for offset in range(start, stop, chunk_size):
            yield content[offset : min(stop, offset + chunk_size)]

    def copy_object(self, source_key: str, destination_key: str) -> StoredObject:
        source_object_key = self._object_key(source_key)
        destination_object_key = self._object_key(destination_key)
        try:
            content, source = self._objects[source_object_key]
        except KeyError as exc:
            raise StorageBackendUnavailable("source object is unavailable") from exc
        return self.put_object(
            destination_object_key,
            content,
            content_type=source.content_type,
        )

    def head_object(self, key: str) -> StoredObject | None:
        object_key = self._object_key(key)
        stored = self._objects.get(object_key)
        return None if stored is None else stored[1]

    def _signed_url(self, method: str, key: str, expires_at: datetime) -> str:
        query = urlencode(
            {
                "x-storage-provider": self.provider,
                "x-method": method,
                "x-expires": str(_timestamp(expires_at)),
            }
        )
        return f"{self.provider}://{self.bucket}/{quote(key)}?{query}"

    def _delete_object(self, key: str) -> None:
        self._objects.pop(key, None)


class LocalStorageAdapter(_BaseStorageAdapter):
    @property
    def cache_namespace(self) -> str:
        return f"{super().cache_namespace}:{self.root}"

    def put_file(self, key: str, path: Path, *, content_type: str) -> StoredObject:
        object_key = self._object_key(key)
        destination = self._path_for(object_key)
        temporary: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, suffix=".part", delete=False
            ) as output:
                temporary = Path(output.name)
                with path.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            stored = _stored_object_from_path(
                provider=self.provider,
                bucket=self.bucket,
                key=object_key,
                path=temporary,
                content_type=content_type,
            )
            temporary.replace(destination)
            return stored
        except OSError as exc:
            raise StorageBackendUnavailable("local object upload failed") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def list_upload_keys(self, asset_id: str, *, after: str = "", limit: int = 100) -> list[str]:
        prefix = self._upload_prefix(asset_id, after, limit)
        base = self._path_for(prefix)

        def keys(directory: Path) -> Iterator[str]:
            if not directory.exists():
                return
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        yield from keys(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        key = Path(entry.path).relative_to(self.root).as_posix()
                        if key > after:
                            yield key

        try:
            return heapq.nsmallest(limit, keys(base))
        except OSError as exc:
            raise StorageBackendUnavailable("local upload inventory failed") from exc

    def __init__(
        self,
        *,
        root: Path,
        provider: Literal["local"] = "local",
        bucket: str = "local-private",
        key_prefix: str = "",
    ) -> None:
        super().__init__(provider=provider, bucket=bucket, key_prefix=key_prefix)
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        object_key = self._object_key(key)
        path = self._path_for(object_key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        except OSError as exc:
            logger.warning(
                "local object write failed for key %s: %s",
                object_key,
                type(exc).__name__,
            )
            raise StorageBackendUnavailable("local object upload failed") from exc
        return _stored_object(
            provider=self.provider,
            bucket=self.bucket,
            key=object_key,
            content=content,
            content_type=content_type,
        )

    def get_object(self, key: str) -> bytes:
        return self._path_for(self._object_key(key)).read_bytes()

    def iter_object(
        self,
        key: str,
        *,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        path = self._path_for(self._object_key(key))
        remaining = None if end is None else end - start + 1
        with path.open("rb") as source:
            source.seek(start)
            while remaining is None or remaining > 0:
                size = chunk_size if remaining is None else min(chunk_size, remaining)
                chunk = source.read(size)
                if not chunk:
                    return
                yield chunk
                if remaining is not None:
                    remaining -= len(chunk)

    def copy_object(self, source_key: str, destination_key: str) -> StoredObject:
        source_object_key = self._object_key(source_key)
        destination_object_key = self._object_key(destination_key)
        source_path = self._path_for(source_object_key)
        destination_path = self._path_for(destination_object_key)
        try:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, destination_path)
        except OSError as exc:
            logger.warning("local object copy failed: %s", type(exc).__name__)
            raise StorageBackendUnavailable("local object copy failed") from exc
        return _stored_object_from_path(
            provider=self.provider,
            bucket=self.bucket,
            key=destination_object_key,
            path=destination_path,
            content_type=guess_type(source_path.name)[0] or "application/octet-stream",
        )

    def head_object(self, key: str) -> StoredObject | None:
        object_key = self._object_key(key)
        path = self._path_for(object_key)
        if not path.exists():
            return None
        return _stored_object_from_path(
            provider=self.provider,
            bucket=self.bucket,
            key=object_key,
            path=path,
            content_type=guess_type(path.name)[0] or "application/octet-stream",
        )

    def _signed_url(self, method: str, key: str, expires_at: datetime) -> str:
        query = urlencode({"method": method, "expires": str(_timestamp(expires_at))})
        return f"local://{self.bucket}/{quote(key)}?{query}"

    def _delete_object(self, key: str) -> None:
        path = self._path_for(key)
        if path.exists():
            path.unlink()

    def _path_for(self, key: str) -> Path:
        path = (self.root / key).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"unsafe object key: {key}") from exc
        return path


class CloudStorageAdapter(_BaseStorageAdapter):
    def put_file(self, key: str, path: Path, *, content_type: str) -> StoredObject:
        """Upload a seekable file with bounded memory and verified hash metadata.

        The caller owns this temporary file until the synchronous SDK finishes,
        including retries. A single COS PUT is atomic; no partial object is published.
        """
        object_key = self._object_key(key)
        stored = _stored_object_from_path(
            provider=self.provider,
            bucket=self.bucket,
            key=object_key,
            path=path,
            content_type=content_type,
        )
        try:
            with path.open("rb") as source:
                self._client.put_object(
                    Bucket=self.bucket,
                    Key=object_key,
                    Body=source,
                    ContentType=content_type,
                    Metadata={"x-cos-meta-sha256": stored.sha256},
                )
        except Exception as exc:
            raise StorageBackendUnavailable("cloud object upload failed") from exc
        return stored

    def list_upload_keys(self, asset_id: str, *, after: str = "", limit: int = 100) -> list[str]:
        prefix = self._upload_prefix(asset_id, after, limit)
        try:
            response = self._client.list_objects(
                Bucket=self.bucket, Prefix=prefix, Marker=after, MaxKeys=limit
            )
            contents = response.get("Contents", [])
            if not isinstance(contents, list) or len(contents) > limit:
                raise ValueError("invalid object inventory")
            keys = [str(item["Key"]) for item in contents]
            if keys != sorted(set(keys)) or any(
                not key.startswith(prefix) or key <= after for key in keys
            ):
                raise ValueError("object inventory escaped upload prefix")
            return keys
        except Exception as exc:
            raise StorageBackendUnavailable("cloud upload inventory failed") from exc

    def __init__(self, config: CloudStorageConfig, *, client: Any | None = None) -> None:
        super().__init__(
            provider=config.provider,
            bucket=config.bucket,
            key_prefix=config.key_prefix,
        )
        self._access_key_id = config.access_key_id
        self._secret_access_key = config.secret_access_key
        self._region = config.region or ""
        self._timeout_seconds = config.timeout_seconds
        self._client = client if client is not None else self._create_provider_client()

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        object_key = self._object_key(key)
        digest = hashlib.sha256(content).hexdigest()
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=content,
                ContentType=content_type,
                Metadata={"x-cos-meta-sha256": digest},
            )
        except Exception as exc:
            raise StorageBackendUnavailable("cloud object upload failed") from exc
        return _stored_object(
            provider=self.provider,
            bucket=self.bucket,
            key=object_key,
            content=content,
            content_type=content_type,
        )

    def create_upload_intent(
        self,
        key: str,
        *,
        content_type: str,
        expires_in: timedelta,
        size_bytes: int | None = None,
    ) -> UploadIntent:
        object_key = self._object_key(key)
        expires_at = _expires_at(expires_in)
        headers = {"Content-Type": content_type}
        signed_headers = dict(headers)
        if size_bytes is not None:
            if size_bytes < 0:
                raise ValueError("upload size must be non-negative")
            # Browsers set Content-Length from the Blob; do not ask JavaScript
            # to set this forbidden header, but require it in the COS signature.
            signed_headers["Content-Length"] = str(size_bytes)
        try:
            url = self._client.get_presigned_url(
                Bucket=self.bucket,
                Key=object_key,
                Method="PUT",
                Expired=_seconds(expires_in),
                # qcloud_cos 会在签名时把 Authorization 原地写入传入的 Headers
                # 字典；传副本避免签名泄漏到下发给客户端的上传头里。
                Headers=signed_headers,
                SignHost=True,
            )
        except Exception as exc:
            raise StorageBackendUnavailable("cloud upload URL signing failed") from exc
        return UploadIntent(
            method="PUT",
            url=str(url),
            key=object_key,
            headers=headers,
            expires_at=expires_at,
        )

    def create_download_intent(
        self,
        key: str,
        *,
        expires_in: timedelta,
        can_read: bool,
    ) -> DownloadIntent:
        object_key = self._object_key(key)
        if not can_read:
            return super().create_download_intent(object_key, expires_in=expires_in, can_read=False)
        expires_at = _expires_at(expires_in)
        try:
            url = self._client.get_presigned_download_url(
                Bucket=self.bucket,
                Key=object_key,
                Expired=_seconds(expires_in),
                SignHost=True,
            )
        except Exception as exc:
            raise StorageBackendUnavailable("cloud download URL signing failed") from exc
        return DownloadIntent(method="GET", url=str(url), key=object_key, expires_at=expires_at)

    def get_object(self, key: str) -> bytes:
        object_key = self._object_key(key)
        stream = None
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=object_key)
            stream = response["Body"].get_raw_stream()
            return bytes(stream.read())
        except Exception as exc:
            raise StorageBackendUnavailable("cloud object download failed") from exc
        finally:
            if stream is not None:
                _close_provider_stream(stream)

    def iter_object(
        self,
        key: str,
        *,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        object_key = self._object_key(key)
        request: dict[str, object] = {"Bucket": self.bucket, "Key": object_key}
        if start or end is not None:
            request["Range"] = f"bytes={start}-{'' if end is None else end}"
        stream = None
        try:
            response = self._client.get_object(**request)
            stream = response["Body"].get_raw_stream()
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    return
                yield bytes(chunk)
        except Exception as exc:
            raise StorageBackendUnavailable("cloud object stream failed") from exc
        finally:
            if stream is not None:
                _close_provider_stream(stream)

    def copy_object(self, source_key: str, destination_key: str) -> StoredObject:
        source_object_key = self._object_key(source_key)
        destination_object_key = self._object_key(destination_key)
        try:
            self._client.copy_object(
                Bucket=self.bucket,
                Key=destination_object_key,
                CopySource={
                    "Bucket": self.bucket,
                    "Key": source_object_key,
                    "Region": self._region,
                },
            )
        except Exception as exc:
            raise StorageBackendUnavailable("cloud object copy failed") from exc
        copied = self.head_object(destination_object_key)
        if copied is None:
            raise StorageBackendUnavailable("copied cloud object is unavailable")
        return copied

    def head_object(self, key: str) -> StoredObject | None:
        object_key = self._object_key(key)
        try:
            headers = dict(self._client.head_object(Bucket=self.bucket, Key=object_key))
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise StorageBackendUnavailable("cloud object metadata lookup failed") from exc
        return StoredObject(
            provider=self.provider,
            bucket=self.bucket,
            key=object_key,
            uri=f"{self.provider}://{self.bucket}/{object_key}",
            size=int(_header(headers, "content-length", "0")),
            content_type=str(_header(headers, "content-type", "application/octet-stream")),
            sha256=str(_header(headers, "x-cos-meta-sha256", "")),
            updated_at=datetime.now(UTC),
        )

    def check_readiness(self) -> None:
        """Verify the bucket itself exists and the credentials can reach it."""
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            raise StorageBackendUnavailable("cloud bucket readiness check failed") from exc

    def archive_result(
        self,
        source: ArchiveSource,
        *,
        destination_key: str,
        actor_id: str | None = None,
    ) -> StoredObject:
        return super().archive_result(source, destination_key=destination_key, actor_id=actor_id)

    def remove_lifecycle_rules(self, *, actor_id: str | None = None) -> None:
        """只移除历史媒体 180 天过期规则，保留桶内其他管理规则。"""
        try:
            response = self._client.get_bucket_lifecycle(Bucket=self.bucket)
        except Exception as exc:
            error_code = getattr(exc, "get_error_code", lambda: None)()
            if error_code in {"NoSuchLifecycle", "NoSuchLifecycleConfiguration"}:
                response = {"Rule": []}
            else:
                raise StorageBackendUnavailable("cloud lifecycle rule read failed") from exc

        raw_rules = response.get("Rule", []) if isinstance(response, dict) else None
        if isinstance(raw_rules, dict):
            rules = [raw_rules]
        elif isinstance(raw_rules, list) and all(isinstance(rule, dict) for rule in raw_rules):
            rules = raw_rules
        else:
            raise StorageBackendUnavailable("cloud lifecycle response is invalid")
        retained = [rule for rule in rules if not _is_legacy_media_retention_rule(rule)]
        if len(retained) != len(rules):
            try:
                if retained:
                    self._client.put_bucket_lifecycle(
                        Bucket=self.bucket,
                        LifecycleConfiguration={"Rule": retained},
                    )
                else:
                    self._client.delete_bucket_lifecycle(Bucket=self.bucket)
            except Exception as exc:
                raise StorageBackendUnavailable("cloud lifecycle rule removal failed") from exc
        self._audit(
            "lifecycle_rules.removed",
            "succeeded",
            f"{self.bucket}/lifecycle-rules",
            actor_id,
        )

    def _signed_url(self, method: str, key: str, expires_at: datetime) -> str:
        seconds_remaining = max(1, _timestamp(expires_at) - _timestamp(datetime.now(UTC)))
        expires_in = timedelta(seconds=seconds_remaining)
        if method == "GET":
            return self.create_download_intent(key, expires_in=expires_in, can_read=True).url
        if method == "PUT":
            return self.create_upload_intent(
                key,
                content_type="application/octet-stream",
                expires_in=expires_in,
            ).url
        raise StorageBackendUnavailable(f"cloud URL signing does not support {method}")

    def _delete_object(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise StorageBackendUnavailable("cloud object deletion failed") from exc

    def _create_provider_client(self) -> Any:
        try:
            from qcloud_cos import CosConfig, CosS3Client  # type: ignore[import-untyped]

            return CosS3Client(
                CosConfig(
                    Region=self._region,
                    SecretId=self._access_key_id,
                    SecretKey=self._secret_access_key,
                    Scheme="https",
                    Timeout=self._timeout_seconds,
                )
            )
        except Exception as exc:
            raise StorageBackendUnavailable("cloud storage client initialization failed") from exc


def _safe_key(key: str) -> str:
    # Object keys are identifiers, not filesystem paths. Reject ambiguous
    # spellings instead of normalizing them after an authorization decision.
    # This keeps the authorized key identical to the key written on disk.
    if (
        key.startswith("/")
        or key in {"", ".", ".."}
        or "\\" in key
        or "\x00" in key
        or any(part in {"", ".", ".."} for part in key.split("/"))
    ):
        raise ValueError(f"unsafe object key: {key}")
    return key


def _expires_at(expires_in: timedelta) -> datetime:
    if expires_in <= timedelta(seconds=0):
        raise ValueError("expires_in must be positive")
    return datetime.now(UTC) + expires_in


def _timestamp(value: datetime) -> int:
    return int(value.timestamp())


def _seconds(value: timedelta) -> int:
    return max(1, int(value.total_seconds()))


def _is_not_found(exc: Exception) -> bool:
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    error_text = str(exc)
    return (
        status == 404
        or "NoSuchKey" in error_text
        or "NoSuchResource" in error_text
        or "NotFound" in error_text
    )


def _header(headers: dict[str, Any], name: str, default: str) -> Any:
    return next((value for key, value in headers.items() if key.lower() == name), default)


def _close_provider_stream(stream: Any) -> None:
    # COS exposes requests.Response.raw (urllib3.HTTPResponse). Closing a
    # partially consumed socket does not return its pool slot by itself.
    try:
        stream.close()
    finally:
        release = getattr(stream, "release_conn", None)
        if release is not None:
            release()


class UploadedObjectSizeMismatch(ValueError):
    pass


def read_uploaded_object(
    storage: StorageAdapter, key: str, *, expected_size: int, max_bytes: int
) -> bytes:
    if expected_size < 0 or expected_size > max_bytes:
        raise UploadedObjectSizeMismatch("declared upload size is outside the allowed budget")
    source = storage.iter_object(key)
    content = bytearray()
    try:
        for chunk in source:
            if len(content) + len(chunk) > expected_size or len(content) + len(chunk) > max_bytes:
                raise UploadedObjectSizeMismatch("object changed while reading the upload")
            content.extend(chunk)
        if len(content) != expected_size:
            raise UploadedObjectSizeMismatch("object ended before the declared upload size")
    finally:
        close = getattr(source, "close", None)
        if close is not None:
            close()
    return bytes(content)


def verified_upload_object_key(*, asset_id: str, digest: str, source_key: str) -> str:
    """The canonical destination key for a verified upload.

    Exposed because deduplication needs the key **before** the bytes are
    written: a caller that wants to reuse an existing copy must know where this
    copy would have landed. Duplicating the format string in two places would
    let them drift apart silently, and a drifted key means a duplicate object.
    """
    return f"verified-uploads/{asset_id}/{digest}/{Path(source_key).name}"


def store_verified_upload(
    storage: StorageAdapter, *, asset_id: str, source_key: str, content: bytes, content_type: str
) -> StoredObject:
    digest = hashlib.sha256(content).hexdigest()
    key = verified_upload_object_key(asset_id=asset_id, digest=digest, source_key=source_key)
    # No upload-intent code ever signs this namespace. Every consumer persists
    # this exact byte snapshot after validating the same bytes.
    return storage.put_object(key, content, content_type=content_type)


def _stored_object(
    *,
    provider: str,
    bucket: str,
    key: str,
    content: bytes,
    content_type: str,
) -> StoredObject:
    digest = hashlib.sha256(content).hexdigest()
    return StoredObject(
        provider=provider,
        bucket=bucket,
        key=key,
        uri=f"{provider}://{bucket}/{key}",
        size=len(content),
        content_type=content_type,
        sha256=digest,
        updated_at=datetime.now(UTC),
    )


def _stored_object_from_path(
    *,
    provider: str,
    bucket: str,
    key: str,
    path: Path,
    content_type: str,
) -> StoredObject:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        if content_type == "audio/mpeg" and source.read(12)[4:8] == b"ftyp":
            content_type = "audio/mp4"
        source.seek(0)
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return StoredObject(
        provider=provider,
        bucket=bucket,
        key=key,
        uri=f"{provider}://{bucket}/{key}",
        size=path.stat().st_size,
        content_type=content_type,
        sha256=digest.hexdigest(),
        updated_at=datetime.now(UTC),
    )
