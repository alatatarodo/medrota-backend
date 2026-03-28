from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session
import json
import uuid
from app.db.database import get_db
from app.db.models import Doctor, Contract, DoctorGrade
from app.core.schemas import DoctorCreate, DoctorResponse, ContractCreate, ContractResponse, BatchDoctorImport, ImportResponse
from app.db.database import Base, engine

router = APIRouter(prefix="/api/v1/doctors", tags=["doctors"])

TRAINING_STAGE_DEFAULTS = {
    DoctorGrade.FY1: "Foundation Year 1",
    DoctorGrade.FY2: "Foundation Year 2",
    DoctorGrade.SHO: "Core Trainee / SHO",
    DoctorGrade.ST1: "Specialty Training Year 1",
    DoctorGrade.ST2: "Specialty Training Year 2",
    DoctorGrade.ST3: "Specialty Training Year 3",
    DoctorGrade.ST4: "Specialty Training Year 4",
    DoctorGrade.ST5: "Specialty Training Year 5",
    DoctorGrade.ST6: "Specialty Training Year 6",
    DoctorGrade.ST7: "Specialty Training Year 7",
    DoctorGrade.ST8: "Specialty Training Year 8",
    DoctorGrade.REGISTRAR: "Senior Registrar",
    DoctorGrade.CONSULTANT: "Consultant Grade",
}


def _doctor_defaults(payload: DoctorCreate | None = None) -> dict:
    grade = payload.grade if payload else DoctorGrade.FY1
    specialty = payload.specialty if payload else "Medicine"
    return {
        "title": (payload.title if payload else None) or "Dr",
        "preferred_name": (payload.preferred_name if payload else None) or (payload.first_name if payload else None),
        "department": (payload.department if payload else None) or specialty,
        "ward": (payload.ward if payload else None) or "Unassigned Base Ward",
        "employment_type": (payload.employment_type if payload else None) or "Substantive",
        "training_stage": (payload.training_stage if payload else None) or TRAINING_STAGE_DEFAULTS.get(grade, grade.value if hasattr(grade, "value") else str(grade)),
        "roster_role": (payload.roster_role if payload else None) or ("Consultant" if grade == DoctorGrade.CONSULTANT else "Resident Doctor"),
    }


def _normalize_competencies(raw_competencies) -> list[str]:
    if not raw_competencies:
        return []
    if isinstance(raw_competencies, list):
        values = raw_competencies
    else:
        try:
            values = json.loads(raw_competencies)
        except (TypeError, json.JSONDecodeError):
            values = str(raw_competencies).split(",")
    seen = set()
    normalized = []
    for value in values:
        cleaned = " ".join(str(value or "").strip().split())
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(cleaned)
    return normalized


def _serialize_doctor(doctor: Doctor) -> dict:
    return {
        "id": doctor.id,
        "gmc_number": doctor.gmc_number,
        "title": doctor.title or "Dr",
        "first_name": doctor.first_name,
        "preferred_name": doctor.preferred_name,
        "last_name": doctor.last_name,
        "email": doctor.email,
        "grade": doctor.grade,
        "specialty": doctor.specialty,
        "department": doctor.department,
        "ward": doctor.ward,
        "competencies": _normalize_competencies(doctor.competencies),
        "employment_type": doctor.employment_type,
        "training_stage": doctor.training_stage,
        "roster_role": doctor.roster_role,
        "hospital_site": doctor.hospital_site,
        "created_at": doctor.created_at,
    }


@router.post("/", response_model=DoctorResponse, status_code=status.HTTP_201_CREATED)
def create_doctor(doctor: DoctorCreate, db: Session = Depends(get_db)):
    """Create a new doctor record"""
    
    # Check if GMC number already exists
    existing = db.query(Doctor).filter(Doctor.gmc_number == doctor.gmc_number).first()
    if existing:
        raise HTTPException(status_code=400, detail="GMC number already exists")
    
    defaults = _doctor_defaults(doctor)

    db_doctor = Doctor(
        id=str(uuid.uuid4()),
        gmc_number=doctor.gmc_number,
        title=defaults["title"],
        first_name=doctor.first_name,
        preferred_name=defaults["preferred_name"],
        last_name=doctor.last_name,
        email=doctor.email,
        grade=doctor.grade,
        specialty=doctor.specialty,
        department=defaults["department"],
        ward=defaults["ward"],
        competencies=json.dumps(_normalize_competencies(doctor.competencies)),
        employment_type=defaults["employment_type"],
        training_stage=defaults["training_stage"],
        roster_role=defaults["roster_role"],
        hospital_site=doctor.hospital_site,
    )
    
    db.add(db_doctor)
    db.commit()
    db.refresh(db_doctor)
    return _serialize_doctor(db_doctor)


@router.get("/", response_model=list[DoctorResponse])
def list_doctors(
    skip: int = 0,
    limit: int = 100,
    hospital_site: str = None,
    grade: DoctorGrade = None,
    department: str = None,
    ward: str = None,
    competency: str = None,
    search: str = None,
    db: Session = Depends(get_db)
):
    """List all doctors with pagination"""
    query = db.query(Doctor)

    if hospital_site:
        query = query.filter(Doctor.hospital_site == hospital_site)

    if grade:
        query = query.filter(Doctor.grade == grade)

    if department:
        query = query.filter(Doctor.department == department)

    if ward:
        query = query.filter(Doctor.ward == ward)

    if competency:
        competency_term = f"%{competency.strip()}%"
        query = query.filter(Doctor.competencies.ilike(competency_term))

    if search:
        search_term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Doctor.id.ilike(search_term),
                Doctor.gmc_number.ilike(search_term),
                Doctor.title.ilike(search_term),
                Doctor.first_name.ilike(search_term),
                Doctor.preferred_name.ilike(search_term),
                Doctor.last_name.ilike(search_term),
                Doctor.email.ilike(search_term),
                Doctor.department.ilike(search_term),
                Doctor.ward.ilike(search_term),
                Doctor.competencies.ilike(search_term),
            )
        )

    doctors = query.offset(skip).limit(limit).all()
    return [_serialize_doctor(doctor) for doctor in doctors]


