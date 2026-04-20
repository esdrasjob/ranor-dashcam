FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn[standard] aiofiles websockets pillow

COPY *.py ./
RUN mkdir -p storage/snapshots storage/videos storage/events logs

# JT808 (808 in prod - needs cap or host network)
EXPOSE 808
# FTP for snapshot upload
EXPOSE 9999
# HTTP API + Dashboard
EXPOSE 8080

CMD ["python", "main.py", \
     "--jt808-port", "808", \
     "--ftp-port", "9999", \
     "--api-port", "8080"]
