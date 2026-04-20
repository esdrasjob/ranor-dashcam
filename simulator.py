"""
RANOR — Simulador de Dashcam MDVR
Replica o comportamento real da dashcam dos logs analisados.

Uso:
    python simulator.py --host 0.tcp.ngrok.io --jt808-port 12345
"""

import asyncio
import struct
import random
import argparse
import logging
import sys
import io
import ftplib
import threading
from datetime import datetime
from PIL import Image, ImageDraw

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger("dashcam-sim")

# ── Protocol helpers ──────────────────────────────────────────────────────────

def escape(data: bytes) -> bytes:
    result = bytearray()
    for b in data:
        if b == 0x7D:   result.extend([0x7D, 0x01])
        elif b == 0x7E: result.extend([0x7D, 0x02])
        else:           result.append(b)
    return bytes(result)

def checksum(data: bytes) -> int:
    cs = 0
    for b in data: cs ^= b
    return cs

def unescape(data: bytes) -> bytes:
    result = bytearray()
    i = 0
    while i < len(data):
        if data[i] == 0x7D and i + 1 < len(data):
            if data[i+1] == 0x01:   result.append(0x7D); i += 2; continue
            elif data[i+1] == 0x02: result.append(0x7E); i += 2; continue
        result.append(data[i]); i += 1
    return bytes(result)

def build_frame(msg_id: int, phone_bcd: bytes, serial: int, body: bytes) -> bytes:
    props = len(body) & 0x03FF
    header = struct.pack('>HH', msg_id, props) + phone_bcd + struct.pack('>H', serial)
    payload = header + body
    cs = checksum(payload)
    return b'\x7E' + escape(payload + bytes([cs])) + b'\x7E'

def phone_to_bcd(phone: str) -> bytes:
    padded = phone.zfill(12)
    return bytes(int(padded[i:i+2], 16) for i in range(0, 12, 2))

def bcd_ts(dt: datetime) -> bytes:
    return bytes([
        int(f'{dt.year-2000:02d}'), int(f'{dt.month:02d}'),
        int(f'{dt.day:02d}'),       int(f'{dt.hour:02d}'),
        int(f'{dt.minute:02d}'),    int(f'{dt.second:02d}'),
    ])

# ── Frame builder helpers ──────────────────────────────────────────────────────

def make_register(phone_bcd, serial):
    """0x0100 Terminal Register"""
    body = (
        struct.pack('>HH', 41, 0) +          # province, city
        b'RANOR' +                             # maker ID (5 bytes)
        b'MDVR-3CH-AI    ' + b'\x00' * 4 +   # model (20 bytes)
        b'SIM001\x00' +                        # device ID (7 bytes)
        bytes([1]) +                           # plate color: blue
        'ABC1D23'.encode('gbk')                # plate number
    )
    return build_frame(0x0100, phone_bcd, serial, body)

def make_auth(phone_bcd, serial, token='RANOR01'):
    """0x0102 Terminal Auth"""
    return build_frame(0x0102, phone_bcd, serial, token.encode('ascii'))

def make_heartbeat(phone_bcd, serial):
    """0x0002 Heartbeat"""
    return build_frame(0x0002, phone_bcd, serial, b'')

def make_location(phone_bcd, serial, lat, lon, speed, direction,
                  alarm_flags=0, status_flags=0b00000011):
    """0x0200 Location Report"""
    now = datetime.now()
    body = struct.pack('>II', alarm_flags, status_flags)
    body += struct.pack('>I', int(abs(lat) * 1e6))
    body += struct.pack('>I', int(abs(lon) * 1e6))
    body += struct.pack('>HHH', 850, int(speed * 10), direction)
    body += bcd_ts(now)
    # Extra: mileage tag 0x01 (4 bytes), RSSI tag 0x30 (1 byte)
    body += bytes([0x01, 0x04]) + struct.pack('>I', 12345)
    body += bytes([0x30, 0x01, 18])  # RSSI = 18
    body += bytes([0x31, 0x01, 7])   # GPS sats = 7
    return build_frame(0x0200, phone_bcd, serial, body)

