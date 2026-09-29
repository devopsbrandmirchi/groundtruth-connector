# Prepare ground-truth-connector for a fresh GitHub remote.
# Usage: .\scripts\setup-new-repo.ps1 -GitHubUser "your-username" -RepoName "ground-truth-connector"

param(
    [Parameter(Mandatory = $true)]
    [string]$GitHubUser,

    [string]$RepoName = "ground-truth-connector"
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$remoteUrl = "https://github.com/$GitHubUser/$RepoName.git"

Write-Host "Ground Truth MCP Connector — new repo setup"
Write-Host "  Remote: $remoteUrl"
Write-Host ""

if (git remote get-url origin 2>$null) {
    Write-Host "Removing old origin..."
    git remote remove origin
}

git remote add origin $remoteUrl
Write-Host "Added origin: $remoteUrl"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Create empty repo at https://github.com/new?name=$RepoName"
Write-Host "  2. git credential-manager github login --force"
Write-Host "  3. git add ."
Write-Host "  4. git commit -m `"Initial commit: Ground Truth MCP connector`""
Write-Host "  5. git push -u origin main"
