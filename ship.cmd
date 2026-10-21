@echo off
where py >nul 2>nul && (py "%~dp0tools\ship.py" %*) || (python "%~dp0tools\ship.py" %*)
