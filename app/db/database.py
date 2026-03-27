from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from app.core.config import settings

database_url = settings.database_url
engine_kwargs = {
    "echo": False,
    "pool_pre_ping": True,
}


def database_backend_name(url: str) -> str:
    if url.startswith("sqlite"):
        return "sqlite"
    if url.startswith("postgresql"):
        return "postgresql"
    return "unknown"

if database_url.startswith("sqlite:///"):
    sqlite_path = database_url.replace("sqlite:///", "", 1)
    if sqlite_path and sqlite_path != ":memory:":
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    engine_kwargs["connect_args"] = {"check_same_thread": False}
elif database_url.startswith("postgresql"):
    engine_kwargs["pool_recycle"] = 300

engine = create_engine(database_url, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Dependency for getting DB session in FastAPI routes"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
