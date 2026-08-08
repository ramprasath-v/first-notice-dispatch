"""Cloud Run entry point exposing only browser-facing claimant operations."""

from app.api.claimant import app


__all__ = ["app"]
