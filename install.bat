@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo  Install playwright (one-time setup)
echo  The workbench will reuse your local Edge/Chrome,
echo  so no browser download is needed.
echo ============================================
echo.

python -m pip install playwright -i https://mirrors.aliyun.com/pypi/simple/ --timeout 60 --retries 5
if errorlevel 1 (
  echo.
  echo [FAILED] Try another mirror:
  echo   python -m pip install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple
  echo   or just:  python -m pip install playwright
  pause
  exit /b 1
)

echo.
echo [OK] playwright installed.
echo Now double-click start.bat to open the workbench.
pause
