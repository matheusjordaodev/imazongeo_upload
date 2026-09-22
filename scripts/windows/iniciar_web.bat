@echo off
REM Inicia a interface web local em http://127.0.0.1:5000
REM Requer as dependencias: python -m pip install -e ".[web]" (na raiz)
cd /d "%~dp0..\.."
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
python -m imazongeo_upload.web
pause
