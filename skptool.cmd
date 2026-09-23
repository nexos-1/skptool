@echo off
rem Startet skptool mit der Projekt-venv, egal aus welchem Ordner.
rem Kein leerer oder relativer PYTHONPATH-Eintrag (die stuenden fuer den aktuellen Ordner) und -P:
rem so werden nie Module aus dem aktuellen Ordner geladen, etwa eine praeparierte glob.py neben einem Modell.
setlocal DisableDelayedExpansion
set "SKPTOOL_PP=%~dp0."
set "SKPTOOL_REST=%PYTHONPATH%"
rem Geerbten PYTHONPATH Eintrag fuer Eintrag pruefen, nur absolute Pfade bleiben (C:\... oder \\server\...).
rem Ohne Klammerbloecke, damit Sonderzeichen wie & oder ! in Pfaden keinen Schaden anrichten.
:naechster
if not defined SKPTOOL_REST goto fertig
set "SKPTOOL_E="
set "SKPTOOL_R=%SKPTOOL_REST%"
set "SKPTOOL_REST="
for /f "eol=| tokens=1* delims=;" %%a in ("%SKPTOOL_R%") do set "SKPTOOL_E=%%a"& set "SKPTOOL_REST=%%b"
if not defined SKPTOOL_E goto fertig
if "%SKPTOOL_E:~1,2%"==":\" goto behalten
if "%SKPTOOL_E:~1,2%"==":/" goto behalten
if "%SKPTOOL_E:~0,2%"=="\\" goto behalten
goto naechster
:behalten
set "SKPTOOL_PP=%SKPTOOL_PP%;%SKPTOOL_E%"
goto naechster
:fertig
set "PYTHONPATH=%SKPTOOL_PP%"
set "SKPTOOL_PP="
set "SKPTOOL_R="
set "SKPTOOL_E="
"%~dp0.venv\Scripts\python.exe" -P -m skptool %*
