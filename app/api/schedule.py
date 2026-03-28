from collections import defaultdict
import csv
import threading
from fastapi import APIRouter, Depends, status, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session
from datetime import date, datetime, timezone
from io import StringIO
import json
import uuid
from app.db.database import get_db, SessionLocal
from app.db.models import GeneratedSchedule, ScheduleAssignment, ComplianceViolation, FairnessMetric, Doctor
from app.core.schemas import (
    ScheduleGenerationRequest,
    ScheduleGenerationStatus,
    ComplianceReportDetail,
    ScheduleAssignmentResponse,
    SchedulePublicationAction,
)
from app.scheduler.engine import SchedulingEngine

router = APIRouter(prefix="/api/v1/schedule", tags=["schedule"])


def _format_doctor_name(doctor: Doctor | None) -> str:
    if not doctor:
        return "Unknown"
    title = (doctor.title or "Dr").strip()
    preferred = (doctor.preferred_name or doctor.first_name or "").strip()
    surname = (doctor.last_name or "").strip()
    return " ".join(part for part in [title, preferred, surname] if part)


def _get_schedule_doctors(schedule_id: str, db: Session) -> list[Doctor]:
    metric_rows = db.query(FairnessMetric).filter(FairnessMetric.schedule_id == schedule_id).all()
    doctor_ids = {row.doctor_id for row in metric_rows}

    if not doctor_ids:
        assignment_rows = db.query(ScheduleAssignment).filter(ScheduleAssignment.schedule_id == schedule_id).all()
        doctor_ids = {row.doctor_id for row in assignment_rows}

    if not doctor_ids:
        return []

    return db.query(Doctor).filter(Doctor.id.in_(doctor_ids)).all()


def _build_hospital_breakdown(doctors: list[Doctor], assignments: list[ScheduleAssignment] = None) -> dict:
    hospital_breakdown = {}
    doctors_by_id = {doctor.id: doctor for doctor in doctors}

    for doctor in doctors:
        site_summary = hospital_breakdown.setdefault(
            doctor.hospital_site,
            {
                "doctor_count": 0,
                "assignment_count": 0,
            }
        )
        site_summary["doctor_count"] += 1

    for assignment in assignments or []:
        doctor = doctors_by_id.get(assignment.doctor_id)
        if not doctor:
            continue
        site_summary = hospital_breakdown.setdefault(
            doctor.hospital_site,
            {
                "doctor_count": 0,
                "assignment_count": 0,
            }
        )
        site_summary["assignment_count"] += 1

    return hospital_breakdown


def _parse_schedule_notes(notes: str) -> dict:
    if not notes:
        return {}

    try:
        return json.loads(notes)
    except (TypeError, json.JSONDecodeError):
        return {}


def _schedule_status(schedule: GeneratedSchedule) -> str:
    notes = _parse_schedule_notes(schedule.notes)
    if schedule.generated_successfully:
        return "complete"
    if notes.get("status") == "processing" and not notes.get("error"):
        return "processing"
    return "failed"


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_timestamp(value: datetime | None) -> datetime | None:
    if not value:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _build_progress_snapshot(schedule: GeneratedSchedule, notes: dict, status_value: str) -> dict | None:
    progress = notes.get("progress")
    if not isinstance(progress, dict) or not progress:
        return None

    snapshot = dict(progress)
    heartbeat_at = _parse_timestamp(snapshot.get("last_heartbeat_at"))
    started_at = _normalize_timestamp(schedule.generated_at)
    reference_time = heartbeat_at or started_at

    age_seconds = None
    is_stale = False
    if reference_time:
        age_seconds = max(0, int((datetime.now(timezone.utc) - reference_time).total_seconds()))
        is_stale = status_value == "processing" and age_seconds >= 180

    snapshot["last_heartbeat_at"] = heartbeat_at.isoformat() if heartbeat_at else snapshot.get("last_heartbeat_at")
    snapshot["age_seconds"] = age_seconds
    snapshot["is_stale"] = is_stale
    return snapshot


