@echo off
echo ============================================
echo  RANOR Dashcam Server - Setup Windows
echo ============================================

echo.
echo [1/3] Verificando Python...
python --version 2>NUL
if errorlevel 1 (
    echo ERRO: Python nao encontrado!
    echo Baixe em: https://python.org/downloads
    echo Marque "Add Python to PATH" na instalacao
    pause
    exit /b 1
)

echo.
echo [2/3] Instalando dependencias...
pip install fastapi "uvicorn[standard]" aiofiles websockets pillow av

echo.
echo [3/3] Criando pastas de storage...
mkdir storage\snapshots 2>NUL
mkdir storage\videos 2>NUL
mkdir storage\events 2>NUL
mkdir logs 2>NUL

echo.
echo ============================================
echo  Setup concluido! 
echo  Execute: start_server.bat
echo ============================================
pause
