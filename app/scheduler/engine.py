from collections import defaultdict
from datetime import timedelta, date
from typing import List, Dict, Tuple, Optional
import json
import uuid
from app.db.models import (
    Doctor, Contract, ShiftType, ServiceRequirement, 
    ScheduleAssignment, ComplianceViolation, GeneratedSchedule, 
    FairnessMetric, SpecialRequirement, DoctorGrade, AssignmentStatus
)
from app.core.config import settings
from sqlalchemy.orm import Session


class ConstraintValidator:
    """Validates all constraints for a schedule"""
    
    def __init__(self, db: Session):
        self.db = db
        self.violations: List[ComplianceViolation] = []
    
    def validate_weekly_hours(self, doctor_id: str, assignments: List[ScheduleAssignment], 
                             contract: Contract) -> List[Dict]:
        """Check if doctor exceeds contracted hours per week"""
        violations = []
        
        # Group assignments by week
        weeks = {}
        for assignment in assignments:
            week_start = assignment.assignment_date - timedelta(days=assignment.assignment_date.weekday())
            if week_start not in weeks:
                weeks[week_start] = []
            weeks[week_start].append(assignment)
        
        # Check each week
        for week_start, week_assignments in weeks.items():
            total_hours = sum(
                self.db.query(ShiftType).filter(ShiftType.id == a.shift_type_id).first().duration_hours
                for a in week_assignments if a.shift_type_id
            )
            
            if total_hours > contract.contracted_hours_per_week:
                violations.append({
                    "type": "EXCESS_HOURS",
                    "severity": "ERROR",
                    "week": str(week_start),
                    "actual_hours": total_hours,
                    "limit": contract.contracted_hours_per_week,
                    "description": f"Week of {week_start}: {total_hours}h exceeds limit of {contract.contracted_hours_per_week}h"
                })
        
        return violations
    
    def validate_rest_periods(self, assignments: List[ScheduleAssignment]) -> List[Dict]:
        """Check minimum 11-hour rest between shifts"""
        violations = []
        
        # Sort assignments by date
        sorted_assignments = sorted(assignments, key=lambda x: x.assignment_date)
        
        MIN_REST_HOURS = 11
        
        for i in range(len(sorted_assignments) - 1):
            current = sorted_assignments[i]
            next_a = sorted_assignments[i + 1]
            
            # Skip if either is OFF or no shift type
            if not current.shift_type_id or not next_a.shift_type_id:
                continue
            
            current_shift = self.db.query(ShiftType).filter(ShiftType.id == current.shift_type_id).first()
            
            # Assume 8-12 hour shifts (simplified; real system would track end times)
            current_end_time = current.assignment_date + timedelta(hours=10)
            rest_gap = (next_a.assignment_date - current_end_time).total_seconds() / 3600
            
            if rest_gap < MIN_REST_HOURS:
                violations.append({
                    "type": "INSUFFICIENT_REST",
                    "severity": "ERROR",
                    "date": str(next_a.assignment_date),
                    "rest_hours": round(rest_gap, 1),
                    "required": MIN_REST_HOURS,
                    "description": f"Only {round(rest_gap, 1)} hours rest before shift on {next_a.assignment_date}"
                })
        
        return violations
    
    def validate_grade_restrictions(self, grade: DoctorGrade, assignments: List[ScheduleAssignment]) -> List[Dict]:
        """Check if doctor is assigned to shifts appropriate for their grade"""
        violations = []
        
        # FY1 cannot do on-calls or nights
        if grade == DoctorGrade.FY1:
            for assignment in assignments:
                if assignment.shift_type_id:
                    shift_type = self.db.query(ShiftType).filter(ShiftType.id == assignment.shift_type_id).first()
                    if shift_type and (shift_type.is_on_call or shift_type.is_night_shift):
                        violations.append({
                            "type": "INVALID_GRADE_ASSIGNMENT",
                            "severity": "ERROR",
                            "date": str(assignment.assignment_date),
                            "shift": shift_type.name,
                            "description": f"FY1 cannot be assigned {shift_type.name} shift"
                        })
        
        return violations
    
    def validate_all(self, doctor_id: str, schedule_id: str, db_session: Session) -> Tuple[float, List[Dict]]:
        """Run all validations and return compliance score + violations"""
        doctor = db_session.query(Doctor).filter(Doctor.id == doctor_id).first()
        contract = db_session.query(Contract).filter(Contract.doctor_id == doctor_id).first()
        assignments = db_session.query(ScheduleAssignment).filter(
            ScheduleAssignment.schedule_id == schedule_id,
            ScheduleAssignment.doctor_id == doctor_id
        ).all()
        
        all_violations = []
        
        if contract and assignments:
            all_violations.extend(self.validate_weekly_hours(doctor_id, assignments, contract))
            all_violations.extend(self.validate_rest_periods(assignments))
        
        if doctor:
            all_violations.extend(self.validate_grade_restrictions(doctor.grade, assignments))
        
        # Calculate compliance score
        error_count = len([v for v in all_violations if v.get("severity") == "ERROR"])
        warning_count = len([v for v in all_violations if v.get("severity") == "WARNING"])
        
        compliance_score = max(0, 100 - (error_count * 5 + warning_count * 1))
        
        return compliance_score, all_violations


