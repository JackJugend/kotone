@echo off
setlocal
cd /d "%~dp0"
for %%D in ("1. profile" "2. album" "3. artist" "0. GOTOWE CSV") do if not exist "%%~D" mkdir "%%~D"
py aoty_local_export.py
if errorlevel 1 (
  echo.
  echo Eksporter nie zostal uruchomiony. Jezeli brakuje biblioteki, wpisz:
  echo py -m pip install -r requirements.txt
)
echo.
pause
