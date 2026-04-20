@echo off
echo ============================================
echo  Instalando PyAV (decodificador H264)
echo ============================================
echo.
echo Isso permite que o servidor decodifique o
echo video H264 da dashcam em tempo real.
echo.
pip install av
echo.
echo Se der erro, tente:
echo   pip install av --pre
echo.
pause
