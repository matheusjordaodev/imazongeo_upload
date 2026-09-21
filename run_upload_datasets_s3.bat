
---

## 2) Script .BAT (Windows)

Salva como **`run_upload_datasets_s3.bat`** na mesma pasta do `upload_datasets_s3.py`:

```bat
@echo off
REM ============================================================
REM  run_upload_datasets_s3.bat
REM  Helper para rodar upload_datasets_s3.py no Windows
REM ============================================================

REM Nome do script Python
set SCRIPT=upload_datasets_s3.py

REM Nome do bucket S3 (ajuste se precisar)
set BUCKET=imazongeo3-web

REM Diretório base local dos dados (ajuste se precisar)
set BASE_DIR=dados

echo ================================================
echo  Upload de datasets para S3 - ImazonGeo / Bluebell
echo ================================================
echo.

echo Selecione o dataset:
echo   1 - sad
echo   2 - floreser
echo   3 - ameaca_pressao
echo   4 - simex
echo   5 - all (todos)
echo.

set DATASET=
set /p DATASET_OP=Opcao (1-5): 

if "%DATASET_OP%"=="1" set DATASET=sad
if "%DATASET_OP%"=="2" set DATASET=floreser
if "%DATASET_OP%"=="3" set DATASET=ameaca_pressao
if "%DATASET_OP%"=="4" set DATASET=simex
if "%DATASET_OP%"=="5" set DATASET=all

if "%DATASET%"=="" (
    echo.
    echo Opcao invalida. Encerrando.
    pause
    goto :EOF
)

echo.
set ANO=
set /p ANO=Informe o ano (ex: 2025, ENTER para ano atual): 

set MES=
set TRIM=

REM Para SAD (mensal), pedir MES
if "%DATASET%"=="sad" (
    echo.
    set /p MES=Informe o MES (1-12, ENTER para mes atual): 
)

REM Para Ameaça & Pressao (trimestral), pedir trimestre
if "%DATASET%"=="ameaca_pressao" (
    echo.
    set /p TRIM=Informe o TRIMESTRE (1-4, ENTER para trimestre atual): 
)

echo.
echo =========================================================
echo  Montando comando...
echo =========================================================

set CMD=python "%SCRIPT%" %DATASET% --bucket %BUCKET% --base-dir "%BASE_DIR%"

if not "%ANO%"=="" (
    set CMD=%CMD% --year %ANO%
)

if not "%MES%"=="" (
    set CMD=%CMD% --month %MES%
)

if not "%TRIM%"=="" (
    set CMD=%CMD% --quarter %TRIM%
)

REM Por padrao, o BAT roda com upload real e objetos publicos
set CMD=%CMD% --no-dry-run --public

echo.
echo Comando que sera executado:
echo   %CMD%
echo.
pause

%CMD%

echo.
echo =========================================================
echo  Execucao finalizada.
echo =========================================================
pause
