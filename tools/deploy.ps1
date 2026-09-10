# 重新部署插件到 AstrBot（上游更新后重打改动 / 本地改动后同步用）
#
# 用法： powershell -File deploy.ps1        （或 pwsh -File deploy.ps1）
#
# 说明：
#   - 源目录是本工作区里的插件副本（含全部本地改动）
#   - 目标目录是 AstrBot 实际加载的插件目录
#   - 部署前会把现场文件备份到 plugin_data（插件更新/重装不会清空该目录）
#   - 部署后调用 AstrBot 面板 API 热重载插件，不需要重启整个 AstrBot

param(
    [string]$Source = "E:\codex\plugincreat\rixiang\astrbot_plugin_portrayal",
    [string]$Live = "C:\Users\theater\.astrbot\data\plugins\astrbot_plugin_portrayal",
    [string]$PluginData = "C:\Users\theater\.astrbot\data\plugin_data\astrbot_plugin_portrayal",
    [string]$CmdConfig = "C:\Users\theater\.astrbot\data\cmd_config.json",
    [string]$AstrBotPython = "E:\codex\AstrBot\backend\python\python.exe",
    [string]$DashUrl = "http://127.0.0.1:6185",
    [string]$PluginName = "astrbot_plugin_portrayal",
    [switch]$SkipReload
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Source)) { throw "源目录不存在：$Source" }
if (-not (Test-Path $Live)) { throw "目标插件目录不存在：$Live" }

# 需要同步的文件（相对路径）
$files = @(
    "main.py", "plugin_api.py", "_conf_schema.json", "builtin_prompts.yaml",
    "CHANGELOG.md", "README.md", "metadata.yaml", "requirements.txt",
    "core\config.py", "core\entry.py", "core\llm.py", "core\message.py",
    "core\persona_service.py"
)

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backup = Join-Path $PluginData "backup_$stamp"
New-Item -ItemType Directory -Force -Path $backup | Out-Null

# 1) 备份现场
foreach ($f in $files) {
    $src = Join-Path $Live $f
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $backup $f
    New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
    Copy-Item $src $dst -Force
}
if (Test-Path (Join-Path $Live "pages")) {
    Copy-Item (Join-Path $Live "pages") $backup -Recurse -Force
}
Write-Host "[1/5] 已备份现场文件到 $backup"

# 2) 覆盖部署代码
foreach ($f in $files) {
    $src = Join-Path $Source $f
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $Live $f
    New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
    Copy-Item $src $dst -Force
}
# core 目录下所有 py（覆盖新增模块）
Get-ChildItem (Join-Path $Source "core") -File -Filter *.py | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $Live "core") -Force
}
# 面板静态资源
$pagesSrc = Join-Path $Source "pages"
$pagesDst = Join-Path $Live "pages"
if (Test-Path $pagesSrc) {
    if (Test-Path $pagesDst) { Remove-Item -Recurse -Force $pagesDst }
    Copy-Item $pagesSrc $pagesDst -Recurse -Force
}
Remove-Item -Recurse -Force (Join-Path $Live "__pycache__") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Live "core\__pycache__") -ErrorAction SilentlyContinue
Write-Host "[2/5] 已覆盖部署到 $Live"

# 3) 校验
$check = @("main.py", "plugin_api.py", "core\config.py", "core\llm.py", "core\message.py",
    "core\persona_service.py", "_conf_schema.json", "builtin_prompts.yaml",
    "metadata.yaml", "pages\dashboard\index.html", "pages\dashboard\app.js", "pages\dashboard\app.css")
$bad = 0
foreach ($f in $check) {
    $a = Join-Path $Source $f
    $b = Join-Path $Live $f
    if (-not (Test-Path $a)) { continue }
    if (-not (Test-Path $b)) { Write-Warning "缺少目标文件：$f"; $bad++; continue }
    if ((Get-FileHash $a).Hash -ne (Get-FileHash $b).Hash) { Write-Warning "校验不一致：$f"; $bad++ }
}
if ($bad -gt 0) { throw "有 $bad 个文件校验失败" }
Write-Host "[3/5] 校验通过"

# 4) 语法自检
& $AstrBotPython -m py_compile (Join-Path $Live "main.py") (Join-Path $Live "plugin_api.py") (Join-Path $Live "core\persona_service.py")
if ($LASTEXITCODE -ne 0) { throw "py_compile 未通过" }
Write-Host "[4/5] 语法自检通过"

# 5) 热重载（用本机 jwt_secret 自签面板 token，无需密码）
if ($SkipReload) { Write-Host "[5/5] 已跳过重载"; exit 0 }

$cfg = Get-Content $CmdConfig -Raw | ConvertFrom-Json
$secret = $cfg.dashboard.jwt_secret
$user = $cfg.dashboard.username
if (-not $secret) { throw "cmd_config.json 里没有 dashboard.jwt_secret，请手动在面板里重载插件" }

$script = @'
import datetime, json, sys, urllib.error, urllib.request
import jwt

secret, user, base, plugin = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
token = jwt.encode(
    {"username": user, "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)},
    secret,
    algorithm="HS256",
)
req = urllib.request.Request(
    base + "/api/plugin/reload",
    data=json.dumps({"name": plugin}).encode(),
    method="POST",
)
req.add_header("Content-Type", "application/json")
req.add_header("Authorization", "Bearer " + token)
try:
    with urllib.request.urlopen(req, timeout=90) as r:
        print("reload:", r.status)
except urllib.error.HTTPError as e:
    print("reload failed:", e.code, e.read().decode("utf-8", "replace"))
    raise SystemExit(1)
'@
$tmp = Join-Path $env:TEMP "astrbot_reload_$stamp.py"
Set-Content -Path $tmp -Value $script -Encoding UTF8
try {
    & $AstrBotPython $tmp $secret $user $DashUrl $PluginName
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}
Write-Host "[5/5] 已请求热重载，请查看 backend.log 确认加载成功"
