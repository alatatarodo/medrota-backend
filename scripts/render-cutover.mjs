import process from 'node:process'

const DEFAULT_SERVICE_ID = 'srv-d7053bdm5p6s73amk5f0'
const DEFAULT_FRONTEND_URL = 'https://frontend-cyan-beta-65.vercel.app'
const API_BASE = 'https://api.render.com/v1'

function parseArgs(argv) {
  const options = {
    serviceId: DEFAULT_SERVICE_ID,
    frontendUrl: DEFAULT_FRONTEND_URL,
    rootDir: undefined,
    dockerContext: '.',
    dockerfilePath: './Dockerfile',
    wait: true,
    timeoutSeconds: 900,
    clearCache: true,
  }

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]

    if (arg === '--service-id') {
      options.serviceId = argv[index + 1]
      index += 1
    } else if (arg === '--frontend-url') {
      options.frontendUrl = argv[index + 1]
      index += 1
    } else if (arg === '--root-dir') {
      options.rootDir = argv[index + 1]
      index += 1
    } else if (arg === '--docker-context') {
      options.dockerContext = argv[index + 1]
      index += 1
    } else if (arg === '--dockerfile-path') {
      options.dockerfilePath = argv[index + 1]
      index += 1
    } else if (arg === '--no-wait') {
      options.wait = false
    } else if (arg === '--no-clear-cache') {
      options.clearCache = false
    } else if (arg === '--timeout-seconds') {
      options.timeoutSeconds = Number(argv[index + 1]) || options.timeoutSeconds
      index += 1
    } else if (arg === '--help') {
      printUsage()
      process.exit(0)
    } else {
      throw new Error(`Unknown argument: ${arg}`)
    }
  }

  return options
}

function printUsage() {
  console.log(`Usage:
  RENDER_API_KEY=... node scripts/render-cutover.mjs [options]

Options:
  --service-id <id>           Render service ID. Default: ${DEFAULT_SERVICE_ID}
  --frontend-url <url>        Frontend URL to allow in ALLOWED_ORIGINS.
  --root-dir <path>           Optional repo root directory for the service.
  --docker-context <path>     Docker build context. Default: .
  --dockerfile-path <path>    Dockerfile path. Default: ./Dockerfile
  --timeout-seconds <n>       Deploy wait timeout. Default: 900
  --no-wait                   Do not poll deploy status after triggering.
  --no-clear-cache            Do not clear build cache on deploy.
  --help                      Show this message.

Environment:
  RENDER_API_KEY              Required Render API key.
  DATABASE_URL                Optional. If omitted, the FastAPI service falls back to SQLite.
  DATA_DIR                    Optional. Persistent data directory for SQLite or the Node compatibility layer.
`)
}

async function renderRequest(path, { method = 'GET', token, body } = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/json',
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  })

  if (!response.ok) {
    const text = await response.text()
    throw new Error(`${method} ${path} failed with ${response.status}: ${text}`)
  }

  if (response.status === 204) {
    return null
  }

  return response.json()
}

function sleep(milliseconds) {
  return new Promise((resolve) => {
    setTimeout(resolve, milliseconds)
  })
}

async function upsertEnvVar(serviceId, key, value, token) {
  const body = { value }
  await renderRequest(`/services/${serviceId}/env-vars/${encodeURIComponent(key)}`, {
    method: 'PUT',
    token,
    body,
  })
  console.log(`Updated env var ${key}`)
}

async function waitForDeploy(serviceId, deployId, token, timeoutSeconds) {
  const startTime = Date.now()

  while (true) {
    const deploy = await renderRequest(`/services/${serviceId}/deploys/${deployId}`, { token })
    console.log(`Deploy status: ${deploy.status}`)

    if (deploy.status === 'live') {
      return deploy
    }

    if (['build_failed', 'update_failed', 'canceled', 'pre_deploy_failed'].includes(deploy.status)) {
      throw new Error(`Deploy ${deployId} ended in status ${deploy.status}`)
    }

    if (Date.now() - startTime > timeoutSeconds * 1000) {
      throw new Error(`Timed out waiting for deploy ${deployId} after ${timeoutSeconds} seconds`)
    }

    await sleep(10000)
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2))
  const token = process.env.RENDER_API_KEY

  if (!token) {
    throw new Error('RENDER_API_KEY is required. Run with --help for usage.')
  }

  console.log(`Switching service ${options.serviceId} to Docker/FastAPI mode...`)

  const updateBody = {
    autoDeploy: 'yes',
    serviceDetails: {
      runtime: 'docker',
      healthCheckPath: '/health',
      envSpecificDetails: {
        dockerContext: options.dockerContext,
        dockerfilePath: options.dockerfilePath,
      },
    },
  }

  if (typeof options.rootDir === 'string') {
    updateBody.rootDir = options.rootDir
  }

  const updatedService = await renderRequest(`/services/${options.serviceId}`, {
    method: 'PATCH',
    token,
    body: updateBody,
  })

  console.log(`Service updated. Dashboard URL: ${updatedService.dashboardUrl || 'Unavailable'}`)

  await upsertEnvVar(options.serviceId, 'ALLOWED_ORIGINS', options.frontendUrl, token)
  await upsertEnvVar(options.serviceId, 'API_TITLE', 'Medical Rostering Automation API', token)
  await upsertEnvVar(options.serviceId, 'API_VERSION', '1.0.0', token)

  if (process.env.DATABASE_URL) {
    await upsertEnvVar(options.serviceId, 'DATABASE_URL', process.env.DATABASE_URL, token)
    const autoSeedValue = process.env.AUTO_SEED_SAMPLE_DATA ?? 'false'
    await upsertEnvVar(options.serviceId, 'AUTO_SEED_SAMPLE_DATA', autoSeedValue, token)
  } else {
    console.log('DATABASE_URL not provided. FastAPI will use the SQLite fallback at ./data/medrota.db.')
  }

  if (process.env.DATA_DIR) {
    await upsertEnvVar(options.serviceId, 'DATA_DIR', process.env.DATA_DIR, token)
  }

  const deploy = await renderRequest(`/services/${options.serviceId}/deploys`, {
    method: 'POST',
    token,
    body: {
      clearCache: options.clearCache ? 'clear' : 'do_not_clear',
    },
  })

  console.log(`Triggered deploy ${deploy.id}`)

  if (!options.wait) {
    return
  }

  const finalDeploy = await waitForDeploy(options.serviceId, deploy.id, token, options.timeoutSeconds)
  console.log(`Deploy ${finalDeploy.id} is live.`)
}

main().catch((error) => {
  console.error(error.message)
  process.exit(1)
})
