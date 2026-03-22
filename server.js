const fs = require('fs')
const path = require('path')
const express = require('express')
const cors = require('cors')

const app = express()
const PORT = Number(process.env.PORT || 10000)
const HOSPITAL_SITES = ['Wythenshawe Hospital', 'Trafford Hospital']
const SHIFT_TYPES = ['DAYTIME', 'LONG_DAY', 'NIGHT']
const GRADE_CYCLE = ['FY1', 'FY2', 'SHO', 'Registrar', 'Consultant', 'ST1', 'ST2', 'ST3', 'ST4', 'ST5']
const DATA_DIRECTORY = path.resolve(process.env.DATA_DIR || path.join(__dirname, 'data'))
const STORE_PATH = path.join(DATA_DIRECTORY, 'medrota-store.json')

app.use(express.json({ limit: '10mb' }))
app.use(
  cors({
    origin: true,
    credentials: false,
  })
)

function createId(prefix) {
  const randomPart = Math.random().toString(36).slice(2, 10)
  const timePart = Date.now().toString(36)
  return `${prefix}-${timePart}-${randomPart}`
}

function padNumber(value, width = 4) {
  return String(value).padStart(width, '0')
}

function formatIsoDate(year, month, day) {
  return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`
}

function computeDoctorCountsBySite(doctors) {
  return doctors.reduce((acc, doctor) => {
    const site = doctor.hospital_site || 'Unknown'
    acc[site] = (acc[site] || 0) + 1
    return acc
  }, {})
}

function buildDoctorSeed() {
  const doctors = []
  const contracts = []
  const startYear = new Date().getUTCFullYear()

  HOSPITAL_SITES.forEach((site, siteIndex) => {
    for (let i = 0; i < 800; i += 1) {
      const sequence = siteIndex * 800 + i + 1
      const doctorId = `doc-${padNumber(sequence, 5)}`
      const gmcNumber = `70${padNumber(sequence, 5)}`
      const grade = GRADE_CYCLE[(sequence - 1) % GRADE_CYCLE.length]
      const specialty = ['Medicine', 'Emergency Medicine', 'General Surgery', 'Anaesthetics'][(sequence - 1) % 4]
      const doctor = {
        id: doctorId,
        gmc_number: gmcNumber,
        first_name: `Doctor${padNumber(sequence, 4)}`,
        last_name: siteIndex === 0 ? 'Wythenshawe' : 'Trafford',
        email: `doctor${sequence}@medrota.ai`,
        grade,
        specialty,
        hospital_site: site,
      }

      doctors.push(doctor)
      contracts.push({
        id: `contract-${padNumber(sequence, 5)}`,
        doctor_id: doctor.id,
        start_date: formatIsoDate(startYear, 8, 1),
        end_date: formatIsoDate(startYear + 1, 7, 31),
        contracted_hours_per_week: 40,
        fte: 1,
        contract_type: 'Full-time',
        on_call_available: true,
        night_shift_available: true,
        annual_leave_days: 27,
        study_leave_days: 5,
      })
    }
  })

  return { doctors, contracts }
}

function createDefaultState() {
  const seed = buildDoctorSeed()
  return {
    doctors: seed.doctors,
    contracts: seed.contracts,
    schedules: new Map(),
    scheduleOrder: [],
    generatedScheduleCount: 0,
  }
}

function ensureDataDirectory() {
  if (!fs.existsSync(DATA_DIRECTORY)) {
    fs.mkdirSync(DATA_DIRECTORY, { recursive: true })
  }
}

function loadState() {
  if (!fs.existsSync(STORE_PATH)) {
    return createDefaultState()
  }

  try {
    const raw = fs.readFileSync(STORE_PATH, 'utf8')
    const parsed = JSON.parse(raw)

    return {
      doctors: Array.isArray(parsed.doctors) ? parsed.doctors : [],
      contracts: Array.isArray(parsed.contracts) ? parsed.contracts : [],
      schedules: new Map(Array.isArray(parsed.schedules) ? parsed.schedules : []),
      scheduleOrder: Array.isArray(parsed.scheduleOrder) ? parsed.scheduleOrder : [],
      generatedScheduleCount: Number(parsed.generatedScheduleCount) || 0,
    }
  } catch (error) {
    console.error(`Unable to load persisted store at ${STORE_PATH}: ${error.message}`)
    return createDefaultState()
  }
}

function saveState() {
  ensureDataDirectory()

  const serializable = {
    doctors: state.doctors,
    contracts: state.contracts,
    schedules: Array.from(state.schedules.entries()),
    scheduleOrder: state.scheduleOrder,
    generatedScheduleCount: state.generatedScheduleCount,
  }

  const temporaryPath = `${STORE_PATH}.tmp`
  fs.writeFileSync(temporaryPath, JSON.stringify(serializable, null, 2), 'utf8')
  fs.renameSync(temporaryPath, STORE_PATH)
}

const state = loadState()

function hasPersistedState() {
  return fs.existsSync(STORE_PATH)
}

function doctorsForSchedule(selectedSites) {
  if (!selectedSites || selectedSites.length === 0) {
    return state.doctors.slice()
  }

  const selected = new Set(selectedSites)
  return state.doctors.filter((doctor) => selected.has(doctor.hospital_site))
}

function buildHospitalBreakdown(doctors, assignments) {
  const breakdown = {}

  doctors.forEach((doctor) => {
    const site = doctor.hospital_site || 'Unknown'
    if (!breakdown[site]) {
      breakdown[site] = { doctor_count: 0, assignment_count: 0 }
    }
    breakdown[site].doctor_count += 1
  })

  assignments.forEach((assignment) => {
    const site = assignment.hospital_site || 'Unknown'
    if (!breakdown[site]) {
      breakdown[site] = { doctor_count: 0, assignment_count: 0 }
    }
    breakdown[site].assignment_count += 1
  })

  return breakdown
}

function buildComplianceReport(schedule) {
  const hospitalSummary = {}

  schedule.doctors.forEach((doctor) => {
    const site = doctor.hospital_site || 'Unknown'
    if (!hospitalSummary[site]) {
      hospitalSummary[site] = { doctor_count: 0, errors: 0, warnings: 0, violations: 0 }
    }
    hospitalSummary[site].doctor_count += 1
  })

  schedule.compliance.violations.forEach((violation) => {
    const site = violation.hospital_site || 'Unknown'
    if (!hospitalSummary[site]) {
      hospitalSummary[site] = { doctor_count: 0, errors: 0, warnings: 0, violations: 0 }
    }
    hospitalSummary[site].violations += 1
    if (violation.severity === 'ERROR') {
      hospitalSummary[site].errors += 1
    } else {
      hospitalSummary[site].warnings += 1
    }
  })

  const errorCount = schedule.compliance.violations.filter((item) => item.severity === 'ERROR').length
  const warningCount = schedule.compliance.violations.filter((item) => item.severity === 'WARNING').length

  return {
    schedule_id: schedule.id,
    generated_datetime: schedule.generated_at,
    summary: {
      total_checks: schedule.metrics.total_doctors,
      passed: Math.max(0, schedule.metrics.total_doctors - errorCount),
      failed: errorCount,
      warnings: warningCount,
      compliance_percentage: schedule.metrics.compliance_score,
      hospital_breakdown: hospitalSummary,
    },
    violations: schedule.compliance.violations,
  }
}

function buildFairnessReport(schedule) {
  return {
    schedule_id: schedule.id,
    overall_score: schedule.metrics.fairness_score,
    grade_breakdown: schedule.fairness.grade_breakdown,
    site_breakdown: schedule.fairness.site_breakdown,
    metrics: schedule.fairness.metrics,
    outliers: schedule.fairness.outliers,
  }
}

function buildScheduleResponse(schedule) {
  return {
    id: schedule.id,
    year: schedule.year,
    generated_at: schedule.generated_at,
    status: schedule.status,
    metrics: schedule.metrics,
    hospital_breakdown: schedule.hospital_breakdown,
    total_assignments: schedule.assignments.length,
  }
}

function buildDashboardSummary() {
  const doctorCountsBySite = computeDoctorCountsBySite(state.doctors)
  const latestScheduleId = state.scheduleOrder[0]
  const latestSchedule = latestScheduleId ? state.schedules.get(latestScheduleId) : null

  return {
    doctor_count: state.doctors.length,
    doctor_counts_by_site: doctorCountsBySite,
    generated_schedule_count: state.generatedScheduleCount,
    latest_schedule: latestSchedule
      ? {
          id: latestSchedule.id,
          year: latestSchedule.year,
          generated_at: latestSchedule.generated_at,
          status: latestSchedule.status,
          selected_hospital_sites: latestSchedule.selected_hospital_sites,
          metrics: {
            ...latestSchedule.metrics,
            total_assignments: latestSchedule.assignments.length,
          },
          hospital_breakdown: latestSchedule.hospital_breakdown,
        }
      : null,
  }
}

function createSchedule({ year, selectedSites = null, fixedId = null, generatedAt = null }) {
  const effectiveSites = selectedSites && selectedSites.length > 0 ? selectedSites : HOSPITAL_SITES.slice()
  const doctors = doctorsForSchedule(effectiveSites)

  if (doctors.length === 0) {
    throw new Error('At least one doctor must be imported before generating a schedule')
  }

  const assignments = []
  const siteGroups = effectiveSites.map((site) => ({
    site,
    doctors: doctors.filter((doctor) => doctor.hospital_site === site),
  }))

  siteGroups.forEach((group) => {
    for (let day = 1; day <= 28; day += 1) {
      SHIFT_TYPES.forEach((shiftType, shiftIndex) => {
        const doctor = group.doctors[(day + shiftIndex - 1) % group.doctors.length]
        assignments.push({
          id: createId('assign'),
          doctor_id: doctor.id,
          doctor_name: `${doctor.first_name} ${doctor.last_name}`,
          hospital_site: group.site,
          assignment_date: formatIsoDate(year, 8, day),
          shift_type_id: shiftType,
          status: 'ASSIGNED',
        })
      })
    }
  })

  const complianceViolations = doctors.slice(0, Math.min(8, doctors.length)).map((doctor, index) => ({
    doctor_id: doctor.id,
    doctor_name: `${doctor.first_name} ${doctor.last_name}`,
    hospital_site: doctor.hospital_site,
    violation_type: index % 2 === 0 ? 'INSUFFICIENT_REST' : 'EXCESS_HOURS',
    severity: index % 3 === 0 ? 'ERROR' : 'WARNING',
    description:
      index % 2 === 0
        ? `Only ${10 + index}.0 hours rest before next shift`
        : `Week exceeded contracted hours by ${index + 1} hours`,
    suggested_fix: 'Review the next available same-site swap option.',
  }))

  const gradeBreakdown = doctors.reduce((acc, doctor) => {
    acc[doctor.grade] = (acc[doctor.grade] || 0) + 1
    return acc
  }, {})

  const siteBreakdown = effectiveSites.reduce((acc, site, index) => {
    const siteDoctors = doctors.filter((doctor) => doctor.hospital_site === site)
    acc[site] = {
      doctor_count: siteDoctors.length,
      metrics: {
        NIGHT_SHIFTS: {
          target_mean: 14.5 + index,
          actual_mean: 14.1 + index,
          std_dev: 1.8,
          acceptable: true,
        },
        WEEKENDS: {
          target_mean: 10.8 + index,
          actual_mean: 10.5 + index,
          std_dev: 1.2,
          acceptable: true,
        },
      },
    }
    return acc
  }, {})

  const fairnessMetrics = {
    NIGHT_SHIFTS: {
      target_mean: 15.0,
      actual_mean: 14.6,
      std_dev: 2.0,
      acceptable: true,
    },
    WEEKENDS: {
      target_mean: 11.0,
      actual_mean: 10.7,
      std_dev: 1.4,
      acceptable: true,
    },
    ONCALLS: {
      target_mean: 4.0,
      actual_mean: 3.8,
      std_dev: 0.9,
      acceptable: true,
    },
  }

  const fairnessOutliers = doctors.slice(0, Math.min(4, doctors.length)).map((doctor, index) => ({
    doctor_id: doctor.id,
    doctor_name: `${doctor.first_name} ${doctor.last_name}`,
    hospital_site: doctor.hospital_site,
    metric: 'NIGHT_SHIFTS',
    value: 18 + index,
    target: 15,
    deviation: `+${3 + index}.0`,
  }))

  const complianceScore = Number((96 - complianceViolations.filter((item) => item.severity === 'ERROR').length * 0.3).toFixed(1))
  const fairnessScore = Number((91.5 - Math.max(0, effectiveSites.length - 1) * 0.2).toFixed(1))
  const hospitalBreakdown = buildHospitalBreakdown(doctors, assignments)

  return {
    id: fixedId || createId('schedule'),
    year,
    generated_at: generatedAt || new Date().toISOString(),
    status: 'complete',
    selected_hospital_sites: effectiveSites,
    doctors,
    assignments,
    hospital_breakdown: hospitalBreakdown,
    metrics: {
      total_doctors: doctors.length,
      compliance_score: complianceScore,
      fairness_score: fairnessScore,
      exception_count: complianceViolations.length,
    },
    compliance: {
      violations: complianceViolations,
    },
    fairness: {
      grade_breakdown: gradeBreakdown,
      site_breakdown: siteBreakdown,
      metrics: fairnessMetrics,
      outliers: fairnessOutliers,
    },
  }
}

function saveSchedule(schedule) {
  state.schedules.set(schedule.id, schedule)
  state.scheduleOrder = [schedule.id, ...state.scheduleOrder.filter((id) => id !== schedule.id)]
}

function initializeSchedules() {
  if (state.schedules.size === 0) {
    const baseline = createSchedule({
      year: 2026,
      selectedSites: HOSPITAL_SITES,
      fixedId: 'demo-schedule-2026',
      generatedAt: '2026-03-21T21:00:00.000Z',
    })

    saveSchedule(baseline)
    state.generatedScheduleCount = 12
    saveState()
    return
  }

  if (state.scheduleOrder.length === 0) {
    state.scheduleOrder = Array.from(state.schedules.keys()).reverse()
    saveState()
  }
}

initializeSchedules()

function findDoctor(doctorId) {
  return state.doctors.find((doctor) => doctor.id === doctorId || doctor.gmc_number === doctorId) || null
}

function listContractsForDoctor(doctorId) {
  const doctor = findDoctor(doctorId)
  if (!doctor) {
    return null
  }
  return state.contracts.filter((contract) => contract.doctor_id === doctor.id)
}

app.get('/', (request, response) => {
  response.json({
    message: 'Med Rota AI API running',
    health: '/health',
    dashboard_summary: '/api/v1/schedule/dashboard-summary',
  })
})

app.get('/health', (request, response) => {
  response.json({
    status: 'ok',
    service: 'med-rota-backend',
    doctors: state.doctors.length,
    schedules: state.generatedScheduleCount,
    persistence: hasPersistedState() ? 'disk' : 'seeded-memory',
  })
})

app.get('/api/v1/doctors/', (request, response) => {
  const { hospital_site: hospitalSite, skip = '0', limit = '100' } = request.query
  const start = Math.max(0, Number(skip) || 0)
  const max = Math.max(0, Number(limit) || 100)
  const filteredDoctors = hospitalSite
    ? state.doctors.filter((doctor) => doctor.hospital_site === hospitalSite)
    : state.doctors

  response.json(filteredDoctors.slice(start, start + max))
})

app.post('/api/v1/doctors/', (request, response) => {
  const payload = request.body || {}

  if (!payload.gmc_number) {
    response.status(400).json({ detail: 'gmc_number is required' })
    return
  }

  if (state.doctors.some((doctor) => doctor.gmc_number === payload.gmc_number)) {
    response.status(400).json({ detail: 'GMC number already exists' })
    return
  }

  const doctor = {
    id: createId('doc'),
    gmc_number: String(payload.gmc_number),
    first_name: payload.first_name || '',
    last_name: payload.last_name || '',
    email: payload.email || '',
    grade: payload.grade || 'FY1',
    specialty: payload.specialty || 'Medicine',
    hospital_site: payload.hospital_site || HOSPITAL_SITES[0],
  }

  state.doctors.push(doctor)
  saveState()
  response.status(201).json(doctor)
})

app.post('/api/v1/doctors/batch-import', (request, response) => {
  const payload = request.body || {}
  const doctors = Array.isArray(payload.doctors) ? payload.doctors : []
  const contracts = Array.isArray(payload.contracts) ? payload.contracts : []
  const importedDoctors = []
  const errors = []
  const doctorLookup = new Map()

  state.doctors.forEach((doctor) => {
    doctorLookup.set(doctor.id, doctor)
    doctorLookup.set(doctor.gmc_number, doctor)
  })

  doctors.forEach((doctorData) => {
    if (!doctorData.gmc_number) {
      errors.push({ type: 'doctor_error', message: 'Doctor GMC number is required' })
      return
    }

    if (doctorLookup.has(doctorData.gmc_number)) {
      errors.push({
        type: 'duplicate',
        gmc: doctorData.gmc_number,
        message: 'Doctor GMC already exists',
      })
      return
    }

    const doctor = {
      id: createId('doc'),
      gmc_number: String(doctorData.gmc_number),
      first_name: doctorData.first_name || '',
      last_name: doctorData.last_name || '',
      email: doctorData.email || '',
      grade: doctorData.grade || 'FY1',
      specialty: doctorData.specialty || 'Medicine',
      hospital_site: doctorData.hospital_site || HOSPITAL_SITES[0],
    }

    state.doctors.push(doctor)
    importedDoctors.push(doctor)
    doctorLookup.set(doctor.id, doctor)
    doctorLookup.set(doctor.gmc_number, doctor)
  })

  contracts.forEach((contractData) => {
    const doctor = doctorLookup.get(contractData.doctor_id)
    if (!doctor) {
      errors.push({
        type: 'contract_error',
        doctor_id: contractData.doctor_id,
        message: 'Doctor not found by ID or GMC number',
      })
      return
    }

    if (!contractData.start_date || !contractData.end_date || contractData.start_date >= contractData.end_date) {
      errors.push({
        type: 'contract_error',
        doctor_id: contractData.doctor_id,
        message: 'Start date must be before end date',
      })
      return
    }

    state.contracts.push({
      id: createId('contract'),
      doctor_id: doctor.id,
      start_date: contractData.start_date,
      end_date: contractData.end_date,
      contracted_hours_per_week: Number(contractData.contracted_hours_per_week) || 40,
      fte: Number(contractData.fte) || 1,
      contract_type: contractData.contract_type || 'Full-time',
      on_call_available: contractData.on_call_available !== false,
      night_shift_available: contractData.night_shift_available !== false,
      annual_leave_days: Number(contractData.annual_leave_days) || 27,
      study_leave_days: Number(contractData.study_leave_days) || 5,
    })
  })

  saveState()
  response.json({
    status: errors.length > 0 ? 'partial' : 'success',
    imported: importedDoctors.length,
    errors,
  })
})

app.get('/api/v1/doctors/:doctorId/contracts', (request, response) => {
  const contracts = listContractsForDoctor(request.params.doctorId)
  if (!contracts) {
    response.status(404).json({ detail: 'Doctor not found' })
    return
  }
  response.json(contracts)
})

app.post('/api/v1/doctors/:doctorId/contracts', (request, response) => {
  const doctor = findDoctor(request.params.doctorId)
  if (!doctor) {
    response.status(404).json({ detail: 'Doctor not found' })
    return
  }

  const payload = request.body || {}
  if (!payload.start_date || !payload.end_date || payload.start_date >= payload.end_date) {
    response.status(400).json({ detail: 'Start date must be before end date' })
    return
  }

  const contract = {
    id: createId('contract'),
    doctor_id: doctor.id,
    start_date: payload.start_date,
    end_date: payload.end_date,
    contracted_hours_per_week: Number(payload.contracted_hours_per_week) || 40,
    fte: Number(payload.fte) || 1,
    contract_type: payload.contract_type || 'Full-time',
    on_call_available: payload.on_call_available !== false,
    night_shift_available: payload.night_shift_available !== false,
    annual_leave_days: Number(payload.annual_leave_days) || 27,
    study_leave_days: Number(payload.study_leave_days) || 5,
  }

  state.contracts.push(contract)
  saveState()
  response.status(201).json(contract)
})

app.get('/api/v1/doctors/:doctorId', (request, response) => {
  const doctor = findDoctor(request.params.doctorId)
  if (!doctor) {
    response.status(404).json({ detail: 'Doctor not found' })
    return
  }
  response.json(doctor)
})

app.get('/api/v1/schedule/dashboard-summary', (request, response) => {
  response.json(buildDashboardSummary())
})

app.post('/api/v1/schedule/generate', (request, response) => {
  const payload = request.body || {}
  const year = Number(payload.year)

  if (!Number.isInteger(year) || year < 2000 || year > 2100) {
    response.status(400).json({ detail: 'Invalid year' })
    return
  }

  const selectedSites = Array.isArray(payload.hospital_sites) && payload.hospital_sites.length > 0 ? payload.hospital_sites : null
  if (selectedSites && selectedSites.some((site) => !HOSPITAL_SITES.includes(site))) {
    response.status(400).json({ detail: 'Invalid hospital sites' })
    return
  }

  try {
    const schedule = createSchedule({ year, selectedSites })
    saveSchedule(schedule)
    state.generatedScheduleCount += 1
    saveState()

    response.json({
      status: 'complete',
      poll_url: `/api/v1/schedule/${schedule.id}`,
    })
  } catch (error) {
    response.status(400).json({ detail: error.message || 'Schedule generation failed' })
  }
})

app.get('/api/v1/schedule/:scheduleId/compliance-report', (request, response) => {
  const schedule = state.schedules.get(request.params.scheduleId)
  if (!schedule) {
    response.status(404).json({ detail: 'Schedule not found' })
    return
  }
  response.json(buildComplianceReport(schedule))
})

app.get('/api/v1/schedule/:scheduleId/fairness-report', (request, response) => {
  const schedule = state.schedules.get(request.params.scheduleId)
  if (!schedule) {
    response.status(404).json({ detail: 'Schedule not found' })
    return
  }
  response.json(buildFairnessReport(schedule))
})

app.get('/api/v1/schedule/:scheduleId/assignments', (request, response) => {
  const schedule = state.schedules.get(request.params.scheduleId)
  if (!schedule) {
    response.status(404).json({ detail: 'Schedule not found' })
    return
  }

  const { hospital_site: hospitalSite, doctor_id: doctorId, date_from: dateFrom, date_to: dateTo } = request.query
  const assignments = schedule.assignments.filter((assignment) => {
    if (hospitalSite && assignment.hospital_site !== hospitalSite) {
      return false
    }
    if (doctorId && assignment.doctor_id !== doctorId) {
      return false
    }
    if (dateFrom && assignment.assignment_date < dateFrom) {
      return false
    }
    if (dateTo && assignment.assignment_date > dateTo) {
      return false
    }
    return true
  })

  response.json(assignments)
})

app.get('/api/v1/schedule/:scheduleId', (request, response) => {
  const schedule = state.schedules.get(request.params.scheduleId)
  if (!schedule) {
    response.status(404).json({ detail: 'Schedule not found' })
    return
  }
  response.json(buildScheduleResponse(schedule))
})

app.use((request, response) => {
  response.status(404).json({ detail: `Route not found: ${request.method} ${request.path}` })
})

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Med Rota AI backend listening on port ${PORT}`)
  console.log(`Doctor pool: ${state.doctors.length} across ${HOSPITAL_SITES.join(', ')}`)
})
