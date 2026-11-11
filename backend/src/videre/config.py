"""Application settings, read from the environment and the repo-root `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

PAYPAL_SANDBOX_HOST = "api-m.sandbox.paypal.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Secrets are SecretStr so they never appear in repr(), logs or tracebacks.
    paypal_client_id: SecretStr = SecretStr("")
    paypal_client_secret: SecretStr = SecretStr("")
    paypal_base_url: str = f"https://{PAYPAL_SANDBOX_HOST}"

    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = ""
    # Client-side request budget for the model provider (free tier).
    gemini_rpm: int = Field(default=10, gt=0)
    # Output cap per model call. 8192 because a whole index.html travels in one tool call and
    # thinking tokens may count against the limit (SPEC-A2 section 9).
    max_output_tokens: int = Field(default=8192, gt=0)

    rail: Literal["paypal", "mock"] = "mock"
    demo_replay: bool = False

    # List price for metering, integer micro-USD per million tokens. Never floats.
    price_in_per_mtok: int = Field(default=0, ge=0, strict=False)
    price_out_per_mtok: int = Field(default=0, ge=0, strict=False)

    db_path: Path = BACKEND_DIR / "videre.db"
    frontend_origin: str = "http://localhost:5173"

    @model_validator(mode="after")
    def _sandbox_only(self) -> Self:
        """Refuse to start on the PayPal rail unless it points at the sandbox."""
        if self.rail != "paypal":
            return self
        url = urlsplit(self.paypal_base_url)
        if url.scheme != "https" or url.hostname != PAYPAL_SANDBOX_HOST:
            raise ValueError(
                f"RAIL=paypal requires PAYPAL_BASE_URL=https://{PAYPAL_SANDBOX_HOST} (sandbox only)"
            )
        if not (
            self.paypal_client_id.get_secret_value()
            and self.paypal_client_secret.get_secret_value()
        ):
            raise ValueError("RAIL=paypal requires PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
