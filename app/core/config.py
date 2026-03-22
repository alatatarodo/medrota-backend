import json

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str = "sqlite:///./data/medrota.db"
    
    # API
    api_title: str = "Medical Rostering Automation API"
    api_version: str = "1.0.0"
    api_description: str = "Automated rostering system for NHS doctors"
    
    # CORS
    allowed_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]
    
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

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_allowed_origins(cls, value):
        if isinstance(value, str):
            trimmed = value.strip()
            if not trimmed:
                return []

            if trimmed.startswith("["):
                return json.loads(trimmed)

            return [origin.strip() for origin in trimmed.split(",") if origin.strip()]

        return value

    @field_validator("database_url", mode="before")
    @classmethod
    def parse_database_url(cls, value):
        if value is None:
            return "sqlite:///./data/medrota.db"

        if isinstance(value, str):
            trimmed = value.strip()
            if not trimmed:
                return "sqlite:///./data/medrota.db"

            if trimmed.startswith("postgres://"):
                return f"postgresql://{trimmed[len('postgres://'):]}"

            return trimmed

        return value
    
    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
