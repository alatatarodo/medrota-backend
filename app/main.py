from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text
from app.core.config import settings
from app.db.database import Base, SessionLocal, engine
from app.bootstrap import seed_sample_data
from app.api import doctors, operations, schedule

# Create tables
Base.metadata.create_all(bind=engine)


def ensure_schema_updates():
    """Apply lightweight schema updates for environments without migrations."""
    inspector = inspect(engine)
    doctor_columns = {column["name"] for column in inspector.get_columns("doctors")}
    availability_columns = {column["name"] for column in inspector.get_columns("doctor_availability_events")}
    locum_columns = {column["name"] for column in inspector.get_columns("locum_requests")}

    if "hospital_site" not in doctor_columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE doctors "
                    "ADD COLUMN hospital_site VARCHAR(100) "
                    "NOT NULL DEFAULT 'Wythenshawe Hospital'"
                )
            )

    with engine.begin() as connection:
        if "approved_by" not in availability_columns:
            connection.execute(text("ALTER TABLE doctor_availability_events ADD COLUMN approved_by VARCHAR(100)"))
        if "approved_at" not in availability_columns:
            connection.execute(text("ALTER TABLE doctor_availability_events ADD COLUMN approved_at DATETIME"))
        if "approval_comment" not in availability_columns:
            connection.execute(text("ALTER TABLE doctor_availability_events ADD COLUMN approval_comment TEXT"))

        if "approved_at" not in locum_columns:
            connection.execute(text("ALTER TABLE locum_requests ADD COLUMN approved_at DATETIME"))
        if "approval_comment" not in locum_columns:
            connection.execute(text("ALTER TABLE locum_requests ADD COLUMN approval_comment TEXT"))


ensure_schema_updates()

with SessionLocal() as bootstrap_session:
    seed_sample_data(bootstrap_session)

# Create FastAPI app
app = FastAPI(
    title=settings.api_title,
    description=settings.api_description,
    version=settings.api_version
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(doctors.router)
app.include_router(operations.router)
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
