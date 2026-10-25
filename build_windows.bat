@echo off
REM Construit dist\KacouAudio.exe (a lancer sur un PC Windows avec Python 3 installe)
cd /d "%~dp0"
if not exist .venv-build (
  py -3 -m venv .venv-build || python -m venv .venv-build
)
.venv-build\Scripts\python -m pip install --quiet --upgrade pip pyinstaller certifi
.venv-build\Scripts\pyinstaller --noconfirm --clean kacou_audio.spec
echo.
echo Termine : dist\KacouAudio.exe
pause
