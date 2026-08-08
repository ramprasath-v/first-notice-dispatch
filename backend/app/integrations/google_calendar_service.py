import os
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import quote

import google.auth
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from pydantic import BaseModel, field_validator
from requests import Response
from requests.exceptions import RequestException


CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_API_BASE_URL = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarConfigurationError(RuntimeError):
    """Raised when enabled Calendar integration is not configured safely."""


class GoogleCalendarError(RuntimeError):
    """Raised when the Calendar API cannot complete an operation."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class GoogleCalendarSettings:
    enabled: bool
    calendar_id: str | None = None

    @classmethod
    def from_env(cls) -> "GoogleCalendarSettings":
        raw_enabled = os.getenv("GOOGLE_CALENDAR_ENABLED", "false").strip().lower()
        if raw_enabled not in {"true", "false"}:
            raise GoogleCalendarConfigurationError(
                "GOOGLE_CALENDAR_ENABLED must be either true or false."
            )
        calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "").strip() or None
        enabled = raw_enabled == "true"
        if enabled and calendar_id is None:
            raise GoogleCalendarConfigurationError(
                "GOOGLE_CALENDAR_ID is required when Google Calendar is enabled."
            )
        return cls(enabled=enabled, calendar_id=calendar_id)


class InspectionCalendarEvent(BaseModel):
    appointment_id: str
    claim_id: str
    scheduled_start: datetime
    scheduled_end: datetime
    inspection_type: str
    location: str
    incident_summary: str
    intake_priority: str
    operational_note: str | None = None
    inspector_name: str | None = None

    @field_validator("scheduled_start", "scheduled_end")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Calendar event timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc)


class CalendarEventResult(BaseModel):
    calendar_event_id: str
    calendar_event_link: str | None = None
    calendar_id: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Calendar creation time must be timezone-aware.")
        return value.astimezone(timezone.utc)


class InspectionCalendar(Protocol):
    def create_inspection_event(
        self, event: InspectionCalendarEvent
    ) -> CalendarEventResult: ...


class HttpSession(Protocol):
    def post(self, url: str, **kwargs: Any) -> Response: ...

    def get(self, url: str, **kwargs: Any) -> Response: ...

    def delete(self, url: str, **kwargs: Any) -> Response: ...


class GoogleCalendarService:
    """Thin Google Calendar API adapter with no claim-routing policy."""

    def __init__(
        self,
        *,
        calendar_id: str,
        session: HttpSession,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.calendar_id = calendar_id
        self._session = session
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_default_credentials(
        cls, settings: GoogleCalendarSettings
    ) -> "GoogleCalendarService":
        if not settings.enabled or not settings.calendar_id:
            raise GoogleCalendarConfigurationError(
                "Google Calendar service cannot start while integration is disabled."
            )
        credentials, _ = google.auth.default(scopes=[CALENDAR_EVENTS_SCOPE])
        return cls(
            calendar_id=settings.calendar_id,
            session=AuthorizedSession(credentials),
        )

    def create_inspection_event(
        self, event: InspectionCalendarEvent
    ) -> CalendarEventResult:
        event_id = calendar_event_id(event.appointment_id)
        url = self._event_collection_url()
        body = {
            "id": event_id,
            "summary": f"FirstNotice Inspection - {event.claim_id}",
            "description": _event_description(event),
            "location": event.location,
            "visibility": "private",
            "start": {"dateTime": event.scheduled_start.isoformat()},
            "end": {"dateTime": event.scheduled_end.isoformat()},
            "extendedProperties": {
                "private": {
                    "appointment_id": event.appointment_id,
                    "claim_id": event.claim_id,
                }
            },
        }
        try:
            response = self._session.post(
                url,
                params={"sendUpdates": "none"},
                json=body,
                timeout=self._timeout_seconds,
            )
        except (GoogleAuthError, RequestException) as exc:
            raise GoogleCalendarError(
                "Google Calendar event creation request failed.", retryable=True
            ) from exc

        if response.status_code == 409:
            existing = self.get_event(event_id)
            if existing is not None:
                return existing
            raise GoogleCalendarError(
                "Google Calendar reported a duplicate event that could not be read.",
                retryable=True,
            )
        if not 200 <= response.status_code < 300:
            raise _response_error("create", response)
        return self._result(response.json())

    def get_event(self, calendar_event_id: str) -> CalendarEventResult | None:
        try:
            response = self._session.get(
                self._event_url(calendar_event_id), timeout=self._timeout_seconds
            )
        except (GoogleAuthError, RequestException) as exc:
            raise GoogleCalendarError(
                "Google Calendar event lookup request failed.", retryable=True
            ) from exc
        if response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise _response_error("read", response)
        return self._result(response.json())

    def cancel_event(self, calendar_event_id: str) -> bool:
        try:
            response = self._session.delete(
                self._event_url(calendar_event_id), timeout=self._timeout_seconds
            )
        except (GoogleAuthError, RequestException) as exc:
            raise GoogleCalendarError(
                "Google Calendar cancellation request failed.", retryable=True
            ) from exc
        if response.status_code in {204, 404, 410}:
            return response.status_code == 204
        raise _response_error("cancel", response)

    def _event_collection_url(self) -> str:
        return f"{CALENDAR_API_BASE_URL}/calendars/{quote(self.calendar_id, safe='')}/events"

    def _event_url(self, event_id: str) -> str:
        return f"{self._event_collection_url()}/{quote(event_id, safe='')}"

    def _result(self, payload: dict[str, Any]) -> CalendarEventResult:
        event_id = str(payload.get("id", "")).strip()
        if not event_id:
            raise GoogleCalendarError(
                "Google Calendar response did not include an event ID.",
                retryable=True,
            )
        created = payload.get("created")
        created_at = (
            _parse_google_datetime(str(created))
            if created
            else datetime.now(timezone.utc)
        )
        return CalendarEventResult(
            calendar_event_id=event_id,
            calendar_event_link=str(payload.get("htmlLink") or "") or None,
            calendar_id=self.calendar_id,
            created_at=created_at,
        )


def calendar_event_id(appointment_id: str) -> str:
    """Return a stable Calendar-compatible ID using base32hex-safe characters."""
    return "fn" + sha256(appointment_id.encode("utf-8")).hexdigest()


def _event_description(event: InspectionCalendarEvent) -> str:
    lines = [
        f"Claim ID: {event.claim_id}",
        f"Inspection type: {event.inspection_type}",
        f"Incident summary: {event.incident_summary}",
        f"Intake priority: {event.intake_priority}",
    ]
    if event.operational_note:
        lines.append(f"Operational note: {event.operational_note}")
    if event.inspector_name:
        lines.append(f"Inspector: {event.inspector_name}")
    return "\n".join(lines)


def _parse_google_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _response_error(operation: str, response: Response) -> GoogleCalendarError:
    retryable = response.status_code in {408, 429} or response.status_code >= 500
    return GoogleCalendarError(
        f"Google Calendar could not {operation} the event "
        f"(HTTP {response.status_code}).",
        retryable=retryable,
    )
