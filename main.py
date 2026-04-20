"""
RANOR Dashcam Server — Entry point
Sobe JT808 (TCP), FTP e API/Dashboard em paralelo

Uso:
    python main.py [--jt808-port 808] [--ftp-port 9999] [--api-port 8080]
"""

import asyncio
import argparse
import logging
import signal
import sys
import os
from pathlib import Path

import uvicorn

from event_bus import EventBus
from jt808_server import JT808Server
from ftp_server import FTPServer
from jt1078_handler import JT1078Server as JT1078StreamServer
from api_server import create_api

# ── Logging ────────────────────────────────────────────────────────────────────

# Garante pastas antes de configurar FileHandler
Path('logs').mkdir(parents=True, exist_ok=True)
for _d in ['storage/snapshots', 'storage/videos', 'storage/events']:
    Path(_d).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('logs/server.log', encoding='utf-8'),
    ]
)
logger = logging.getLogger("ranor.main")


# ── Banner ─────────────────────────────────────────────────────────────────────

BANNER = """
╔══════════════════════════════════════════════════════╗
║          RANOR — Dashcam Integration Server          ║
║   JT/T 808-2013 + JT/T 1078 + FTP + REST + WS       ║
╚══════════════════════════════════════════════════════╝
"""


async def main(args):
    print(BANNER)
    storage = Path('storage')

    # Core components
    event_bus   = EventBus(max_history=1000)
    jt808_srv   = JT808Server(storage, event_bus, host='0.0.0.0', port=args.jt808_port)
    ftp_srv     = FTPServer(storage, event_bus, host='0.0.0.0', port=args.ftp_port)
    jt1078_srv  = JT1078StreamServer(event_bus, storage, host='0.0.0.0', port=args.jt1078_port)
    api_app     = create_api(storage, event_bus, jt808_srv, jt1078_srv)

    # Start TCP servers
    await jt808_srv.start()
    await ftp_srv.start()
    await jt1078_srv.start()

    logger.info(f"✅ JT808  → tcp://0.0.0.0:{args.jt808_port}")
    logger.info(f"✅ FTP    → ftp://0.0.0.0:{args.ftp_port}")
    logger.info(f"✅ JT1078 → tcp://0.0.0.0:{args.jt1078_port}  (stream de vídeo)")
    logger.info(f"✅ API    → http://0.0.0.0:{args.api_port}")
    logger.info(f"📊 Dashboard → http://localhost:{args.api_port}")
    logger.info("Aguardando conexões da dashcam...")

    # Uvicorn config (runs in same event loop)
    config = uvicorn.Config(
        app=api_app,
        host='0.0.0.0',
        port=args.api_port,
        log_level='warning',
        loop='asyncio',
    )
    server = uvicorn.Server(config)

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()

    def _shutdown():
        logger.info("Shutting down...")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass  # Windows

    await server.serve()

    await jt808_srv.stop()
    await ftp_srv.stop()
    await jt1078_srv.stop()
    logger.info("Server stopped.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='RANOR Dashcam Server')
    parser.add_argument('--jt808-port', type=int, default=8080,   # Use 808 in production (needs root or setcap)
                        help='JT808 TCP port (default 8080, use 808 in prod)')
    parser.add_argument('--jt1078-port', type=int, default=1078,
                        help='JT1078 stream port (default 1078)')
    parser.add_argument('--ftp-port',   type=int, default=9999,
                        help='FTP port for snapshot upload (default 9999)')
    parser.add_argument('--api-port',   type=int, default=8888,
                        help='HTTP API + Dashboard port (default 8888)')
    args = parser.parse_args()

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
