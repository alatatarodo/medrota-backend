param(
  [Parameter(Mandatory = $true)]
  [string]$RenderApiKey,

  [Parameter(Mandatory = $true)]
  [string]$DatabaseUrl,

  [string]$ServiceId = "srv-d7053bdm5p6s73amk5f0",
  [string]$FrontendUrl = "https://frontend-cyan-beta-65.vercel.app",
  [string]$DataDir = "",
  [string]$AutoSeedSampleData = "false"
)

$env:RENDER_API_KEY = $RenderApiKey
$env:DATABASE_URL = $DatabaseUrl
$env:AUTO_SEED_SAMPLE_DATA = $AutoSeedSampleData

if ($DataDir) {
  $env:DATA_DIR = $DataDir
}

node scripts/render-cutover.mjs --service-id $ServiceId --frontend-url $FrontendUrl
