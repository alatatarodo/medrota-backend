from sqlalchemy import Column, String, Integer, Date, Boolean, Float, DateTime, Text, Enum, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.database import Base
from datetime import datetime
import enum


# Enums for grades and constraint types
class DoctorGrade(str, enum.Enum):
    FY1 = "FY1"
    FY2 = "FY2"
    SHO = "SHO"
    REGISTRAR = "Registrar"
    CONSULTANT = "Consultant"
    ST1 = "ST1"
    ST2 = "ST2"
    ST3 = "ST3"
    ST4 = "ST4"
    ST5 = "ST5"
    ST6 = "ST6"
    ST7 = "ST7"
    ST8 = "ST8"


class ConstraintType(str, enum.Enum):
    HARD = "HARD"
    SOFT = "SOFT"


class AssignmentStatus(str, enum.Enum):
    ASSIGNED = "ASSIGNED"
    PENDING_REVIEW = "PENDING_REVIEW"
    EXCEPTION = "EXCEPTION"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"


class AvailabilityEventType(str, enum.Enum):
    ZERO_DAY = "ZERO_DAY"
    TCPD_DAY = "TCPD_DAY"
    TEACHING_DAY = "TEACHING_DAY"
    SICKNESS = "SICKNESS"
    PATERNITY_LEAVE = "PATERNITY_LEAVE"
    MATERNITY_LEAVE = "MATERNITY_LEAVE"
    ANNUAL_LEAVE = "ANNUAL_LEAVE"
    SHIFT_SWAP = "SHIFT_SWAP"


class ComplianceLevel(str, enum.Enum):
    STANDARD = "STANDARD"
    ENHANCED = "ENHANCED"
    CRITICAL = "CRITICAL"


class LocumStaffType(str, enum.Enum):
    BANK = "BANK"
    AGENCY = "AGENCY"
    INTERNAL = "INTERNAL"


class LocumApprovalStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    FILLED = "FILLED"
    DECLINED = "DECLINED"


# Doctor Model
class Doctor(Base):
    __tablename__ = "doctors"
    
    id = Column(String(36), primary_key=True)
    gmc_number = Column(String(8), unique=True, nullable=False, index=True)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    email = Column(String(100), nullable=False)
    grade = Column(Enum(DoctorGrade), nullable=False)
    specialty = Column(String(100), nullable=False)
    hospital_site = Column(String(100), nullable=False, default="Wythenshawe Hospital")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    # Relationships
    contracts = relationship("Contract", back_populates="doctor", cascade="all, delete-orphan")
    special_requirements = relationship("SpecialRequirement", back_populates="doctor", cascade="all, delete-orphan")
    assignments = relationship("ScheduleAssignment", back_populates="doctor")
    availability_events = relationship(
        "DoctorAvailabilityEvent",
        back_populates="doctor",
        cascade="all, delete-orphan",
        foreign_keys="DoctorAvailabilityEvent.doctor_id",
    )


