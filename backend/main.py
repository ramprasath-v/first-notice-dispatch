"""Cloud Run entry point for claimant intake and Pub/Sub delivery."""

from app.api.pubsub import app


__all__ = ["app"]
