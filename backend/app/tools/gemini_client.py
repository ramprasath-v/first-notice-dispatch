from google import genai

from app.config import Settings


def create_gemini_client(settings: Settings) -> genai.Client:
    """Create one ADC-authenticated google-genai client for Vertex AI."""
    return genai.Client(
        vertexai=True,
        project=settings.google_cloud_project,
        location=settings.google_cloud_location,
    )
