from datetime import date, timedelta
import json
import uuid

from sqlalchemy.orm import Session

from app.db.models import (
    AvailabilityEventType,
    ComplianceLevel,
    Contract,
    Doctor,
    DoctorAvailabilityEvent,
    DoctorGrade,
    LocumApprovalStatus,
    LocumRequest,
    LocumStaffType,
    ShiftType,
)

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
SHIFT_BLUEPRINTS = [
    {
        "code": "MORNING",
        "name": "Morning Shift",
        "duration_hours": 8,
        "availability_grades": [
            "FY1",
            "FY2",
            "SHO",
            "Registrar",
            "Consultant",
            "ST1",
            "ST2",
            "ST3",
            "ST4",
            "ST5",
            "ST6",
            "ST7",
            "ST8",
        ],
        "is_night_shift": False,
        "is_on_call": False,
    },
    {
        "code": "EVENING",
        "name": "Evening Shift",
        "duration_hours": 8,
        "availability_grades": [
            "FY2",
            "SHO",
            "Registrar",
            "Consultant",
            "ST1",
            "ST2",
            "ST3",
            "ST4",
            "ST5",
            "ST6",
            "ST7",
            "ST8",
        ],
        "is_night_shift": False,
        "is_on_call": False,
    },
    {
        "code": "TWILIGHT",
        "name": "Twilight Shift",
        "duration_hours": 10,
        "availability_grades": [
            "SHO",
            "Registrar",
            "Consultant",
            "ST2",
            "ST3",
            "ST4",
            "ST5",
            "ST6",
            "ST7",
            "ST8",
        ],
        "is_night_shift": False,
        "is_on_call": False,
    },
    {
        "code": "NIGHT",
        "name": "Night Shift",
        "duration_hours": 10,
        "availability_grades": [
            "SHO",
            "Registrar",
            "Consultant",
            "ST3",
            "ST4",
            "ST5",
            "ST6",
            "ST7",
            "ST8",
        ],
        "is_night_shift": True,
        "is_on_call": False,
    },
    {
        "code": "LONG_DAY",
        "name": "Long Day",
        "duration_hours": 12,
        "availability_grades": [
            "FY2",
            "SHO",
            "Registrar",
            "Consultant",
            "ST2",
            "ST3",
            "ST4",
            "ST5",
            "ST6",
            "ST7",
            "ST8",
        ],
        "is_night_shift": False,
        "is_on_call": False,
    },
    {
        "code": "ONCALL",
        "name": "On-Call",
        "duration_hours": 12,
        "availability_grades": ["Registrar", "Consultant", "ST4", "ST5", "ST6", "ST7", "ST8"],
        "is_night_shift": False,
        "is_on_call": True,
    },
]


def _seed_doctors_and_contracts(db: Session) -> None:
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


def _seed_shift_types(db: Session) -> dict[str, ShiftType]:
    existing_by_code = {shift.code: shift for shift in db.query(ShiftType).all()}

    for blueprint in SHIFT_BLUEPRINTS:
        if blueprint["code"] in existing_by_code:
            continue

        shift = ShiftType(
            id=str(uuid.uuid4()),
            code=blueprint["code"],
            name=blueprint["name"],
            duration_hours=blueprint["duration_hours"],
            availability_grades=json.dumps(blueprint["availability_grades"]),
            is_night_shift=blueprint["is_night_shift"],
            is_on_call=blueprint["is_on_call"],
        )
        db.add(shift)

    db.commit()
    return {shift.code: shift for shift in db.query(ShiftType).all()}


