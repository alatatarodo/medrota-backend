from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql://postgres:password@localhost:5432/med_rota"
    
    # API
    api_title: str = "Medical Rostering Automation API"
    api_version: str = "1.0.0"
    api_description: str = "Automated rostering system for NHS doctors"
    
    # CORS
    allowed_origins: list = ["http://localhost:3000", "http://localhost:5173"]
    
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
    
    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
