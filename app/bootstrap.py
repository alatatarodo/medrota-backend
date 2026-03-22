from datetime import date
import uuid

from sqlalchemy.orm import Session

from app.db.models import Contract, Doctor, DoctorGrade

HOSPITAL_SITES = ["Wythenshawe Hospital", "Trafford Hospital"]
GRADE_CYCLE = [
    DoctorGrade.FY1,
    DoctorGrade.FY2,
    DoctorGrade.SHO,
    DoctorGrade.REGISTRAR,
    DoctorGrade.CONSULTANT,
    DoctorGrade.ST1,
    DoctorGrade.ST2,
    DoctorGrade.ST3,
    DoctorGrade.ST4,
    DoctorGrade.ST5,
]
SPECIALTY_CYCLE = [
    "Medicine",
    "Emergency Medicine",
    "General Surgery",
    "Anaesthetics",
]


def seed_sample_data(db: Session) -> None:
    """Seed a baseline 1,600-doctor sample dataset when the database is empty."""
    if db.query(Doctor).count() > 0:
        return

    start_year = date.today().year
    doctors_to_create = []
    contracts_to_create = []

    for site_index, site_name in enumerate(HOSPITAL_SITES):
        for site_offset in range(800):
            sequence = site_index * 800 + site_offset + 1
            doctor_id = f"doc-{sequence:05d}"
            doctor = Doctor(
                id=doctor_id,
                gmc_number=f"70{sequence:05d}",
                first_name=f"Doctor{sequence:04d}",
                last_name="Wythenshawe" if site_name == "Wythenshawe Hospital" else "Trafford",
                email=f"doctor{sequence}@medrota.ai",
                grade=GRADE_CYCLE[(sequence - 1) % len(GRADE_CYCLE)],
                specialty=SPECIALTY_CYCLE[(sequence - 1) % len(SPECIALTY_CYCLE)],
                hospital_site=site_name,
            )
            doctors_to_create.append(doctor)
            contracts_to_create.append(
                Contract(
                    id=str(uuid.uuid4()),
                    doctor_id=doctor_id,
                    start_date=date(start_year, 8, 1),
                    end_date=date(start_year + 1, 7, 31),
                    contracted_hours_per_week=40,
                    fte=1.0,
                    contract_type="Full-time",
                    on_call_available=True,
                    night_shift_available=True,
                    annual_leave_days=27,
                    study_leave_days=5,
                )
            )

    db.bulk_save_objects(doctors_to_create)
    db.bulk_save_objects(contracts_to_create)
    db.commit()
