from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from app.config import ConfigurationError, Settings
from app.models.intake_result import IntakeResult
from app.models.claim_document import ClaimDocument
from app.services.claim_review_service import ClaimReviewError, ClaimReviewService
from app.services.intake_extraction_service import (
    IntakeExtractionService,
    file_part,
)
from app.tools.firestore_repository import (
    ClaimRepositoryError,
    FirestoreClaimRepository,
    generate_document_id,
)
from app.tools.gemini_client import create_gemini_client
from app.tools.adk_workflow_tools import build_initial_review_metadata


load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DATA_DIR = PROJECT_ROOT / "sample-data"

IMAGE_PATH = SAMPLE_DATA_DIR / "accident-photo.jpg"
PDF_PATH = SAMPLE_DATA_DIR / "police-report.pdf"


def main() -> None:
    try:
        settings = Settings.from_env()
        client = create_gemini_client(settings)
        result = IntakeExtractionService(client, settings.gemini_model).extract(
            [IMAGE_PATH, PDF_PATH]
        )

        print("\nValidated IntakeResult:\n")
        print(result.model_dump_json(indent=2))

        repository = FirestoreClaimRepository.from_default_credentials(
            settings.google_cloud_project, settings.firestore_database
        )
        claim_id = repository.save_completed_intake(result)

        print(f"\nCreated claim: {claim_id}")
        print("Firestore status: intake_complete (claim and event committed atomically)")

        received_at = datetime.now(timezone.utc)
        documents = [
            ClaimDocument(
                document_id=generate_document_id(),
                claim_id=claim_id,
                document_type="damage_evidence",
                filename=IMAGE_PATH.name,
                storage_path=str(IMAGE_PATH),
                received_at=received_at,
            ),
            ClaimDocument(
                document_id=generate_document_id(),
                claim_id=claim_id,
                document_type="police_report",
                filename=PDF_PATH.name,
                storage_path=str(PDF_PATH),
                received_at=received_at,
            ),
        ]
        for document in documents:
            repository.add_document(document)

        repository.update_claim_status(claim_id, "review_processing")
        evidence_metadata = build_initial_review_metadata(result, documents)
        review = ClaimReviewService(client, settings.gemini_model).review(
            result,
            evidence_metadata,
            evidence_parts=[file_part(IMAGE_PATH), file_part(PDF_PATH)],
        )
        final_status = repository.save_review_result(claim_id, review)

        print("\nReview:")
        print(f"intake_complete: {str(review.intake_complete).lower()}")
        print(f"intake_priority: {review.intake_priority}")
        print(f"inspection_required: {str(review.inspection_required).lower()}")
        print("missing_documents:")
        for missing in review.missing_documents:
            print(f"  - {missing.type}")
        print("unusable_evidence:")
        for unusable in review.unusable_evidence:
            print(f"  - {unusable.evidence_type}")
        print("conflicts:")
        for conflict in review.conflicts:
            print(f"  - {conflict.field}")
        print(
            "requires_human_review: "
            f"{str(review.requires_human_review).lower()}"
        )
        print(f"status: {final_status.value}")

    except ValidationError as exc:
        print("Gemini returned JSON, but it failed IntakeResult validation:")
        print(exc)
        raise
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        raise
    except ClaimRepositoryError as exc:
        print(f"Firestore persistence failed: {exc}")
        raise
    except ClaimReviewError as exc:
        print(f"Claim review failed: {exc}")
        raise
    except Exception as exc:
        print(f"Multimodal intake failed: {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()
