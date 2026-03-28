from collections import defaultdict
from datetime import date, datetime, timedelta
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.schemas import (
    AvailabilityEventCreate,
    AvailabilityEventUpdate,
    LocumRequestCreate,
    LocumRequestUpdate,
    ServiceRequirementCreate,
    ServiceRequirementUpdate,
)
from app.db.database import get_db
from app.db.models import (
    AvailabilityEventType,
    ComplianceLevel,
    Doctor,
    DoctorAvailabilityEvent,
    DoctorGrade,
    GeneratedSchedule,
    LocumApprovalStatus,
    LocumRequest,
    OperationAuditLog,
    ScheduleAssignment,
    ServiceRequirement,
    ShiftType,
)

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])

GRADE_ORDER = [
    "FY1",
    "FY2",
    "SHO",
    "ST1",
    "ST2",
    "ST3",
    "ST4",
    "ST5",
    "ST6",
    "ST7",
    "ST8",
    "Registrar",
    "Consultant",
]

SHIFT_METADATA = {
    "MORNING": {"window": "07:00 - 15:00", "label": "Morning cover", "rate": 42, "min_grade": "FY1"},
    "EVENING": {"window": "13:00 - 21:00", "label": "Evening cover", "rate": 54, "min_grade": "FY2"},
    "TWILIGHT": {"window": "16:00 - 02:00", "label": "Twilight surge cover", "rate": 68, "min_grade": "SHO"},
    "NIGHT": {"window": "21:00 - 07:00", "label": "Resident night cover", "rate": 82, "min_grade": "ST3"},
    "LONG_DAY": {"window": "08:00 - 20:00", "label": "Long day cover", "rate": 64, "min_grade": "FY2"},
    "ONCALL": {"window": "20:00 - 08:00", "label": "Non-resident on-call", "rate": 95, "min_grade": "Registrar"},
    "DAYTIME": {"window": "08:00 - 16:00", "label": "Day shift", "rate": 42, "min_grade": "FY1"},
}

DAY_TEMPLATE_ORDER = {
    "ALL": 0,
    "WEEKDAY": 1,
    "WEEKEND": 2,
    "BANK_HOLIDAY": 3,
    "MON": 4,
    "TUE": 5,
    "WED": 6,
    "THU": 7,
    "FRI": 8,
    "SAT": 9,
    "SUN": 10,
}

EVENT_LABELS = {
    AvailabilityEventType.ZERO_DAY: "Zero Day",
    AvailabilityEventType.TCPD_DAY: "TCPD Day",
    AvailabilityEventType.TEACHING_DAY: "Teaching Day",
    AvailabilityEventType.SICKNESS: "Sickness",
    AvailabilityEventType.PATERNITY_LEAVE: "Paternity Leave",
    AvailabilityEventType.MATERNITY_LEAVE: "Maternity Leave",
    AvailabilityEventType.ANNUAL_LEAVE: "Annual Leave",
    AvailabilityEventType.SHIFT_SWAP: "Shift Swap",
}

GRADE_RATE_CARD = {
    "FY1": 38,
    "FY2": 42,
    "SHO": 52,
    "ST1": 44,
    "ST2": 48,
    "ST3": 58,
    "ST4": 62,
    "ST5": 66,
    "ST6": 72,
    "ST7": 78,
    "ST8": 82,
    "Registrar": 72,
    "Consultant": 110,
}

STAFF_MULTIPLIERS = {
    "BANK": 1.0,
    "INTERNAL": 0.92,
    "AGENCY": 1.25,
}

APPROVAL_ROLE_LEVELS = {
    "Service Manager": 1,
    "Medical Staffing Lead": 2,
    "Clinical Director": 3,
    "Chief of Service": 4,
}

ROLE_BY_LEVEL = {level: role for role, level in APPROVAL_ROLE_LEVELS.items()}

APPROVAL_GOVERNANCE_BASE = {
    ComplianceLevel.STANDARD.value: {
        "tier": "Tier 1",
        "approver": "Service Manager",
        "spend_cap": 650.0,
    },
    ComplianceLevel.ENHANCED.value: {
        "tier": "Tier 2",
        "approver": "Medical Staffing Lead",
        "spend_cap": 900.0,
    },
    ComplianceLevel.CRITICAL.value: {
        "tier": "Tier 3",
        "approver": "Clinical Director",
        "spend_cap": 1250.0,
    },
}


def _grade_value(grade: str) -> int:
    try:
        return GRADE_ORDER.index(grade)
    except ValueError:
        return len(GRADE_ORDER)


def _parse_allowed_grades(raw_value) -> list[str]:
    if not raw_value:
        return []
    if isinstance(raw_value, list):
        return [str(item) for item in raw_value]
    try:
        parsed = json.loads(raw_value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except (TypeError, json.JSONDecodeError):
        pass
    return [item.strip() for item in str(raw_value).split(",") if item.strip()]


def _estimate_locum_cost(required_grade: DoctorGrade, staff_type, requested_hours: int) -> float:
    grade_key = required_grade.value if hasattr(required_grade, "value") else str(required_grade)
    staff_key = staff_type.value if hasattr(staff_type, "value") else str(staff_type)
    base_rate = GRADE_RATE_CARD.get(grade_key, 50)
    multiplier = STAFF_MULTIPLIERS.get(staff_key, 1.0)
    return round(base_rate * multiplier * requested_hours, 2)


def _format_doctor_name(doctor: Doctor | None) -> str:
    if not doctor:
        return "Unknown"
    title = (doctor.title or "Dr").strip()
    preferred = (doctor.preferred_name or doctor.first_name or "").strip()
    surname = (doctor.last_name or "").strip()
    return " ".join(part for part in [title, preferred, surname] if part)


def _higher_approver(current_role: str, candidate_role: str) -> str:
    current_level = APPROVAL_ROLE_LEVELS.get(current_role, 0)
    candidate_level = APPROVAL_ROLE_LEVELS.get(candidate_role, 0)
    return ROLE_BY_LEVEL.get(max(current_level, candidate_level), candidate_role if candidate_level >= current_level else current_role)


def _build_locum_governance(request: LocumRequest, shift_pattern: dict) -> dict:
    grade_key = request.required_grade.value if hasattr(request.required_grade, "value") else str(request.required_grade)
    compliance_key = request.compliance_level.value if hasattr(request.compliance_level, "value") else str(request.compliance_level)
    staff_key = request.staff_type.value if hasattr(request.staff_type, "value") else str(request.staff_type)
    cost = float(request.estimated_cost or 0)

    baseline = APPROVAL_GOVERNANCE_BASE.get(compliance_key, APPROVAL_GOVERNANCE_BASE[ComplianceLevel.STANDARD.value])
    recommended_approver = baseline["approver"]
    approval_tier = baseline["tier"]
    spend_cap = baseline["spend_cap"]
    guidance_notes = []

    if _grade_value(grade_key) >= _grade_value("Registrar"):
        recommended_approver = _higher_approver(recommended_approver, "Clinical Director")
        approval_tier = "Tier 3"
        spend_cap = max(spend_cap, 1250.0)
        guidance_notes.append("Registrar-and-above cover requires senior sign-off.")

    if grade_key == DoctorGrade.CONSULTANT.value:
        recommended_approver = _higher_approver(recommended_approver, "Chief of Service")
        approval_tier = "Tier 4"
        spend_cap = max(spend_cap, 1800.0)
        guidance_notes.append("Consultant locums require executive-level approval.")

    if staff_key == "AGENCY":
        recommended_approver = _higher_approver(recommended_approver, "Clinical Director")
        approval_tier = "Tier 3" if approval_tier in {"Tier 1", "Tier 2"} else approval_tier
        spend_cap = max(spend_cap, 950.0)
        guidance_notes.append("Agency usage should include finance visibility.")

    if shift_pattern.get("is_on_call") or shift_pattern.get("is_night_shift"):
        recommended_approver = _higher_approver(recommended_approver, "Medical Staffing Lead")
        spend_cap = max(spend_cap, 900.0)
        guidance_notes.append("Out-of-hours cover should be reviewed against escalation policy.")

    requires_finance_signoff = staff_key == "AGENCY" or cost > spend_cap
    spend_cap_status = "OVER_CAP" if cost > spend_cap else "WITHIN_CAP"

    if cost > spend_cap:
        guidance_notes.append(f"Estimated cost exceeds the policy cap of GBP {spend_cap:,.0f}.")
    elif cost > spend_cap * 0.8:
        guidance_notes.append("Estimated cost is approaching the approval cap.")

    if not guidance_notes:
        guidance_notes.append("Request sits within the standard governance pathway.")

    return {
        "approval_tier": approval_tier,
        "recommended_approver": recommended_approver,
        "spend_cap": spend_cap,
        "spend_cap_status": spend_cap_status,
        "requires_finance_signoff": requires_finance_signoff,
        "governance_note": " ".join(guidance_notes),
    }


def _build_shift_pattern(shift: ShiftType) -> dict:
    shift_code = shift.code or "UNKNOWN"
    metadata = SHIFT_METADATA.get(shift_code, {})
    eligible_grades = sorted(_parse_allowed_grades(shift.availability_grades), key=_grade_value)
    compliance_level = (
        ComplianceLevel.CRITICAL.value if shift.is_on_call
        else ComplianceLevel.ENHANCED.value if shift.is_night_shift or shift_code == "TWILIGHT"
        else ComplianceLevel.STANDARD.value
    )
    approval_required = compliance_level != ComplianceLevel.STANDARD.value

    return {
        "id": shift.id,
        "code": shift_code,
        "name": shift.name,
        "shift_window": metadata.get("window", "TBC"),
        "focus": metadata.get("label", "Operational shift"),
        "duration_hours": shift.duration_hours,
        "eligible_grades": eligible_grades,
        "minimum_grade": metadata.get("min_grade") or (eligible_grades[0] if eligible_grades else "FY1"),
        "compliance_level": compliance_level,
        "approval_required": approval_required,
        "estimated_bank_rate_per_hour": metadata.get("rate", 50),
        "is_night_shift": shift.is_night_shift,
        "is_on_call": shift.is_on_call,
    }


def _serialize_event(event: DoctorAvailabilityEvent, doctors_by_id: dict[str, Doctor]) -> dict:
    doctor = doctors_by_id.get(event.doctor_id)
    related_doctor = doctors_by_id.get(event.related_doctor_id) if event.related_doctor_id else None
    return {
        "id": event.id,
        "doctor_id": event.doctor_id,
        "doctor_name": _format_doctor_name(doctor),
        "doctor_grade": doctor.grade.value if doctor and hasattr(doctor.grade, "value") else str(doctor.grade) if doctor else "Unknown",
        "doctor_department": doctor.department if doctor else None,
        "doctor_ward": doctor.ward if doctor else None,
        "hospital_site": event.hospital_site,
        "event_type": event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type),
        "event_label": EVENT_LABELS.get(event.event_type, str(event.event_type)),
        "start_date": event.start_date.isoformat(),
        "end_date": event.end_date.isoformat(),
        "session_label": event.session_label,
        "status": event.status,
        "reason_category": event.reason_category,
        "related_doctor_id": event.related_doctor_id,
        "related_doctor_name": _format_doctor_name(related_doctor) if related_doctor else None,
        "approved_by": event.approved_by,
        "approved_at": event.approved_at.isoformat() if event.approved_at else None,
        "approval_comment": event.approval_comment,
        "notes": event.notes,
    }


