@echo off
cd /d "%~dp0"
if exist "C:\Python314\pythonw.exe" (
    start "" /d "%~dp0" "C:\Python314\pythonw.exe" "%~dp0desktop_app.py"
    goto :eof
)
where pythonw >nul 2>nul
if %errorlevel% equ 0 (
    start "" /d "%~dp0" pythonw "%~dp0desktop_app.py"
    goto :eof
)
start "" /d "%~dp0" python "%~dp0desktop_app.py"
goto :eof
