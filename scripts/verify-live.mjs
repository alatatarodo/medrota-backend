import process from 'node:process'

const frontendUrl = process.env.FRONTEND_URL || 'https://frontend-cyan-beta-65.vercel.app'
const backendUrl = process.env.BACKEND_URL || 'https://medrota-backend.onrender.com'

async function fetchJson(url) {
  const response = await fetch(url, {
    headers: {
      Accept: 'application/json',
    },
  })

  if (!response.ok) {
    throw new Error(`${url} returned ${response.status}`)
  }

  return response.json()
}

async function fetchText(url) {
  const response = await fetch(url)
  if (!response.ok) {
    throw new Error(`${url} returned ${response.status}`)
  }

  return response.text()
}

async function main() {
  const health = await fetchJson(`${backendUrl}/health`)
  const summary = await fetchJson(`${backendUrl}/api/v1/schedule/dashboard-summary`)
  const homepage = await fetchText(frontendUrl)

  if (health.status !== 'ok') {
    throw new Error(`Unexpected backend health payload: ${JSON.stringify(health)}`)
  }

  if (typeof summary.doctor_count !== 'number' || summary.doctor_count < 1500) {
    throw new Error(`Unexpected doctor_count in dashboard summary: ${JSON.stringify(summary)}`)
  }

  if (!homepage.includes('<!doctype html') && !homepage.includes('<!DOCTYPE html')) {
    throw new Error('Frontend response did not look like HTML')
  }

  console.log(
    JSON.stringify(
      {
        backendUrl,
        frontendUrl,
        health,
        doctorCount: summary.doctor_count,
        sites: summary.doctor_counts_by_site,
      },
      null,
      2
    )
  )
}

main().catch((error) => {
  console.error(error.message)
  process.exit(1)
})
