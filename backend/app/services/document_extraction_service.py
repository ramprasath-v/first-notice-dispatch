from typing import Any, Protocol

from google.genai import types
from pydantic import ValidationError

from app.models.claim_document import ClaimDocument, DocumentExtractionResult
from app.services.intake_extraction_service import evidence_part


SUPPORTED_RESUME_DOCUMENT_TYPES = {
    "license_plate_photo",
    "police_report_page",
}


class DocumentExtractionError(RuntimeError):
    """Raised when new evidence cannot be inspected safely."""


class DocumentExtractor(Protocol):
    def extract(
        self, document: ClaimDocument, candidate_requirement: str
    ) -> DocumentExtractionResult: ...


class GeminiDocumentExtractor:
    """Narrow evidence-quality hook; this does not rerun claim intake."""

    def __init__(self, client: Any, model_name: str) -> None:
        self._client = client
        self._model_name = model_name

    def extract(
        self, document: ClaimDocument, candidate_requirement: str
    ) -> DocumentExtractionResult:
        if not _is_supported_document_type(document.document_type):
            raise DocumentExtractionError(
                f"Unsupported resume document type: {document.document_type}"
            )
        if not document.storage_path:
            raise DocumentExtractionError(
                f"Document {document.document_id} has no evidence storage path."
            )

        document_part = evidence_part(
            document.storage_path,
            mime_type=document.content_type,
        )
        prompt = f"""
Inspect this newly submitted claim document for evidence quality only.

Document type: {document.document_type}
Deterministically matched requirement: {candidate_requirement}

Rules:
1. Decide whether the submitted file is usable for the matched requirement.
2. For a license plate photo, the plate must be readable enough to verify the
   vehicle identity.
3. For a police-report page, the page must be readable and relevant to the
   matched missing page.
4. Report factual conflicts only when visible in this file and comparable with
   the supplied requirement. Do not invent values or sources.
5. Do not create document requirements or choose workflow state.
6. Do not decide liability, coverage, fraud, payout, approval, or denial.
7. Return only DocumentExtractionResult.
""".strip()

        try:
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=[
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=prompt), document_part],
                    )
                ],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=DocumentExtractionResult,
                ),
            )
        except Exception as exc:
            raise DocumentExtractionError(
                f"New document quality inspection failed: {exc}"
            ) from exc

        if not response.text:
            raise DocumentExtractionError(
                "Gemini returned an empty new-document inspection response."
            )

        try:
            result = DocumentExtractionResult.model_validate_json(response.text)
        except ValidationError as exc:
            raise DocumentExtractionError(
                f"New document inspection failed validation: {exc}"
            ) from exc

        return result.model_copy(
            update={
                "satisfies_requirement": (
                    candidate_requirement if result.usable else None
                )
            }
        )


def _is_supported_document_type(document_type: str) -> bool:
    return document_type in SUPPORTED_RESUME_DOCUMENT_TYPES or document_type.startswith(
        "police_report_page_"
    )
