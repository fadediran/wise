@echo off
rem Arrete le rappel de pause active et supprime son installation (utilisateur courant).
rem Aucun droit administrateur requis.
setlocal
set "SCRIPT=%~dp0rappel_pause.py"
set "PYTHON="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON python -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>nul && set "PYTHON=python"
if not defined PYTHON goto sans_python
%PYTHON% "%SCRIPT%" --desinstaller %*
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
