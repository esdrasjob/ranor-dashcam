@echo off
echo ============================================
echo  RANOR Dashcam — Simulador
echo ============================================
echo.
set /p HOST="Host do servidor (Enter = localhost): "
if "%HOST%"=="" set HOST=localhost

set /p PORT="Porta JT808 (Enter = 8080): "
if "%PORT%"=="" set PORT=8080

echo.
echo Conectando em %HOST%:%PORT%...
python simulator.py --host %HOST% --jt808-port %PORT% --ftp-port 9999
pause
