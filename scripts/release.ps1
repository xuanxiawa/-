<#
  发布新版本：升版本号 -> 写 CHANGELOG -> 提交 -> 打 tag -> 推送

  用法：
    .\scripts\release.ps1                                修订号 +1    0.1.1 -> 0.1.2
    .\scripts\release.ps1 minor                          次版本 +1    0.1.2 -> 0.2.0
    .\scripts\release.ps1 major                          主版本 +1    0.2.0 -> 1.0.0
    .\scripts\release.ps1 -Note "修复xx" -Section 修复    一句话说明 + 分类
    .\scripts\release.ps1 minor -Label beta.1            预发布       0.2.0-beta.1
    .\scripts\release.ps1 -Stable                        转正式       0.2.0-beta.1 -> 0.2.0
    .\scripts\release.ps1 -NoPush                        只打本地 tag

  主版本位只在不兼容变更时才动（配置格式、数据结构、插件接口变了，老用户升级会坏）。
  普通重构、加功能、修 bug 都不要碰它。
#>
param(
    [ValidateSet('patch', 'minor', 'major')]
    [string]$Part = 'patch',

    [string]$Note,

    [ValidateSet('新增', '变更', '修复', '移除', '安全')]
    [string]$Section = '变更',

    # 预发布标识，例如 beta.1 / alpha.2 / rc.1
    [string]$Label,

    # 把当前预发布版转成正式版，不改数字
    [switch]$Stable,

    [switch]$NoPush,

    # 允许带着未提交的改动发版（默认不允许，免得把别的改动一起提交进去）
    [switch]$AllowDirty
)

$ErrorActionPreference = 'Stop'

# 控制台统一 UTF-8，避免中文提交信息变乱码
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding  = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 便携版 git（没装进系统 PATH），找不到再退回 PATH
$git = 'D:\tools\git\cmd\git.exe'
if (-not (Test-Path $git)) { $git = 'git' }
if ($git -eq 'git' -and -not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "找不到 git。便携版应该在 D:\tools\git\cmd\git.exe"
}

& $git rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) { throw "这里不是 git 仓库：$root" }

# 提交身份没配好会让后面的 commit 白跑一趟，先查掉
if (-not (& $git config user.name)) {
    throw "还没配置提交身份，先执行：git config user.name 你的名字; git config user.email 你的邮箱"
}

# 工作区必须干净，否则发版提交里会混进无关改动
$dirty = @(& $git status --porcelain)
if ($dirty.Count -gt 0 -and -not $AllowDirty) {
    Write-Host "工作区还有未提交的改动：" -ForegroundColor Yellow
    $dirty | ForEach-Object { Write-Host "  $_" }
    throw "先提交或撤销这些改动；确实要一起发版就加 -AllowDirty"
}

$current = (Get-Content "$root\VERSION" -Raw).Trim()
if ($current -notmatch '^(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z][0-9A-Za-z.-]*)?$') {
    throw "VERSION 内容不是合法的语义化版本：$current"
}
$base = ($current -split '-')[0]
$major, $minor, $patch = $base.Split('.') | ForEach-Object { [int]$_ }

if ($Stable -and $Label) { throw "-Stable 和 -Label 不能同时用" }

if ($Stable) {
    if ($current -notmatch '-') { throw "当前版本 $current 不是预发布版，-Stable 用不上" }
    $new = $base
}
else {
    switch ($Part) {
        'major' { $major++; $minor = 0; $patch = 0 }
        'minor' { $minor++; $patch = 0 }
        default { $patch++ }
    }
    $new = "$major.$minor.$patch"
    if (-not [string]::IsNullOrWhiteSpace($Label)) {
        $new = "$new-" + $Label.TrimStart('-')
    }
}

$tag = "v$new"
if (& $git tag -l $tag) { throw "tag $tag 已经存在了" }

$date = Get-Date -Format 'yyyy-MM-dd'

Write-Host "当前版本: $current" -ForegroundColor Cyan
Write-Host "新版本  : $new"     -ForegroundColor Green
if ($new -match '-') {
    Write-Host "这是预发布版，发布页记得勾 Pre-release" -ForegroundColor DarkYellow
}

if ([string]::IsNullOrWhiteSpace($Note)) {
    $Note = Read-Host "这次改了什么（一句话，写进 CHANGELOG）"
}
if ([string]::IsNullOrWhiteSpace($Note)) { $Note = '例行更新' }

# 1) VERSION
Set-Content "$root\VERSION" "$new" -NoNewline -Encoding ASCII

# 2) CHANGELOG：插在最新一条记录前面
$cl = Get-Content "$root\CHANGELOG.md" -Raw -Encoding UTF8
$entry = "## [$new] - $date`r`n`r`n### $Section`r`n- $Note`r`n`r`n"
$pos = $cl.IndexOf('## [')
if ($pos -ge 0) {
    $cl = $cl.Substring(0, $pos) + $entry + $cl.Substring($pos)
}
else {
    $cl = $cl.TrimEnd() + "`r`n`r`n" + $entry
}
[System.IO.File]::WriteAllText("$root\CHANGELOG.md", $cl, (New-Object System.Text.UTF8Encoding($false)))

# 3) 提交 + 打标注 tag
& $git add -A
& $git commit -m "release: $tag - $Note" | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "提交失败，发版中止（VERSION / CHANGELOG 已经改了，看完报错再重跑一次即可）"
}
& $git tag -a $tag -m "$tag $Note"
if ($LASTEXITCODE -ne 0) {
    throw "打 tag 失败，发版中止（提交已经完成，补一句 git tag -a $tag -m `"$tag $Note`"）"
}

# 4) 推送（配了 origin 才推）
$remotes = @(& $git remote)
if (-not $NoPush -and $remotes -contains 'origin') {
    Write-Host "`n推送到 origin ..." -ForegroundColor Cyan
    & $git push origin HEAD | Out-Host
    & $git push origin $tag | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Host "推送失败（网络或登录问题）。本地 tag 已经打好，之后可以手动补推：" -ForegroundColor Yellow
        Write-Host "  git push origin HEAD; git push origin $tag" -ForegroundColor Yellow
    }
}
elseif (-not $NoPush) {
    Write-Host "`n还没有配置远端，这次只发到本地。" -ForegroundColor Yellow
    Write-Host "  配好远端后补推：git push -u origin main; git push --tags" -ForegroundColor Yellow
}

& $git --no-pager log --oneline -5 | Out-Host
& $git tag --sort=-v:refname | Select-Object -First 5 | Out-Host

Write-Host "`n本地已发布 $tag" -ForegroundColor Green
Write-Host "接着去托管平台的 Releases 页面新建 Release：选 tag $tag、贴更新说明、上传构建产物。" -ForegroundColor Green
if ($new -match '-') {
    Write-Host "预发布版本记得勾选 Pre-release。" -ForegroundColor DarkYellow
}