def _build_schedule_summary(schedule: GeneratedSchedule, db: Session) -> dict:
    assignments = db.query(ScheduleAssignment).filter(
        ScheduleAssignment.schedule_id == schedule.id
    ).all()
    doctors = _get_schedule_doctors(schedule.id, db)
    notes = _parse_schedule_notes(schedule.notes)
    status_value = _schedule_status(schedule)
    progress = _build_progress_snapshot(schedule, notes, status_value)

    return {
        "id": schedule.id,
        "year": schedule.schedule_year,
        "generated_at": schedule.generated_at.isoformat(),
        "status": status_value,
        "selected_hospital_sites": notes.get("hospital_sites", []),
        "error": notes.get("error"),
        "progress": progress,
        # Publication state sits alongside generation status so rota runs can move
        # through draft, published, and archived workflow without changing history.
        "publication": {
            "status": (schedule.publication_status or "DRAFT").upper(),
            "published_at": schedule.published_at.isoformat() if schedule.published_at else None,
            "published_by": schedule.published_by,
            "archived_at": schedule.archived_at.isoformat() if schedule.archived_at else None,
            "archived_by": schedule.archived_by,
        },
        "metrics": {
            "total_doctors": schedule.total_doctors,
            "compliance_score": schedule.compliance_score,
            "fairness_score": schedule.fairness_score,
            "exception_count": schedule.exception_count,
            "total_assignments": len(assignments),
        },
        "hospital_breakdown": _build_hospital_breakdown(doctors, assignments),
    }


def _run_schedule_generation(
    schedule_id: str,
    year: int,
    doctor_ids: list[str] | None,
    hospital_sites: list[str] | None,
    config: dict | None,
):
    db = SessionLocal()
    try:
        engine = SchedulingEngine(db)
        engine.generate_rota(
            year=year,
            doctor_ids=doctor_ids,
            hospital_sites=hospital_sites,
            config=config,
            schedule_id=schedule_id,
        )
    finally:
        db.close()


def _build_assignment_query(
    schedule_id: str,
    db: Session,
    doctor_id: str = None,
    hospital_site: str = None,
    date_from: date = None,
    date_to: date = None,
):
    query = db.query(ScheduleAssignment, Doctor).join(Doctor, Doctor.id == ScheduleAssignment.doctor_id).filter(
        ScheduleAssignment.schedule_id == schedule_id
    )

    if doctor_id:
        query = query.filter(ScheduleAssignment.doctor_id == doctor_id)

    if hospital_site:
        query = query.filter(Doctor.hospital_site == hospital_site)

    if date_from:
        query = query.filter(ScheduleAssignment.assignment_date >= date_from)

    if date_to:
        query = query.filter(ScheduleAssignment.assignment_date <= date_to)

    return query


def _build_schedule_bundle(schedule: GeneratedSchedule, db: Session) -> dict:
    assignments = db.query(ScheduleAssignment).filter(
        ScheduleAssignment.schedule_id == schedule.id
    ).order_by(ScheduleAssignment.assignment_date).all()
    violations = db.query(ComplianceViolation).filter(
        ComplianceViolation.schedule_id == schedule.id
    ).all()
    fairness_metrics = db.query(FairnessMetric).filter(
        FairnessMetric.schedule_id == schedule.id
    ).all()

    return {
        "schedule": {
            "id": schedule.id,
            "schedule_year": schedule.schedule_year,
            "generated_at": schedule.generated_at.isoformat() if schedule.generated_at else None,
            "algorithm_version": schedule.algorithm_version,
            "total_doctors": schedule.total_doctors,
            "generated_successfully": schedule.generated_successfully,
            "publication_status": schedule.publication_status,
            "published_at": schedule.published_at.isoformat() if schedule.published_at else None,
            "published_by": schedule.published_by,
            "archived_at": schedule.archived_at.isoformat() if schedule.archived_at else None,
            "archived_by": schedule.archived_by,
            "compliance_score": schedule.compliance_score,
            "fairness_score": schedule.fairness_score,
            "exception_count": schedule.exception_count,
            "notes": schedule.notes,
        },
        "assignments": [
            {
                "id": assignment.id,
                "doctor_id": assignment.doctor_id,
                "assignment_date": assignment.assignment_date.isoformat(),
                "shift_type_id": assignment.shift_type_id,
                "status": assignment.status.value if hasattr(assignment.status, "value") else str(assignment.status),
                "notes": assignment.notes,
            }
            for assignment in assignments
        ],
        "violations": [
            {
                "id": violation.id,
                "doctor_id": violation.doctor_id,
                "violation_type": violation.violation_type,
                "severity": violation.severity,
                "description": violation.description,
                "suggested_fix": violation.suggested_fix,
            }
            for violation in violations
        ],
        "fairness_metrics": [
            {
                "id": metric.id,
                "doctor_id": metric.doctor_id,
                "metric_type": metric.metric_type,
                "assigned_count": metric.assigned_count,
                "target_count": metric.target_count,
                "variance": metric.variance,
                "within_acceptable_range": metric.within_acceptable_range,
            }
            for metric in fairness_metrics
        ],
    }


