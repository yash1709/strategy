# One-time: publish the local tracking state to the repository's `data` branch so the
# GitHub Actions job continues from it (see DEPLOY.md step 2). Safe to re-run; it replaces
# the data branch with the current local state.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$remote = (git remote get-url origin)
if (-not $remote) { throw "No 'origin' remote. Run: git remote add origin https://github.com/<you>/<repo>.git" }

$work = Join-Path $root "publish"
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Force "$work", "$root\cloud" | Out-Null

& "$root\.venv\Scripts\python.exe" -m nse_monitor.cloud_state pack --db data\nse_monitor.db `
    --state "$work\state.db" --prices "$root\cloud\prices.db" --meta "$work\meta.json"
if ($LASTEXITCODE -ne 0) { throw "pack failed" }
& "$root\.venv\Scripts\python.exe" -m nse_monitor export | Out-Null
Copy-Item -Recurse "$root\reports" "$work\reports"
"# data branch (generated - do not edit)`nSeeded from the local database; updated by the Daily NSE monitor workflow." |
    Set-Content -Encoding utf8 "$work\README.md"

Push-Location $work
git init -q -b data
git add -A
git commit -q -m "Seed data from local database"
git push --force $remote data
Pop-Location
Remove-Item -Recurse -Force $work
Write-Host "data branch published to $remote"
