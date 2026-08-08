import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def allowed_origins_from_env() -> list[str]:
    configured = os.getenv("ALLOWED_ORIGINS", "http://localhost:4200")
    origins = [origin.strip().rstrip("/") for origin in configured.split(",")]
    origins = [origin for origin in origins if origin]
    if "*" in origins:
        raise ValueError("ALLOWED_ORIGINS must list explicit frontend origins.")
    return origins


def configure_cors(
    app: FastAPI,
    allowed_origins: list[str] | None = None,
) -> None:
    origins = (
        allowed_origins
        if allowed_origins is not None
        else allowed_origins_from_env()
    )
    if not origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Idempotency-Key",
            "X-Review-Token",
        ],
    )
