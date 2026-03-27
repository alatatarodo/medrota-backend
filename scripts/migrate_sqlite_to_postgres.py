import argparse

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import build_default_sqlite_url, settings
from app.db.database import Base
from app.db.models import (
    ComplianceViolation,
    Contract,
    Doctor,
    DoctorAvailabilityEvent,
    FairnessMetric,
    GeneratedSchedule,
    LocumRequest,
    OperationAuditLog,
    ScheduleAssignment,
    ServiceRequirement,
    ShiftType,
    SpecialRequirement,
)

MODEL_ORDER = [
    Doctor,
    Contract,
    SpecialRequirement,
    ShiftType,
    ServiceRequirement,
    GeneratedSchedule,
    ScheduleAssignment,
    ComplianceViolation,
    FairnessMetric,
    DoctorAvailabilityEvent,
    LocumRequest,
    OperationAuditLog,
]


def parse_args():
    parser = argparse.ArgumentParser(description="Copy Med Rota data from SQLite into Postgres.")
    parser.add_argument(
        "--source-url",
        default=build_default_sqlite_url(settings.data_dir),
        help="Source database URL. Defaults to the local SQLite fallback.",
    )
    parser.add_argument(
        "--target-url",
        default=settings.database_url,
        help="Target database URL. This should point to Postgres.",
    )
    parser.add_argument(
        "--reset-target",
        action="store_true",
        help="Delete existing target data before importing.",
    )
    return parser.parse_args()


def row_from_instance(instance):
    return {
        column.name: getattr(instance, column.name)
        for column in instance.__table__.columns
    }


def existing_target_tables(session: Session) -> list[str]:
    populated = []
    for model in MODEL_ORDER:
        if session.execute(select(model).limit(1)).scalars().first():
            populated.append(model.__tablename__)
    return populated


def reset_target_data(session: Session) -> None:
    for model in reversed(MODEL_ORDER):
        session.execute(model.__table__.delete())
    session.commit()


def main():
    args = parse_args()

    source_url = args.source_url.strip()
    target_url = args.target_url.strip()

    if not source_url:
        raise SystemExit("A source database URL is required.")

    if not target_url:
        raise SystemExit("A target database URL is required.")

    if source_url == target_url:
        raise SystemExit("Source and target database URLs must be different.")

    if not target_url.startswith("postgresql://"):
        raise SystemExit("Target database must use a postgresql:// URL.")

    source_engine = create_engine(source_url, pool_pre_ping=True)
    target_engine = create_engine(target_url, pool_pre_ping=True, pool_recycle=300)

    Base.metadata.create_all(bind=target_engine)

    with Session(source_engine) as source_session, Session(target_engine) as target_session:
        populated_tables = existing_target_tables(target_session)
        if populated_tables and not args.reset_target:
            joined = ", ".join(populated_tables)
            raise SystemExit(
                f"Target database is not empty ({joined}). Re-run with --reset-target if you want to replace it."
            )

        if populated_tables and args.reset_target:
            reset_target_data(target_session)

        copied_rows = 0
        for model in MODEL_ORDER:
            rows = [
                row_from_instance(instance)
                for instance in source_session.execute(select(model)).scalars().all()
            ]

            if rows:
                target_session.execute(model.__table__.insert(), rows)
                target_session.commit()

            copied_rows += len(rows)
            print(f"{model.__tablename__}: copied {len(rows)} rows")

    print(f"Migration complete. Total rows copied: {copied_rows}")


if __name__ == "__main__":
    main()
