from pydantic import BaseModel, EmailStr, Field
from datetime import date, datetime
from typing import Any, Optional, List
from app.db.models import (
    AvailabilityEventType,
    ComplianceLevel,
    ConstraintType,
    DoctorGrade,
    LocumStaffType,
)


# Doctor Schemas
class DoctorCreate(BaseModel):
    gmc_number: str
    title: Optional[str] = "Dr"
    first_name: str
    preferred_name: Optional[str] = None
    last_name: str
    email: EmailStr
    grade: DoctorGrade
    specialty: str
    department: Optional[str] = None
    ward: Optional[str] = None
    employment_type: Optional[str] = "Substantive"
    training_stage: Optional[str] = None
    roster_role: Optional[str] = None
    hospital_site: str = "Wythenshawe Hospital"


class DoctorUpdate(BaseModel):
    title: Optional[str] = None
    preferred_name: Optional[str] = None
    grade: Optional[DoctorGrade] = None
    specialty: Optional[str] = None
    department: Optional[str] = None
    ward: Optional[str] = None
    employment_type: Optional[str] = None
    training_stage: Optional[str] = None
    roster_role: Optional[str] = None
    hospital_site: Optional[str] = None


class DoctorResponse(BaseModel):
    id: str
    gmc_number: str
    title: str
    first_name: str
    preferred_name: Optional[str] = None
    last_name: str
    email: str
    grade: DoctorGrade
    specialty: str
    department: Optional[str] = None
    ward: Optional[str] = None
    employment_type: str
    training_stage: Optional[str] = None
    roster_role: Optional[str] = None
    hospital_site: str
    created_at: datetime

    class Config:
        from_attributes = True


# Contract Schemas
class ContractCreate(BaseModel):
    doctor_id: str
    start_date: date
    end_date: date
    contracted_hours_per_week: int = Field(ge=1, le=56)
    fte: float = Field(ge=0.1, le=1.0)
    contract_type: str
    on_call_available: bool = True
    night_shift_available: bool = True
    annual_leave_days: int = 27
    study_leave_days: int = 5


class ContractResponse(BaseModel):
    id: str
    doctor_id: str
    start_date: date
    end_date: date
    contracted_hours_per_week: int
    fte: float
    contract_type: str
    on_call_available: bool
    night_shift_available: bool
    annual_leave_days: int
    study_leave_days: int

    class Config:
        from_attributes = True


# Special Requirements Schemas
class SpecialRequirementCreate(BaseModel):
    doctor_id: str
    requirement_type: str
    description: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    constraint_type: ConstraintType


class SpecialRequirementResponse(BaseModel):
    id: str
    doctor_id: str
    requirement_type: str
    description: Optional[str]
    constraint_type: ConstraintType

    class Config:
        from_attributes = True


# Shift Type Schemas
class ShiftTypeCreate(BaseModel):
    code: str
    name: str
    duration_hours: int
    availability_grades: List[str]
    is_weekend_eligible: bool = True
    is_night_shift: bool = False
    is_on_call: bool = False


class ShiftTypeResponse(BaseModel):
    id: str
    code: str
    name: str
    duration_hours: int
    availability_grades: List[str]
    is_night_shift: bool
    is_on_call: bool

    class Config:
        from_attributes = True


# Schedule Upload/Validation Schemas
class ScheduleValidationResult(BaseModel):
    status: str  # success, partial, error
    valid_records: int
    invalid_records: int
    warnings: int
    errors: List[dict] = []


class ScheduleGenerationRequest(BaseModel):
    year: int
    doctors: Optional[List[str]] = None  # If None, use all
    hospital_sites: Optional[List[str]] = None
    algorithm_config: Optional[dict] = None


class ScheduleGenerationStatus(BaseModel):
    status: str  # processing, complete, error
    job_id: Optional[str] = None
    estimated_time_seconds: Optional[int] = None
    poll_url: Optional[str] = None


# Report Schemas
class ComplianceReportDetail(BaseModel):
    doctor_id: str
    doctor_name: str
    hospital_site: Optional[str] = None
    violation_type: str
    severity: str  # ERROR, WARNING
    description: str
    suggested_fix: Optional[str]


class ComplianceReport(BaseModel):
    schedule_id: str
    generated_datetime: datetime
    summary: dict
    violations: List[ComplianceReportDetail]


class FairnessReportMetric(BaseModel):
    metric: str  # NIGHTS, WEEKENDS, etc.
    target_mean: float
    actual_mean: float
    std_dev: float
    acceptable: bool


class FairnessReport(BaseModel):
    schedule_id: str
    overall_score: float  # 0-100
    grade_breakdown: dict
    metrics: dict
    outliers: List[dict]


# Schedule Assignment Response
class ScheduleAssignmentResponse(BaseModel):
    id: str
    doctor_id: str
    doctor_name: Optional[str] = None
    hospital_site: Optional[str] = None
    assignment_date: date
    shift_type_id: Optional[str]
    status: str

    class Config:
        from_attributes = True


# Batch Import Schemas
class BatchDoctorImport(BaseModel):
    doctors: List[DoctorCreate]
    contracts: List[ContractCreate]


class ImportResponse(BaseModel):
    status: str
    imported: int
    errors: List[dict]


class AvailabilityEventCreate(BaseModel):
    doctor_id: str
    event_type: AvailabilityEventType
    start_date: date
    end_date: date
    session_label: str = "ALL_DAY"
    status: str = "APPROVED"
    reason_category: Optional[str] = None
    related_doctor_id: Optional[str] = None
    notes: Optional[str] = None


class AvailabilityEventUpdate(AvailabilityEventCreate):
    pass


class LocumRequestCreate(BaseModel):
    hospital_site: str
    department: str
    ward: str
    requested_date: date
    shift_code: str
    required_grade: DoctorGrade
    compliance_level: ComplianceLevel = ComplianceLevel.STANDARD
    staff_type: LocumStaffType = LocumStaffType.BANK
    approval_required: bool = True
    requested_hours: int = Field(default=8, ge=1, le=24)
    shortage_reason: str
    requested_by: str
    notes: Optional[str] = None


class LocumRequestUpdate(LocumRequestCreate):
    pass


class CopilotQuickAction(BaseModel):
    label: str
    action_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CopilotStatusResponse(BaseModel):
    configured: bool
    mode: str
    model: str
    starter_prompts: List[str]
    guardrails: List[str]


class CopilotQueryRequest(BaseModel):
    message: str = Field(min_length=2, max_length=3000)
    active_module: Optional[str] = None
    hospital_site: Optional[str] = None
    schedule_id: Optional[str] = None


class CopilotQueryResponse(BaseModel):
    mode: str
    configured: bool
    headline: str
    answer: str
    risk_level: str
    quick_actions: List[CopilotQuickAction] = Field(default_factory=list)
    follow_up_questions: List[str] = Field(default_factory=list)


class CopilotDraftRequest(BaseModel):
    draft_type: str = Field(min_length=3, max_length=100)
    hospital_site: Optional[str] = None
    context: dict[str, Any] = Field(default_factory=dict)


class CopilotDraftResponse(BaseModel):
    mode: str
    configured: bool
    draft_type: str
    title: str
    text: str