def _seed_availability_events(db: Session, doctors: list[Doctor]) -> None:
    if db.query(DoctorAvailabilityEvent).count() > 0 or not doctors:
        return

    doctors_by_site = {site: [doctor for doctor in doctors if doctor.hospital_site == site] for site in HOSPITAL_SITES}
    today = date.today()

    event_definitions = [
        ("Wythenshawe Hospital", 0, AvailabilityEventType.ZERO_DAY, today + timedelta(days=1), today + timedelta(days=1), "ALL_DAY", "APPROVED", "Recovery", "Post-nights zero day"),
        ("Wythenshawe Hospital", 4, AvailabilityEventType.TCPD_DAY, today + timedelta(days=3), today + timedelta(days=3), "ALL_DAY", "APPROVED", "Development", "TCPD day for QI teaching"),
        ("Wythenshawe Hospital", 8, AvailabilityEventType.TEACHING_DAY, today + timedelta(days=4), today + timedelta(days=4), "MORNING", "APPROVED", "Teaching", "Simulation training"),
        ("Wythenshawe Hospital", 12, AvailabilityEventType.SICKNESS, today, today + timedelta(days=2), "ALL_DAY", "RECORDED", "Respiratory illness", "Short notice absence"),
        ("Wythenshawe Hospital", 16, AvailabilityEventType.SHIFT_SWAP, today + timedelta(days=2), today + timedelta(days=2), "EVENING", "PENDING", "Shift swap", "Requested swap with colleague"),
        ("Trafford Hospital", 0, AvailabilityEventType.ZERO_DAY, today + timedelta(days=2), today + timedelta(days=2), "ALL_DAY", "APPROVED", "Recovery", "Zero day after long weekend"),
        ("Trafford Hospital", 5, AvailabilityEventType.TCPD_DAY, today + timedelta(days=5), today + timedelta(days=5), "ALL_DAY", "APPROVED", "Training", "Leadership development day"),
        ("Trafford Hospital", 10, AvailabilityEventType.TEACHING_DAY, today + timedelta(days=6), today + timedelta(days=6), "AFTERNOON", "APPROVED", "Teaching", "Departmental grand round"),
        ("Trafford Hospital", 15, AvailabilityEventType.PATERNITY_LEAVE, today + timedelta(days=7), today + timedelta(days=14), "ALL_DAY", "APPROVED", "Family leave", "Planned paternity leave"),
        ("Trafford Hospital", 20, AvailabilityEventType.MATERNITY_LEAVE, today + timedelta(days=10), today + timedelta(days=50), "ALL_DAY", "APPROVED", "Family leave", "Ongoing maternity leave cover"),
    ]

    events = []
    for site, index, event_type, start_date, end_date, session_label, status, reason_category, notes in event_definitions:
        site_doctors = doctors_by_site.get(site, [])
        if not site_doctors:
            continue

        doctor = site_doctors[index % len(site_doctors)]
        related_doctor_id = None
        if event_type == AvailabilityEventType.SHIFT_SWAP and len(site_doctors) > 1:
            related_doctor_id = site_doctors[(index + 1) % len(site_doctors)].id

        events.append(
            DoctorAvailabilityEvent(
                id=str(uuid.uuid4()),
                doctor_id=doctor.id,
                hospital_site=site,
                event_type=event_type,
                start_date=start_date,
                end_date=end_date,
                session_label=session_label,
                status=status,
                reason_category=reason_category,
                related_doctor_id=related_doctor_id,
                notes=notes,
            )
        )

    db.bulk_save_objects(events)
    db.commit()


def _calculate_estimated_cost(hours: int, grade: DoctorGrade, staff_type: LocumStaffType) -> float:
    grade_rates = {
        DoctorGrade.FY1: 38,
        DoctorGrade.FY2: 42,
        DoctorGrade.SHO: 52,
        DoctorGrade.ST1: 44,
        DoctorGrade.ST2: 48,
        DoctorGrade.ST3: 58,
        DoctorGrade.ST4: 62,
        DoctorGrade.ST5: 66,
        DoctorGrade.ST6: 72,
        DoctorGrade.ST7: 78,
        DoctorGrade.ST8: 82,
        DoctorGrade.REGISTRAR: 72,
        DoctorGrade.CONSULTANT: 110,
    }
    staff_multipliers = {
        LocumStaffType.BANK: 1.0,
        LocumStaffType.INTERNAL: 0.92,
        LocumStaffType.AGENCY: 1.25,
    }
    base_rate = grade_rates.get(grade, 50)
    multiplier = staff_multipliers.get(staff_type, 1.0)
    return round(hours * base_rate * multiplier, 2)


