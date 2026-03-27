import json

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings


def build_default_sqlite_url(data_dir: str) -> str:
    normalized = (data_dir or "./data").rstrip("/\\")
    return f"sqlite:///{normalized}/medrota.db"


class Settings(BaseSettings):
    # Database
    data_dir: str = "./data"
    database_url: str = build_default_sqlite_url("./data")
    auto_seed_sample_data: bool = True
    
    # API
    api_title: str = "Medical Rostering Automation API"
    api_version: str = "1.0.0"
    api_description: str = "Automated rostering system for NHS doctors"
    
    # CORS
    allowed_origins: str = "http://localhost:3000,http://localhost:5173"
    
    # Scheduler
    scheduler_timeout_seconds: int = 300
    scheduler_max_iterations: int = 1000
    
    # Fairness targets
    fairness_night_tolerance: int = 2
    fairness_weekend_tolerance: int = 1
    fairness_oncall_tolerance: int = 1
    fairness_score_target: float = 85.0
    
    # Redis (for async jobs)
    redis_url: str = "redis://localhost:6379/0"

    # OpenAI Copilot
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout_seconds: float = 30.0

    @field_validator("database_url", mode="before")
    @classmethod
    def parse_database_url(cls, value, info: ValidationInfo):
        data_dir = (info.data or {}).get("data_dir", "./data")

        if value is None:
            return build_default_sqlite_url(data_dir)

        if isinstance(value, str):
            trimmed = value.strip()
            if not trimmed:
                return build_default_sqlite_url(data_dir)

            if trimmed.startswith("postgres://"):
                return f"postgresql://{trimmed[len('postgres://'):]}"

            return trimmed

        return value
    
    class Config:
        env_file = ".env"
        case_sensitive = False

    @property
    def allowed_origins_list(self) -> list[str]:
        value = self.allowed_origins

        if isinstance(value, list):
            return value

        if not isinstance(value, str):
            return []

        trimmed = value.strip()
        if not trimmed:
            return []

        if trimmed.startswith("["):
            parsed = json.loads(trimmed)
            return [str(origin).strip() for origin in parsed if str(origin).strip()]

        return [origin.strip() for origin in trimmed.split(",") if origin.strip()]


settings = Settings()