def _serialize_locum_request(request: LocumRequest, shift_patterns_by_id: dict[str, dict]) -> dict:
    shift_pattern = shift_patterns_by_id.get(request.shift_type_id, {})
    governance = _build_locum_governance(request, shift_pattern)
    finance_status = _effective_finance_approval_status(request, governance)
    return {
        "id": request.id,
        "hospital_site": request.hospital_site,
        "department": request.department,
        "ward": request.ward,
        "requested_date": request.requested_date.isoformat(),
        "shift_code": shift_pattern.get("code"),
        "shift_name": shift_pattern.get("name", "Unmapped shift"),
        "shift_window": shift_pattern.get("shift_window"),
        "required_grade": request.required_grade.value if hasattr(request.required_grade, "value") else str(request.required_grade),
        "compliance_level": request.compliance_level.value if hasattr(request.compliance_level, "value") else str(request.compliance_level),
        "staff_type": request.staff_type.value if hasattr(request.staff_type, "value") else str(request.staff_type),
        "approval_status": request.approval_status.value if hasattr(request.approval_status, "value") else str(request.approval_status),
        "approval_required": request.approval_required,
        "requested_hours": request.requested_hours,
        "estimated_cost": request.estimated_cost,
        "shortage_reason": request.shortage_reason,
        "requested_by": request.requested_by,
        "approved_by": request.approved_by,
        "approved_at": request.approved_at.isoformat() if request.approved_at else None,
        "approval_comment": request.approval_comment,
        "finance_approval_status": finance_status,
        "finance_approved_by": request.finance_approved_by,
        "finance_approved_at": request.finance_approved_at.isoformat() if request.finance_approved_at else None,
        "finance_approval_comment": request.finance_approval_comment,
        "booked_doctor_name": request.booked_doctor_name,
        "notes": request.notes,
        **governance,
    }


def _sync_finance_approval_state(request: LocumRequest, shift_pattern: dict) -> None:
    governance = _build_locum_governance(request, shift_pattern)
    if governance["requires_finance_signoff"]:
        current_status = (request.finance_approval_status or "").upper().strip()
        request.finance_approval_status = current_status if current_status in {"APPROVED", "DECLINED"} else "PENDING"
        if request.finance_approval_status != "APPROVED":
            request.finance_approved_by = None
            request.finance_approved_at = None
            request.finance_approval_comment = None
    else:
        request.finance_approval_status = "NOT_REQUIRED"
        request.finance_approved_by = None
        request.finance_approved_at = None
        request.finance_approval_comment = None


def _effective_finance_approval_status(request: LocumRequest, governance: dict) -> str:
    current_status = (request.finance_approval_status or "").upper().strip()
    if governance["requires_finance_signoff"]:
        return current_status if current_status in {"APPROVED", "DECLINED", "PENDING"} else "PENDING"
    return "NOT_REQUIRED"


def _build_grade_mix(doctors: list[Doctor]) -> list[dict]:
    grade_counts = defaultdict(int)
    for doctor in doctors:
        grade_counts[doctor.grade.value if hasattr(doctor.grade, "value") else str(doctor.grade)] += 1

    return [
        {"grade": grade, "count": grade_counts[grade]}
        for grade in GRADE_ORDER
        if grade_counts.get(grade)
    ]


def _build_coverage_pressure(events: list[DoctorAvailabilityEvent], locum_requests: list[LocumRequest]) -> list[dict]:
    pressure_by_site = defaultdict(lambda: {
        "active_absences": 0,
        "zero_days": 0,
        "teaching_days": 0,
        "sickness_events": 0,
        "pending_locums": 0,
        "estimated_spend": 0.0,
    })
    today = date.today()

    for event in events:
        if event.end_date < today or str(event.status).upper() == "CANCELLED":
            continue
        site_summary = pressure_by_site[event.hospital_site]
        site_summary["active_absences"] += 1
        if event.event_type == AvailabilityEventType.ZERO_DAY:
            site_summary["zero_days"] += 1
        if event.event_type in (AvailabilityEventType.TCPD_DAY, AvailabilityEventType.TEACHING_DAY):
            site_summary["teaching_days"] += 1
        if event.event_type == AvailabilityEventType.SICKNESS:
            site_summary["sickness_events"] += 1

    for request in locum_requests:
        approval_status = request.approval_status.value if hasattr(request.approval_status, "value") else str(request.approval_status)
        if approval_status == LocumApprovalStatus.DECLINED.value:
            continue
        site_summary = pressure_by_site[request.hospital_site]
        if request.approval_status in (LocumApprovalStatus.PENDING_APPROVAL, LocumApprovalStatus.APPROVED):
            site_summary["pending_locums"] += 1
        site_summary["estimated_spend"] += float(request.estimated_cost or 0)

    rows = []
    for site, summary in pressure_by_site.items():
        rows.append({
            "site": site,
            **summary,
            "vacant_shifts": summary["pending_locums"] + summary["sickness_events"],
            "estimated_spend": round(summary["estimated_spend"], 2),
        })

    return sorted(rows, key=lambda row: row["site"])


def _parse_service_key(raw_value: str) -> tuple[str, str, str]:
    site, department, ward = (str(raw_value or "").split("::") + ["Unknown", "Unknown", "Unknown"])[:3]
    return site, department, ward


def _normalize_day_template(raw_value: str | None) -> str:
    return str(raw_value or "ALL").strip().upper() or "ALL"


def _is_supported_bank_holiday(target_date: date) -> bool:
    return (target_date.month, target_date.day) in {(1, 1), (12, 25), (12, 26)}


def _service_templates_for_date(target_date: date) -> set[str]:
    templates = {"ALL"}
    weekday_codes = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    templates.add(weekday_codes[target_date.weekday()])
    templates.add("WEEKEND" if target_date.weekday() >= 5 else "WEEKDAY")
    if _is_supported_bank_holiday(target_date):
        templates.add("BANK_HOLIDAY")
    return templates


def _sanitize_grade_distribution(raw_distribution: dict | None) -> dict[str, int]:
    sanitized_distribution = {}
    for grade, count in (raw_distribution or {}).items():
        if not str(grade).strip():
            continue
        try:
            parsed_count = int(count)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid grade count for {grade}")
        if parsed_count > 0:
            sanitized_distribution[str(grade)] = parsed_count
    return sanitized_distribution


def _parse_skill_list(raw_value) -> list[str]:
    if not raw_value:
        return []
    if isinstance(raw_value, list):
        values = raw_value
    else:
        try:
            values = json.loads(raw_value)
        except (TypeError, json.JSONDecodeError):
            values = str(raw_value).split(",")
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


def _sanitize_skill_list(raw_skills: list[str] | None) -> list[str]:
    return _parse_skill_list(raw_skills or [])


def _doctor_competencies(doctor: Doctor) -> list[str]:
    return _parse_skill_list(getattr(doctor, "competencies", None))


def _doctor_has_required_skills(doctor: Doctor, required_skills: list[str]) -> bool:
    if not required_skills:
        return True
    normalized_doctor_skills = {skill.casefold() for skill in _doctor_competencies(doctor)}
    return all(skill.casefold() in normalized_doctor_skills for skill in required_skills)