class FairnessAnalyzer:
    """Analyzes fairness of shift distribution"""
    
    def __init__(self, db: Session, config: dict = None):
        self.db = db
        self.config = config or {
            "night_tolerance": settings.fairness_night_tolerance,
            "weekend_tolerance": settings.fairness_weekend_tolerance,
            "oncall_tolerance": settings.fairness_oncall_tolerance,
        }
    
    def calculate_metrics(self, schedule_id: str, doctors: List[str]) -> Tuple[float, List[Dict], List[Dict]]:
        """Calculate fairness metrics for all doctors in schedule"""
        
        metrics_by_doctor = {}
        
        for doctor_id in doctors:
            assignments = self.db.query(ScheduleAssignment).filter(
                ScheduleAssignment.schedule_id == schedule_id,
                ScheduleAssignment.doctor_id == doctor_id
            ).all()
            
            # Count shift types
            night_count = 0
            weekend_count = 0
            oncall_count = 0
            
            for assignment in assignments:
                if assignment.shift_type_id:
                    shift = self.db.query(ShiftType).filter(ShiftType.id == assignment.shift_type_id).first()
                    if shift:
                        if shift.is_night_shift:
                            night_count += 1
                        if shift.is_on_call:
                            oncall_count += 1
                
                # Check if weekend
                if assignment.assignment_date.weekday() >= 4:  # Friday=4, Saturday=5, Sunday=6
                    weekend_count += 1
            
            metrics_by_doctor[doctor_id] = {
                "nights": night_count,
                "weekends": weekend_count,
                "oncalls": oncall_count,
            }
        
        # Calculate means and variances
        if metrics_by_doctor:
            night_counts = [m["nights"] for m in metrics_by_doctor.values()]
            weekend_counts = [m["weekends"] for m in metrics_by_doctor.values()]
            oncall_counts = [m["oncalls"] for m in metrics_by_doctor.values()]
            
            night_mean = sum(night_counts) / len(night_counts) if night_counts else 0
            weekend_mean = sum(weekend_counts) / len(weekend_counts) if weekend_counts else 0
            oncall_mean = sum(oncall_counts) / len(oncall_counts) if oncall_counts else 0
            
            # Calculate fairness score
            night_variance = sum((c - night_mean) ** 2 for c in night_counts) / len(night_counts) if night_counts else 0
            weekend_variance = sum((c - weekend_mean) ** 2 for c in weekend_counts) / len(weekend_counts) if weekend_counts else 0
            oncall_variance = sum((c - oncall_mean) ** 2 for c in oncall_counts) / len(oncall_counts) if oncall_counts else 0
            
            avg_variance = (night_variance + weekend_variance + oncall_variance) / 3
            fairness_score = max(0, 100 - (avg_variance * 5))  # Penalty for variance
            
            outliers = []
            fairness_records = []
            for doctor_id, metrics in metrics_by_doctor.items():
                metric_definitions = [
                    ("NIGHT_SHIFTS", metrics["nights"], night_mean, self.config["night_tolerance"]),
                    ("WEEKENDS", metrics["weekends"], weekend_mean, self.config["weekend_tolerance"]),
                    ("ONCALLS", metrics["oncalls"], oncall_mean, self.config["oncall_tolerance"]),
                ]

                for metric_type, assigned_count, target_count, tolerance in metric_definitions:
                    variance = assigned_count - target_count
                    within_acceptable_range = abs(variance) <= tolerance

                    fairness_records.append({
                        "doctor_id": doctor_id,
                        "metric_type": metric_type,
                        "assigned_count": assigned_count,
                        "target_count": int(round(target_count)),
                        "variance": round(variance, 2),
                        "within_acceptable_range": within_acceptable_range,
                    })

                    if not within_acceptable_range:
                        outliers.append({
                            "doctor_id": doctor_id,
                            "metric": metric_type,
                            "value": assigned_count,
                            "target": round(target_count, 2)
                        })
            
            return fairness_score, outliers, fairness_records
        
        return 100, [], []


