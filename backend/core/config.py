import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Settings:
    """Application configuration loaded from environment variables."""

    cube_api_url: str = field(
        default_factory=lambda: os.getenv(
            "CUBE_API_URL", "http://localhost:4000/cubejs-api/v1/load"
        )
    )
    cube_api_token: str | None = field(
        default_factory=lambda: os.getenv("CUBE_API_TOKEN")
    )
    allow_mock_data: bool = field(
        default_factory=lambda: os.getenv("ALLOW_MOCK_DATA", "true").lower() == "true"
    )
    default_chart_type: str = field(
        default_factory=lambda: os.getenv("DEFAULT_CHART_TYPE", "line")
    )
    default_dataset: str = field(
        default_factory=lambda: os.getenv("DEFAULT_DATASET", "tutorial")
    )
    api_host: str = field(default_factory=lambda: os.getenv("API_HOST", "0.0.0.0"))
    api_port: int = field(default_factory=lambda: int(os.getenv("API_PORT", "8000")))
    cors_origins: List[str] = field(
        default_factory=lambda: [
            origin.strip()
            for origin in os.getenv(
                "CORS_ORIGINS", "http://localhost:8501,http://localhost:8000"
            ).split(",")
            if origin.strip()
        ]
    )


settings = Settings()

