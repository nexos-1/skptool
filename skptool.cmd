@echo off
rem Startet skptool mit der Projekt-venv, egal aus welchem Ordner.
rem Kein leerer PYTHONPATH-Eintrag (der stuende fuer den aktuellen Ordner) und -P: so werden nie
rem Module aus dem aktuellen Ordner geladen, etwa eine praeparierte glob.py neben einem Modell.
setlocal
if defined PYTHONPATH (set "PYTHONPATH=%~dp0.;%PYTHONPATH%") else (set "PYTHONPATH=%~dp0.")
"%~dp0.venv\Scripts\python.exe" -P -m skptool %*
