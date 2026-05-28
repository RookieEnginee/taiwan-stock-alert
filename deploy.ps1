$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot

function Step($m)  { Write-Host "`n  >> $m" -ForegroundColor Cyan }
function OK($m)    { Write-Host "  OK  $m"  -ForegroundColor Green }
function Warn($m)  { Write-Host "  **  $m"  -ForegroundColor Yellow }
function Fail($m)  { Write-Host "  !!  $m"  -ForegroundColor Red }

Clear-Host
Write-Host ""
Write-Host "  =================================================" -ForegroundColor Magenta
Write-Host "   Taiwan Stock Alert System - Deploy to Cloud"     -ForegroundColor Magenta
Write-Host "  =================================================" -ForegroundColor Magenta
Write-Host ""

# STEP 1 - Check / install Git
Step "Checking Git..."
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Warn "Git not found. Installing via winget..."
    winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + `
                [System.Environment]::GetEnvironmentVariable('Path','User')
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Fail "Git still not found. Please reopen this window and try again."
        Read-Host "Press Enter to close"
        exit 1
    }
}
OK "Git ready: $(git --version)"

# STEP 2 - Check / install GitHub CLI
Step "Checking GitHub CLI..."
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Warn "GitHub CLI not found. Installing via winget..."
    winget install --id GitHub.cli -e --source winget --accept-package-agreements --accept-source-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + `
                [System.Environment]::GetEnvironmentVariable('Path','User')
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Fail "GitHub CLI still not found. Please reopen this window and try again."
        Read-Host "Press Enter to close"
        exit 1
    }
}
OK "GitHub CLI ready: $(gh --version | Select-Object -First 1)"

# STEP 3 - GitHub login
Step "Checking GitHub login..."
gh auth status *>$null
if ($LASTEXITCODE -ne 0) {
    Warn "Not logged in. Opening browser for GitHub OAuth..."
    gh auth login --web --git-protocol https
    if ($LASTEXITCODE -ne 0) {
        Fail "Login failed. Please try again."
        Read-Host "Press Enter to close"
        exit 1
    }
}
OK "GitHub login confirmed"

# STEP 4 - Get username
$ghUser = gh api user --jq '.login' 2>$null
if (-not $ghUser) {
    Fail "Cannot get GitHub username."
    Read-Host "Press Enter to close"
    exit 1
}
OK "Logged in as: $ghUser"

# STEP 5 - Repo name
$repoName = "taiwan-stock-alert"
Step "Repository name: $repoName"

# STEP 6 - Init git repo
Step "Initializing local git repo..."
if (-not (Test-Path ".git")) {
    git init
    OK "Initialized"
} else {
    OK "Already initialized"
}

git checkout -b main 2>$null
if ($LASTEXITCODE -ne 0) {
    git checkout main 2>$null
}

# STEP 7 - Set git user if not configured
$gitUser = git config --global user.name 2>$null
$gitEmail = git config --global user.email 2>$null
if (-not $gitUser)  { git config --global user.name $ghUser }
if (-not $gitEmail) {
    $ghEmail = gh api user --jq '.email' 2>$null
    if (-not $ghEmail -or $ghEmail -eq 'null') { $ghEmail = "$ghUser@users.noreply.github.com" }
    git config --global user.email $ghEmail
}

# STEP 8 - Commit all files
Step "Committing all files..."
git add -A
$cr = git commit -m "init: taiwan stock alert system" 2>&1
if ($LASTEXITCODE -ne 0 -and ("$cr" -notmatch "nothing to commit")) {
    Warn "Commit note: $cr"
} else {
    OK "Files committed"
}

# STEP 9 - Create repo and push
Step "Creating GitHub repo and pushing..."
gh repo view "$ghUser/$repoName" *>$null
if ($LASTEXITCODE -eq 0) {
    Warn "Repo '$repoName' already exists. Force pushing..."
    git remote remove origin 2>$null
    git remote add origin "https://github.com/$ghUser/$repoName.git"
    git push -u origin main --force
} else {
    gh repo create $repoName --public --source=. --remote=origin --push
}

if ($LASTEXITCODE -ne 0) {
    Fail "Push to GitHub failed. Check your connection and try again."
    Read-Host "Press Enter to close"
    exit 1
}
OK "Pushed to: https://github.com/$ghUser/$repoName"

# STEP 10 - Open Render
Step "Opening Render.com deploy page..."
Write-Host ""
Write-Host "  +--------------------------------------------------+" -ForegroundColor Yellow
Write-Host "  |  Complete these steps in your browser (2 min):  |" -ForegroundColor Yellow
Write-Host "  |                                                  |" -ForegroundColor Yellow
Write-Host "  |  1. Sign in to Render.com with GitHub           |" -ForegroundColor Yellow
Write-Host "  |  2. Click 'New +' -> 'Web Service'              |" -ForegroundColor Yellow
Write-Host "  |  3. Select repo: $ghUser/$repoName" -ForegroundColor Yellow
Write-Host "  |  4. Settings auto-filled by render.yaml         |" -ForegroundColor Yellow
Write-Host "  |  5. Click 'Deploy Web Service'                  |" -ForegroundColor Yellow
Write-Host "  +--------------------------------------------------+" -ForegroundColor Yellow
Write-Host ""

Start-Process "https://dashboard.render.com/select-repo?type=web"

Write-Host "  GitHub:  https://github.com/$ghUser/$repoName" -ForegroundColor Green
Write-Host "  Render deploy page opened in browser."           -ForegroundColor Green
Write-Host ""
Read-Host "  Press Enter to close"
