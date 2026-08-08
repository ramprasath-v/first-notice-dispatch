import os
from dataclasses import dataclass


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is missing."""


@dataclass(frozen=True)
class Settings:
    gemini_model: str
    google_cloud_project: str
    google_cloud_location: str
    firestore_database: str

    @classmethod
    def from_env(cls) -> "Settings":
        values = {
            "GEMINI_MODEL": os.getenv("GEMINI_MODEL", "").strip(),
            "GOOGLE_CLOUD_PROJECT": os.getenv("GOOGLE_CLOUD_PROJECT", "").strip(),
            "GOOGLE_CLOUD_LOCATION": os.getenv(
                "GOOGLE_CLOUD_LOCATION", ""
            ).strip(),
            "FIRESTORE_DATABASE": os.getenv("FIRESTORE_DATABASE", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]

        if missing:
            raise ConfigurationError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )

        return cls(
            gemini_model=values["GEMINI_MODEL"],
            google_cloud_project=values["GOOGLE_CLOUD_PROJECT"],
            google_cloud_location=values["GOOGLE_CLOUD_LOCATION"],
            firestore_database=values["FIRESTORE_DATABASE"],
        )
