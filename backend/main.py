"""Cloud Run entry point for claimant intake and Pub/Sub delivery."""

from app.logging_config import configure_application_logging


configure_application_logging()

from app.api.pubsub import app


__all__ = ["app"]
