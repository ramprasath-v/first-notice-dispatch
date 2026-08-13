import unittest
from unittest.mock import patch

from app.config import ConfigurationError, Settings
from app.tools.gemini_client import (
    GEMINI_MAX_ATTEMPTS,
    GEMINI_TIMEOUT_MS,
    create_gemini_client,
)


class VertexAiConfigurationTests(unittest.TestCase):
    def test_settings_require_vertex_and_firestore_values_without_api_key(self) -> None:
        environment = {
            "GOOGLE_CLOUD_PROJECT": "firstnotice-ai",
            "GOOGLE_CLOUD_LOCATION": "us-central1",
            "GEMINI_MODEL": "configured-model-id",
            "FIRESTORE_DATABASE": "firstnotice-app",
        }

        with patch.dict("os.environ", environment, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.google_cloud_project, "firstnotice-ai")
        self.assertEqual(settings.google_cloud_location, "us-central1")
        self.assertEqual(settings.gemini_model, "configured-model-id")
        self.assertEqual(settings.firestore_database, "firstnotice-app")
        self.assertFalse(hasattr(settings, "gemini_api_key"))

    def test_missing_vertex_location_is_reported(self) -> None:
        environment = {
            "GOOGLE_CLOUD_PROJECT": "firstnotice-ai",
            "GEMINI_MODEL": "configured-model-id",
            "FIRESTORE_DATABASE": "firstnotice-app",
        }

        with patch.dict("os.environ", environment, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "GOOGLE_CLOUD_LOCATION"):
                Settings.from_env()

    @patch("app.tools.gemini_client.genai.Client")
    def test_gemini_client_uses_vertex_ai_and_adc(self, client_constructor) -> None:
        settings = Settings(
            google_cloud_project="firstnotice-ai",
            google_cloud_location="us-central1",
            gemini_model="configured-model-id",
            firestore_database="firstnotice-app",
        )

        client = create_gemini_client(settings)

        self.assertIs(client, client_constructor.return_value)
        call = client_constructor.call_args
        self.assertEqual(call.kwargs["vertexai"], True)
        self.assertEqual(call.kwargs["project"], "firstnotice-ai")
        self.assertEqual(call.kwargs["location"], "us-central1")
        options = call.kwargs["http_options"]
        self.assertEqual(options.timeout, GEMINI_TIMEOUT_MS)
        self.assertIsNone(options.retry_options)
        self.assertEqual(GEMINI_MAX_ATTEMPTS, 1)


if __name__ == "__main__":
    unittest.main()
