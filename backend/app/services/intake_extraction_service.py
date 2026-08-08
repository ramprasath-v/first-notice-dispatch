import mimetypes
from pathlib import Path
from typing import Any, Sequence

from google.genai import types

from app.models.intake_result import IntakeResult


INTAKE_PROMPT = """
You are performing first-mile insurance claim intake.

Analyze all submitted multimodal claim evidence together.

Rules:
1. Use only facts visible or explicitly stated in the submitted evidence.
2. Do not determine liability, coverage, fraud, payout, or claim approval.
3. Do not invent missing values.
4. Use null for unavailable scalar values.
5. Add uncertain or conflicting details to uncertainties.
6. For every important factual observation, identify the supporting filename.
7. Return the result using the required structured schema.
8. For every submitted image, add one image_evidence_capabilities entry using
   the supplied source filename. Assess image content, not its upload category.
9. A single image may support damage_evidence, vehicle_identity, and
   license_plate_photo simultaneously.
10. Mark vehicle_identity and license_plate_photo as supported only when the
    plate or other vehicle identifier is visible and readable enough to verify.
    If it is visible but blurry, dark, obstructed, or unreadable, list those
    capabilities as unusable. If no plate or identifier is visible, do not list
    them as supported and explain that in quality_observations.
"""


class IntakeExtractionService:
    """Existing multimodal intake behavior behind a reusable service boundary."""

    def __init__(self, client: Any, model_name: str) -> None:
        self._client = client
        self._model_name = model_name

    def extract(
        self,
        evidence_sources: Sequence[Path | str],
        *,
        incident_description: str | None = None,
        policy_number_hint: str | None = None,
    ) -> IntakeResult:
        context_parts = []
        if incident_description:
            context_parts.append(
                types.Part.from_text(
                    text=(
                        "Claimant-provided incident description:\n"
                        f"{incident_description}"
                    )
                )
            )
        if policy_number_hint:
            context_parts.append(
                types.Part.from_text(
                    text=f"Claimant-provided policy number hint: {policy_number_hint}"
                )
            )
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(text=INTAKE_PROMPT),
                        *context_parts,
                        *(
                            part
                            for source in evidence_sources
                            for part in (
                                types.Part.from_text(
                                    text=(
                                        "Evidence source filename: "
                                        f"{evidence_source_name(source)}"
                                    )
                                ),
                                evidence_part(source),
                            )
                        ),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
                response_schema=IntakeResult,
            ),
        )
        if not response.text:
            raise RuntimeError("Gemini returned an empty response.")
        return IntakeResult.model_validate_json(response.text)


def file_part(path: Path) -> types.Part:
    """Read a local evidence file as a google-genai multimodal part."""
    if not path.exists():
        raise FileNotFoundError(f"Required evidence file was not found: {path}")
    mime_type, _ = mimetypes.guess_type(path.name)
    if not mime_type:
        raise ValueError(f"Could not determine MIME type for: {path.name}")
    return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type)


def evidence_part(
    source: Path | str,
    *,
    mime_type: str | None = None,
) -> types.Part:
    """Create a supported local-file or Vertex AI GCS evidence part."""
    if isinstance(source, str) and source.startswith("gs://"):
        return types.Part.from_uri(file_uri=source, mime_type=mime_type)
    return file_part(Path(source))


def evidence_source_name(source: Path | str) -> str:
    if isinstance(source, str):
        return source.rstrip("/").rsplit("/", 1)[-1]
    return source.name