class SchedulingEngine:
    """Main scheduling engine using constraint solver approach"""
    
    def __init__(self, db: Session):
        self.db = db
        self.validator = ConstraintValidator(db)
        self.fairness_analyzer = FairnessAnalyzer(db)
    
    def generate_rota(
        self,
        year: int,
        doctor_ids: List[str] = None,
        hospital_sites: List[str] = None,
        config: dict = None
    ) -> Dict:
        """
        Main method to generate rota for given year and doctors
        Returns: dict with schedule_id, compliance_score, fairness_score, exceptions
        """
        
        if not config:
            config = {
                "fairness_tolerance": settings.fairness_night_tolerance,
                "max_iterations": settings.scheduler_max_iterations,
                "optimization_level": "HIGH"
            }
        
        # Create schedule record
        schedule = GeneratedSchedule(
            id=str(uuid.uuid4()),
            schedule_year=year,
            algorithm_version="1.0.0",
            generated_successfully=False,
            total_doctors=len(doctor_ids) if doctor_ids else 0,
        )
        self.db.add(schedule)
        self.db.commit()
        
        try:
            # Get all doctors if not specified
            if not doctor_ids:
                doctor_query = self.db.query(Doctor)
                if hospital_sites:
                    doctor_query = doctor_query.filter(Doctor.hospital_site.in_(hospital_sites))
                doctors = doctor_query.all()
            else:
                doctor_query = self.db.query(Doctor).filter(Doctor.id.in_(doctor_ids))
                if hospital_sites:
                    doctor_query = doctor_query.filter(Doctor.hospital_site.in_(hospital_sites))
                doctors = doctor_query.all()

            doctor_ids = [doctor.id for doctor in doctors]
            schedule.total_doctors = len(doctor_ids)
            selected_sites = sorted({doctor.hospital_site for doctor in doctors})
            schedule.notes = json.dumps({
                "hospital_sites": selected_sites,
                "site_mode": "selected" if hospital_sites else "all",
            })

            if not doctors:
                raise ValueError("No doctors available for schedule generation")
            
            # Basic scheduling: assign doctors to shifts
            # This is a simplified greedy algorithm; Phase 2 will use OR-Tools
            success = self._schedule_doctors_greedy(schedule.id, doctors, year)
            if not success:
                raise ValueError("Unable to generate assignments with the current configuration")
            
            # Run validation
            total_compliance = 0
            total_violations = []
            
            for doctor_id in doctor_ids:
                compliance, violations = self.validator.validate_all(doctor_id, schedule.id, self.db)
                total_compliance += compliance
                total_violations.extend([
                    {
                        **violation,
                        "doctor_id": doctor_id,
                    }
                    for violation in violations
                ])
            
            avg_compliance = total_compliance / len(doctor_ids) if doctor_ids else 100
            
            # Calculate fairness
            fairness_score, outliers, fairness_records = self.fairness_analyzer.calculate_metrics(schedule.id, doctor_ids)

            for violation in total_violations:
                self.db.add(ComplianceViolation(
                    id=str(uuid.uuid4()),
                    schedule_id=schedule.id,
                    doctor_id=violation["doctor_id"],
                    violation_type=violation["type"],
                    severity=violation["severity"],
                    description=violation["description"],
                    suggested_fix=violation.get("suggested_fix"),
                ))

            for record in fairness_records:
                self.db.add(FairnessMetric(
                    id=str(uuid.uuid4()),
                    schedule_id=schedule.id,
                    doctor_id=record["doctor_id"],
                    metric_type=record["metric_type"],
                    assigned_count=record["assigned_count"],
                    target_count=record["target_count"],
                    variance=record["variance"],
                    within_acceptable_range=record["within_acceptable_range"],
                ))
            
            # Update schedule
            schedule.compliance_score = avg_compliance
            schedule.fairness_score = fairness_score
            schedule.exception_count = len(total_violations)
            schedule.generated_successfully = True
            
            self.db.commit()
            
            return {
                "schedule_id": schedule.id,
                "compliance_score": round(avg_compliance, 2),
                "fairness_score": round(fairness_score, 2),
                "exception_count": len(total_violations),
                "outliers": outliers,
                "status": "success"
            }
        
        except Exception as e:
            schedule.generated_successfully = False
            self.db.commit()
            return {
                "schedule_id": schedule.id,
                "status": "error",
                "error": str(e)
            }
    
    def _schedule_doctors_greedy(self, schedule_id: str, doctors: List[Doctor], year: int) -> bool:
        """
        Simple greedy scheduling algorithm
        Phase 2 will replace with proper OR-Tools CSP solver
        """
        
        try:
            if not doctors:
                raise ValueError("No doctors available for assignment")

            # Define year date range (August to July)
            start_date = date(year, 8, 1)
            end_date = date(year + 1, 7, 31)
            
            # Get available shift types
            shift_types = self.db.query(ShiftType).all()
            if not shift_types:
                # Create default shift types if none exist
                self._create_default_shift_types()
                shift_types = self.db.query(ShiftType).all()
            
            # Site-aware assignment: cover each hospital site independently each day.
            current_date = start_date
            doctors_by_site = defaultdict(list)

            for doctor in doctors:
                doctors_by_site[doctor.hospital_site].append(doctor)

            site_indices = {site: 0 for site in doctors_by_site}
            
            while current_date <= end_date:
                for site_name, site_doctors in doctors_by_site.items():
                    if not site_doctors:
                        continue

                    eligible_doctors = [
                        doctor for doctor in site_doctors
                        if self.db.query(Contract).filter(
                            Contract.doctor_id == doctor.id,
                            Contract.start_date <= current_date,
                            Contract.end_date >= current_date
                        ).first()
                    ]

                    if not eligible_doctors:
                        continue

                    doctor = eligible_doctors[site_indices[site_name] % len(eligible_doctors)]
                    shift = self._select_shift_for_doctor(doctor, shift_types, current_date)

                    if shift:
                        assignment = ScheduleAssignment(
                            id=str(uuid.uuid4()),
                            schedule_id=schedule_id,
                            doctor_id=doctor.id,
                            assignment_date=current_date,
                            shift_type_id=shift.id,
                            status=AssignmentStatus.ASSIGNED,
                            notes=f"Hospital site: {site_name}",
                        )
                        self.db.add(assignment)

                    site_indices[site_name] += 1

                current_date += timedelta(days=1)
            
            self.db.commit()
            return True
        
        except Exception as e:
            print(f"Scheduling error: {e}")
            return False
    
    def _select_shift_for_doctor(self, doctor: Doctor, shift_types: List[ShiftType], 
                                current_date: date) -> Optional[ShiftType]:
        """Select appropriate shift type for doctor"""
        
        # FY1: daytime only
        if doctor.grade == DoctorGrade.FY1:
            return next((s for s in shift_types if s.code == "DAYTIME"), None)
        
        # Rotate shift types for other grades
        day_of_week = current_date.weekday()
        
        # Weekends get different distribution
        if day_of_week >= 4:  # Weekend
            return next((s for s in shift_types if s.code in ["DAYTIME", "NIGHT"]), None)
        
        # Weekdays
        if day_of_week % 3 == 0:
            return next((s for s in shift_types if s.code == "NIGHT"), None)
        else:
            return next((s for s in shift_types if s.code == "DAYTIME"), None)
    
    def _create_default_shift_types(self):
        """Create default shift types if none exist"""
        
        default_shifts = [
            {
                "code": "DAYTIME",
                "name": "Day Shift",
                "duration_hours": 8,
                "availability_grades": ["FY1", "FY2", "SHO", "Registrar", "Consultant"],
                "is_night_shift": False,
                "is_on_call": False,
            },
            {
                "code": "LONG_DAY",
                "name": "Long Day",
                "duration_hours": 10,
                "availability_grades": ["FY2", "SHO", "Registrar", "Consultant"],
                "is_night_shift": False,
                "is_on_call": False,
            },
            {
                "code": "NIGHT",
                "name": "Night Shift",
                "duration_hours": 10,
                "availability_grades": ["FY2", "SHO", "Registrar", "Consultant"],
                "is_night_shift": True,
                "is_on_call": False,
            },
            {
                "code": "ONCALL",
                "name": "On-Call",
                "duration_hours": 24,
                "availability_grades": ["SHO", "Registrar", "Consultant"],
                "is_night_shift": False,
                "is_on_call": True,
            },
        ]
        
        for shift_data in default_shifts:
            shift = ShiftType(
                id=str(uuid.uuid4()),
                code=shift_data["code"],
                name=shift_data["name"],
                duration_hours=shift_data["duration_hours"],
                availability_grades=json.dumps(shift_data["availability_grades"]),
                is_night_shift=shift_data["is_night_shift"],
                is_on_call=shift_data["is_on_call"],
            )
            self.db.add(shift)
        
        self.db.commit()