def make_media_event(phone_bcd, serial, media_id, channel, event_code=1):
    """0x0800 Media Event Upload"""
    body = struct.pack('>IBBBBB', media_id, 2, 3, event_code, channel, 0)
    return build_frame(0x0800, phone_bcd, serial, body)

def make_media_data(phone_bcd, serial, media_id, channel, data: bytes,
                    media_type=2, event_code=1):
    """0x0801 Media Data Upload (image chunk)"""
    # 28 bytes location placeholder
    loc_placeholder = bytes(28)
    body = struct.pack('>IBBBB', media_id, media_type, 3, channel, event_code)
    body += loc_placeholder + data
    return build_frame(0x0801, phone_bcd, serial, body)

# ── JPEG generator ─────────────────────────────────────────────────────────────

def make_fake_jpeg(channel: int, label: str, width=320, height=240) -> bytes:
    """Generate a realistic-looking dashcam snapshot"""
    img = Image.new('RGB', (width, height))
    draw = ImageDraw.Draw(img)

    # Sky gradient
    for y in range(height // 2):
        r = int(30 + y * 0.3)
        g = int(60 + y * 0.5)
        b = int(120 + y * 0.8)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # Ground
    for y in range(height // 2, height):
        shade = int(40 + (y - height//2) * 0.4)
        draw.line([(0, y), (width, y)], fill=(shade, shade+5, shade-5))

    # Road lines
    road_color = (80, 80, 80)
    draw.rectangle([width//3, height//2, 2*width//3, height], fill=road_color)
    # Dashes
    for i in range(3):
        y = height//2 + 20 + i * 40
        draw.rectangle([width//2 - 5, y, width//2 + 5, y + 25], fill=(220, 220, 50))

    # Timestamp overlay (dashcam style)
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # Black background for text
    draw.rectangle([0, 0, width, 20], fill=(0, 0, 0, 180))
    draw.rectangle([0, height-20, width, height], fill=(0, 0, 0, 180))
    draw.text((5, 3),  f'CH{channel} | {label}', fill=(0, 220, 0))
    draw.text((5, height-17), f'{ts} | RANOR TELEMETRIA', fill=(200, 200, 200))

    # Speed overlay
    speed = random.randint(0, 80)
    draw.text((width-60, 3), f'{speed} km/h', fill=(255, 200, 0))

    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=75)
    return buf.getvalue()

# ── Route simulation ───────────────────────────────────────────────────────────

class RouteSimulator:
    """Simulates a vehicle moving around Passos-MG area"""
    # Coordenadas reais: Passos, MG
    BASE_LAT = -20.7189
    BASE_LON = -46.6097

    def __init__(self):
        self.lat = self.BASE_LAT + random.uniform(-0.01, 0.01)
        self.lon = self.BASE_LON + random.uniform(-0.01, 0.01)
        self.speed = 40.0
        self.direction = random.randint(0, 359)
        self.distance = 0.0

    def step(self):
        # Gradual speed variation
        self.speed += random.uniform(-5, 5)
        self.speed = max(0, min(120, self.speed))

        # Gradual direction variation (curves)
        self.direction = (self.direction + random.randint(-10, 10)) % 360

        # Move based on speed and direction
        import math
        dt = 5.0 / 3600  # 5 seconds in hours
        dist = self.speed * dt  # km
        self.distance += dist

        rad = math.radians(self.direction)
        dlat = dist * math.cos(rad) / 111.0
        dlon = dist * math.sin(rad) / (111.0 * math.cos(math.radians(self.lat)))
        self.lat += dlat
        self.lon += dlon

        return self.lat, self.lon, self.speed, self.direction

# ── FTP uploader ───────────────────────────────────────────────────────────────

def ftp_upload_snapshot(host: str, port: int, filename: str, data: bytes):
    """Upload snapshot via FTP, como a dashcam real faz"""
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=10)
        ftp.login('ranor', 'ranor')
        ftp.set_pasv(True)
        ftp.storbinary(f'STOR {filename}', io.BytesIO(data))
        ftp.quit()
        logger.info(f"FTP ✅ {filename} ({len(data)/1024:.1f} KB) enviado")
    except Exception as e:
        logger.warning(f"FTP ❌ {e}")

# ── Main simulator ─────────────────────────────────────────────────────────────

class DashcamSimulator:
    ALARM_SCENARIOS = [
        (0b1 << 11, "Colisão detectada (acelerômetro)"),
        (0b1 << 18, "Câmera CH2 obstruída"),
        (0b1 << 19, "Motorista anormal (IA)"),
        (0b1 << 1,  "Excesso de velocidade"),
        (0b1 << 3,  "Risco de colisão frontal"),
    ]

    def __init__(self, args):
        self.host = args.host
        self.jt808_port = args.jt808_port
        self.ftp_port = args.ftp_port
        self.phone = args.phone
        self.phone_bcd = phone_to_bcd(self.phone)
        self.serial = 0
        self.reader = None
        self.writer = None
        self.route = RouteSimulator()
        self.media_id = 1
        self._running = False
        self._token = 'RANOR01'

    def next_serial(self):
        self.serial = (self.serial + 1) % 0xFFFF
        return self.serial

    async def connect(self):
        logger.info(f"Conectando em {self.host}:{self.jt808_port}...")
        self.reader, self.writer = await asyncio.open_connection(self.host, self.jt808_port)
        logger.info("✅ Conectado ao servidor JT808")

    def send(self, frame: bytes):
        self.writer.write(frame)

    async def recv_response(self, timeout=5.0):
        try:
            data = await asyncio.wait_for(self.reader.read(256), timeout=timeout)
            if data:
                logger.debug(f"← Resposta servidor: {data.hex()}")
            return data
        except asyncio.TimeoutError:
            logger.debug("Sem resposta (timeout)")
            return b''

    async def register(self):
        logger.info("→ Enviando REGISTER (0x0100)...")
        frame = make_register(self.phone_bcd, self.next_serial())
        self.send(frame)
        resp = await self.recv_response()
        if resp:
            logger.info("✅ Registro aceito pelo servidor")

    async def auth(self):
        logger.info(f"→ Enviando AUTH (0x0102) token={self._token}...")
        frame = make_auth(self.phone_bcd, self.next_serial(), self._token)
        self.send(frame)
        resp = await self.recv_response()
        if resp:
            logger.info("✅ Autenticação OK")

    async def send_heartbeat(self):
        frame = make_heartbeat(self.phone_bcd, self.next_serial())
        self.send(frame)
        logger.debug("→ Heartbeat")

    async def send_location(self, alarm_flags=0):
        lat, lon, speed, direction = self.route.step()
        frame = make_location(
            self.phone_bcd, self.next_serial(),
            lat, lon, speed, direction, alarm_flags
        )
        self.send(frame)
        await self.recv_response(timeout=2)
        alarm_str = f" ⚠ ALARME flags={alarm_flags:#010b}" if alarm_flags else ""
        logger.info(f"→ Posição: {lat:.5f},{lon:.5f} | {speed:.1f}km/h | {direction}°{alarm_str}")

    async def send_media_event(self, channel=2, event_code=1):
        """0x0800 - Notifica servidor que vai enviar mídia"""
        mid = self.media_id
        frame = make_media_event(self.phone_bcd, self.next_serial(), mid, channel, event_code)
        self.send(frame)
        await self.recv_response(timeout=2)
        logger.info(f"→ Media Event (0x0800): canal CH{channel}, id={mid}")
        return mid

    async def send_snapshot_jt808(self, channel=2, label="ALARME"):
        """Envia snapshot via protocolo JT808 0x0801"""
        jpeg = make_fake_jpeg(channel, label)
        mid = await self.send_media_event(channel)

        # Send in chunks of 512 bytes (como dispositivo real)
        chunk_size = 512
        chunks = [jpeg[i:i+chunk_size] for i in range(0, len(jpeg), chunk_size)]
        for i, chunk in enumerate(chunks):
            frame = make_media_data(
                self.phone_bcd, self.next_serial(),
                mid, channel, chunk
            )
            self.send(frame)
            await asyncio.sleep(0.05)

        await self.recv_response(timeout=3)
        logger.info(f"→ Snapshot JT808 (0x0801): CH{channel} {len(jpeg)/1024:.1f}KB em {len(chunks)} chunks")
        self.media_id += 1

    def send_snapshot_ftp(self, channel=2, label="ALARME"):
        """Envia snapshot via FTP (como a dashcam real faz nos alarmes de IA)"""
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        filename = f"CH{channel}IMG{ts}-{self.media_id}.jpg"
        jpeg = make_fake_jpeg(channel, label)
        self.media_id += 1

        # FTP roda em thread separada para não bloquear o loop asyncio
        t = threading.Thread(
            target=ftp_upload_snapshot,
            args=(self.host, self.ftp_port, filename, jpeg),
            daemon=True
        )
        t.start()
        return filename

    async def run_scenario(self):
        """Executa sequência realista baseada nos logs reais"""
        self._running = True
        tick = 0
        heartbeat_interval = 12   # posição a cada 5s, heartbeat a cada ~60s
        alarm_next = random.randint(20, 40)  # primeiro alarme após N posições

        logger.info("=" * 55)
        logger.info("  Simulador MDVR iniciado — comportamento real")
        logger.info("=" * 55)

        # 1. Registro e autenticação (como nos logs)
        await self.register()
        await asyncio.sleep(1)
        await self.auth()
        await asyncio.sleep(1)

        logger.info("Iniciando loop de telemetria...")

        while self._running:
            tick += 1

            # Posição a cada ciclo (~5s)
            alarm_flags = 0

            # Disparar alarme no tick programado
            if tick == alarm_next:
                scenario = random.choice(self.ALARM_SCENARIOS)
                alarm_flags, alarm_name = scenario
                logger.warning(f"🚨 ALARME SIMULADO: {alarm_name}")

                # Enviar posição com flag de alarme
                await self.send_location(alarm_flags)

                # Capturar snapshot em CH2 via FTP (como nos logs reais)
                logger.info("📷 Capturando 3 snapshots via FTP (CH2)...")
                for i in range(3):
                    self.send_snapshot_ftp(channel=2, label=alarm_name[:20])
                    await asyncio.sleep(0.3)

                # Também enviar 1 snapshot via JT808 0x0801
                await self.send_snapshot_jt808(channel=2, label=alarm_name[:20])

                # Próximo alarme
                alarm_next = tick + random.randint(25, 50)
                logger.info(f"Próximo alarme em ~{alarm_next - tick} posições")

            else:
                await self.send_location(0)

            # Heartbeat periódico
            if tick % heartbeat_interval == 0:
                await self.send_heartbeat()

            # Aguardar próximo ciclo (5 segundos como dispositivo real)
            await asyncio.sleep(5)

    async def run(self):
        retry = 0
        while True:
            try:
                await self.connect()
                await self.run_scenario()
            except ConnectionRefusedError:
                retry += 1
                wait = min(30, 5 * retry)
                logger.error(f"Servidor recusou conexão. Tentando novamente em {wait}s...")
                await asyncio.sleep(wait)
            except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                logger.warning("Conexão perdida. Reconectando em 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Erro: {e}. Reconectando em 10s...")
                await asyncio.sleep(10)
            finally:
                if self.writer:
                    try: self.writer.close()
                    except: pass


def main():
    parser = argparse.ArgumentParser(description='RANOR Dashcam Simulator')
    parser.add_argument('--host',        default='localhost',
                        help='Endereço do servidor JT808 (ngrok: 0.tcp.ngrok.io)')
    parser.add_argument('--jt808-port',  type=int, default=8080,
                        help='Porta JT808 (default 8080)')
    parser.add_argument('--ftp-port',    type=int, default=9999,
                        help='Porta FTP para snapshots (default 9999)')
    parser.add_argument('--phone',       default='13912345678',
                        help='Número de telefone simulado (ID do device)')
    args = parser.parse_args()

    print("""
╔══════════════════════════════════════════════════╗
║     RANOR — Simulador de Dashcam MDVR            ║
║  JT808 + FTP + Alarmes de IA + Snapshots JPEG    ║
╚══════════════════════════════════════════════════╝""")
    print(f"  Servidor : {args.host}:{args.jt808_port}")
    print(f"  FTP      : {args.host}:{args.ftp_port}")
    print(f"  Device   : {args.phone}")
    print()

    sim = DashcamSimulator(args)
    try:
        asyncio.run(sim.run())
    except KeyboardInterrupt:
        print("\nSimulador encerrado.")

if __name__ == '__main__':
    main()
