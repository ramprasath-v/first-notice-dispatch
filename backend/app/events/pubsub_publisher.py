import os
from dataclasses import dataclass
from typing import Any

from google.cloud import pubsub_v1

from app.events.claim_events import ClaimEvent


class PubSubConfigurationError(RuntimeError):
    """Raised when Pub/Sub publisher configuration is incomplete."""


class PubSubPublishError(RuntimeError):
    """Raised when an event cannot be published."""


@dataclass(frozen=True)
class PubSubSettings:
    google_cloud_project: str
    claim_events_topic: str

    @classmethod
    def from_env(cls) -> "PubSubSettings":
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        topic = os.getenv("PUBSUB_CLAIM_EVENTS_TOPIC", "").strip()
        missing = []
        if not project:
            missing.append("GOOGLE_CLOUD_PROJECT")
        if not topic:
            missing.append("PUBSUB_CLAIM_EVENTS_TOPIC")
        if missing:
            raise PubSubConfigurationError(
                "Missing required Pub/Sub environment variable(s): "
                + ", ".join(missing)
            )
        return cls(project, topic)


class ClaimEventPublisher:
    def __init__(
        self,
        settings: PubSubSettings,
        client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or pubsub_v1.PublisherClient()
        self._topic_path = self._client.topic_path(
            settings.google_cloud_project, settings.claim_events_topic
        )

    def publish(self, event: ClaimEvent) -> str:
        data = event.model_dump_json().encode("utf-8")
        try:
            future = self._client.publish(
                self._topic_path,
                data,
                event_id=event.event_id,
                event_type=event.event_type,
                correlation_id=event.correlation_id,
                event_version=event.event_version,
            )
            return str(future.result())
        except Exception as exc:
            raise PubSubPublishError(
                f"Could not publish {event.event_type} event {event.event_id}: {exc}"
            ) from exc
