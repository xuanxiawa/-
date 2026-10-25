$log = 'D:\大肥鱼\插件\uninstall-vs.log'
"=== $(Get-Date -Format 'HH:mm:ss') uninstall VS Build Tools ===" | Out-File $log -Encoding UTF8
$vsInstaller = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\setup.exe'
if (Test-Path $vsInstaller) {
    & $vsInstaller uninstall --installPath "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools" --quiet --norestart 2>&1 | Out-File $log -Append -Encoding UTF8
    "installer exit: $LASTEXITCODE" | Out-File $log -Append -Encoding UTF8
} else {
    "setup.exe not found, trying winget" | Out-File $log -Append -Encoding UTF8
    & winget uninstall --id Microsoft.VisualStudio.2022.BuildTools --silent --disable-interactivity 2>&1 | Out-File $log -Append -Encoding UTF8
    "winget exit: $LASTEXITCODE" | Out-File $log -Append -Encoding UTF8
}
"=== $(Get-Date -Format 'HH:mm:ss') done ===" | Out-File $log -Append -Encoding UTF8