def _build_establishment_matrix(
    doctors: list[Doctor],
    service_requirements: list[ServiceRequirement],
    shift_patterns_by_id: dict[str, dict],
) -> list[dict]:
    doctors_by_home_base = defaultdict(list)
    for doctor in doctors:
        doctors_by_home_base[(doctor.hospital_site, doctor.department or doctor.specialty, doctor.ward or "Unassigned Base Ward")].append(doctor)

    requirements_by_home_base = defaultdict(list)
    for requirement in service_requirements:
        site, department, ward = _parse_service_key(requirement.ward_or_clinic)
        requirements_by_home_base[(site, department, ward)].append(requirement)

    rows = []
    for home_base, requirement_rows in requirements_by_home_base.items():
        site, department, ward = home_base
        assigned_doctors = doctors_by_home_base.get(home_base, [])
        home_grade_mix = defaultdict(int)
        home_skill_mix = defaultdict(int)
        for doctor in assigned_doctors:
            grade_key = doctor.grade.value if hasattr(doctor.grade, "value") else str(doctor.grade)
            home_grade_mix[grade_key] += 1
            for skill in _doctor_competencies(doctor):
                home_skill_mix[skill] += 1

        shift_requirements = []
        for requirement in requirement_rows:
            shift_pattern = shift_patterns_by_id.get(requirement.shift_type_id, {})
            try:
                grade_distribution = json.loads(requirement.grade_distribution or "{}")
            except (TypeError, json.JSONDecodeError):
                grade_distribution = {}
            required_skills = _parse_skill_list(requirement.required_skills)
            missing_skills = [skill for skill in required_skills if home_skill_mix.get(skill, 0) == 0]

            shift_requirements.append({
                "requirement_id": requirement.id,
                "shift_code": shift_pattern.get("code"),
                "shift_name": shift_pattern.get("name", "Unmapped shift"),
                "day_of_week": _normalize_day_template(requirement.day_of_week),
                "required_doctors": requirement.required_doctors,
                "grade_distribution": grade_distribution,
                "required_skills": required_skills,
                "skill_gap_risk": "GAP_RISK" if missing_skills else "ALIGNED",
                "missing_skills": missing_skills,
                "supervising_consultant": requirement.supervising_consultant,
            })

        core_requirement = max((item["required_doctors"] for item in shift_requirements), default=0)
        home_allocated_doctors = len(assigned_doctors)
        pressure_level = (
            "HIGH" if home_allocated_doctors < core_requirement
            else "MEDIUM" if home_allocated_doctors < core_requirement + 2
            else "LOW"
        )

        rows.append({
            "hospital_site": site,
            "department": department,
            "ward": ward,
            "home_allocated_doctors": home_allocated_doctors,
            "core_requirement": core_requirement,
            "pressure_level": pressure_level,
            "home_grade_mix": dict(home_grade_mix),
            "home_competencies": sorted(home_skill_mix.keys()),
            "supervising_consultants": sorted({item["supervising_consultant"] for item in shift_requirements if item.get("supervising_consultant")}),
            "shift_requirements": sorted(
                shift_requirements,
                key=lambda item: (DAY_TEMPLATE_ORDER.get(item["day_of_week"], 99), item["shift_name"] or ""),
            ),
        })

    return sorted(rows, key=lambda item: (item["hospital_site"], item["department"], item["ward"]))


def _resolve_requirement_grade(requirement: ServiceRequirement, shift_pattern: dict, grade_distribution: dict) -> DoctorGrade:
    ranked_grades = sorted(grade_distribution.keys(), key=_grade_value, reverse=True)
    grade_choice = ranked_grades[0] if ranked_grades else shift_pattern.get("minimum_grade", "FY1")
    try:
        return DoctorGrade(grade_choice)
    except ValueError:
        return DoctorGrade.FY1


def _parse_assignment_context(notes: str | None) -> dict[str, str]:
    context = {}
    if not notes:
        return context
    for segment in str(notes).split(";"):
        if ":" not in segment:
            continue
        key, value = segment.split(":", 1)
        normalized_key = key.strip().lower().replace(" ", "_")
        cleaned_value = value.strip()
        if cleaned_value:
            context[normalized_key] = cleaned_value
    return context


def _build_requirement_shortfalls(
    latest_schedule: GeneratedSchedule | None,
    doctors_by_id: dict[str, Doctor],
    service_requirements: list[ServiceRequirement],
    shift_patterns_by_id: dict[str, dict],
    db: Session,
) -> list[dict]:
    today = date.today()
    window_end = today + timedelta(days=6)
    assignment_counts = defaultdict(int)
    doctors_by_home_base = defaultdict(list)
    for doctor in doctors_by_id.values():
        doctors_by_home_base[(doctor.hospital_site, doctor.department or doctor.specialty, doctor.ward or "Unassigned Base Ward")].append(doctor)

    if latest_schedule:
        assignments = db.query(ScheduleAssignment).filter(
            ScheduleAssignment.schedule_id == latest_schedule.id,
            ScheduleAssignment.assignment_date >= today,
            ScheduleAssignment.assignment_date <= window_end,
        ).all()
        for assignment in assignments:
            doctor = doctors_by_id.get(assignment.doctor_id)
            if not doctor:
                continue
            assignment_context = _parse_assignment_context(assignment.notes)
            assignment_counts[(
                assignment.assignment_date.isoformat(),
                assignment_context.get("hospital_site") or doctor.hospital_site,
                assignment_context.get("department") or doctor.department or doctor.specialty,
                assignment_context.get("ward") or doctor.ward or "Unassigned Base Ward",
                assignment.shift_type_id,
            )] += 1

    shortfalls = []
    current_date = today
    while current_date <= window_end:
        active_templates = _service_templates_for_date(current_date)
        for requirement in service_requirements:
            day_template = _normalize_day_template(requirement.day_of_week)
            if day_template not in active_templates:
                continue
            site, department, ward = _parse_service_key(requirement.ward_or_clinic)
            shift_pattern = shift_patterns_by_id.get(requirement.shift_type_id, {})
            try:
                grade_distribution = json.loads(requirement.grade_distribution or "{}")
            except (TypeError, json.JSONDecodeError):
                grade_distribution = {}
            required_skills = _parse_skill_list(requirement.required_skills)
            assigned_count = assignment_counts[(
                current_date.isoformat(),
                site,
                department,
                ward,
                requirement.shift_type_id,
            )]
            gap_count = max(int(requirement.required_doctors or 0) - assigned_count, 0)
            if gap_count <= 0:
                continue

            required_grade = _resolve_requirement_grade(requirement, shift_pattern, grade_distribution)
            unit_cost = _estimate_locum_cost(required_grade, "BANK", shift_pattern.get("duration_hours", 8))
            home_base_doctors = doctors_by_home_base[(site, department, ward)]
            matching_home_doctors = len([
                doctor for doctor in home_base_doctors
                if _doctor_has_required_skills(doctor, required_skills)
            ])
            shortfalls.append({
                "site": site,
                "ward": ward,
                "department": department,
                "date": current_date.isoformat(),
                "day_template": day_template,
                "shift_name": shift_pattern.get("name", "Unmapped shift"),
                "shift_code": shift_pattern.get("code"),
                "required_grade": required_grade.value if hasattr(required_grade, "value") else str(required_grade),
                "required_skills": required_skills,
                "required_doctors": int(requirement.required_doctors or 0),
                "assigned_doctors": assigned_count,
                "gap_count": gap_count,
                "matching_home_doctors": matching_home_doctors,
                "skill_gap_risk": "GAP_RISK" if required_skills and matching_home_doctors < int(requirement.required_doctors or 0) else "ALIGNED",
                "approval_status": "TARGET_GAP",
                "estimated_cost": round(unit_cost * gap_count, 2),
                "supervising_consultant": requirement.supervising_consultant,
            })
        current_date += timedelta(days=1)

    return sorted(shortfalls, key=lambda item: (-item["gap_count"], -item["estimated_cost"], item["date"], item["site"], item["ward"]))[:20]


def _build_board_entries(
    doctors_by_id: dict[str, Doctor],
    latest_schedule: GeneratedSchedule | None,
    locum_requests: list[LocumRequest],
    shift_patterns_by_id: dict[str, dict],
    db: Session,
) -> list[dict]:
    today = date.today()
    window_end = today + timedelta(days=6)
    assignments = []

    if latest_schedule:
        assignments = db.query(ScheduleAssignment).filter(
            ScheduleAssignment.schedule_id == latest_schedule.id,
            ScheduleAssignment.assignment_date >= today,
            ScheduleAssignment.assignment_date <= window_end,
        ).all()

    internal_cover = defaultdict(int)
    for assignment in assignments:
        doctor = doctors_by_id.get(assignment.doctor_id)
        if not doctor:
            continue
        internal_cover[(assignment.assignment_date.isoformat(), doctor.hospital_site)] += 1

    entries = []
    for request in locum_requests:
        approval_status = request.approval_status.value if hasattr(request.approval_status, "value") else str(request.approval_status)
        if approval_status == LocumApprovalStatus.DECLINED.value:
            continue
        if request.requested_date < today or request.requested_date > window_end:
            continue
        shift_pattern = shift_patterns_by_id.get(request.shift_type_id, {})
        governance = _build_locum_governance(request, shift_pattern)
        finance_status = _effective_finance_approval_status(request, governance)
        entries.append({
            "request_id": request.id,
            "date": request.requested_date.isoformat(),
            "hospital_site": request.hospital_site,
            "ward": request.ward,
            "department": request.department,
            "shift_code": shift_pattern.get("code"),
            "shift_name": shift_pattern.get("name", "Unmapped shift"),
            "shift_window": shift_pattern.get("shift_window", "TBC"),
            "required_grade": request.required_grade.value if hasattr(request.required_grade, "value") else str(request.required_grade),
            "internal_cover_count": internal_cover[(request.requested_date.isoformat(), request.hospital_site)],
            "locum_status": request.approval_status.value if hasattr(request.approval_status, "value") else str(request.approval_status),
            "compliance_level": request.compliance_level.value if hasattr(request.compliance_level, "value") else str(request.compliance_level),
            "approval_required": request.approval_required,
            "booked_doctor_name": request.booked_doctor_name,
            "estimated_cost": request.estimated_cost,
            "requested_hours": request.requested_hours,
            "staff_type": request.staff_type.value if hasattr(request.staff_type, "value") else str(request.staff_type),
            "shortage_reason": request.shortage_reason,
            "finance_approval_status": finance_status,
            "finance_approved_by": request.finance_approved_by,
            "finance_approved_at": request.finance_approved_at.isoformat() if request.finance_approved_at else None,
            "finance_approval_comment": request.finance_approval_comment,
            **governance,
        })

    return sorted(entries, key=lambda item: (item["date"], item["hospital_site"], item["shift_name"]))


