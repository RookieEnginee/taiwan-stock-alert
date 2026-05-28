@echo off
chcp 65001 >nul
title Deploy Taiwan Stock Alert

cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1"

if %errorlevel% neq 0 (
    echo.
    echo  Error code: %errorlevel%
    pause
)
