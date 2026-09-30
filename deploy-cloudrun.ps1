# Deploy Ground Truth MCP connector to Google Cloud Run (mirrors meta-mcp-connector pattern).
#
# Usage:
#   .\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID"
#   .\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID" -Region "us-central1" -Service "ground-truth-mcp"
#
# Secrets (Secret Manager — never committed):
#   GROUND_TRUTH_API_KEY    → ground-truth-api-key
#   MCP_OAUTH_JWT_SECRET    → ground-truth-oauth-jwt-secret (generated once, never rotated)
#
# Non-secret env vars on Cloud Run:
#   GROUND_TRUTH_USER_ID, GROUND_TRUTH_ORG_ID, GROUND_TRUTH_ACCOUNT_ID, MCP_PUBLIC_URL
#
# Always-connected settings:
#   stateless MCP transport (in code) + signed OAuth tokens/clients + permanent JWT secret
#   + min-instances 1 + no CPU throttling + 60-minute request timeout.

param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [string]$Region = "us-central1",
    [string]$Service = "ground-truth-mcp",
    [string]$ApiKeySecretName = "ground-truth-api-key",
    [string]$JwtSecretName = "ground-truth-oauth-jwt-secret"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path -LiteralPath ".\Dockerfile")) {
    throw "Dockerfile not found in $PSScriptRoot"
}
if (-not (Test-Path -LiteralPath ".\ground_truth_mcp_server.py")) {
    throw "ground_truth_mcp_server.py not found in $PSScriptRoot"
}

function Get-EnvValue([string]$Key) {
    $envPath = Join-Path $PSScriptRoot ".env"
    if (-not (Test-Path -LiteralPath $envPath)) { return "" }
    foreach ($line in Get-Content -LiteralPath $envPath) {
        $t = $line.Trim()
        if ($t -match '^\s*#' -or $t -eq "") { continue }
        if ($t -match "^(?:export\s+)?$Key\s*=\s*(.*)$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

function Read-SecretValue([string]$Key, [string]$Prompt) {
    $fromEnv = Get-EnvValue $Key
    if ($fromEnv) { return $fromEnv }

    Write-Host "$Prompt (input hidden), then Enter:"
    $secure = Read-Host -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Test-SecretExists([string]$Name) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    gcloud secrets describe $Name --project $ProjectId *> $null
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prev
    return $ok
}

function Set-SecretValue([string]$Name, [string]$Value) {
    if (-not $Value) { throw "Secret value for $Name is empty." }
    $tmp = Join-Path $env:TEMP "ground-truth-secret-$([guid]::NewGuid().ToString('N')).txt"
    try {
        [IO.File]::WriteAllText($tmp, $Value)
        if (-not (Test-SecretExists $Name)) {
            Write-Host "Creating secret $Name ..."
            gcloud secrets create $Name --project $ProjectId --replication-policy=automatic --data-file=$tmp
        } else {
            Write-Host "Adding new version to secret $Name ..."
            gcloud secrets versions add $Name --project $ProjectId --data-file=$tmp
        }
    } finally {
        if (Test-Path -LiteralPath $tmp) {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "Using project $ProjectId / region $Region / service $Service"

gcloud config set project $ProjectId

gcloud services enable `
    run.googleapis.com `
    cloudbuild.googleapis.com `
    secretmanager.googleapis.com `
    artifactregistry.googleapis.com `
    --project $ProjectId

$userId = Get-EnvValue "GROUND_TRUTH_USER_ID"
$orgId = Get-EnvValue "GROUND_TRUTH_ORG_ID"
$accountId = Get-EnvValue "GROUND_TRUTH_ACCOUNT_ID"
if (-not $userId) { throw "GROUND_TRUTH_USER_ID missing in .env" }
if (-not $orgId) { Write-Host "WARNING: GROUND_TRUTH_ORG_ID missing — account/campaign listing and name lookup will be disabled." }

$apiKey = Read-SecretValue "GROUND_TRUTH_API_KEY" "Paste GROUND_TRUTH_API_KEY"
Set-SecretValue $ApiKeySecretName $apiKey

# The OAuth signing secret must stay the same forever: changing it logs Claude out.
if (-not (Test-SecretExists $JwtSecretName)) {
    $bytes = New-Object byte[] 48
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    Set-SecretValue $JwtSecretName ([Convert]::ToBase64String($bytes))
} else {
    Write-Host "Reusing existing OAuth signing secret $JwtSecretName (keeps Claude connected)."
}

$projectNumber = (gcloud projects describe $ProjectId --format="value(projectNumber)").Trim()
$runtimeSa = "$projectNumber-compute@developer.gserviceaccount.com"
foreach ($secret in @($ApiKeySecretName, $JwtSecretName)) {
    gcloud secrets add-iam-policy-binding $secret `
        --project $ProjectId `
        --member="serviceAccount:$runtimeSa" `
        --role="roles/secretmanager.secretAccessor" *> $null
}

# Cloud Run URLs are deterministic, so MCP_PUBLIC_URL can be set on the first deploy.
$publicUrl = "https://$Service-$projectNumber.$Region.run.app"

$envVars = @(
    "MCP_TRANSPORT=http",
    "HOST=0.0.0.0",
    "GROUND_TRUTH_USER_ID=$userId",
    "CLOUD_RUN_REGION=$Region",
    "CLOUD_RUN_PROJECT_NUMBER=$projectNumber",
    "MCP_PUBLIC_URL=$publicUrl"
)
if ($orgId) { $envVars += "GROUND_TRUTH_ORG_ID=$orgId" }
if ($accountId) { $envVars += "GROUND_TRUTH_ACCOUNT_ID=$accountId" }

$secretsArg = "GROUND_TRUTH_API_KEY=${ApiKeySecretName}:latest,MCP_OAUTH_JWT_SECRET=${JwtSecretName}:latest"

Write-Host "Building and deploying from source (Dockerfile)..."

gcloud run deploy $Service `
    --project $ProjectId `
    --region $Region `
    --source . `
    --set-env-vars ($envVars -join ",") `
    --set-secrets $secretsArg `
    --allow-unauthenticated `
    --session-affinity `
    --min-instances 1 `
    --max-instances 1 `
    --no-cpu-throttling `
    --cpu-boost `
    --timeout 3600 `
    --port 8080

$url = (gcloud run services describe $Service `
    --project $ProjectId `
    --region $Region `
    --format="value(status.url)").Trim()

if ($url -and $url -ne $publicUrl) {
    Write-Host "Service URL differs from expected; updating MCP_PUBLIC_URL=$url ..."
    gcloud run services update $Service `
        --project $ProjectId `
        --region $Region `
        --update-env-vars "MCP_PUBLIC_URL=$url"
    $publicUrl = $url
}

Write-Host ""
Write-Host "Deployed. MCP connector URL (use this exact path):"
Write-Host "  $publicUrl/mcp"
Write-Host ""
Write-Host "Verify:"
Write-Host "  $publicUrl/health"
Write-Host "  $publicUrl/.well-known/oauth-authorization-server"
Write-Host ""
Write-Host "Claude.ai:"
Write-Host "  Settings -> Connectors -> Add custom connector"
Write-Host "  Name: Ground Truth"
Write-Host "  URL:  $publicUrl/mcp"
Write-Host "  Leave Advanced OAuth Client ID empty (server supports DCR)."
