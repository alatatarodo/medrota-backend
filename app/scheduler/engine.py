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
                if assignment.assignment_date.weekday() >= 5:  # Saturday=5, Sunday=6
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
            schedule.notes = json.dumps({
                "error": str(e),
                "hospital_sites": hospital_sites or [],
            })
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

            shift_by_code = {shift.code: shift for shift in shift_types}
            shift_by_id = {shift.id: shift for shift in shift_types}
            service_requirements = self.db.query(ServiceRequirement).all()
            requirements_by_home_base = defaultdict(list)
            for requirement in service_requirements:
                site, department, ward = self._parse_service_key(requirement.ward_or_clinic)
                requirements_by_home_base[(site, department, ward)].append(requirement)

            contracts = self.db.query(Contract).filter(
                Contract.doctor_id.in_([doctor.id for doctor in doctors]),
                Contract.start_date <= end_date,
                Contract.end_date >= start_date,
            ).all()
            contracts_by_doctor = defaultdict(list)
            for contract in contracts:
                contracts_by_doctor[contract.doctor_id].append(contract)
            
            # Site-aware assignment: cover each hospital site independently each day.
            current_date = start_date
            doctors_by_site = defaultdict(list)
            pending_assignments = []

            for doctor in doctors:
                doctors_by_site[doctor.hospital_site].append(doctor)

            site_indices = {site: 0 for site in doctors_by_site}
            home_base_indices = defaultdict(int)
            department_indices = defaultdict(int)
            assignment_counts_by_doctor = defaultdict(int)
            
            while current_date <= end_date:
                for site_name, site_doctors in doctors_by_site.items():
                    if not site_doctors:
                        continue

                    eligible_doctors = [
                        doctor for doctor in site_doctors
                        if any(
                            contract.start_date <= current_date <= contract.end_date
                            for contract in contracts_by_doctor.get(doctor.id, [])
                        )
                    ]

                    if not eligible_doctors:
                        continue

                    eligible_by_home_base = defaultdict(list)
                    eligible_by_department = defaultdict(list)
                    for doctor in eligible_doctors:
                        eligible_by_home_base[self._doctor_home_base_key(doctor)].append(doctor)
                        eligible_by_department[(doctor.hospital_site, doctor.department or doctor.specialty)].append(doctor)

                    assigned_doctor_ids = set()
                    daily_site_quota = self._daily_site_assignment_quota(len(eligible_doctors))
                    applicable_requirements = []
                    active_templates = self._service_templates_for_date(current_date)

                    for home_base_key, requirement_rows in requirements_by_home_base.items():
                        if home_base_key[0] != site_name:
                            continue
                        for requirement in requirement_rows:
                            if str(requirement.day_of_week or "ALL").upper() not in active_templates:
                                continue
                            shift = shift_by_id.get(requirement.shift_type_id)
                            if not shift:
                                continue
                            applicable_requirements.append((int(requirement.required_doctors or 0), home_base_key, requirement, shift))

                    applicable_requirements.sort(key=lambda item: item[0], reverse=True)

                    for required_count, home_base_key, requirement, shift in applicable_requirements:
                        if len(assigned_doctor_ids) >= daily_site_quota:
                            break

                        home_base_doctors = eligible_by_home_base.get(home_base_key, [])
                        if not home_base_doctors:
                            continue

                        slots_to_fill = min(required_count, daily_site_quota - len(assigned_doctor_ids))
                        filled_slots = 0

                        while filled_slots < slots_to_fill:
                            doctor = None
                            fill_source = "home_base"
                            candidate_pools = [
                                ("home_base", home_base_doctors, home_base_indices[home_base_key]),
                                ("department", eligible_by_department.get((home_base_key[0], home_base_key[1]), []), department_indices[(home_base_key[0], home_base_key[1])]),
                                ("site", eligible_doctors, site_indices[site_name]),
                            ]
                            for source_name, pool, rotation_index in candidate_pools:
                                doctor = self._select_balanced_doctor(
                                    pool,
                                    rotation_index,
                                    assigned_doctor_ids,
                                    assignment_counts_by_doctor,
                                    shift,
                                )
                                if doctor:
                                    fill_source = source_name
                                    if source_name == "home_base":
                                        home_base_indices[home_base_key] += 1
                                    elif source_name == "department":
                                        department_indices[(home_base_key[0], home_base_key[1])] += 1
                                    else:
                                        site_indices[site_name] += 1
                                    break
                            if not doctor:
                                break

                            pending_assignments.append(ScheduleAssignment(
                                id=str(uuid.uuid4()),
                                schedule_id=schedule_id,
                                doctor_id=doctor.id,
                                assignment_date=current_date,
                                shift_type_id=shift.id,
                                status=AssignmentStatus.ASSIGNED,
                                notes=(
                                    f"Hospital site: {site_name}; "
                                    f"Department: {home_base_key[1]}; "
                                    f"Ward: {home_base_key[2]}; "
                                    f"Day template: {requirement.day_of_week or 'ALL'}; "
                                    f"Fill source: {fill_source}"
                                ),
                            ))
                            assigned_doctor_ids.add(doctor.id)
                            assignment_counts_by_doctor[doctor.id] += 1
                            filled_slots += 1

                    if not assigned_doctor_ids:
                        doctor = self._select_balanced_doctor(
                            eligible_doctors,
                            site_indices[site_name],
                            assigned_doctor_ids,
                            assignment_counts_by_doctor,
                            None,
                        )
                        site_indices[site_name] += 1
                        if not doctor:
                            continue
                        shift = self._select_shift_for_doctor(
                            doctor,
                            shift_by_code,
                            shift_by_id,
                            current_date,
                            requirements_by_home_base,
                        )

                        if shift:
                            pending_assignments.append(ScheduleAssignment(
                                id=str(uuid.uuid4()),
                                schedule_id=schedule_id,
                                doctor_id=doctor.id,
                                assignment_date=current_date,
                                shift_type_id=shift.id,
                                status=AssignmentStatus.ASSIGNED,
                                notes=f"Hospital site: {site_name}",
                            ))
                            assignment_counts_by_doctor[doctor.id] += 1

                current_date += timedelta(days=1)

            if pending_assignments:
                self.db.bulk_save_objects(pending_assignments)

            self.db.commit()
            return True
        
        except Exception as e:
            print(f"Scheduling error: {e}")
            return False

    def _parse_service_key(self, raw_value: str) -> Tuple[str, str, str]:
        site, department, ward = (str(raw_value or "").split("::") + ["Unknown", "Unknown", "Unknown"])[:3]
        return site, department, ward

    def _doctor_home_base_key(self, doctor: Doctor) -> Tuple[str, str, str]:
        return (doctor.hospital_site, doctor.department or doctor.specialty, doctor.ward or "Unassigned Base Ward")

    def _daily_site_assignment_quota(self, eligible_count: int) -> int:
        return max(2, min(18, max(1, eligible_count // 50)))

    def _select_balanced_doctor(
        self,
        doctors: List[Doctor],
        rotation_index: int,
        assigned_doctor_ids: set[str],
        assignment_counts_by_doctor: Dict[str, int],
        shift: Optional[ShiftType],
    ) -> Optional[Doctor]:
        if not doctors:
            return None

        ordered_doctors = doctors[rotation_index % len(doctors):] + doctors[:rotation_index % len(doctors)]
        candidates = [
            (index, doctor)
            for index, doctor in enumerate(ordered_doctors)
            if doctor.id not in assigned_doctor_ids and (shift is None or self._shift_allows_grade(shift, doctor))
        ]
        if not candidates:
            return None

        _, selected_doctor = min(
            candidates,
            key=lambda item: (assignment_counts_by_doctor[item[1].id], item[0]),
        )
        return selected_doctor

    def _is_supported_bank_holiday(self, current_date: date) -> bool:
        return (current_date.month, current_date.day) in {(1, 1), (12, 25), (12, 26)}

    def _service_templates_for_date(self, current_date: date) -> set[str]:
        templates = {"ALL"}
        weekday_codes = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        templates.add(weekday_codes[current_date.weekday()])
        templates.add("WEEKEND" if current_date.weekday() >= 5 else "WEEKDAY")
        if self._is_supported_bank_holiday(current_date):
            templates.add("BANK_HOLIDAY")
        return templates

    def _shift_allows_grade(self, shift: ShiftType, doctor: Doctor) -> bool:
        try:
            allowed_grades = json.loads(shift.availability_grades or "[]")
        except (TypeError, json.JSONDecodeError):
            allowed_grades = []
        grade_key = doctor.grade.value if hasattr(doctor.grade, "value") else str(doctor.grade)
        return not allowed_grades or grade_key in allowed_grades
    
    def _select_shift_for_doctor(
        self,
        doctor: Doctor,
        shift_types_by_code: Dict[str, ShiftType],
        shift_types_by_id: Dict[str, ShiftType],
        current_date: date,
        requirements_by_home_base: Dict[Tuple[str, str, str], List[ServiceRequirement]],
    ) -> Optional[ShiftType]:
        """Select appropriate shift type for doctor"""

        def pick(*codes: str) -> Optional[ShiftType]:
            for code in codes:
                shift = shift_types_by_code.get(code)
                if shift and self._shift_allows_grade(shift, doctor):
                    return shift
            return None

        home_base_key = self._doctor_home_base_key(doctor)
        preferred_requirements = []
        for requirement in requirements_by_home_base.get(home_base_key, []):
            day_template = str(requirement.day_of_week or "ALL").upper()
            if day_template not in self._service_templates_for_date(current_date):
                continue
            shift = shift_types_by_id.get(requirement.shift_type_id)
            if shift and self._shift_allows_grade(shift, doctor):
                preferred_requirements.append((int(requirement.required_doctors or 0), shift))

        if preferred_requirements:
            preferred_requirements.sort(key=lambda item: item[0], reverse=True)
            return preferred_requirements[0][1]

        # FY1: morning/daytime only
        if doctor.grade == DoctorGrade.FY1:
            return pick("MORNING", "DAYTIME")

        # Rotate shift types for other grades
        day_of_week = current_date.weekday()

        if doctor.grade in {DoctorGrade.FY2, DoctorGrade.ST1, DoctorGrade.ST2}:
            if day_of_week >= 4:
                return pick("LONG_DAY", "EVENING", "MORNING", "DAYTIME")
            return pick("MORNING", "EVENING", "DAYTIME")

        if doctor.grade in {DoctorGrade.CONSULTANT, DoctorGrade.REGISTRAR, DoctorGrade.ST6, DoctorGrade.ST7, DoctorGrade.ST8}:
            if day_of_week == 0:
                return pick("ONCALL", "MORNING", "DAYTIME")
            if day_of_week in {4, 5}:
                return pick("NIGHT", "TWILIGHT", "LONG_DAY")
            return pick("TWILIGHT", "MORNING", "EVENING")

        if day_of_week >= 5:
            return pick("TWILIGHT", "LONG_DAY", "NIGHT", "DAYTIME")
        if day_of_week % 3 == 0:
            return pick("NIGHT", "TWILIGHT", "EVENING")
        if day_of_week % 2 == 0:
            return pick("EVENING", "MORNING", "DAYTIME")
        return pick("MORNING", "DAYTIME")
    
    def _create_default_shift_types(self):
        """Create default shift types if none exist"""
        
        default_shifts = [
            {
                "code": "MORNING",
                "name": "Morning Shift",
                "duration_hours": 8,
                "availability_grades": ["FY1", "FY2", "SHO", "Registrar", "Consultant", "ST1", "ST2", "ST3", "ST4", "ST5", "ST6", "ST7", "ST8"],
                "is_night_shift": False,
                "is_on_call": False,
            },
            {
                "code": "EVENING",
                "name": "Evening Shift",
                "duration_hours": 8,
                "availability_grades": ["FY2", "SHO", "Registrar", "Consultant", "ST1", "ST2", "ST3", "ST4", "ST5", "ST6", "ST7", "ST8"],
                "is_night_shift": False,
                "is_on_call": False,
            },
            {
                "code": "TWILIGHT",
                "name": "Twilight Shift",
                "duration_hours": 10,
                "availability_grades": ["SHO", "Registrar", "Consultant", "ST2", "ST3", "ST4", "ST5", "ST6", "ST7", "ST8"],
                "is_night_shift": False,
                "is_on_call": False,
            },
            {
                "code": "LONG_DAY",
                "name": "Long Day",
                "duration_hours": 12,
                "availability_grades": ["FY2", "SHO", "Registrar", "Consultant", "ST2", "ST3", "ST4", "ST5", "ST6", "ST7", "ST8"],
                "is_night_shift": False,
                "is_on_call": False,
            },
            {
                "code": "NIGHT",
                "name": "Night Shift",
                "duration_hours": 10,
                "availability_grades": ["SHO", "Registrar", "Consultant", "ST3", "ST4", "ST5", "ST6", "ST7", "ST8"],
                "is_night_shift": True,
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
