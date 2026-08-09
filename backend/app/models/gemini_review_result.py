from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.requested_action import (
    EnterTextRequestedAction,
    RequestedAction,
    UploadDocumentRequestedAction,
)
from app.models.review_result import ReviewResult


class GeminiRequestedAction(BaseModel):
    """Flat Gemini contract converted into the stricter domain action union."""

    model_config = ConfigDict(extra="forbid")

    action_type: Literal["enter_text", "upload_document"]
    action_id: str
    review_id: str
    instruction: str
    field_name: str | None = None
    document_type: str | None = None
    replaces_document_id: str | None = None

    @model_validator(mode="after")
    def normalize_action_shape(self) -> "GeminiRequestedAction":
        if not self.instruction.strip():
            raise ValueError("requested action requires instruction")
        if self.action_type == "enter_text":
            if not self.field_name:
                raise ValueError("enter_text requires field_name")
            self.document_type = None
            self.replaces_document_id = None
        else:
            if not self.document_type:
                raise ValueError("upload_document requires document_type")
            self.field_name = None
            self.replaces_document_id = None
        return self

    def to_domain(self) -> RequestedAction:
        common = {
            "action_id": self.action_id,
            "review_id": self.review_id,
            "instruction": self.instruction,
        }
        if self.action_type == "enter_text":
            return EnterTextRequestedAction(
                **common,
                field_name=self.field_name,
            )
        return UploadDocumentRequestedAction(
            **common,
            document_type=self.document_type,
            replaces_document_id=None,
        )


class GeminiReviewResult(ReviewResult):
    """Gemini-compatible response schema for the evidence review call."""

    requested_actions: list[GeminiRequestedAction] = Field(default_factory=list)

    def to_domain(self) -> ReviewResult:
        values = self.model_dump(mode="python", exclude={"requested_actions"})
        values["requested_actions"] = [
            action.to_domain().model_dump(mode="python")
            for action in self.requested_actions
        ]
        return ReviewResult.model_validate(values)
