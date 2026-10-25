@echo off
chcp 65001 >nul
echo 正在卸载 Visual Studio Build Tools，请在弹出的 UAC 窗口点「是」...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\setup.exe' -ArgumentList 'uninstall','--installPath','C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools','--quiet','--norestart' -Verb RunAs -Wait"
echo.
echo 完成。按任意键关闭。
pause >nul