def _build_compliance_payload(
    locum_requests: list[LocumRequest],
    shift_patterns: list[dict],
    events: list[DoctorAvailabilityEvent],
    doctors_by_id: dict[str, Doctor],
) -> dict:
    shift_patterns_by_id = {pattern["id"]: pattern for pattern in shift_patterns}
    rules = []
    for pattern in shift_patterns:
        rules.append({
            "shift_code": pattern["code"],
            "shift_name": pattern["name"],
            "minimum_grade": pattern["minimum_grade"],
            "allowed_grades": pattern["eligible_grades"],
            "compliance_level": pattern["compliance_level"],
            "approval_required_for_locum": pattern["approval_required"],
            "booking_rule": "Approval required before attachment" if pattern["approval_required"] else "Can be filled directly from bank pool",
        })

    approval_queue = []
    finance_queue = []
    shift_swap_queue = []
    escalation_flags = []
    risk_register = []
    approver_workloads = defaultdict(lambda: {"approver": "", "pending_items": 0, "overdue_items": 0, "finance_signoffs": 0})
    overdue_items = 0
    finance_signoffs = 0
    breached_items = 0
    at_risk_items = 0
    for request in locum_requests:
        approval_status = request.approval_status.value if hasattr(request.approval_status, "value") else str(request.approval_status)
        if approval_status == LocumApprovalStatus.DECLINED.value:
            continue
        shift_pattern = shift_patterns_by_id.get(request.shift_type_id, {})
        governance = _build_locum_governance(request, shift_pattern)
        if approval_status == LocumApprovalStatus.PENDING_APPROVAL.value:
            age_days = _approval_age_days(request.created_at)
            age_hours = _approval_age_hours(request.created_at)
            aging_status = _approval_aging_status(age_days)
            sla_hours = _locum_sla_hours(request.compliance_level.value if hasattr(request.compliance_level, "value") else str(request.compliance_level))
            escalation_flag = _build_escalation_flag(
                queue_type="LOCUM",
                site=request.hospital_site,
                title=f"{request.ward} {request.required_grade.value if hasattr(request.required_grade, 'value') else str(request.required_grade)} approval",
                recommended_approver=governance["recommended_approver"],
                age_hours=age_hours,
                sla_hours=sla_hours,
                detail=request.shortage_reason,
                finance_signoff=governance["requires_finance_signoff"],
            )
            approval_queue.append({
                "id": request.id,
                "queue_type": "LOCUM",
                "site": request.hospital_site,
                "ward": request.ward,
                "requested_date": request.requested_date.isoformat(),
                "required_grade": request.required_grade.value if hasattr(request.required_grade, "value") else str(request.required_grade),
                "compliance_level": request.compliance_level.value if hasattr(request.compliance_level, "value") else str(request.compliance_level),
                "estimated_cost": request.estimated_cost,
                "recommended_approver": governance["recommended_approver"],
                "approval_tier": governance["approval_tier"],
                "spend_cap_status": governance["spend_cap_status"],
                "requires_finance_signoff": governance["requires_finance_signoff"],
                "age_days": age_days,
                "age_hours": age_hours,
                "aging_status": aging_status,
                "sla_hours": sla_hours,
                "hours_remaining": sla_hours - age_hours,
            })
            if escalation_flag:
                escalation_flags.append(escalation_flag)
                breached_items += 1 if escalation_flag["status"] == "BREACHED" else 0
                at_risk_items += 1 if escalation_flag["status"] == "AT_RISK" else 0
            approver_workloads[governance["recommended_approver"]]["approver"] = governance["recommended_approver"]
            approver_workloads[governance["recommended_approver"]]["pending_items"] += 1
            approver_workloads[governance["recommended_approver"]]["overdue_items"] += 1 if aging_status == "OVERDUE" else 0
            approver_workloads[governance["recommended_approver"]]["finance_signoffs"] += 1 if governance["requires_finance_signoff"] else 0
            overdue_items += 1 if aging_status == "OVERDUE" else 0
            finance_signoffs += 1 if governance["requires_finance_signoff"] else 0

        finance_status = _effective_finance_approval_status(request, governance)
        if governance["requires_finance_signoff"] and finance_status != "APPROVED":
            finance_queue.append({
                "id": request.id,
                "site": request.hospital_site,
                "ward": request.ward,
                "requested_date": request.requested_date.isoformat(),
                "required_grade": request.required_grade.value if hasattr(request.required_grade, "value") else str(request.required_grade),
                "compliance_level": request.compliance_level.value if hasattr(request.compliance_level, "value") else str(request.compliance_level),
                "estimated_cost": request.estimated_cost,
                "staff_type": request.staff_type.value if hasattr(request.staff_type, "value") else str(request.staff_type),
                "recommended_approver": "Finance Business Partner",
                "approval_tier": governance["approval_tier"],
                "finance_approval_status": finance_status,
                "spend_cap_status": governance["spend_cap_status"],
                "age_days": _approval_age_days(request.created_at),
                "age_hours": _approval_age_hours(request.created_at),
                "aging_status": _approval_aging_status(_approval_age_days(request.created_at)),
                "finance_approved_by": request.finance_approved_by,
                "finance_approved_at": request.finance_approved_at.isoformat() if request.finance_approved_at else None,
                "finance_approval_comment": request.finance_approval_comment,
            })

        if request.compliance_level == ComplianceLevel.CRITICAL or approval_status == LocumApprovalStatus.PENDING_APPROVAL.value or governance["spend_cap_status"] == "OVER_CAP":
            risk_register.append({
                "id": request.id,
                "severity": "High" if request.compliance_level == ComplianceLevel.CRITICAL or governance["spend_cap_status"] == "OVER_CAP" else "Medium",
                "site": request.hospital_site,
                "title": f"{request.ward} requires {request.required_grade.value if hasattr(request.required_grade, 'value') else str(request.required_grade)} cover",
                "detail": governance["governance_note"],
                "recommended_action": f"Route to {governance['recommended_approver']} and secure cover before shift start" if request.approval_required else "Book internal bank cover",
            })

    for event in events:
        if event.event_type != AvailabilityEventType.SHIFT_SWAP or str(event.status).upper() != "PENDING":
            continue
        doctor = doctors_by_id.get(event.doctor_id)
        related_doctor = doctors_by_id.get(event.related_doctor_id) if event.related_doctor_id else None
        age_days = _approval_age_days(event.created_at)
        age_hours = _approval_age_hours(event.created_at)
        aging_status = _approval_aging_status(age_days)
        sla_hours = 12
        escalation_flag = _build_escalation_flag(
            queue_type="SHIFT_SWAP",
            site=event.hospital_site,
            title=f"Shift swap for {_format_doctor_name(doctor)}" if doctor else f"Shift swap for {event.doctor_id}",
            recommended_approver="Rota Coordinator",
            age_hours=age_hours,
            sla_hours=sla_hours,
            detail=event.reason_category or "Pending shift swap approval",
        )
        shift_swap_queue.append({
            "id": event.id,
            "queue_type": "SHIFT_SWAP",
            "site": event.hospital_site,
            "doctor_name": _format_doctor_name(doctor) if doctor else event.doctor_id,
            "related_doctor_name": _format_doctor_name(related_doctor) if related_doctor else None,
            "shift_date": event.start_date.isoformat(),
            "session_label": event.session_label,
            "age_days": age_days,
            "age_hours": age_hours,
            "aging_status": aging_status,
            "recommended_approver": "Rota Coordinator",
            "sla_hours": sla_hours,
            "hours_remaining": sla_hours - age_hours,
        })
        if escalation_flag:
            escalation_flags.append(escalation_flag)
            breached_items += 1 if escalation_flag["status"] == "BREACHED" else 0
            at_risk_items += 1 if escalation_flag["status"] == "AT_RISK" else 0
        approver_workloads["Rota Coordinator"]["approver"] = "Rota Coordinator"
        approver_workloads["Rota Coordinator"]["pending_items"] += 1
        approver_workloads["Rota Coordinator"]["overdue_items"] += 1 if aging_status == "OVERDUE" else 0
        overdue_items += 1 if aging_status == "OVERDUE" else 0

    return {
        "grade_rules": rules,
        "approval_queue": approval_queue,
        "finance_queue": finance_queue,
        "shift_swap_queue": shift_swap_queue,
        "approval_overview": {
            "pending_locum_approvals": len(approval_queue),
            "pending_finance_reviews": len(finance_queue),
            "pending_shift_swaps": len(shift_swap_queue),
            "overdue_items": overdue_items,
            "finance_signoffs": finance_signoffs,
            "breached_items": breached_items,
            "at_risk_items": at_risk_items,
        },
        "escalation_flags": sorted(escalation_flags, key=lambda item: (0 if item["status"] == "BREACHED" else 1, item["hours_remaining"])),
        "approver_workloads": sorted(approver_workloads.values(), key=lambda item: (-item["pending_items"], item["approver"])),
        "risk_register": risk_register,
    }


