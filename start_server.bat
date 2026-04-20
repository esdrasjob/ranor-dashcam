@echo off
echo ============================================
echo  RANOR Dashcam Server
echo  JT808:  porta 8080
echo  JT1078: porta 1078  (streaming de video)
echo  FTP:    porta 9999
echo  Dashboard: http://localhost:8888
echo ============================================
echo.
python main.py --jt808-port 8080 --jt1078-port 1078 --ftp-port 9999 --api-port 8888
pause
