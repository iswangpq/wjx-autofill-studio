@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting WJX Studio...
echo Browser will open http://127.0.0.1:8765
echo Close this window to stop the server.
python wjx_studio.py
pause