def _build_recommended_actions(coverage_pressure: list[dict], locum_requests: list[LocumRequest], requirement_shortfalls: list[dict]) -> list[dict]:
    actions = []
    if requirement_shortfalls:
        biggest_gap = requirement_shortfalls[0]
        actions.append({
            "title": f"{biggest_gap['site']}: protect {biggest_gap['ward']} {biggest_gap['day_template'].replace('_', ' ')} cover",
            "detail": (
                f"{biggest_gap['gap_count']} uncovered {biggest_gap['shift_name']} slots remain for "
                f"{biggest_gap['required_grade']} cover on {biggest_gap['date']}."
            ),
            "impact": "Critical Target Gap",
        })

    consultant_gaps = [
        item for item in requirement_shortfalls
        if item.get("required_grade") in {"Consultant", "Registrar"} and item.get("gap_count", 0) > 0
    ]
    if consultant_gaps:
        actions.append({
            "title": "Senior-grade target gaps need escalation",
            "detail": f"{len(consultant_gaps)} consultant or registrar-led target gaps are still open across the next 7 days.",
            "impact": "Senior Coverage",
        })

    skill_gap_shortfalls = [item for item in requirement_shortfalls if item.get("skill_gap_risk") == "GAP_RISK"]
    if skill_gap_shortfalls:
        skills = ", ".join(skill_gap_shortfalls[0].get("required_skills", [])[:3]) or "specialist skills"
        actions.append({
            "title": "Competency-led gaps need targeted cover",
            "detail": f"{len(skill_gap_shortfalls)} open ward targets need skills such as {skills}.",
            "impact": "Skills Risk",
        })

    for site_summary in coverage_pressure:
        if site_summary["sickness_events"] > 0:
            actions.append({
                "title": f"{site_summary['site']}: backfill sickness-related gaps",
                "detail": f"{site_summary['sickness_events']} sickness events are active across the next rota window.",
                "impact": "Coverage",
            })
        if site_summary["pending_locums"] > 0:
            actions.append({
                "title": f"{site_summary['site']}: clear pending locum approvals",
                "detail": f"{site_summary['pending_locums']} locum requests are still pending or approved but not yet filled.",
                "impact": "Approvals",
            })

    if not actions and locum_requests:
        actions.append({
            "title": "Coverage is stable",
            "detail": "Use this window to review grade balance and release bank shifts early to reduce spend.",
            "impact": "Optimisation",
        })

    return actions[:4]


def _approval_age_days(created_at) -> int:
    if not created_at:
        return 0
    created_date = created_at.date() if hasattr(created_at, "date") else created_at
    return max((date.today() - created_date).days, 0)


def _approval_aging_status(age_days: int) -> str:
    if age_days >= 2:
        return "OVERDUE"
    if age_days >= 1:
        return "DUE_SOON"
    return "NEW"