def _seed_locum_requests(db: Session, shifts_by_code: dict[str, ShiftType]) -> None:
    if db.query(LocumRequest).count() > 0:
        return

    today = date.today()
    locum_definitions = [
        {
            "hospital_site": "Wythenshawe Hospital",
            "department": "Acute Medicine",
            "ward": "AMU",
            "requested_date": today + timedelta(days=1),
            "shift_code": "EVENING",
            "required_grade": DoctorGrade.SHO,
            "compliance_level": ComplianceLevel.ENHANCED,
            "staff_type": LocumStaffType.BANK,
            "approval_status": LocumApprovalStatus.PENDING_APPROVAL,
            "approval_required": True,
            "requested_hours": 8,
            "shortage_reason": "Unexpected sickness cover gap",
            "requested_by": "Site Manager",
            "notes": "Bank request raised after same-day absence",
        },
        {
            "hospital_site": "Wythenshawe Hospital",
            "department": "General Surgery",
            "ward": "Ward F6",
            "requested_date": today + timedelta(days=2),
            "shift_code": "NIGHT",
            "required_grade": DoctorGrade.REGISTRAR,
            "compliance_level": ComplianceLevel.CRITICAL,
            "staff_type": LocumStaffType.AGENCY,
            "approval_status": LocumApprovalStatus.APPROVED,
            "approval_required": True,
            "requested_hours": 10,
            "shortage_reason": "Registrar gap after rota escalation",
            "requested_by": "Rota Coordinator",
            "approved_by": "Medical Staffing Lead",
            "notes": "Awaiting doctor attachment after approval",
        },
        {
            "hospital_site": "Trafford Hospital",
            "department": "Emergency Medicine",
            "ward": "ED Majors",
            "requested_date": today + timedelta(days=1),
            "shift_code": "TWILIGHT",
            "required_grade": DoctorGrade.ST3,
            "compliance_level": ComplianceLevel.ENHANCED,
            "staff_type": LocumStaffType.BANK,
            "approval_status": LocumApprovalStatus.FILLED,
            "approval_required": True,
            "requested_hours": 10,
            "shortage_reason": "Demand surge above planned establishment",
            "requested_by": "ED Coordinator",
            "approved_by": "Duty Consultant",
            "booked_doctor_name": "Dr Maya Reed",
            "notes": "Booked from bank pool",
        },
        {
            "hospital_site": "Trafford Hospital",
            "department": "Paediatrics",
            "ward": "Children's Assessment Unit",
            "requested_date": today + timedelta(days=4),
            "shift_code": "MORNING",
            "required_grade": DoctorGrade.FY2,
            "compliance_level": ComplianceLevel.STANDARD,
            "staff_type": LocumStaffType.INTERNAL,
            "approval_status": LocumApprovalStatus.PENDING_APPROVAL,
            "approval_required": False,
            "requested_hours": 8,
            "shortage_reason": "Teaching day backfill",
            "requested_by": "Service Manager",
            "notes": "Internal bank preferred before agency escalation",
        },
        {
            "hospital_site": "Wythenshawe Hospital",
            "department": "Anaesthetics",
            "ward": "Theatres",
            "requested_date": today + timedelta(days=5),
            "shift_code": "ONCALL",
            "required_grade": DoctorGrade.CONSULTANT,
            "compliance_level": ComplianceLevel.CRITICAL,
            "staff_type": LocumStaffType.AGENCY,
            "approval_status": LocumApprovalStatus.PENDING_APPROVAL,
            "approval_required": True,
            "requested_hours": 12,
            "shortage_reason": "Consultant on-call vacancy",
            "requested_by": "Clinical Director",
            "notes": "Needs divisional approval before booking",
        },
    ]

    requests = []
    for definition in locum_definitions:
        shift = shifts_by_code.get(definition["shift_code"])
        requests.append(
            LocumRequest(
                id=str(uuid.uuid4()),
                hospital_site=definition["hospital_site"],
                department=definition["department"],
                ward=definition["ward"],
                requested_date=definition["requested_date"],
                shift_type_id=shift.id if shift else None,
                required_grade=definition["required_grade"],
                compliance_level=definition["compliance_level"],
                staff_type=definition["staff_type"],
                approval_status=definition["approval_status"],
                approval_required=definition["approval_required"],
                requested_hours=definition["requested_hours"],
                estimated_cost=_calculate_estimated_cost(
                    definition["requested_hours"],
                    definition["required_grade"],
                    definition["staff_type"],
                ),
                shortage_reason=definition["shortage_reason"],
                requested_by=definition["requested_by"],
                approved_by=definition.get("approved_by"),
                booked_doctor_name=definition.get("booked_doctor_name"),
                notes=definition.get("notes"),
            )
        )

    db.bulk_save_objects(requests)
    db.commit()


def seed_sample_data(db: Session) -> None:
    """Seed baseline doctors and richer operational demo data."""
    _seed_doctors_and_contracts(db)
    shifts_by_code = _seed_shift_types(db)
    doctors = db.query(Doctor).all()
    _seed_availability_events(db, doctors)
    _seed_locum_requests(db, shifts_by_code)
