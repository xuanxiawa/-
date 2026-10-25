# 发布新版本：升版本号 -> 写 CHANGELOG -> 提交 -> 打 tag
# 用法：
#   .\release.ps1            修订号 +1（0.1.0 -> 0.1.1）
#   .\release.ps1 minor      次版本 +1（0.1.1 -> 0.2.0）
#   .\release.ps1 major      主版本 +1（0.2.0 -> 1.0.0）
param(
    [ValidateSet('patch', 'minor', 'major')]
    [string]$Part = 'patch'
)

$ErrorActionPreference = 'Stop'

# 控制台统一用 UTF-8，避免中文提交信息变乱码
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$git = 'D:\tools\git\cmd\git.exe'
if (-not (Test-Path $git)) { $git = 'git' }

$version = (Get-Content "$root\VERSION" -Raw).Trim()
$parts = $version.Split('.')
$major = [int]$parts[0]; $minor = [int]$parts[1]; $patch = [int]$parts[2]

switch ($Part) {
    'major' { $major++; $minor = 0; $patch = 0 }
    'minor' { $minor++; $patch = 0 }
    default { $patch++ }
}
$new = "$major.$minor.$patch"
$date = Get-Date -Format 'yyyy-MM-dd'

Write-Host "当前版本: $version" -ForegroundColor Cyan
Write-Host "新版本  : $new" -ForegroundColor Green

$desc = Read-Host "这次改了什么（一句话，写进 CHANGELOG）"
if ([string]::IsNullOrWhiteSpace($desc)) { $desc = '例行更新' }

# 更新 VERSION
Set-Content "$root\VERSION" "$new" -NoNewline -Encoding ASCII

# 更新 CHANGELOG
$cl = Get-Content "$root\CHANGELOG.md" -Raw -Encoding UTF8
$entry = "## [$new] - $date`r`n`r`n### 变更`r`n- $desc`r`n`r`n"
$marker = '## [0.1.0]'
if ($cl.Contains($marker)) {
    $cl = $cl.Replace($marker, $entry + $marker)
} else {
    $cl = $cl + "`r`n" + $entry
}
[System.IO.File]::WriteAllText("$root\CHANGELOG.md", $cl, (New-Object System.Text.UTF8Encoding($false)))

# 提交 + 打 tag
& $git add -A
& $git commit -m "release: v$new - $desc" | Out-Host
& $git tag -a "v$new" -m "v$new $desc"
& $git --no-pager log --oneline -5 | Out-Host
& $git tag --sort=-v:refname | Select-Object -First 5 | Out-Host

Write-Host "`n已发布 v$new（tag: v$new）" -ForegroundColor Green