@router.get("/dashboard-summary", response_model=dict)
def get_dashboard_summary(db: Session = Depends(get_db)):
    """Return summary metrics for the dashboard overview."""

    doctors = db.query(Doctor).all()
    schedules = db.query(GeneratedSchedule).order_by(GeneratedSchedule.generated_at.desc()).all()

    doctor_counts_by_site = defaultdict(int)
    for doctor in doctors:
        doctor_counts_by_site[doctor.hospital_site] += 1

    latest_schedule = schedules[0] if schedules else None
    latest_schedule_summary = None
    latest_published_schedule = next(
        (schedule for schedule in schedules if (schedule.publication_status or "DRAFT").upper() == "PUBLISHED"),
        None,
    )

    if latest_schedule:
        latest_schedule_summary = _build_schedule_summary(latest_schedule, db)

    return {
        "doctor_count": len(doctors),
        "doctor_counts_by_site": dict(doctor_counts_by_site),
        "generated_schedule_count": len(schedules),
        "latest_schedule": latest_schedule_summary,
        "latest_published_schedule": _build_schedule_summary(latest_published_schedule, db) if latest_published_schedule else None,
    }


@router.get("/", response_model=list[dict])
def list_schedules(limit: int = 10, db: Session = Depends(get_db)):
    """List recent generated schedules with summary metrics."""

    schedules = db.query(GeneratedSchedule).order_by(GeneratedSchedule.generated_at.desc()).limit(limit).all()
    return [_build_schedule_summary(schedule, db) for schedule in schedules]


@router.post("/generate", response_model=ScheduleGenerationStatus)
def generate_schedule(
    request: ScheduleGenerationRequest,
    db: Session = Depends(get_db)
):
    """
    Trigger schedule generation and return the new schedule resource.
    """
    
    # Validate year
    if request.year < 2000 or request.year > 2100:
        raise HTTPException(status_code=400, detail="Invalid year")

    if request.doctors is None and db.query(Doctor).count() == 0:
        raise HTTPException(status_code=400, detail="At least one doctor must be imported before generating a schedule")

    if request.hospital_sites:
        valid_sites = {"Wythenshawe Hospital", "Trafford Hospital"}
        invalid_sites = [site for site in request.hospital_sites if site not in valid_sites]
        if invalid_sites:
            raise HTTPException(status_code=400, detail=f"Invalid hospital sites: {', '.join(invalid_sites)}")
    
    schedule = GeneratedSchedule(
        id=str(uuid.uuid4()),
        schedule_year=request.year,
        algorithm_version="1.0.0",
        generated_successfully=False,
        total_doctors=0,
        publication_status="DRAFT",
        notes=json.dumps({
            "status": "processing",
            "hospital_sites": request.hospital_sites or [],
            "site_mode": "selected" if request.hospital_sites else "all",
            "progress": {
                "phase": "queued",
                "percent": 0,
                "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
            },
        }),
    )
    db.add(schedule)
    db.commit()

    generation_thread = threading.Thread(
        target=_run_schedule_generation,
        args=(
            schedule.id,
            request.year,
            request.doctors,
            request.hospital_sites,
            request.algorithm_config,
        ),
        daemon=True,
    )
    generation_thread.start()

    return ScheduleGenerationStatus(
        status="processing",
        poll_url=f"/api/v1/schedule/{schedule.id}"
    )


