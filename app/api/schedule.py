from collections import defaultdict
import csv
from fastapi import APIRouter, Depends, status, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session
from datetime import date
from io import StringIO
import json
from app.db.database import get_db
from app.db.models import GeneratedSchedule, ScheduleAssignment, ComplianceViolation, FairnessMetric, Doctor
from app.core.schemas import ScheduleGenerationRequest, ScheduleGenerationStatus, ComplianceReportDetail, ScheduleAssignmentResponse
from app.scheduler.engine import SchedulingEngine

router = APIRouter(prefix="/api/v1/schedule", tags=["schedule"])


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


def _build_schedule_summary(schedule: GeneratedSchedule, db: Session) -> dict:
    assignments = db.query(ScheduleAssignment).filter(
        ScheduleAssignment.schedule_id == schedule.id
    ).all()
    doctors = _get_schedule_doctors(schedule.id, db)
    notes = _parse_schedule_notes(schedule.notes)

    return {
        "id": schedule.id,
        "year": schedule.schedule_year,
        "generated_at": schedule.generated_at.isoformat(),
        "status": "complete" if schedule.generated_successfully else "failed",
        "selected_hospital_sites": notes.get("hospital_sites", []),
        "error": notes.get("error"),
        "metrics": {
            "total_doctors": schedule.total_doctors,
            "compliance_score": schedule.compliance_score,
            "fairness_score": schedule.fairness_score,
            "exception_count": schedule.exception_count,
            "total_assignments": len(assignments),
        },
        "hospital_breakdown": _build_hospital_breakdown(doctors, assignments),
    }


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

    if latest_schedule:
        latest_schedule_summary = _build_schedule_summary(latest_schedule, db)

    return {
        "doctor_count": len(doctors),
        "doctor_counts_by_site": dict(doctor_counts_by_site),
        "generated_schedule_count": len(schedules),
        "latest_schedule": latest_schedule_summary,
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
    
    # Create scheduling engine
    engine = SchedulingEngine(db)
    
    # Run scheduling (could be moved to Celery for async in Phase 2)
    result = engine.generate_rota(
        year=request.year,
        doctor_ids=request.doctors,
        hospital_sites=request.hospital_sites,
        config=request.algorithm_config
    )
    
    if result["status"] == "success":
        return ScheduleGenerationStatus(
            status="complete",
            poll_url=f"/api/v1/schedule/{result['schedule_id']}"
        )
    else:
        raise HTTPException(status_code=500, detail=result.get("error", "Schedule generation failed"))


@router.get("/{schedule_id}", response_model=dict)
def get_schedule(schedule_id: str, db: Session = Depends(get_db)):
    """Get schedule details and metrics"""
    
    schedule = db.query(GeneratedSchedule).filter(GeneratedSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

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
            doctor_name=f"{doctor.first_name} {doctor.last_name}" if doctor else "Unknown",
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
                        "doctor_name": f"{doctor.first_name} {doctor.last_name}" if doctor else "Unknown",
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
            doctor_name=f"{doctor.first_name} {doctor.last_name}",
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
            f"{doctor.first_name} {doctor.last_name}",
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
