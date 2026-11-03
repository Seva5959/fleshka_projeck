@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

if exist "python\python.exe" (
    "python\python.exe" server.py
    goto done
)

python --version >nul 2>nul
if %errorlevel%==0 (
    python server.py
    goto done
)

py -3 --version >nul 2>nul
if %errorlevel%==0 (
    py -3 server.py
    goto done
)

echo.
echo Python ne nayden. Ustanovite Python s python.org
echo (galochka "Add python.exe to PATH") ili polozhite portativnyy Python v papku python\
echo.

:done
echo.
echo Server ostanovlen. Esli vyshla oshibka - sdelayte skrinshot etogo okna.
pause