# Contract Model
class Contract(Base):
    __tablename__ = "contracts"
    
    id = Column(String(36), primary_key=True)
    doctor_id = Column(String(36), ForeignKey("doctors.id"), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    contracted_hours_per_week = Column(Integer, nullable=False)  # e.g., 40
    fte = Column(Float, nullable=False)  # 1.0, 0.5, etc.
    contract_type = Column(String(50), nullable=False)  # Full-time, Part-time, etc.
    on_call_available = Column(Boolean, default=True)
    night_shift_available = Column(Boolean, default=True)
    annual_leave_days = Column(Integer, default=27)
    study_leave_days = Column(Integer, default=5)
    maternity_leave_available = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    # Relationships
    doctor = relationship("Doctor", back_populates="contracts")
    
    __table_args__ = (UniqueConstraint('doctor_id', 'start_date', name='uq_doctor_startdate'),)


# Special Requirements Model
class SpecialRequirement(Base):
    __tablename__ = "special_requirements"
    
    id = Column(String(36), primary_key=True)
    doctor_id = Column(String(36), ForeignKey("doctors.id"), nullable=False)
    requirement_type = Column(String(50), nullable=False)  # MEDICAL_RESTRICTION, TRAINING_REQUIREMENT, etc.
    description = Column(String(500))
    start_date = Column(Date)
    end_date = Column(Date)
    constraint_type = Column(Enum(ConstraintType), nullable=False)
    created_at = Column(DateTime, default=func.now())
    
    # Relationships
    doctor = relationship("Doctor", back_populates="special_requirements")


# Shift Types Model
class ShiftType(Base):
    __tablename__ = "shift_types"
    
    id = Column(String(36), primary_key=True)
    code = Column(String(20), unique=True, nullable=False)  # DAYTIME, LONG_DAY, NIGHT, ONCALL
    name = Column(String(100), nullable=False)
    duration_hours = Column(Integer, nullable=False)
    availability_grades = Column(String(500))  # JSON string of allowed grades
    is_weekend_eligible = Column(Boolean, default=True)
    is_night_shift = Column(Boolean, default=False)
    is_on_call = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())


# Service Requirements Model
class ServiceRequirement(Base):
    __tablename__ = "service_requirements"
    
    id = Column(String(36), primary_key=True)
    ward_or_clinic = Column(String(100), nullable=False)
    day_of_week = Column(String(10), nullable=False)  # MON, TUE, etc. or ALL
    shift_type_id = Column(String(36), ForeignKey("shift_types.id"))
    required_doctors = Column(Integer, nullable=False)
    grade_distribution = Column(Text)  # JSON string of grade requirements
    created_at = Column(DateTime, default=func.now())


# Generated Schedules Model
class GeneratedSchedule(Base):
    __tablename__ = "generated_schedules"
    
    id = Column(String(36), primary_key=True)
    schedule_year = Column(Integer, nullable=False)
    generated_at = Column(DateTime, default=func.now())
    algorithm_version = Column(String(20))
    total_doctors = Column(Integer)
    generated_successfully = Column(Boolean)
    compliance_score = Column(Float)  # 0-100
    fairness_score = Column(Float)    # 0-100
    exception_count = Column(Integer, default=0)
    notes = Column(Text)
    created_at = Column(DateTime, default=func.now())
    
    # Relationships
    assignments = relationship("ScheduleAssignment", back_populates="schedule", cascade="all, delete-orphan")
    violations = relationship("ComplianceViolation", back_populates="schedule", cascade="all, delete-orphan")
    fairness_metrics = relationship("FairnessMetric", back_populates="schedule", cascade="all, delete-orphan")


# Schedule Assignments Model
class ScheduleAssignment(Base):
    __tablename__ = "schedule_assignments"
    
    id = Column(String(36), primary_key=True)
    schedule_id = Column(String(36), ForeignKey("generated_schedules.id"), nullable=False)
    doctor_id = Column(String(36), ForeignKey("doctors.id"), nullable=False)
    assignment_date = Column(Date, nullable=False)
    shift_type_id = Column(String(36), ForeignKey("shift_types.id"))
    status = Column(Enum(AssignmentStatus), default=AssignmentStatus.ASSIGNED)
    notes = Column(Text)
    created_at = Column(DateTime, default=func.now())
    
    # Relationships
    schedule = relationship("GeneratedSchedule", back_populates="assignments")
    doctor = relationship("Doctor", back_populates="assignments")
    
    __table_args__ = (
        UniqueConstraint('schedule_id', 'doctor_id', 'assignment_date', name='uq_schedule_doctor_date'),
        Index('idx_schedule_id', 'schedule_id'),
    )


