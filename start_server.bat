@echo off
echo ============================================
echo  RANOR Dashcam Server
echo ============================================
echo.
echo Abra o terminal do ngrok e procure a linha:
echo   tcp://0.tcp.sa.ngrok.io:XXXXX -^> localhost:1078
echo.
echo Digite APENAS o numero da porta (XXXXX):
set /p NGROK_1078="Porta ngrok do stream (1078): "
if "%NGROK_1078%"=="" set NGROK_1078=1078

echo.
echo Iniciando servidor...
echo   JT808  : porta 8080
echo   JT1078 : porta 1078 (publico: 0.tcp.sa.ngrok.io:%NGROK_1078%)
echo   FTP    : porta 9999
echo   Dashboard: http://localhost:8888
echo.

python main.py --jt808-port 8080 --jt1078-port 1078 --ftp-port 9999 --api-port 8888 --public-host 0.tcp.sa.ngrok.io --public-jt1078-port %NGROK_1078%

pause
