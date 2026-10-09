@echo off
rem Battery Guard - Control Panel Launcher (source mode)
rem Double-click to start the web control panel once.
rem If the panel is already running, it just opens the existing page (no duplicate process).

setlocal
set SCRIPT_DIR=%~dp0
set PANEL=%SCRIPT_DIR%battery_guard_panel.py
set URL=http://127.0.0.1:8899

rem Pick an available Python (pythonw preferred: no console window)
set PYTHON=pythonw
where pythonw >nul 2>nul
if errorlevel 1 (
    set PYTHON=python
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python not found in PATH. Please install Python 3.8+ and try again.
        pause
        exit /b 1
    )
)

rem Already listening? just open the page
netstat -ano | findstr ":8899" | findstr "LISTENING" >nul 2>nul
if %errorlevel%==0 (
    echo Panel is already running. Opening existing panel...
    start "" "%URL%"
) else (
    echo Starting control panel...
    start "" "%PYTHON%" "%PANEL%" --open-browser
)

echo.
echo Control panel: %URL%
echo To stop, quit from the tray icon right-click menu.
echo.
timeout /t 3 >nul
