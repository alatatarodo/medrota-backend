from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from app.core.config import settings
from app.db.database import Base, engine
from app.api import doctors, schedule

# Create tables
Base.metadata.create_all(bind=engine)


def ensure_schema_updates():
    """Apply lightweight schema updates for environments without migrations."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE doctors "
                "ADD COLUMN IF NOT EXISTS hospital_site VARCHAR(100) "
                "NOT NULL DEFAULT 'Wythenshawe Hospital'"
            )
        )


ensure_schema_updates()

# Create FastAPI app
app = FastAPI(
    title=settings.api_title,
    description=settings.api_description,
    version=settings.api_version
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(doctors.router)
app.include_router(schedule.router)


@app.get("/health")
def health_check():
    """Health check endpoint"""
    return {"status": "ok", "service": "Medical Rostering API"}


@app.get("/")
def root():
    """Root endpoint"""
    return {
        "service": settings.api_title,
        "version": settings.api_version,
        "docs": "/docs",
        "api_version": "v1"
    }
