import base64
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr
from typing import Any, Protocol

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from pydantic import BaseModel, Field, field_validator
from requests import Response
from requests.exceptions import RequestException


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GmailConfigurationError(RuntimeError):
    """Raised when enabled Gmail delivery is missing safe configuration."""


class GmailError(RuntimeError):
    """Raised when Gmail cannot complete a delivery."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class GmailSettings:
    enabled: bool
    adjuster_email: str | None = None
    sender_email: str | None = None
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_refresh_token: str | None = None

    @classmethod
    def from_env(cls) -> "GmailSettings":
        raw_enabled = os.getenv("GMAIL_NOTIFICATION_ENABLED", "false").strip().lower()
        if raw_enabled not in {"true", "false"}:
            raise GmailConfigurationError(
                "GMAIL_NOTIFICATION_ENABLED must be either true or false."
            )
        enabled = raw_enabled == "true"
        values = {
            "adjuster_email": os.getenv("ADJUSTER_EMAIL", "").strip() or None,
            "sender_email": os.getenv("GMAIL_SENDER_EMAIL", "").strip() or None,
            "oauth_client_id": os.getenv("GMAIL_OAUTH_CLIENT_ID", "").strip() or None,
            "oauth_client_secret": os.getenv(
                "GMAIL_OAUTH_CLIENT_SECRET", ""
            ).strip()
            or None,
            "oauth_refresh_token": os.getenv(
                "GMAIL_OAUTH_REFRESH_TOKEN", ""
            ).strip()
            or None,
        }
        if enabled:
            missing = [name for name, value in values.items() if value is None]
            if missing:
                env_names = {
                    "adjuster_email": "ADJUSTER_EMAIL",
                    "sender_email": "GMAIL_SENDER_EMAIL",
                    "oauth_client_id": "GMAIL_OAUTH_CLIENT_ID",
                    "oauth_client_secret": "GMAIL_OAUTH_CLIENT_SECRET",
                    "oauth_refresh_token": "GMAIL_OAUTH_REFRESH_TOKEN",
                }
                raise GmailConfigurationError(
                    "Missing Gmail configuration: "
                    + ", ".join(env_names[name] for name in missing)
                )
        return cls(enabled=enabled, **values)


class AdjusterEmailRequest(BaseModel):
    notification_id: str
    claim_id: str
    recipient: str
    sender: str
    subject: str
    adjuster_summary: str
    incident_summary: str
    intake_priority: str
    inspection_start: datetime
    inspection_end: datetime
    inspection_location: str
    inspection_type: str
    calendar_event_link: str | None = None
    evidence_summary: list[str] = Field(default_factory=list)
    unresolved_notes: list[str] = Field(default_factory=list)
    action_requested: str

    @field_validator("recipient", "sender")
    @classmethod
    def require_email_address(cls, value: str) -> str:
        address = parseaddr(value)[1]
        if address != value or "@" not in address or "\n" in value or "\r" in value:
            raise ValueError("A plain valid email address is required.")
        return address

    @field_validator("subject")
    @classmethod
    def prevent_header_injection(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("Email subject cannot contain line breaks.")
        return value

    @field_validator("inspection_start", "inspection_end")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Email inspection timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc)


class HumanReviewEmailRequest(BaseModel):
    notification_id: str
    claim_id: str
    recipient: str
    sender: str
    subject: str
    reason: str
    summary: str
    conflicts: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    recommended_next_action: str
    review_url: str

    @field_validator("recipient", "sender")
    @classmethod
    def require_email_address(cls, value: str) -> str:
        address = parseaddr(value)[1]
        if address != value or "@" not in address or "\n" in value or "\r" in value:
            raise ValueError("A plain valid email address is required.")
        return address

    @field_validator("subject")
    @classmethod
    def prevent_header_injection(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("Email subject cannot contain line breaks.")
        return value


class GmailSendResult(BaseModel):
    gmail_message_id: str
    gmail_thread_id: str | None = None
    sent_at: datetime
    recipient: str
    sender: str

    @field_validator("sent_at")
    @classmethod
    def normalize_sent_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Gmail sent timestamp must be timezone-aware.")
        return value.astimezone(timezone.utc)


class HttpSession(Protocol):
    def post(self, url: str, **kwargs: Any) -> Response: ...


class GmailSender(Protocol):
    def send_adjuster_email(self, request: AdjusterEmailRequest) -> GmailSendResult: ...

    def send_human_review_email(
        self, request: HumanReviewEmailRequest
    ) -> GmailSendResult: ...


class GmailService:
    """Thin Gmail API adapter; claim decisions stay in the dispatch workflow."""

    def __init__(self, *, session: HttpSession, timeout_seconds: float = 15.0) -> None:
        self._session = session
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_oauth_settings(cls, settings: GmailSettings) -> "GmailService":
        if not settings.enabled:
            raise GmailConfigurationError(
                "Gmail service cannot start while notification delivery is disabled."
            )
        if not all(
            (
                settings.oauth_client_id,
                settings.oauth_client_secret,
                settings.oauth_refresh_token,
            )
        ):
            raise GmailConfigurationError("Gmail OAuth configuration is incomplete.")
        credentials = Credentials(
            token=None,
            refresh_token=settings.oauth_refresh_token,
            token_uri=GOOGLE_TOKEN_URI,
            client_id=settings.oauth_client_id,
            client_secret=settings.oauth_client_secret,
            scopes=[GMAIL_SEND_SCOPE],
        )
        return cls(session=AuthorizedSession(credentials))

    def send_adjuster_email(self, request: AdjusterEmailRequest) -> GmailSendResult:
        return self._send(_build_message(request), request.recipient, request.sender)

    def send_human_review_email(
        self, request: HumanReviewEmailRequest
    ) -> GmailSendResult:
        return self._send(
            _build_human_review_message(request), request.recipient, request.sender
        )

    def _send(
        self, message: EmailMessage, recipient: str, sender: str
    ) -> GmailSendResult:
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        try:
            response = self._session.post(
                GMAIL_SEND_URL,
                json={"raw": encoded},
                timeout=self._timeout_seconds,
            )
        except (RefreshError, GoogleAuthError, RequestException) as exc:
            raise GmailError("Gmail delivery request failed.", retryable=True) from exc

        if not 200 <= response.status_code < 300:
            raise _response_error(response)
        payload = response.json()
        message_id = str(payload.get("id", "")).strip()
        if not message_id:
            raise GmailError(
                "Gmail response did not include a message ID.", retryable=True
            )
        return GmailSendResult(
            gmail_message_id=message_id,
            gmail_thread_id=str(payload.get("threadId") or "") or None,
            sent_at=datetime.now(timezone.utc),
            recipient=recipient,
            sender=sender,
        )


def _build_message(request: AdjusterEmailRequest) -> EmailMessage:
    message = EmailMessage()
    message["To"] = request.recipient
    message["From"] = request.sender
    message["Subject"] = request.subject
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = (
        f"<{request.notification_id.lower()}@firstnotice-dispatch.invalid>"
    )
    message["X-FirstNotice-Notification-ID"] = request.notification_id
    message.set_content(_email_body(request))
    return message


def _build_human_review_message(request: HumanReviewEmailRequest) -> EmailMessage:
    message = EmailMessage()
    message["To"] = request.recipient
    message["From"] = request.sender
    message["Subject"] = request.subject
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = (
        f"<{request.notification_id.lower()}@firstnotice-dispatch.invalid>"
    )
    message["X-FirstNotice-Notification-ID"] = request.notification_id
    conflicts = "\n".join(f"- {item}" for item in request.conflicts) or "- None"
    questions = (
        "\n".join(f"- {item}" for item in request.unresolved_questions)
        or "- Verify the stated operational ambiguity."
    )
    message.set_content(
        f"""FirstNotice Dispatch paused operational intake for human verification.

