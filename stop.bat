@echo off
chcp 65001 >nul
echo Ostanavlivayu server (port 5000-5010)...
for %%P in (5000 5001 5002 5003 5004 5005 5006 5007 5008 5009 5010) do (
  for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%P " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
  )
)
echo Gotovo. Teper zapusti run.bat dlya novogo servera.
pause
