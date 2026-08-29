from typing import Any, Protocol

from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.services.intake_extraction_service import evidence_part


VOICE_INCIDENT_PROMPT = """
Extract only the claimant-stated incident timing, factual incident context, and
injury signal from this voice recording.

Rules:
1. Return incident_date as YYYY-MM-DD only when the recording states one
   unambiguously. Otherwise return null.
2. Return incident_time as HH:MM in 24-hour time only when stated clearly.
3. Set injury_mentioned true only when the claimant explicitly mentions pain,
   injury, symptoms, or receiving medical attention.
4. Return incident_description only when the claimant states what happened.
   Keep it concise, factual, and grounded in the claimant's words.
5. Keep injury_description short and grounded in the claimant's words. Do not
   diagnose, assess severity, or add medical conclusions.
6. Do not extract or alter policy, vehicle, coverage, liability, fraud, payout,
   or claim-decision information.
7. Do not invent missing values.
""".strip()


class VoiceIncidentExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_date: str | None = Field(
        default=None,
        description="Unambiguous claimant-stated date in YYYY-MM-DD format.",
    )
    incident_time: str | None = Field(
        default=None,
        description="Optional claimant-stated time in HH:MM 24-hour format.",
    )
    incident_description: str | None = Field(
        default=None,
        max_length=2000,
        description="Concise factual incident context stated by the claimant.",
    )
    injury_mentioned: bool
    injury_description: str | None = Field(default=None, max_length=500)

    @field_validator("incident_date")
    @classmethod
    def validate_date_shape(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 10 or value[4] != "-" or value[7] != "-"
        ):
            raise ValueError("incident_date must use YYYY-MM-DD format")
        return value

    @field_validator("incident_time")
    @classmethod
    def validate_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            hour_text, minute_text = value.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except (TypeError, ValueError) as exc:
            raise ValueError("incident_time must use HH:MM format") from exc
        if len(value) != 5 or not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("incident_time must use HH:MM format")
        return value


class VoiceIncidentExtractionError(RuntimeError):
    """Raised when the provider cannot return a validated voice result."""


class VoiceIncidentExtractor(Protocol):
    def extract(
        self, source: str, *, mime_type: str, filename: str
    ) -> VoiceIncidentExtractionResult: ...


class GeminiVoiceIncidentExtractor:
    def __init__(self, client: Any, model_name: str) -> None:
        self._client = client
        self._model_name = model_name

    def extract(
        self, source: str, *, mime_type: str, filename: str
    ) -> VoiceIncidentExtractionResult:
        try:
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_text(text=VOICE_INCIDENT_PROMPT),
                            types.Part.from_text(
                                text=f"Claimant voice source filename: {filename}"
                            ),
                            evidence_part(source, mime_type=mime_type),
                        ],
                    )
                ],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=VoiceIncidentExtractionResult,
                ),
            )
        except Exception as exc:
            raise VoiceIncidentExtractionError(
                "Voice incident extraction failed."
            ) from exc
        if not response.text:
            raise VoiceIncidentExtractionError(
                "Gemini returned an empty voice incident response."
            )
        try:
            return VoiceIncidentExtractionResult.model_validate_json(response.text)
        except ValidationError as exc:
            raise VoiceIncidentExtractionError(
                "Voice incident extraction failed validation."
            ) from exc
