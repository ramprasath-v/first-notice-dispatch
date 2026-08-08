import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from requests import Response

from app.events.claim_event_handler import _is_retryable
from app.integrations.google_calendar_service import (
    CalendarEventResult,
    GoogleCalendarConfigurationError,
    GoogleCalendarError,
    GoogleCalendarService,
    GoogleCalendarSettings,
    InspectionCalendarEvent,
    calendar_event_id,
)


START = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


def api_response(status: int, payload: dict[str, object] | None = None) -> MagicMock:
    response = MagicMock(spec=Response)
    response.status_code = status
    response.json.return_value = payload or {}
    return response


def inspection_event() -> InspectionCalendarEvent:
    return InspectionCalendarEvent(
        appointment_id="APT-A1B2C3D4",
        claim_id="CLM-A1B2C3D4",
        scheduled_start=START,
        scheduled_end=END,
        inspection_type="virtual",
        location="Secure virtual inspection session",
        incident_summary="The vehicle was struck from behind.",
        intake_priority="routine",
        operational_note="No urgent operational indicator.",
        inspector_name="Demo Inspector",
    )


class GoogleCalendarServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = MagicMock()
        self.service = GoogleCalendarService(
            calendar_id="demo-calendar@group.calendar.google.com",
            session=self.session,
        )
        self.payload = {
            "id": calendar_event_id("APT-A1B2C3D4"),
            "htmlLink": "https://calendar.google.com/event?eid=demo",
            "created": "2026-08-07T12:30:00.000Z",
        }

    def test_create_uses_deterministic_slot_and_safe_operational_content(self) -> None:
        self.session.post.return_value = api_response(200, self.payload)

        result = self.service.create_inspection_event(inspection_event())

        request = self.session.post.call_args
        body = request.kwargs["json"]
        self.assertEqual(body["summary"], "FirstNotice Inspection - CLM-A1B2C3D4")
        self.assertEqual(body["start"]["dateTime"], START.isoformat())
        self.assertEqual(body["end"]["dateTime"], END.isoformat())
        self.assertEqual(body["visibility"], "private")
        self.assertNotIn("attendees", body)
        self.assertEqual(request.kwargs["params"]["sendUpdates"], "none")
        self.assertIn("Incident summary", body["description"])
        self.assertNotIn("policy", body["description"].lower())
        self.assertEqual(result.calendar_event_id, self.payload["id"])
        self.assertEqual(result.calendar_event_link, self.payload["htmlLink"])

    def test_duplicate_insert_returns_existing_deterministic_event(self) -> None:
        self.session.post.return_value = api_response(409)
        self.session.get.return_value = api_response(200, self.payload)

        result = self.service.create_inspection_event(inspection_event())

        self.assertEqual(result.calendar_event_id, self.payload["id"])
        self.session.post.assert_called_once()
        self.session.get.assert_called_once()

    def test_retryable_api_failure_is_surfaced(self) -> None:
        self.session.post.return_value = api_response(503)

        with self.assertRaises(GoogleCalendarError) as raised:
            self.service.create_inspection_event(inspection_event())

        self.assertTrue(raised.exception.retryable)

    def test_permission_failure_is_non_retryable(self) -> None:
        self.session.post.return_value = api_response(403)

        with self.assertRaises(GoogleCalendarError) as raised:
            self.service.create_inspection_event(inspection_event())

        self.assertFalse(raised.exception.retryable)
        self.assertFalse(_is_retryable(raised.exception))

    def test_retryable_error_is_retryable_for_pubsub_delivery(self) -> None:
        self.assertTrue(
            _is_retryable(GoogleCalendarError("Calendar unavailable", retryable=True))
        )

    def test_cancel_is_idempotent_for_missing_event(self) -> None:
        self.session.delete.return_value = api_response(404)

        self.assertFalse(self.service.cancel_event(self.payload["id"]))


class GoogleCalendarSettingsTests(unittest.TestCase):
    @patch.dict("os.environ", {"GOOGLE_CALENDAR_ENABLED": "false"}, clear=True)
    def test_disabled_does_not_require_calendar_id(self) -> None:
        self.assertEqual(
            GoogleCalendarSettings.from_env(),
            GoogleCalendarSettings(enabled=False),
        )

    @patch.dict("os.environ", {"GOOGLE_CALENDAR_ENABLED": "true"}, clear=True)
    def test_enabled_requires_calendar_id(self) -> None:
        with self.assertRaises(GoogleCalendarConfigurationError):
            GoogleCalendarSettings.from_env()

if __name__ == "__main__":
    unittest.main()