@router.get("/{doctor_id}", response_model=DoctorResponse)
def get_doctor(doctor_id: str, db: Session = Depends(get_db)):
    """Get a specific doctor by ID"""
    doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    return _serialize_doctor(doctor)


@router.post("/batch-import", response_model=ImportResponse)
def batch_import_doctors(payload: BatchDoctorImport, db: Session = Depends(get_db)):
    """
    Batch import doctors and their contracts
    Useful for CSV/Excel uploads
    """
    
    imported_count = 0
    errors = []
    doctor_lookup = {}
    
    try:
        # Import doctors
        for doctor_data in payload.doctors:
            try:
                # Check for duplicates
                existing = db.query(Doctor).filter(Doctor.gmc_number == doctor_data.gmc_number).first()
                if existing:
                    doctor_lookup[existing.id] = existing
                    doctor_lookup[existing.gmc_number] = existing
                    errors.append({
                        "type": "duplicate",
                        "gmc": doctor_data.gmc_number,
                        "message": "Doctor GMC already exists"
                    })
                    continue
                
                defaults = _doctor_defaults(doctor_data)

                db_doctor = Doctor(
                    id=str(uuid.uuid4()),
                    gmc_number=doctor_data.gmc_number,
                    title=defaults["title"],
                    first_name=doctor_data.first_name,
                    preferred_name=defaults["preferred_name"],
                    last_name=doctor_data.last_name,
                    email=doctor_data.email,
                    grade=doctor_data.grade,
                    specialty=doctor_data.specialty,
                    department=defaults["department"],
                    ward=defaults["ward"],
                    competencies=json.dumps(_normalize_competencies(doctor_data.competencies)),
                    employment_type=defaults["employment_type"],
                    training_stage=defaults["training_stage"],
                    roster_role=defaults["roster_role"],
                    hospital_site=doctor_data.hospital_site,
                )
                db.add(db_doctor)
                db.flush()
                doctor_lookup[db_doctor.id] = db_doctor
                doctor_lookup[db_doctor.gmc_number] = db_doctor
                imported_count += 1
            
            except Exception as e:
                errors.append({
                    "type": "doctor_error",
                    "gmc": doctor_data.gmc_number,
                    "message": str(e)
                })
        
        db.commit()
        
        # Import contracts
        for contract_data in payload.contracts:
            try:
                doctor = doctor_lookup.get(contract_data.doctor_id)
                if not doctor:
                    doctor = db.query(Doctor).filter(
                        or_(
                            Doctor.id == contract_data.doctor_id,
                            Doctor.gmc_number == contract_data.doctor_id,
                        )
                    ).first()

                if not doctor:
                    errors.append({
                        "type": "contract_error",
                        "doctor_id": contract_data.doctor_id,
                        "message": "Doctor not found by ID or GMC number"
                    })
                    continue
                
                # Validate contract dates
                if contract_data.start_date >= contract_data.end_date:
                    errors.append({
                        "type": "contract_error",
                        "doctor_id": contract_data.doctor_id,
                        "message": "Start date must be before end date"
                    })
                    continue
                
                db_contract = Contract(
                    id=str(uuid.uuid4()),
                    doctor_id=doctor.id,
                    start_date=contract_data.start_date,
                    end_date=contract_data.end_date,
                    contracted_hours_per_week=contract_data.contracted_hours_per_week,
                    fte=contract_data.fte,
                    contract_type=contract_data.contract_type,
                    on_call_available=contract_data.on_call_available,
                    night_shift_available=contract_data.night_shift_available,
                    annual_leave_days=contract_data.annual_leave_days,
                    study_leave_days=contract_data.study_leave_days
                )
                db.add(db_contract)
            
            except Exception as e:
                errors.append({
                    "type": "contract_error",
                    "doctor_id": contract_data.doctor_id,
                    "message": str(e)
                })
        
        db.commit()
        
        return ImportResponse(
            status="success" if not errors else "partial",
            imported=imported_count,
            errors=errors
        )
    
    except Exception as e:
        db.rollback()
        return ImportResponse(
            status="error",
            imported=0,
            errors=[{"type": "batch_error", "message": str(e)}]
        )


@router.post("/{doctor_id}/contracts", response_model=ContractResponse, status_code=status.HTTP_201_CREATED)
def create_contract(doctor_id: str, contract: ContractCreate, db: Session = Depends(get_db)):
    """Create a contract for a doctor"""
    
    doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    
    db_contract = Contract(
        id=str(uuid.uuid4()),
        doctor_id=doctor_id,
        start_date=contract.start_date,
        end_date=contract.end_date,
        contracted_hours_per_week=contract.contracted_hours_per_week,
        fte=contract.fte,
        contract_type=contract.contract_type,
        on_call_available=contract.on_call_available,
        night_shift_available=contract.night_shift_available,
        annual_leave_days=contract.annual_leave_days,
        study_leave_days=contract.study_leave_days
    )
    
    db.add(db_contract)
    db.commit()
    db.refresh(db_contract)
    return db_contract


@router.get("/{doctor_id}/contracts", response_model=list[ContractResponse])
def list_contracts(doctor_id: str, db: Session = Depends(get_db)):
    """Get all contracts for a doctor"""
    
    doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    
    contracts = db.query(Contract).filter(Contract.doctor_id == doctor_id).all()
    return contracts
