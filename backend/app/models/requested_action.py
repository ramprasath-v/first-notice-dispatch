from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class EvidenceSourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    filename: str
    document_type: str
    conflict_fields: list[str] = Field(default_factory=list)
    replacement_eligible: bool = False


class EnterTextRequestedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: Literal["enter_text"] = "enter_text"
    action_id: str
    review_id: str
    field_name: str
    instruction: str


class UploadDocumentRequestedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: Literal["upload_document"] = "upload_document"
    action_id: str
    review_id: str
    document_type: str
    instruction: str
    replaces_document_id: str | None = None


RequestedAction = Annotated[
    EnterTextRequestedAction | UploadDocumentRequestedAction,
    Field(discriminator="action_type"),
]

REQUESTED_ACTION_ADAPTER = TypeAdapter(RequestedAction)


def parse_requested_actions(values: object) -> list[RequestedAction]:
    if not isinstance(values, list):
        return []
    actions: list[RequestedAction] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        candidate = dict(value)
        if candidate.get("action_type") == "enter_text" and not candidate.get(
            "action_id"
        ):
            review_id = str(candidate.get("review_id") or "legacy")
            field_name = str(candidate.get("field_name") or "incident_summary")
            candidate["action_id"] = f"{review_id}:text:{field_name}"
        actions.append(REQUESTED_ACTION_ADAPTER.validate_python(candidate))
    return actions