def _approval_age_hours(created_at) -> int:
    if not created_at:
        return 0
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at)
        except ValueError:
            return 0
    if not isinstance(created_at, datetime):
        return 0
    return max(int((datetime.utcnow() - created_at).total_seconds() // 3600), 0)


def _locum_sla_hours(compliance_level: str) -> int:
    if compliance_level == ComplianceLevel.CRITICAL.value:
        return 4
    if compliance_level == ComplianceLevel.ENHANCED.value:
        return 12
    return 24


def _build_escalation_flag(*, queue_type: str, site: str, title: str, recommended_approver: str, age_hours: int, sla_hours: int, detail: str, finance_signoff: bool = False) -> dict | None:
    hours_remaining = sla_hours - age_hours
    if age_hours >= sla_hours:
        status = "BREACHED"
        escalate_to = "Chief of Service" if finance_signoff else "Clinical Director"
    elif hours_remaining <= max(2, sla_hours // 4):
        status = "AT_RISK"
        escalate_to = "Medical Staffing Lead" if recommended_approver != "Chief of Service" else "Chief of Service"
    else:
        return None

    return {
        "queue_type": queue_type,
        "site": site,
        "title": title,
        "recommended_approver": recommended_approver,
        "escalate_to": escalate_to,
        "age_hours": age_hours,
        "sla_hours": sla_hours,
        "hours_remaining": hours_remaining,
        "status": status,
        "detail": detail,
        "finance_signoff": finance_signoff,
    }


def _record_audit_event(
    db: Session,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    hospital_site: str | None,
    actor_name: str | None,
    summary: str,
    detail: str | None = None,
) -> None:
    db.add(
        OperationAuditLog(
            id=str(uuid.uuid4()),
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            hospital_site=hospital_site,
            actor_name=actor_name,
            summary=summary,
            detail=detail,
        )
    )


def _serialize_audit_log(entry: OperationAuditLog) -> dict:
    return {
        "id": entry.id,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "action": entry.action,
        "hospital_site": entry.hospital_site,
        "actor_name": entry.actor_name,
        "summary": entry.summary,
        "detail": entry.detail,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


def _resolve_availability_dependencies(payload: AvailabilityEventCreate | AvailabilityEventUpdate, db: Session) -> tuple[Doctor, Doctor | None]:
    doctor = db.query(Doctor).filter(Doctor.id == payload.doctor_id).first()
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")

    if payload.start_date > payload.end_date:
        raise HTTPException(status_code=400, detail="Start date must be on or before end date")

    related_doctor = None
    if payload.related_doctor_id:
        related_doctor = db.query(Doctor).filter(Doctor.id == payload.related_doctor_id).first()
        if not related_doctor:
            raise HTTPException(status_code=404, detail="Related doctor not found")
        if related_doctor.hospital_site != doctor.hospital_site:
            raise HTTPException(status_code=400, detail="Related doctor must be from the same hospital site")

    if payload.event_type == AvailabilityEventType.SHIFT_SWAP and not payload.related_doctor_id:
        raise HTTPException(status_code=400, detail="Shift swap requests require a related doctor")

    return doctor, related_doctor


def _resolve_shift_or_404(shift_code: str, db: Session) -> ShiftType:
    shift = db.query(ShiftType).filter(ShiftType.code == shift_code).first()
    if not shift:
        raise HTTPException(status_code=404, detail="Shift pattern not found")
    return shift


def build_operations_workspace_payload(db: Session) -> dict:
    doctors = db.query(Doctor).all()
    doctors_by_id = {doctor.id: doctor for doctor in doctors}
    shift_patterns = [_build_shift_pattern(shift) for shift in db.query(ShiftType).all()]
    shift_patterns_by_id = {pattern["id"]: pattern for pattern in shift_patterns}
    events = db.query(DoctorAvailabilityEvent).order_by(DoctorAvailabilityEvent.start_date.asc()).limit(20).all()
    locum_requests = db.query(LocumRequest).order_by(LocumRequest.requested_date.asc()).all()
    service_requirements = db.query(ServiceRequirement).all()
    audit_logs = db.query(OperationAuditLog).order_by(OperationAuditLog.created_at.desc()).limit(12).all()
    latest_schedule = db.query(GeneratedSchedule).filter(
        GeneratedSchedule.generated_successfully == True  # noqa: E712
    ).order_by(GeneratedSchedule.generated_at.desc()).first()

    coverage_pressure = _build_coverage_pressure(events, locum_requests)
    establishment_matrix = _build_establishment_matrix(doctors, service_requirements, shift_patterns_by_id)
    requirement_shortfalls = _build_requirement_shortfalls(latest_schedule, doctors_by_id, service_requirements, shift_patterns_by_id, db)
    active_absences = [event for event in events if event.end_date >= date.today()]
    active_absences = [event for event in active_absences if str(event.status).upper() != "CANCELLED"]
    pending_shift_swaps = [
        event for event in active_absences
        if event.event_type == AvailabilityEventType.SHIFT_SWAP and str(event.status).upper() == "PENDING"
    ]
    pending_locum_requests = [
        request for request in locum_requests
        if (request.approval_status.value if hasattr(request.approval_status, "value") else str(request.approval_status))
        == LocumApprovalStatus.PENDING_APPROVAL.value
    ]

    return {
        "summary": {
            "tracked_sites": len({doctor.hospital_site for doctor in doctors}),
            "doctor_count": len(doctors),
            "grade_mix": _build_grade_mix(doctors),
            "active_absence_count": len(active_absences),
            "pending_shift_swaps": len(pending_shift_swaps),
            "pending_locum_requests": len(pending_locum_requests),
            "estimated_weekly_locum_cost": round(sum(float(request.estimated_cost or 0) for request in locum_requests), 2),
            "target_gap_count": len(requirement_shortfalls),
            "senior_target_gap_count": len([
                item for item in requirement_shortfalls
                if item.get("required_grade") in {"Consultant", "Registrar"} and item.get("gap_count", 0) > 0
            ]),
            "latest_schedule_id": latest_schedule.id if latest_schedule else None,
        },
        "rota_planning": {
            "coverage_pressure": coverage_pressure,
            "grade_distribution": _build_grade_mix(doctors),
            "recommended_actions": _build_recommended_actions(coverage_pressure, locum_requests, requirement_shortfalls),
            "establishment_matrix": establishment_matrix,
            "ward_shortfalls": requirement_shortfalls[:8] if requirement_shortfalls else [
                {
                    "site": item["hospital_site"],
                    "ward": item["ward"],
                    "department": item["department"],
                    "shift_name": item["shift_name"],
                    "required_grade": item["required_grade"],
                    "approval_status": item["approval_status"],
                    "estimated_cost": item["estimated_cost"],
                }
                for item in [_serialize_locum_request(request, shift_patterns_by_id) for request in locum_requests]
            ][:8],
        },
        "rota_board": {
            "window_start": date.today().isoformat(),
            "window_end": (date.today() + timedelta(days=6)).isoformat(),
            "generated_from_schedule_id": latest_schedule.id if latest_schedule else None,
            "entries": _build_board_entries(doctors_by_id, latest_schedule, locum_requests, shift_patterns_by_id, db),
        },
        "shift_patterns": shift_patterns,
        "leave_events": [_serialize_event(event, doctors_by_id) for event in events],
        "locum_requests": [_serialize_locum_request(request, shift_patterns_by_id) for request in locum_requests],
        "activity_feed": [_serialize_audit_log(entry) for entry in audit_logs],
        "compliance": _build_compliance_payload(locum_requests, shift_patterns, events, doctors_by_id),
        "reference_data": {
            "hospital_sites": sorted({doctor.hospital_site for doctor in doctors}),
            "department_options": sorted({doctor.department for doctor in doctors if doctor.department}),
            "ward_options": sorted({doctor.ward for doctor in doctors if doctor.ward}),
            "competency_options": sorted({skill for doctor in doctors for skill in _doctor_competencies(doctor)}),
            "doctor_grades": [grade.value for grade in DoctorGrade],
            "availability_event_types": [
                {"value": event_type.value, "label": EVENT_LABELS.get(event_type, event_type.value.replace("_", " ").title())}
                for event_type in AvailabilityEventType
            ],
            "compliance_levels": [level.value for level in ComplianceLevel],
            "staff_types": ["BANK", "INTERNAL", "AGENCY"],
            "availability_statuses": ["APPROVED", "PENDING", "RECORDED", "CANCELLED"],
            "service_day_templates": ["ALL", "WEEKDAY", "WEEKEND", "BANK_HOLIDAY", "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
        },
    }


@router.get("/workspace")
def get_operations_workspace(db: Session = Depends(get_db)):
    return build_operations_workspace_payload(db)


@router.post("/service-requirements", status_code=201)
def create_service_requirement(payload: ServiceRequirementCreate, db: Session = Depends(get_db)):
    shift_pattern = db.query(ShiftType).filter(ShiftType.code == payload.shift_code.upper()).first()
    if not shift_pattern:
        raise HTTPException(status_code=404, detail="Shift pattern not found")

    normalized_day_template = _normalize_day_template(payload.day_of_week)
    ward_key = f"{payload.hospital_site}::{payload.department}::{payload.ward}"
    existing_requirement = db.query(ServiceRequirement).filter(
        ServiceRequirement.ward_or_clinic == ward_key,
        ServiceRequirement.day_of_week == normalized_day_template,
        ServiceRequirement.shift_type_id == shift_pattern.id,
    ).first()
    if existing_requirement:
        raise HTTPException(status_code=400, detail="A service requirement already exists for this ward, shift, and day pattern")

    sanitized_distribution = _sanitize_grade_distribution(payload.grade_distribution)
    sanitized_skills = _sanitize_skill_list(payload.required_skills)
    total_distribution = sum(sanitized_distribution.values())
    if total_distribution > payload.required_doctors:
        raise HTTPException(status_code=400, detail="Grade distribution cannot exceed the required doctor count")

    requirement = ServiceRequirement(
        id=str(uuid.uuid4()),
        ward_or_clinic=ward_key,
        day_of_week=normalized_day_template,
        shift_type_id=shift_pattern.id,
        required_doctors=payload.required_doctors,
        grade_distribution=json.dumps(sanitized_distribution),
        required_skills=json.dumps(sanitized_skills),
        supervising_consultant=payload.supervising_consultant,
    )
    db.add(requirement)
    _record_audit_event(
        db,
        entity_type="SERVICE_REQUIREMENT",
        entity_id=requirement.id,
        action="CREATED",
        hospital_site=payload.hospital_site,
        actor_name=payload.created_by or "Rota Planning Desk",
        summary=f"Created establishment rule for {payload.ward}",
        detail=payload.note or f"{payload.ward} now has a {shift_pattern.name} requirement of {payload.required_doctors}",
    )
    db.commit()
    db.refresh(requirement)

    return {
        "id": requirement.id,
        "hospital_site": payload.hospital_site,
        "department": payload.department,
        "ward": payload.ward,
        "shift_name": shift_pattern.name,
        "shift_code": shift_pattern.code,
        "required_doctors": requirement.required_doctors,
        "grade_distribution": sanitized_distribution,
        "required_skills": sanitized_skills,
        "day_of_week": requirement.day_of_week,
        "supervising_consultant": requirement.supervising_consultant,
    }


@router.put("/service-requirements/{requirement_id}")
def update_service_requirement(requirement_id: str, payload: ServiceRequirementUpdate, db: Session = Depends(get_db)):
    requirement = db.query(ServiceRequirement).filter(ServiceRequirement.id == requirement_id).first()
    if not requirement:
        raise HTTPException(status_code=404, detail="Service requirement not found")

    site, department, ward = _parse_service_key(requirement.ward_or_clinic)
    shift_pattern = db.query(ShiftType).filter(ShiftType.id == requirement.shift_type_id).first()
    normalized_day_template = _normalize_day_template(payload.day_of_week)

    conflicting_requirement = db.query(ServiceRequirement).filter(
        ServiceRequirement.id != requirement_id,
        ServiceRequirement.ward_or_clinic == requirement.ward_or_clinic,
        ServiceRequirement.day_of_week == normalized_day_template,
        ServiceRequirement.shift_type_id == requirement.shift_type_id,
    ).first()
    if conflicting_requirement:
        raise HTTPException(status_code=400, detail="A service requirement already exists for this ward, shift, and day pattern")

    sanitized_distribution = _sanitize_grade_distribution(payload.grade_distribution)
    sanitized_skills = _sanitize_skill_list(payload.required_skills)
    total_distribution = sum(sanitized_distribution.values())
    if total_distribution > payload.required_doctors:
        raise HTTPException(status_code=400, detail="Grade distribution cannot exceed the required doctor count")

    requirement.required_doctors = payload.required_doctors
    requirement.day_of_week = normalized_day_template
    requirement.grade_distribution = json.dumps(sanitized_distribution)
    requirement.required_skills = json.dumps(sanitized_skills)
    requirement.supervising_consultant = payload.supervising_consultant
    db.add(requirement)

    shift_name = shift_pattern.name if shift_pattern else "Shift"
    detail = payload.note or f"{ward} now requires {payload.required_doctors} doctors for {shift_name}"
    _record_audit_event(
        db,
        entity_type="SERVICE_REQUIREMENT",
        entity_id=requirement.id,
        action="UPDATED",
        hospital_site=site,
        actor_name=payload.updated_by or "Rota Planning Desk",
        summary=f"Updated establishment rule for {ward}",
        detail=detail,
    )

    db.commit()
    db.refresh(requirement)

    return {
        "id": requirement.id,
        "hospital_site": site,
        "department": department,
        "ward": ward,
        "shift_name": shift_name,
        "required_doctors": requirement.required_doctors,
        "grade_distribution": sanitized_distribution,
        "required_skills": sanitized_skills,
        "day_of_week": requirement.day_of_week,
        "supervising_consultant": requirement.supervising_consultant,
    }


@router.delete("/service-requirements/{requirement_id}")
def delete_service_requirement(
    requirement_id: str,
    deleted_by: str | None = None,
    note: str | None = None,
    db: Session = Depends(get_db),
):
    requirement = db.query(ServiceRequirement).filter(ServiceRequirement.id == requirement_id).first()
    if not requirement:
        raise HTTPException(status_code=404, detail="Service requirement not found")

    site, department, ward = _parse_service_key(requirement.ward_or_clinic)
    shift_pattern = db.query(ShiftType).filter(ShiftType.id == requirement.shift_type_id).first()
    shift_name = shift_pattern.name if shift_pattern else "Shift"

    _record_audit_event(
        db,
        entity_type="SERVICE_REQUIREMENT",
        entity_id=requirement.id,
        action="DELETED",
        hospital_site=site,
        actor_name=deleted_by or "Rota Planning Desk",
        summary=f"Removed establishment rule for {ward}",
        detail=note or f"{shift_name} requirement removed from {ward}",
    )
    db.delete(requirement)
    db.commit()

    return {
        "deleted": True,
        "id": requirement_id,
        "hospital_site": site,
        "department": department,
        "ward": ward,
        "shift_name": shift_name,
    }


@router.post("/availability-events", status_code=201)
def create_availability_event(payload: AvailabilityEventCreate, db: Session = Depends(get_db)):
    doctor, related_doctor = _resolve_availability_dependencies(payload, db)

    event = DoctorAvailabilityEvent(
        id=str(uuid.uuid4()),
        doctor_id=payload.doctor_id,
        hospital_site=doctor.hospital_site,
        event_type=payload.event_type,
        start_date=payload.start_date,
        end_date=payload.end_date,
        session_label=payload.session_label,
        status=payload.status,
        reason_category=payload.reason_category,
        related_doctor_id=payload.related_doctor_id,
        notes=payload.notes,
    )
    db.add(event)
    _record_audit_event(
        db,
        entity_type="availability_event",
        entity_id=event.id,
        action="CREATED",
        hospital_site=doctor.hospital_site,
        actor_name="Operations Workspace",
        summary=f"{EVENT_LABELS.get(payload.event_type, payload.event_type.value)} created for {_format_doctor_name(doctor)}",
        detail=payload.notes,
    )
    db.commit()
    db.refresh(event)

    doctors_by_id = {item.id: item for item in [doctor] + ([related_doctor] if related_doctor else [])}
    return _serialize_event(event, doctors_by_id)


@router.put("/availability-events/{event_id}")
def update_availability_event(event_id: str, payload: AvailabilityEventUpdate, db: Session = Depends(get_db)):
    event = db.query(DoctorAvailabilityEvent).filter(DoctorAvailabilityEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Availability event not found")
    if str(event.status).upper() == "CANCELLED":
        raise HTTPException(status_code=400, detail="Cancelled events cannot be edited")

    doctor, related_doctor = _resolve_availability_dependencies(payload, db)
    event.doctor_id = payload.doctor_id
    event.hospital_site = doctor.hospital_site
    event.event_type = payload.event_type
    event.start_date = payload.start_date
    event.end_date = payload.end_date
    event.session_label = payload.session_label
    event.status = payload.status
    event.reason_category = payload.reason_category
    event.related_doctor_id = payload.related_doctor_id
    if payload.status != "APPROVED":
        event.approved_by = None
        event.approved_at = None
        event.approval_comment = None
    event.notes = payload.notes
    _record_audit_event(
        db,
        entity_type="availability_event",
        entity_id=event.id,
        action="UPDATED",
        hospital_site=doctor.hospital_site,
        actor_name="Operations Workspace",
        summary=f"Availability event updated for {_format_doctor_name(doctor)}",
        detail=payload.notes,
    )
    db.commit()
    db.refresh(event)

    doctors_by_id = {item.id: item for item in [doctor] + ([related_doctor] if related_doctor else [])}
    return _serialize_event(event, doctors_by_id)


@router.post("/locum-requests", status_code=201)
def create_locum_request(payload: LocumRequestCreate, db: Session = Depends(get_db)):
    shift = _resolve_shift_or_404(payload.shift_code, db)

    approval_status = (
        LocumApprovalStatus.PENDING_APPROVAL
        if payload.approval_required
        else LocumApprovalStatus.APPROVED
    )

    locum_request = LocumRequest(
        id=str(uuid.uuid4()),
        hospital_site=payload.hospital_site,
        department=payload.department,
        ward=payload.ward,
        requested_date=payload.requested_date,
        shift_type_id=shift.id,
        required_grade=payload.required_grade,
        compliance_level=payload.compliance_level,
        staff_type=payload.staff_type,
        approval_status=approval_status,
        approval_required=payload.approval_required,
        requested_hours=payload.requested_hours,
        estimated_cost=_estimate_locum_cost(payload.required_grade, payload.staff_type, payload.requested_hours),
        shortage_reason=payload.shortage_reason,
        requested_by=payload.requested_by,
        notes=payload.notes,
    )
    _sync_finance_approval_state(locum_request, _build_shift_pattern(shift))
    db.add(locum_request)
    _record_audit_event(
        db,
        entity_type="locum_request",
        entity_id=locum_request.id,
        action="CREATED",
        hospital_site=payload.hospital_site,
        actor_name=payload.requested_by or "Operations Workspace",
        summary=f"Locum request created for {payload.ward} ({payload.shift_code})",
        detail=payload.shortage_reason,
    )
    db.commit()
    db.refresh(locum_request)

    shift_pattern = _build_shift_pattern(shift)
    return _serialize_locum_request(locum_request, {shift.id: shift_pattern})


@router.put("/locum-requests/{request_id}")
def update_locum_request(request_id: str, payload: LocumRequestUpdate, db: Session = Depends(get_db)):
    locum_request = db.query(LocumRequest).filter(LocumRequest.id == request_id).first()
    if not locum_request:
        raise HTTPException(status_code=404, detail="Locum request not found")

    approval_status = locum_request.approval_status.value if hasattr(locum_request.approval_status, "value") else str(locum_request.approval_status)
    if approval_status in {LocumApprovalStatus.FILLED.value, LocumApprovalStatus.DECLINED.value}:
        raise HTTPException(status_code=400, detail="Filled or cancelled requests cannot be edited")

    shift = _resolve_shift_or_404(payload.shift_code, db)
    locum_request.hospital_site = payload.hospital_site
    locum_request.department = payload.department
    locum_request.ward = payload.ward
    locum_request.requested_date = payload.requested_date
    locum_request.shift_type_id = shift.id
    locum_request.required_grade = payload.required_grade
    locum_request.compliance_level = payload.compliance_level
    locum_request.staff_type = payload.staff_type
    locum_request.approval_required = payload.approval_required
    locum_request.requested_hours = payload.requested_hours
    locum_request.estimated_cost = _estimate_locum_cost(payload.required_grade, payload.staff_type, payload.requested_hours)
    locum_request.shortage_reason = payload.shortage_reason
    locum_request.requested_by = payload.requested_by
    locum_request.notes = payload.notes

    locum_request.approval_status = (
        LocumApprovalStatus.PENDING_APPROVAL if payload.approval_required else LocumApprovalStatus.APPROVED
    )
    if locum_request.approval_status == LocumApprovalStatus.PENDING_APPROVAL:
        locum_request.approved_by = None
        locum_request.approved_at = None
        locum_request.approval_comment = None
    _sync_finance_approval_state(locum_request, _build_shift_pattern(shift))

    _record_audit_event(
        db,
        entity_type="locum_request",
        entity_id=locum_request.id,
        action="UPDATED",
        hospital_site=payload.hospital_site,
        actor_name=payload.requested_by or "Operations Workspace",
        summary=f"Locum request updated for {payload.ward} ({payload.shift_code})",
        detail=payload.shortage_reason,
    )
    db.commit()
    db.refresh(locum_request)

    shift_pattern = _build_shift_pattern(shift)
    return _serialize_locum_request(locum_request, {shift.id: shift_pattern})


@router.post("/locum-requests/{request_id}/approve")
def approve_locum_request(request_id: str, body: dict | None = None, db: Session = Depends(get_db)):
    locum_request = db.query(LocumRequest).filter(LocumRequest.id == request_id).first()
    if not locum_request:
        raise HTTPException(status_code=404, detail="Locum request not found")

    approval_status = locum_request.approval_status.value if hasattr(locum_request.approval_status, "value") else str(locum_request.approval_status)
    if approval_status == LocumApprovalStatus.FILLED.value:
        raise HTTPException(status_code=400, detail="Filled requests do not need approval")

    approved_by = (body or {}).get("approved_by", "Medical Staffing Lead")
    approval_comment = (body or {}).get("comment")
    locum_request.approval_status = LocumApprovalStatus.APPROVED
    locum_request.approved_by = approved_by
    locum_request.approved_at = datetime.utcnow()
    locum_request.approval_comment = approval_comment
    _record_audit_event(
        db,
        entity_type="locum_request",
        entity_id=locum_request.id,
        action="APPROVED",
        hospital_site=locum_request.hospital_site,
        actor_name=approved_by,
        summary=f"Locum request approved for {locum_request.ward}",
        detail=approval_comment or locum_request.shortage_reason,
    )
    db.commit()

    return {"status": "success", "request_id": request_id, "approval_status": LocumApprovalStatus.APPROVED.value}


@router.post("/locum-requests/{request_id}/reject")
def reject_locum_request(request_id: str, body: dict | None = None, db: Session = Depends(get_db)):
    locum_request = db.query(LocumRequest).filter(LocumRequest.id == request_id).first()
    if not locum_request:
        raise HTTPException(status_code=404, detail="Locum request not found")

    approval_status = locum_request.approval_status.value if hasattr(locum_request.approval_status, "value") else str(locum_request.approval_status)
    if approval_status == LocumApprovalStatus.FILLED.value:
        raise HTTPException(status_code=400, detail="Filled requests cannot be rejected")

    reason = str((body or {}).get("reason", "")).strip()
    if not reason:
        raise HTTPException(status_code=400, detail="A rejection reason is required")

    approved_by = (body or {}).get("approved_by", "Medical Staffing Lead")
    locum_request.approval_status = LocumApprovalStatus.DECLINED
    locum_request.approved_by = approved_by
    locum_request.approved_at = datetime.utcnow()
    locum_request.approval_comment = reason
    existing_notes = locum_request.notes or ""
    locum_request.notes = f"{existing_notes}\nRejected: {reason}".strip()
    _record_audit_event(
        db,
        entity_type="locum_request",
        entity_id=locum_request.id,
        action="REJECTED",
        hospital_site=locum_request.hospital_site,
        actor_name=approved_by,
        summary=f"Locum request rejected for {locum_request.ward}",
        detail=reason,
    )
    db.commit()

    shift = db.query(ShiftType).filter(ShiftType.id == locum_request.shift_type_id).first()
    shift_pattern = _build_shift_pattern(shift) if shift else {}
    return _serialize_locum_request(locum_request, {shift.id: shift_pattern} if shift else {})


@router.post("/locum-requests/{request_id}/finance-approve")
def approve_finance_signoff(request_id: str, body: dict | None = None, db: Session = Depends(get_db)):
    locum_request = db.query(LocumRequest).filter(LocumRequest.id == request_id).first()
    if not locum_request:
        raise HTTPException(status_code=404, detail="Locum request not found")

    shift = db.query(ShiftType).filter(ShiftType.id == locum_request.shift_type_id).first()
    shift_pattern = _build_shift_pattern(shift) if shift else {}
    governance = _build_locum_governance(locum_request, shift_pattern)
    if not governance["requires_finance_signoff"]:
        raise HTTPException(status_code=400, detail="This request does not need finance sign-off")

    approved_by = (body or {}).get("approved_by", "Finance Business Partner")
    comment = (body or {}).get("comment")
    locum_request.finance_approval_status = "APPROVED"
    locum_request.finance_approved_by = approved_by
    locum_request.finance_approved_at = datetime.utcnow()
    locum_request.finance_approval_comment = comment
    _record_audit_event(
        db,
        entity_type="locum_request",
        entity_id=locum_request.id,
        action="FINANCE_APPROVED",
        hospital_site=locum_request.hospital_site,
        actor_name=approved_by,
        summary=f"Finance sign-off approved for {locum_request.ward}",
        detail=comment or locum_request.shortage_reason,
    )
    db.commit()
    db.refresh(locum_request)

    return _serialize_locum_request(locum_request, {shift.id: shift_pattern} if shift else {})


@router.post("/locum-requests/{request_id}/finance-reject")
def reject_finance_signoff(request_id: str, body: dict | None = None, db: Session = Depends(get_db)):
    locum_request = db.query(LocumRequest).filter(LocumRequest.id == request_id).first()
    if not locum_request:
        raise HTTPException(status_code=404, detail="Locum request not found")

    shift = db.query(ShiftType).filter(ShiftType.id == locum_request.shift_type_id).first()
    shift_pattern = _build_shift_pattern(shift) if shift else {}
    governance = _build_locum_governance(locum_request, shift_pattern)
    if not governance["requires_finance_signoff"]:
        raise HTTPException(status_code=400, detail="This request does not need finance sign-off")

    reason = str((body or {}).get("reason", "")).strip()
    if not reason:
        raise HTTPException(status_code=400, detail="A finance rejection reason is required")

    approved_by = (body or {}).get("approved_by", "Finance Business Partner")
    locum_request.finance_approval_status = "DECLINED"
    locum_request.finance_approved_by = approved_by
    locum_request.finance_approved_at = datetime.utcnow()
    locum_request.finance_approval_comment = reason
    locum_request.approval_status = LocumApprovalStatus.DECLINED
    existing_notes = locum_request.notes or ""
    locum_request.notes = f"{existing_notes}\nFinance rejected: {reason}".strip()
    _record_audit_event(
        db,
        entity_type="locum_request",
        entity_id=locum_request.id,
        action="FINANCE_REJECTED",
        hospital_site=locum_request.hospital_site,
        actor_name=approved_by,
        summary=f"Finance sign-off rejected for {locum_request.ward}",
        detail=reason,
    )
    db.commit()
    db.refresh(locum_request)

    return _serialize_locum_request(locum_request, {shift.id: shift_pattern} if shift else {})


@router.post("/availability-events/{event_id}/cancel")
def cancel_availability_event(event_id: str, body: dict | None = None, db: Session = Depends(get_db)):
    event = db.query(DoctorAvailabilityEvent).filter(DoctorAvailabilityEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Availability event not found")

    event.status = "CANCELLED"
    notes = (body or {}).get("reason")
    if notes:
        existing_notes = event.notes or ""
        event.notes = f"{existing_notes}\nCancelled: {notes}".strip()
    _record_audit_event(
        db,
        entity_type="availability_event",
        entity_id=event.id,
        action="CANCELLED",
        hospital_site=event.hospital_site,
        actor_name="Operations Workspace",
        summary=f"Availability event cancelled for doctor {event.doctor_id}",
        detail=notes,
    )
    db.commit()

    doctors = db.query(Doctor).filter(Doctor.id.in_([doctor_id for doctor_id in [event.doctor_id, event.related_doctor_id] if doctor_id])).all()
    return _serialize_event(event, {doctor.id: doctor for doctor in doctors})


@router.post("/availability-events/{event_id}/reject")
def reject_availability_event(event_id: str, body: dict | None = None, db: Session = Depends(get_db)):
    event = db.query(DoctorAvailabilityEvent).filter(DoctorAvailabilityEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Availability event not found")

    reason = str((body or {}).get("reason", "")).strip()
    if not reason:
        raise HTTPException(status_code=400, detail="A rejection reason is required")

    approved_by = (body or {}).get("approved_by", "Rota Coordinator")
    event.status = "REJECTED"
    event.approved_by = approved_by
    event.approved_at = datetime.utcnow()
    event.approval_comment = reason
    existing_notes = event.notes or ""
    event.notes = f"{existing_notes}\nRejected: {reason}".strip()
    _record_audit_event(
        db,
        entity_type="availability_event",
        entity_id=event.id,
        action="REJECTED",
        hospital_site=event.hospital_site,
        actor_name=approved_by,
        summary=f"Availability event rejected for doctor {event.doctor_id}",
        detail=reason,
    )
    db.commit()

    doctors = db.query(Doctor).filter(Doctor.id.in_([doctor_id for doctor_id in [event.doctor_id, event.related_doctor_id] if doctor_id])).all()
    return _serialize_event(event, {doctor.id: doctor for doctor in doctors})


@router.post("/availability-events/{event_id}/status")
def update_availability_event_status(event_id: str, body: dict | None = None, db: Session = Depends(get_db)):
    event = db.query(DoctorAvailabilityEvent).filter(DoctorAvailabilityEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Availability event not found")

    requested_status = str((body or {}).get("status", "")).upper().strip()
    allowed_statuses = {"APPROVED", "PENDING", "RECORDED"}
    if requested_status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="Unsupported availability status")

    event.status = requested_status
    note = (body or {}).get("note")
    approved_by = (body or {}).get("approved_by")
    comment = (body or {}).get("comment")
    if note:
        existing_notes = event.notes or ""
        event.notes = f"{existing_notes}\nStatus update: {note}".strip()
    if requested_status == "APPROVED":
        event.approved_by = approved_by or event.approved_by or "Rota Coordinator"
        event.approved_at = datetime.utcnow()
        event.approval_comment = comment or note or event.approval_comment
    _record_audit_event(
        db,
        entity_type="availability_event",
        entity_id=event.id,
        action=f"STATUS_{requested_status}",
        hospital_site=event.hospital_site,
        actor_name=approved_by or "Operations Workspace",
        summary=f"Availability event moved to {requested_status}",
        detail=comment or note,
    )
    db.commit()

    doctors = db.query(Doctor).filter(Doctor.id.in_([doctor_id for doctor_id in [event.doctor_id, event.related_doctor_id] if doctor_id])).all()
    return _serialize_event(event, {doctor.id: doctor for doctor in doctors})


@router.post("/locum-requests/{request_id}/cancel")
def cancel_locum_request(request_id: str, body: dict | None = None, db: Session = Depends(get_db)):
    locum_request = db.query(LocumRequest).filter(LocumRequest.id == request_id).first()
    if not locum_request:
        raise HTTPException(status_code=404, detail="Locum request not found")

    approval_status = locum_request.approval_status.value if hasattr(locum_request.approval_status, "value") else str(locum_request.approval_status)
    if approval_status == LocumApprovalStatus.FILLED.value:
        raise HTTPException(status_code=400, detail="Filled requests must be manually reviewed before cancellation")

    locum_request.approval_status = LocumApprovalStatus.DECLINED
    reason = (body or {}).get("reason")
    if reason:
        existing_notes = locum_request.notes or ""
        locum_request.notes = f"{existing_notes}\nCancelled: {reason}".strip()
    _record_audit_event(
        db,
        entity_type="locum_request",
        entity_id=locum_request.id,
        action="CANCELLED",
        hospital_site=locum_request.hospital_site,
        actor_name="Operations Workspace",
        summary=f"Locum request cancelled for {locum_request.ward}",
        detail=reason,
    )
    db.commit()

    shift = db.query(ShiftType).filter(ShiftType.id == locum_request.shift_type_id).first()
    shift_pattern = _build_shift_pattern(shift) if shift else {}
    return _serialize_locum_request(locum_request, {shift.id: shift_pattern} if shift else {})


@router.post("/locum-requests/{request_id}/book")
def book_locum_request(request_id: str, body: dict | None = None, db: Session = Depends(get_db)):
    locum_request = db.query(LocumRequest).filter(LocumRequest.id == request_id).first()
    if not locum_request:
        raise HTTPException(status_code=404, detail="Locum request not found")

    approval_status = locum_request.approval_status.value if hasattr(locum_request.approval_status, "value") else str(locum_request.approval_status)
    if locum_request.approval_required and approval_status != LocumApprovalStatus.APPROVED.value:
        raise HTTPException(status_code=400, detail="Request must be approved before a bank or agency doctor can be attached")
    if (locum_request.finance_approval_status or "").upper() == "PENDING":
        raise HTTPException(status_code=400, detail="Finance sign-off must be completed before a bank or agency doctor can be attached")
    if (locum_request.finance_approval_status or "").upper() == "DECLINED":
        raise HTTPException(status_code=400, detail="Finance has declined this request and it cannot be booked")

    booked_name = (body or {}).get("booked_doctor_name", "Bank Staff Pool")
    locum_request.booked_doctor_name = booked_name
    locum_request.approval_status = LocumApprovalStatus.FILLED
    if not locum_request.approved_by:
        locum_request.approved_by = (body or {}).get("approved_by", "Medical Staffing Lead")
    _record_audit_event(
        db,
        entity_type="locum_request",
        entity_id=locum_request.id,
        action="BOOKED",
        hospital_site=locum_request.hospital_site,
        actor_name=locum_request.approved_by or "Operations Workspace",
        summary=f"Locum cover booked for {locum_request.ward}",
        detail=f"Booked staff: {booked_name}",
    )
    db.commit()

    return {"status": "success", "request_id": request_id, "approval_status": LocumApprovalStatus.FILLED.value}
