from collections import defaultdict
from datetime import date, timedelta
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.schemas import AvailabilityEventCreate, LocumRequestCreate
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
    ScheduleAssignment,
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
        "doctor_name": f"{doctor.first_name} {doctor.last_name}" if doctor else "Unknown",
        "doctor_grade": doctor.grade.value if doctor and hasattr(doctor.grade, "value") else str(doctor.grade) if doctor else "Unknown",
        "hospital_site": event.hospital_site,
        "event_type": event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type),
        "event_label": EVENT_LABELS.get(event.event_type, str(event.event_type)),
        "start_date": event.start_date.isoformat(),
        "end_date": event.end_date.isoformat(),
        "session_label": event.session_label,
        "status": event.status,
        "reason_category": event.reason_category,
        "related_doctor_name": f"{related_doctor.first_name} {related_doctor.last_name}" if related_doctor else None,
        "notes": event.notes,
    }


def _serialize_locum_request(request: LocumRequest, shift_patterns_by_id: dict[str, dict]) -> dict:
    shift_pattern = shift_patterns_by_id.get(request.shift_type_id, {})
    governance = _build_locum_governance(request, shift_pattern)
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
        "booked_doctor_name": request.booked_doctor_name,
        "notes": request.notes,
        **governance,
    }


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
        entries.append({
            "date": request.requested_date.isoformat(),
            "hospital_site": request.hospital_site,
            "ward": request.ward,
            "department": request.department,
            "shift_name": shift_pattern.get("name", "Unmapped shift"),
            "shift_window": shift_pattern.get("shift_window", "TBC"),
            "required_grade": request.required_grade.value if hasattr(request.required_grade, "value") else str(request.required_grade),
            "internal_cover_count": internal_cover[(request.requested_date.isoformat(), request.hospital_site)],
            "locum_status": request.approval_status.value if hasattr(request.approval_status, "value") else str(request.approval_status),
            "compliance_level": request.compliance_level.value if hasattr(request.compliance_level, "value") else str(request.compliance_level),
            "approval_required": request.approval_required,
            "booked_doctor_name": request.booked_doctor_name,
            "estimated_cost": request.estimated_cost,
            "shortage_reason": request.shortage_reason,
        })

    return sorted(entries, key=lambda item: (item["date"], item["hospital_site"], item["shift_name"]))


def _build_compliance_payload(locum_requests: list[LocumRequest], shift_patterns: list[dict]) -> dict:
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
    risk_register = []
    for request in locum_requests:
        approval_status = request.approval_status.value if hasattr(request.approval_status, "value") else str(request.approval_status)
        if approval_status == LocumApprovalStatus.DECLINED.value:
            continue
        shift_pattern = shift_patterns_by_id.get(request.shift_type_id, {})
        governance = _build_locum_governance(request, shift_pattern)
        if approval_status == LocumApprovalStatus.PENDING_APPROVAL.value:
            approval_queue.append({
                "id": request.id,
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

    return {
        "grade_rules": rules,
        "approval_queue": approval_queue,
        "risk_register": risk_register,
    }


def _build_recommended_actions(coverage_pressure: list[dict], locum_requests: list[LocumRequest]) -> list[dict]:
    actions = []
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


@router.get("/workspace")
def get_operations_workspace(db: Session = Depends(get_db)):
    doctors = db.query(Doctor).all()
    doctors_by_id = {doctor.id: doctor for doctor in doctors}
    shift_patterns = [_build_shift_pattern(shift) for shift in db.query(ShiftType).all()]
    shift_patterns_by_id = {pattern["id"]: pattern for pattern in shift_patterns}
    events = db.query(DoctorAvailabilityEvent).order_by(DoctorAvailabilityEvent.start_date.asc()).limit(20).all()
    locum_requests = db.query(LocumRequest).order_by(LocumRequest.requested_date.asc()).all()
    latest_schedule = db.query(GeneratedSchedule).filter(
        GeneratedSchedule.generated_successfully == True  # noqa: E712
    ).order_by(GeneratedSchedule.generated_at.desc()).first()

    coverage_pressure = _build_coverage_pressure(events, locum_requests)
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
            "latest_schedule_id": latest_schedule.id if latest_schedule else None,
        },
        "rota_planning": {
            "coverage_pressure": coverage_pressure,
            "grade_distribution": _build_grade_mix(doctors),
            "recommended_actions": _build_recommended_actions(coverage_pressure, locum_requests),
            "ward_shortfalls": [
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
        "compliance": _build_compliance_payload(locum_requests, shift_patterns),
        "reference_data": {
            "hospital_sites": sorted({doctor.hospital_site for doctor in doctors}),
            "doctor_grades": [grade.value for grade in DoctorGrade],
            "availability_event_types": [
                {"value": event_type.value, "label": EVENT_LABELS.get(event_type, event_type.value.replace("_", " ").title())}
                for event_type in AvailabilityEventType
            ],
            "compliance_levels": [level.value for level in ComplianceLevel],
            "staff_types": ["BANK", "INTERNAL", "AGENCY"],
            "availability_statuses": ["APPROVED", "PENDING", "RECORDED", "CANCELLED"],
        },
    }


@router.post("/availability-events", status_code=201)
def create_availability_event(payload: AvailabilityEventCreate, db: Session = Depends(get_db)):
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
    db.commit()
    db.refresh(event)

    doctors_by_id = {item.id: item for item in [doctor] + ([related_doctor] if related_doctor else [])}
    return _serialize_event(event, doctors_by_id)


@router.post("/locum-requests", status_code=201)
def create_locum_request(payload: LocumRequestCreate, db: Session = Depends(get_db)):
    shift = db.query(ShiftType).filter(ShiftType.code == payload.shift_code).first()
    if not shift:
        raise HTTPException(status_code=404, detail="Shift pattern not found")

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
    db.add(locum_request)
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
    locum_request.approval_status = LocumApprovalStatus.APPROVED
    locum_request.approved_by = approved_by
    db.commit()

    return {"status": "success", "request_id": request_id, "approval_status": LocumApprovalStatus.APPROVED.value}


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
    if note:
        existing_notes = event.notes or ""
        event.notes = f"{existing_notes}\nStatus update: {note}".strip()
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

    booked_name = (body or {}).get("booked_doctor_name", "Bank Staff Pool")
    locum_request.booked_doctor_name = booked_name
    locum_request.approval_status = LocumApprovalStatus.FILLED
    if not locum_request.approved_by:
        locum_request.approved_by = (body or {}).get("approved_by", "Medical Staffing Lead")
    db.commit()

    return {"status": "success", "request_id": request_id, "approval_status": LocumApprovalStatus.FILLED.value}
