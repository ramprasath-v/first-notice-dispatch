import argparse
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from app.config import Settings
from app.models.claim_document import ClaimDocument
from app.services.claim_review_service import ClaimReviewService
from app.services.document_extraction_service import GeminiDocumentExtractor
from app.tools.firestore_repository import (
    FirestoreClaimRepository,
    generate_document_id,
)
from app.tools.gemini_client import create_gemini_client
from app.workflows.claim_resume_workflow import ClaimResumeWorkflow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resume an awaiting FirstNotice claim with new evidence."
    )
    parser.add_argument("--claim-id", required=True)
    parser.add_argument(
        "--document-type",
        required=True,
        help="license_plate_photo or police_report_page_N",
    )
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--document-id", default=None)
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()
    file_path = args.file.expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Evidence file was not found: {file_path}")

    settings = Settings.from_env()
    client = create_gemini_client(settings)
    repository = FirestoreClaimRepository.from_default_credentials(
        settings.google_cloud_project,
        settings.firestore_database,
    )
    workflow = ClaimResumeWorkflow(
        repository=repository,
        review_service=ClaimReviewService(client, settings.gemini_model),
        document_extractor=GeminiDocumentExtractor(client, settings.gemini_model),
    )
    claim = repository.get_claim(args.claim_id)
    if claim is None:
        raise RuntimeError(f"Claim {args.claim_id} was not found.")

    document = ClaimDocument(
        document_id=args.document_id or generate_document_id(),
        claim_id=args.claim_id,
        document_type=args.document_type,
        filename=file_path.name,
        storage_path=str(file_path),
        received_at=datetime.now(timezone.utc),
    )

    print(f"Claim: {args.claim_id}")
    print(f"Current status: {claim.get('status')}")
    print("\nNew document received:")
    print(document.document_type)

    result = workflow.resume(args.claim_id, document)

    quality = (
        "not checked"
        if result.evidence_usable is None
        else "usable" if result.evidence_usable else "unusable"
    )
    print(f"\nEvidence quality: {quality}")
    print(f"reason: {result.reason}")
    if result.matched_requirement:
        print(f"Matched requirement: {result.matched_requirement}")
    if result.idempotent_replay:
        print("Idempotency: already processed; no duplicate work created")
    print("\nStatus:")
    print(f"{result.previous_status} -> {result.final_status}")


if __name__ == "__main__":
    main()