Claim ID: {request.claim_id}
Reason: {request.reason}

Briefing
{request.summary}

Conflicts
{conflicts}

What to verify
{questions}

Recommended next step
{request.recommended_next_action}

Review claim
{request.review_url}

This review controls operational intake/routing only. It does not approve or deny
the insurance claim, determine liability or coverage, or calculate a payout.
""".strip()
    )
    return message


def _email_body(request: AdjusterEmailRequest) -> str:
    evidence = (
        "\n".join(f"- {item}" for item in request.evidence_summary)
        or "- No unresolved evidence requirement was reported."
    )
    unresolved = (
        "\n".join(f"- {item}" for item in request.unresolved_notes)
        or "- None reported."
    )
    calendar_link = request.calendar_event_link or "Not available"
    return f"""FirstNotice Dispatch completed intake and inspection scheduling for this claim.
Please review the handoff below.

Claim ID: {request.claim_id}
Intake priority: {request.intake_priority}
Incident summary: {request.incident_summary}

Inspection
- Type: {request.inspection_type}
- Start: {request.inspection_start.isoformat()}
- End: {request.inspection_end.isoformat()}
- Location: {request.inspection_location}
- Calendar event: {calendar_link}

Evidence completeness
{evidence}

Unresolved operational notes
{unresolved}

Adjuster summary
{request.adjuster_summary}

Action requested
{request.action_requested}
""".strip()


def _response_error(response: Response) -> GmailError:
    retryable = response.status_code in {408, 429} or response.status_code >= 500
    return GmailError(
        f"Gmail could not send the adjuster notification (HTTP {response.status_code}).",
        retryable=retryable,
    )