@router.get("/{schedule_id}", response_model=dict)
def get_schedule(schedule_id: str, db: Session = Depends(get_db)):
    """Get schedule details and metrics"""
    
    schedule = db.query(GeneratedSchedule).filter(GeneratedSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    return _build_schedule_summary(schedule, db)


@router.post("/{schedule_id}/publish", response_model=dict)
def publish_schedule(schedule_id: str, payload: SchedulePublicationAction, db: Session = Depends(get_db)):
    schedule = db.query(GeneratedSchedule).filter(GeneratedSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if not schedule.generated_successfully:
        raise HTTPException(status_code=400, detail="Only completed schedules can be published")

    schedule.publication_status = "PUBLISHED"
    schedule.published_at = datetime.now(timezone.utc)
    schedule.published_by = payload.actor_name
    schedule.archived_at = None
    schedule.archived_by = None
    db.commit()
    db.refresh(schedule)
    return _build_schedule_summary(schedule, db)


@router.post("/{schedule_id}/archive", response_model=dict)
def archive_schedule(schedule_id: str, payload: SchedulePublicationAction, db: Session = Depends(get_db)):
    schedule = db.query(GeneratedSchedule).filter(GeneratedSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if not schedule.generated_successfully:
        raise HTTPException(status_code=400, detail="Only completed schedules can be archived")

    schedule.publication_status = "ARCHIVED"
    schedule.archived_at = datetime.now(timezone.utc)
    schedule.archived_by = payload.actor_name
    db.commit()
    db.refresh(schedule)
    return _build_schedule_summary(schedule, db)


@router.post("/{schedule_id}/mark-draft", response_model=dict)
def mark_schedule_draft(schedule_id: str, payload: SchedulePublicationAction, db: Session = Depends(get_db)):
    schedule = db.query(GeneratedSchedule).filter(GeneratedSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    schedule.publication_status = "DRAFT"
    schedule.published_at = None
    schedule.published_by = payload.actor_name
    schedule.archived_at = None
    schedule.archived_by = None
    db.commit()
    db.refresh(schedule)
    return _build_schedule_summary(schedule, db)


@router.get("/{schedule_id}/compliance-report")
def get_compliance_report(schedule_id: str, db: Session = Depends(get_db)):
    """Get detailed compliance report"""
    
    schedule = db.query(GeneratedSchedule).filter(GeneratedSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    
    doctors = _get_schedule_doctors(schedule_id, db)
    doctors_by_id = {doctor.id: doctor for doctor in doctors}

    # Get all violations
    violations_db = db.query(ComplianceViolation).filter(
        ComplianceViolation.schedule_id == schedule_id
    ).all()
    
    violations = []
    hospital_summary = defaultdict(lambda: {"doctor_count": 0, "errors": 0, "warnings": 0, "violations": 0})

    for doctor in doctors:
        hospital_summary[doctor.hospital_site]["doctor_count"] += 1

    for v in violations_db:
        doctor = doctors_by_id.get(v.doctor_id)
        hospital_site = doctor.hospital_site if doctor else "Unknown"
        hospital_summary[hospital_site]["violations"] += 1
        if v.severity == "ERROR":
            hospital_summary[hospital_site]["errors"] += 1
        elif v.severity == "WARNING":
            hospital_summary[hospital_site]["warnings"] += 1

        violations.append(ComplianceReportDetail(
            doctor_id=v.doctor_id,
            doctor_name=_format_doctor_name(doctor),
            hospital_site=hospital_site,
            violation_type=v.violation_type,
            severity=v.severity,
            description=v.description,
            suggested_fix=v.suggested_fix
        ))
    
    error_count = len([v for v in violations if v.severity == "ERROR"])
    warning_count = len([v for v in violations if v.severity == "WARNING"])
    
    return {
        "schedule_id": schedule_id,
        "generated_datetime": schedule.generated_at.isoformat(),
        "summary": {
            "total_checks": schedule.total_doctors or 0,
            "passed": max(0, (schedule.total_doctors or 0) - error_count),
            "failed": error_count,
            "warnings": warning_count,
            "compliance_percentage": schedule.compliance_score or 0,
            "hospital_breakdown": dict(hospital_summary),
        },
        "violations": violations
    }


@router.get("/{schedule_id}/fairness-report")
def get_fairness_report(schedule_id: str, db: Session = Depends(get_db)):
    """Get fairness analysis report"""
    
    schedule = db.query(GeneratedSchedule).filter(GeneratedSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    
    doctors = _get_schedule_doctors(schedule_id, db)
    doctors_by_id = {doctor.id: doctor for doctor in doctors}

    # Get fairness metrics
    metrics_db = db.query(FairnessMetric).filter(
        FairnessMetric.schedule_id == schedule_id
    ).all()
    
    # Group by metric type
    metrics_by_type = {}
    for m in metrics_db:
        if m.metric_type not in metrics_by_type:
            metrics_by_type[m.metric_type] = []
        metrics_by_type[m.metric_type].append(m)
    
    # Calculate stats for each type
    outliers = []
    metrics_summary = {}
    grade_breakdown = defaultdict(int)
    site_breakdown = {}

    for doctor in doctors:
        grade_breakdown[doctor.grade.value if hasattr(doctor.grade, "value") else str(doctor.grade)] += 1

    for site in sorted({doctor.hospital_site for doctor in doctors}):
        site_breakdown[site] = {
            "doctor_count": len([doctor for doctor in doctors if doctor.hospital_site == site]),
            "metrics": {},
        }
    
    for metric_type, metrics in metrics_by_type.items():
        assigned_counts = [m.assigned_count for m in metrics]
        target_counts = [m.target_count for m in metrics]
        
        if assigned_counts:
            avg_assigned = sum(assigned_counts) / len(assigned_counts)
            avg_target = sum(target_counts) / len(target_counts)
            
            # Calculate std dev
            variance = sum((x - avg_assigned) ** 2 for x in assigned_counts) / len(assigned_counts)
            std_dev = variance ** 0.5
            
            metrics_summary[metric_type] = {
                "target_mean": round(avg_target, 2),
                "actual_mean": round(avg_assigned, 2),
                "std_dev": round(std_dev, 2),
                "acceptable": std_dev < 3  # Threshold for acceptable variance
            }
            
            # Find outliers
            for m in metrics:
                if m.variance and abs(m.variance) > 2:
                    doctor = doctors_by_id.get(m.doctor_id)
                    outliers.append({
                        "doctor_id": m.doctor_id,
                        "doctor_name": _format_doctor_name(doctor),
                        "hospital_site": doctor.hospital_site if doctor else "Unknown",
                        "metric": m.metric_type,
                        "value": m.assigned_count,
                        "target": m.target_count,
                        "deviation": f"+{m.variance:.1f}" if m.variance > 0 else f"{m.variance:.1f}"
                    })

            for site_name in site_breakdown:
                site_metrics = [
                    metric for metric in metrics
                    if doctors_by_id.get(metric.doctor_id) and doctors_by_id[metric.doctor_id].hospital_site == site_name
                ]
                if not site_metrics:
                    continue

                site_assigned_counts = [metric.assigned_count for metric in site_metrics]
                site_target_counts = [metric.target_count for metric in site_metrics]
                site_avg_assigned = sum(site_assigned_counts) / len(site_assigned_counts)
                site_avg_target = sum(site_target_counts) / len(site_target_counts)
                site_variance = sum((value - site_avg_assigned) ** 2 for value in site_assigned_counts) / len(site_assigned_counts)
                site_std_dev = site_variance ** 0.5

                site_breakdown[site_name]["metrics"][metric_type] = {
                    "target_mean": round(site_avg_target, 2),
                    "actual_mean": round(site_avg_assigned, 2),
                    "std_dev": round(site_std_dev, 2),
                    "acceptable": site_std_dev < 3,
                }
    
    return {
        "schedule_id": schedule_id,
        "overall_score": schedule.fairness_score or 0,
        "grade_breakdown": dict(grade_breakdown),
        "site_breakdown": site_breakdown,
        "metrics": metrics_summary,
        "outliers": outliers
    }


@router.get("/{schedule_id}/assignments", response_model=list[ScheduleAssignmentResponse])
def list_assignments(
    schedule_id: str,
    doctor_id: str = None,
    hospital_site: str = None,
    date_from: date = None,
    date_to: date = None,
    db: Session = Depends(get_db)
):
    """List assignments with optional filters"""

    assignment_rows = _build_assignment_query(
        schedule_id=schedule_id,
        db=db,
        doctor_id=doctor_id,
        hospital_site=hospital_site,
        date_from=date_from,
        date_to=date_to,
    ).order_by(ScheduleAssignment.assignment_date).all()
    return [
        ScheduleAssignmentResponse(
            id=assignment.id,
            doctor_id=assignment.doctor_id,
            doctor_name=_format_doctor_name(doctor),
            hospital_site=doctor.hospital_site,
            assignment_date=assignment.assignment_date,
            shift_type_id=assignment.shift_type_id,
            status=assignment.status.value if hasattr(assignment.status, "value") else str(assignment.status),
        )
        for assignment, doctor in assignment_rows
    ]


@router.get("/{schedule_id}/assignments/export")
def export_assignments(
    schedule_id: str,
    doctor_id: str = None,
    hospital_site: str = None,
    date_from: date = None,
    date_to: date = None,
    db: Session = Depends(get_db)
):
    """Export schedule assignments as CSV."""

    schedule = db.query(GeneratedSchedule).filter(GeneratedSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    assignment_rows = _build_assignment_query(
        schedule_id=schedule_id,
        db=db,
        doctor_id=doctor_id,
        hospital_site=hospital_site,
        date_from=date_from,
        date_to=date_to,
    ).order_by(ScheduleAssignment.assignment_date, Doctor.last_name, Doctor.first_name).all()

    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["assignment_date", "doctor_id", "doctor_name", "hospital_site", "shift_type_id", "status"])

    for assignment, doctor in assignment_rows:
        writer.writerow([
            assignment.assignment_date.isoformat(),
            assignment.doctor_id,
            _format_doctor_name(doctor),
            doctor.hospital_site,
            assignment.shift_type_id or "",
            assignment.status.value if hasattr(assignment.status, "value") else str(assignment.status),
        ])

    scope_label = hospital_site.replace(" ", "-").lower() if hospital_site else "all-sites"
    date_label = ""
    if date_from or date_to:
        date_label = f"-{date_from.isoformat() if date_from else 'start'}-to-{date_to.isoformat() if date_to else 'end'}"
    doctor_label = f"-{doctor_id}" if doctor_id else ""
    filename = f"schedule-{schedule_id}-{scope_label}{doctor_label}{date_label}.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/{schedule_id}/export-bundle")
def export_schedule_bundle(schedule_id: str, db: Session = Depends(get_db)):
    """Export the full schedule bundle as JSON for backup/restore."""

    schedule = db.query(GeneratedSchedule).filter(GeneratedSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    filename = f"schedule-{schedule_id}-bundle.json"
    return Response(
        content=json.dumps(_build_schedule_bundle(schedule, db), indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.post("/import-bundle", status_code=status.HTTP_201_CREATED)
def import_schedule_bundle(bundle: dict, db: Session = Depends(get_db)):
    """Import a previously exported schedule bundle as a new schedule record."""

    schedule_payload = bundle.get("schedule")
    assignments_payload = bundle.get("assignments", [])
    violations_payload = bundle.get("violations", [])
    fairness_payload = bundle.get("fairness_metrics", [])

    if not schedule_payload:
        raise HTTPException(status_code=400, detail="Bundle must include a schedule payload")

    referenced_doctor_ids = {
        *[item.get("doctor_id") for item in assignments_payload if item.get("doctor_id")],
        *[item.get("doctor_id") for item in violations_payload if item.get("doctor_id")],
        *[item.get("doctor_id") for item in fairness_payload if item.get("doctor_id")],
    }

    if referenced_doctor_ids:
        existing_doctor_ids = {
            doctor.id for doctor in db.query(Doctor).filter(Doctor.id.in_(referenced_doctor_ids)).all()
        }
        missing_doctor_ids = sorted(referenced_doctor_ids - existing_doctor_ids)
        if missing_doctor_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Bundle references doctors not present in this environment: {', '.join(missing_doctor_ids[:10])}"
            )

    new_schedule_id = str(uuid.uuid4())
    imported_schedule = GeneratedSchedule(
        id=new_schedule_id,
        schedule_year=schedule_payload.get("schedule_year"),
        generated_at=datetime.fromisoformat(schedule_payload["generated_at"]) if schedule_payload.get("generated_at") else datetime.utcnow(),
        algorithm_version=schedule_payload.get("algorithm_version"),
        total_doctors=schedule_payload.get("total_doctors"),
        generated_successfully=schedule_payload.get("generated_successfully"),
        publication_status=schedule_payload.get("publication_status") or "DRAFT",
        published_at=datetime.fromisoformat(schedule_payload["published_at"]) if schedule_payload.get("published_at") else None,
        published_by=schedule_payload.get("published_by"),
        archived_at=datetime.fromisoformat(schedule_payload["archived_at"]) if schedule_payload.get("archived_at") else None,
        archived_by=schedule_payload.get("archived_by"),
        compliance_score=schedule_payload.get("compliance_score"),
        fairness_score=schedule_payload.get("fairness_score"),
        exception_count=schedule_payload.get("exception_count", 0),
        notes=schedule_payload.get("notes"),
    )
    db.add(imported_schedule)
    db.flush()

    for item in assignments_payload:
        db.add(ScheduleAssignment(
            id=str(uuid.uuid4()),
            schedule_id=new_schedule_id,
            doctor_id=item["doctor_id"],
            assignment_date=date.fromisoformat(item["assignment_date"]),
            shift_type_id=item.get("shift_type_id"),
            status=item.get("status"),
            notes=item.get("notes"),
        ))

    for item in violations_payload:
        db.add(ComplianceViolation(
            id=str(uuid.uuid4()),
            schedule_id=new_schedule_id,
            doctor_id=item["doctor_id"],
            violation_type=item["violation_type"],
            severity=item["severity"],
            description=item["description"],
            suggested_fix=item.get("suggested_fix"),
        ))

    for item in fairness_payload:
        db.add(FairnessMetric(
            id=str(uuid.uuid4()),
            schedule_id=new_schedule_id,
            doctor_id=item["doctor_id"],
            metric_type=item["metric_type"],
            assigned_count=item["assigned_count"],
            target_count=item["target_count"],
            variance=item.get("variance"),
            within_acceptable_range=item.get("within_acceptable_range"),
        ))

    db.commit()

    return {
        "status": "success",
        "schedule_id": new_schedule_id,
        "imported_assignments": len(assignments_payload),
        "imported_violations": len(violations_payload),
        "imported_fairness_metrics": len(fairness_payload),
    }


@router.post("/{schedule_id}/assignments/{assignment_id}/override")
def override_assignment(
    schedule_id: str,
    assignment_id: str,
    body: dict,
    db: Session = Depends(get_db)
):
    """
    Manually override an assignment
    body: {"doctor_id": "...", "shift_type_id": "...", "reason": "..."}
    """
    
    assignment = db.query(ScheduleAssignment).filter(
        ScheduleAssignment.id == assignment_id,
        ScheduleAssignment.schedule_id == schedule_id
    ).first()
    
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Update assignment
    assignment.doctor_id = body.get("doctor_id", assignment.doctor_id)
    assignment.shift_type_id = body.get("shift_type_id", assignment.shift_type_id)
    assignment.status = "MANUAL_OVERRIDE"
    assignment.notes = body.get("reason", "")
    
    db.commit()
    
    return {"status": "success", "assignment_id": assignment_id}
