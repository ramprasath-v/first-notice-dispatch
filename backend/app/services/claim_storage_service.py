import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, BinaryIO

from google.cloud import storage
from pydantic import BaseModel, Field


SUPPORTED_CONTENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "application/pdf",
        "audio/mpeg",
        "audio/wav",
        "audio/x-wav",
        "audio/mp4",
    }
)
MAX_EVIDENCE_SIZE_BYTES = 15 * 1024 * 1024


class ClaimStorageConfigurationError(RuntimeError):
    """Raised when the evidence bucket is not configured."""


class ClaimStorageValidationError(ValueError):
    """Raised when an evidence upload is unsafe or unsupported."""


class ClaimStorageError(RuntimeError):
    """Raised when Cloud Storage cannot persist an evidence object."""


@dataclass(frozen=True)
class GcsSettings:
    google_cloud_project: str
    claim_bucket: str

    @classmethod
    def from_env(cls) -> "GcsSettings":
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        bucket = os.getenv("GCS_CLAIM_BUCKET", "").strip()
        missing = []
        if not project:
            missing.append("GOOGLE_CLOUD_PROJECT")
        if not bucket:
            missing.append("GCS_CLAIM_BUCKET")
        if missing:
            raise ClaimStorageConfigurationError(
                "Missing required Cloud Storage environment variable(s): "
                + ", ".join(missing)
            )
        return cls(project, bucket)


class ValidatedUpload(BaseModel):
    filename: str
    content_type: str
    size_bytes: int = Field(gt=0)


class StoredClaimObject(ValidatedUpload):
    bucket: str
    object_name: str
    gs_uri: str
    document_id: str


class ClaimStorageService:
    def __init__(
        self,
        settings: GcsSettings,
        *,
        client: Any | None = None,
        max_size_bytes: int = MAX_EVIDENCE_SIZE_BYTES,
    ) -> None:
        self._settings = settings
        self._client = client or storage.Client(project=settings.google_cloud_project)
        self._bucket = self._client.bucket(settings.claim_bucket)
        self._max_size_bytes = max_size_bytes

    def validate_upload(
        self,
        file_obj: BinaryIO,
        *,
        filename: str | None,
        content_type: str | None,
    ) -> ValidatedUpload:
        normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
        if normalized_type not in SUPPORTED_CONTENT_TYPES:
            raise ClaimStorageValidationError(
                f"Unsupported evidence content type: {normalized_type or 'missing'}"
            )
        safe_name = sanitize_filename(filename or "evidence")
        try:
            file_obj.seek(0, 2)
            size = file_obj.tell()
            file_obj.seek(0)
        except Exception as exc:
            raise ClaimStorageValidationError(
                "Could not determine the uploaded evidence size."
            ) from exc
        if size <= 0:
            raise ClaimStorageValidationError("Evidence files must not be empty.")
        if size > self._max_size_bytes:
            raise ClaimStorageValidationError(
                f"Evidence file exceeds the {self._max_size_bytes}-byte limit."
            )
        return ValidatedUpload(
            filename=safe_name,
            content_type=normalized_type,
            size_bytes=size,
        )

    def upload_claim_document(
        self,
        *,
        claim_id: str,
        document_id: str,
        file_obj: BinaryIO,
        upload: ValidatedUpload,
    ) -> StoredClaimObject:
        object_name = (
            f"claims/{claim_id}/documents/{document_id}/{upload.filename}"
        )
        blob = self._bucket.blob(object_name)
        try:
            blob.upload_from_file(
                file_obj,
                rewind=True,
                size=upload.size_bytes,
                content_type=upload.content_type,
                if_generation_match=0,
            )
        except Exception as exc:
            raise ClaimStorageError(
                f"Could not upload evidence for claim {claim_id}: {exc}"
            ) from exc
        return StoredClaimObject(
            **upload.model_dump(),
            bucket=self._settings.claim_bucket,
            object_name=object_name,
            gs_uri=f"gs://{self._settings.claim_bucket}/{object_name}",
            document_id=document_id,
        )


def sanitize_filename(filename: str) -> str:
    basename = re.split(r"[/\\]+", filename)[-1]
    normalized = unicodedata.normalize("NFKD", basename).encode(
        "ascii", "ignore"
    ).decode("ascii")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", normalized).strip("._-")
    if not safe:
        safe = "evidence"
    return safe[:120]


def infer_document_type(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "damage_evidence"
    if content_type == "application/pdf":
        return "police_report"
    if content_type.startswith("audio/"):
        return "voice_note"
    raise ClaimStorageValidationError(
        f"Cannot infer a document type for {content_type}."
    )