# Compliance Violations Model
class ComplianceViolation(Base):
    __tablename__ = "compliance_violations"
    
    id = Column(String(36), primary_key=True)
    schedule_id = Column(String(36), ForeignKey("generated_schedules.id"), nullable=False)
    doctor_id = Column(String(36), ForeignKey("doctors.id"), nullable=False)
    violation_type = Column(String(100), nullable=False)  # e.g., EXCESS_HOURS, INSUFFICIENT_REST
    severity = Column(String(20), nullable=False)  # ERROR, WARNING
    description = Column(Text, nullable=False)
    suggested_fix = Column(Text)
    created_at = Column(DateTime, default=func.now())
    
    # Relationships
    schedule = relationship("GeneratedSchedule", back_populates="violations")


# Fairness Metrics Model
class FairnessMetric(Base):
    __tablename__ = "fairness_metrics"
    
    id = Column(String(36), primary_key=True)
    schedule_id = Column(String(36), ForeignKey("generated_schedules.id"), nullable=False)
    doctor_id = Column(String(36), ForeignKey("doctors.id"), nullable=False)
    metric_type = Column(String(50), nullable=False)  # NIGHT_SHIFTS, WEEKENDS, ONCALLS, LONG_DAYS
    assigned_count = Column(Integer, nullable=False)
    target_count = Column(Integer, nullable=False)
    variance = Column(Float)
    within_acceptable_range = Column(Boolean)
    created_at = Column(DateTime, default=func.now())
    
    # Relationships
    schedule = relationship("GeneratedSchedule", back_populates="fairness_metrics")


class DoctorAvailabilityEvent(Base):
    __tablename__ = "doctor_availability_events"

    id = Column(String(36), primary_key=True)
    doctor_id = Column(String(36), ForeignKey("doctors.id"), nullable=False)
    hospital_site = Column(String(100), nullable=False)
    event_type = Column(Enum(AvailabilityEventType), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    session_label = Column(String(50), default="ALL_DAY")
    status = Column(String(30), default="APPROVED")
    reason_category = Column(String(100))
    related_doctor_id = Column(String(36), ForeignKey("doctors.id"))
    notes = Column(Text)
    created_at = Column(DateTime, default=func.now())

    doctor = relationship("Doctor", back_populates="availability_events", foreign_keys=[doctor_id])

    __table_args__ = (
        Index("idx_availability_doctor_dates", "doctor_id", "start_date", "end_date"),
    )


class LocumRequest(Base):
    __tablename__ = "locum_requests"

    id = Column(String(36), primary_key=True)
    hospital_site = Column(String(100), nullable=False)
    department = Column(String(100), nullable=False)
    ward = Column(String(100), nullable=False)
    requested_date = Column(Date, nullable=False)
    shift_type_id = Column(String(36), ForeignKey("shift_types.id"))
    required_grade = Column(Enum(DoctorGrade), nullable=False)
    compliance_level = Column(Enum(ComplianceLevel), nullable=False, default=ComplianceLevel.STANDARD)
    staff_type = Column(Enum(LocumStaffType), nullable=False, default=LocumStaffType.BANK)
    approval_status = Column(Enum(LocumApprovalStatus), nullable=False, default=LocumApprovalStatus.PENDING_APPROVAL)
    approval_required = Column(Boolean, default=True)
    requested_hours = Column(Integer, nullable=False, default=8)
    estimated_cost = Column(Float, default=0)
    shortage_reason = Column(String(255))
    requested_by = Column(String(100))
    approved_by = Column(String(100))
    booked_doctor_name = Column(String(100))
    notes = Column(Text)
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        Index("idx_locum_request_site_date", "hospital_site", "requested_date"),
    )


class OperationAuditLog(Base):
    __tablename__ = "operation_audit_logs"

    id = Column(String(36), primary_key=True)
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(String(36), nullable=False)
    action = Column(String(50), nullable=False)
    hospital_site = Column(String(100))
    actor_name = Column(String(100))
    summary = Column(String(255), nullable=False)
    detail = Column(Text)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_operation_audit_created_at", "created_at"),
        Index("idx_operation_audit_entity", "entity_type", "entity_id"),
    )
