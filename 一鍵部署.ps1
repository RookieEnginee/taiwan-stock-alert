# ============================================================
#  台股注意股・處置股查詢系統 — 一鍵部署腳本
#  自動完成：安裝工具 → GitHub 登入 → 建立 Repo → 推送 → 開啟 Render
# ============================================================

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Write-Step($msg) {
    Write-Host "`n  ➤  $msg" -ForegroundColor Cyan
}
function Write-OK($msg) {
    Write-Host "  ✅  $msg" -ForegroundColor Green
}
function Write-Warn($msg) {
    Write-Host "  ⚠️   $msg" -ForegroundColor Yellow
}
function Write-Fail($msg) {
    Write-Host "  ❌  $msg" -ForegroundColor Red
}

Clear-Host
Write-Host ""
Write-Host "  ╔══════════════════════════════════════════════════╗" -ForegroundColor Magenta
Write-Host "  ║   台股注意股・處置股查詢系統  一鍵部署          ║" -ForegroundColor Magenta
Write-Host "  ╚══════════════════════════════════════════════════╝" -ForegroundColor Magenta
Write-Host ""

# 切換到腳本所在目錄
Set-Location $PSScriptRoot

# ── STEP 1：檢查 / 安裝 Git ───────────────────────────────
Write-Step "檢查 Git..."
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Warn "找不到 Git，正在用 winget 安裝..."
    winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
    # 重新整理 PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                [System.Environment]::GetEnvironmentVariable('Path','User')
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Fail "Git 安裝後仍無法找到，請重新開啟 PowerShell 再執行此腳本。"
        Read-Host "按 Enter 關閉"
        exit 1
    }
}
Write-OK "Git 已就緒：$(git --version)"

# ── STEP 2：檢查 / 安裝 GitHub CLI (gh) ──────────────────
Write-Step "檢查 GitHub CLI..."
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Warn "找不到 GitHub CLI，正在用 winget 安裝..."
    winget install --id GitHub.cli -e --source winget --accept-package-agreements --accept-source-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                [System.Environment]::GetEnvironmentVariable('Path','User')
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Fail "GitHub CLI 安裝後仍無法找到，請重新開啟 PowerShell 再執行此腳本。"
        Read-Host "按 Enter 關閉"
        exit 1
    }
}
Write-OK "GitHub CLI 已就緒：$(gh --version | Select-Object -First 1)"

# ── STEP 3：GitHub 登入 ───────────────────────────────────
Write-Step "確認 GitHub 登入狀態..."
$authStatus = gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Warn "尚未登入 GitHub，即將開啟瀏覽器進行授權..."
    Write-Host "  （請在瀏覽器中完成 GitHub 登入，然後回到此視窗）" -ForegroundColor Gray
    gh auth login --web --git-protocol https
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "GitHub 登入失敗，請確認後重新執行。"
        Read-Host "按 Enter 關閉"
        exit 1
    }
}
Write-OK "GitHub 已登入"

# ── STEP 4：取得 GitHub 使用者名稱 ───────────────────────
$ghUser = gh api user --jq '.login' 2>$null
if (-not $ghUser) {
    Write-Fail "無法取得 GitHub 使用者名稱。"
    Read-Host "按 Enter 關閉"
    exit 1
}
Write-OK "登入帳號：$ghUser"

# ── STEP 5：決定 Repo 名稱 ────────────────────────────────
$repoName = "taiwan-stock-alert"
Write-Step "GitHub Repo 名稱：$repoName"

# ── STEP 6：初始化 Git Repo（若尚未初始化）────────────────
Write-Step "初始化本地 Git Repo..."
if (-not (Test-Path ".git")) {
    git init
    Write-OK "Git Repo 初始化完成"
} else {
    Write-OK "Git Repo 已存在，跳過初始化"
}

# 設定預設分支為 main
git checkout -b main 2>$null
if ($LASTEXITCODE -ne 0) {
    git checkout main 2>$null
}

# ── STEP 7：設定 Git 使用者（若未設定）────────────────────
$gitUser = git config --global user.name 2>$null
$gitEmail = git config --global user.email 2>$null
if (-not $gitUser) {
    git config --global user.name $ghUser
}
if (-not $gitEmail) {
    $ghEmail = gh api user --jq '.email' 2>$null
    if (-not $ghEmail -or $ghEmail -eq 'null') {
        $ghEmail = "$ghUser@users.noreply.github.com"
    }
    git config --global user.email $ghEmail
}

# ── STEP 8：Commit 所有檔案 ───────────────────────────────
Write-Step "加入並提交所有檔案..."
git add -A
$commitResult = git commit -m "初始部署：台股注意股處置股查詢系統" 2>&1
if ($LASTEXITCODE -ne 0 -and $commitResult -notmatch "nothing to commit") {
    # 可能是已有 commit，繼續即可
    Write-Warn "Commit 訊息：$commitResult"
} else {
    Write-OK "檔案已提交"
}

# ── STEP 9：建立 GitHub Repo 並推送 ──────────────────────
Write-Step "建立 GitHub 公開 Repo 並推送..."

# 先確認 Repo 是否已存在
$repoExists = gh repo view "$ghUser/$repoName" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Warn "Repo '$repoName' 已存在，直接推送..."
    git remote remove origin 2>$null
    git remote add origin "https://github.com/$ghUser/$repoName.git"
    git push -u origin main --force
} else {
    gh repo create $repoName --public --source=. --remote=origin --push
}

if ($LASTEXITCODE -ne 0) {
    Write-Fail "推送至 GitHub 失敗，請確認網路連線或手動執行：gh repo create $repoName --public --source=. --push"
    Read-Host "按 Enter 關閉"
    exit 1
}
Write-OK "成功推送至：https://github.com/$ghUser/$repoName"

# ── STEP 10：開啟 Render 部署頁面 ─────────────────────────
Write-Step "準備在 Render.com 部署..."
Write-Host ""
Write-Host "  ┌─────────────────────────────────────────────────┐" -ForegroundColor Yellow
Write-Host "  │  接下來請在瀏覽器中完成以下步驟（約 2 分鐘）：  │" -ForegroundColor Yellow
Write-Host "  │                                                 │" -ForegroundColor Yellow
Write-Host "  │  1. 用 GitHub 帳號登入 Render.com               │" -ForegroundColor Yellow
Write-Host "  │  2. 點選「New +」→「Web Service」              │" -ForegroundColor Yellow
Write-Host "  │  3. 選擇 Repo：$ghUser/$repoName" -ForegroundColor Yellow
Write-Host "  │  4. 其餘設定已由 render.yaml 自動填入           │" -ForegroundColor Yellow
Write-Host "  │  5. 點「Deploy Web Service」完成！              │" -ForegroundColor Yellow
Write-Host "  └─────────────────────────────────────────────────┘" -ForegroundColor Yellow
Write-Host ""

$renderUrl = "https://dashboard.render.com/select-repo?type=web"
Start-Process $renderUrl

Write-Host ""
Write-Host "  🎉  GitHub Repo 已建立：" -ForegroundColor Green
Write-Host "      https://github.com/$ghUser/$repoName" -ForegroundColor White
Write-Host ""
Write-Host "  🌐  Render 部署頁面已在瀏覽器開啟" -ForegroundColor Green
Write-Host "      完成 Render 設定後，你的網站將在約 1 分鐘後上線！" -ForegroundColor White
Write-Host ""

Read-Host "  按 Enter 關閉此視窗"
