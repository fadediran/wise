@echo off
rem Installe le rappel de pause active pour l'utilisateur courant et le demarre.
rem Aucun droit administrateur requis.
rem Options facultatives, par exemple : installer_windows.bat --intervalle 90 --message "Bougez !"
setlocal
set "SCRIPT=%~dp0rappel_pause.py"
set "PYTHON="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON python -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>nul && set "PYTHON=python"
if not defined PYTHON goto sans_python
%PYTHON% "%SCRIPT%" --installer %*
set "CODE=%ERRORLEVEL%"
goto fin

:sans_python
echo Python 3.8 ou plus recent est introuvable.
echo Installez-le depuis https://www.python.org/downloads/ puis relancez ce fichier.
set "CODE=1"

:fin
echo.
pause
exit /b %CODE%
