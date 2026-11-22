@echo off
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0DesktopAI.pyw" %*
    exit /b
)
where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3 "%~dp0DesktopAI.pyw" %*
    exit /b
)
for /d %%P in ("%LocalAppData%\Programs\Python\Python3*") do if exist "%%~fP\pythonw.exe" (
    start "" "%%~fP\pythonw.exe" "%~dp0DesktopAI.pyw" %*
    exit /b
)
echo Python 3 with Tk is required to start Desktop AI.
pause
