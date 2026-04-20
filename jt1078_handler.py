"""
JT/T 1078-2016 — Handler de streaming de vídeo
A dashcam envia stream RTP com H.264 por TCP.
Este módulo:
  1. Recebe a conexão da câmera (ela conecta no servidor)
  2. Parseia os pacotes RTP/1078
  3. Remonta os frames H.264
  4. Converte para JPEG via PyAV
  5. Distribui os frames para clientes WebSocket via EventBus
"""

import asyncio
import struct
import logging
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Set, Optional

logger = logging.getLogger("ranor.jt1078")

# ── JT1078 Packet Types ───────────────────────────────────────────────────────
PT_VIDEO_I   = 0x00   # I-frame
PT_VIDEO_P   = 0x01   # P-frame
PT_VIDEO_B   = 0x02   # B-frame
PT_AUDIO     = 0x03
PT_COMBINED  = 0x06   # Pacote combinado (mais comum em MDVR)

# Sub-packet types
SUB_ATOM     = 0x00   # Pacote único (frame completo)
SUB_FIRST    = 0x01   # Primeiro de N
SUB_LAST     = 0x02   # Último de N
SUB_MIDDLE   = 0x03   # Meio

CODEC_H264   = 0x98
CODEC_H265   = 0x99
CODEC_G711A  = 0x03
CODEC_G711U  = 0x04


def parse_jt1078_header(data: bytes) -> Optional[dict]:
    """
    JT1078 RTP Header (固定30字节):
    [0]     标识符高 (0x30)
    [1]     标识符低 (0x31)  — together: magic 0x3031
    [2]     V(2)+P(1)+X(1)+CC(4)
    [3]     M(1)+PT(7)       — PT: codec type
    [4:6]   Sequence Number
    [6:10]  Timestamp (ms)
    [10:16] SIM (BCD 6 bytes = 12 digits phone)
    [16]    Channel (1-based)
    [17]    DataType (high 4=frame type, low 4=sub-packet type)
    [18:22] SimTime (Unix ms, big-endian)  — some devices use this
    [22:26] LastIFrameInterval (ms)
    [26:30] LastFrameInterval (ms)
    [30:32] DataLength
    [32:]   Payload
    """
    if len(data) < 32:
        return None

    magic = struct.unpack_from('>H', data, 0)[0]
    if magic != 0x3031:
        return None

    seq      = struct.unpack_from('>H', data, 4)[0]
    ts       = struct.unpack_from('>I', data, 6)[0]
    sim_raw  = data[10:16]
    sim      = ''.join(f'{b:02X}' for b in sim_raw).lstrip('0') or '0'
    channel  = data[16]
    datatype = data[17]
    frame_type = (datatype >> 4) & 0x0F
    sub_type   = datatype & 0x0F
    data_len   = struct.unpack_from('>H', data, 30)[0]
    payload    = data[32:32 + data_len]

    pt = data[3] & 0x7F  # codec

    return {
        'seq': seq,
        'ts': ts,
        'sim': sim,
        'channel': channel,
        'frame_type': frame_type,
        'sub_type': sub_type,
        'codec': pt,
        'data_len': data_len,
        'payload': payload,
        'total_size': 32 + data_len,
    }


class FrameAssembler:
    """Remonta frames fragmentados de múltiplos pacotes 1078"""

    def __init__(self):
        self._buffers: Dict[tuple, bytearray] = {}
        self._last_seq: Dict[tuple, int] = {}

    def feed(self, pkt: dict) -> Optional[bytes]:
        key = (pkt['sim'], pkt['channel'])
        sub = pkt['sub_type']
        payload = pkt['payload']

        if sub == SUB_ATOM:
            # Pacote completo — retorna direto
            return bytes(payload)

        elif sub == SUB_FIRST:
            self._buffers[key] = bytearray(payload)

        elif sub == SUB_MIDDLE:
            if key in self._buffers:
                self._buffers[key].extend(payload)

        elif sub == SUB_LAST:
            if key in self._buffers:
                self._buffers[key].extend(payload)
                frame = bytes(self._buffers.pop(key))
                return frame

        return None


class JT1078Session:
    """Uma sessão de streaming de uma câmera"""

    def __init__(self, sim: str, channel: int, event_bus, storage: Path):
        self.sim = sim
        self.channel = channel
        self.event_bus = event_bus
        self.storage = storage
        self.assembler = FrameAssembler()
        self._jpeg_clients: Set = set()
        self._frame_count = 0
        self._last_jpeg_ts = 0
        self._jpeg_interval = 0.5   # extrair JPEG a cada 500ms
        self._av_converter = None
        self._init_converter()

    def _init_converter(self):
        """Inicializa o conversor H264 → JPEG via PyAV"""
        try:
            import av
            self._codec = av.CodecContext.create('h264', 'r')
            self._codec.options = {'threads': '1'}
            logger.info(f"[{self.sim}/CH{self.channel}] PyAV H264 decoder OK")
        except Exception as e:
            logger.warning(f"PyAV init failed: {e} — usando modo raw")
            self._codec = None

    def feed_packet(self, pkt: dict) -> Optional[bytes]:
        """Alimenta um pacote 1078, retorna JPEG se disponível"""
        if pkt['frame_type'] == PT_AUDIO:
            return None

        raw_frame = self.assembler.feed(pkt)
        if not raw_frame:
            return None

        self._frame_count += 1
        now = time.time()

        # Limitar extração de JPEG pela taxa configurada
        if now - self._last_jpeg_ts < self._jpeg_interval:
            return None

        self._last_jpeg_ts = now
        return self._h264_to_jpeg(raw_frame, pkt)

    def _h264_to_jpeg(self, raw: bytes, pkt: dict) -> Optional[bytes]:
        """Converte frame H264 raw para JPEG"""
        if self._codec is None:
            return self._fallback_jpeg(pkt)

        try:
            import av
            packets = self._codec.parse(raw)
            for packet in packets:
                frames = self._codec.decode(packet)
                for frame in frames:
                    # Converter para JPEG
                    img = frame.to_image()
                    import io
                    buf = io.BytesIO()
                    img.save(buf, format='JPEG', quality=70)
                    return buf.getvalue()
        except Exception as e:
            logger.debug(f"H264 decode error: {e}")
            return self._fallback_jpeg(pkt)

        return None

    def _fallback_jpeg(self, pkt: dict) -> Optional[bytes]:
        """Gera JPEG informativo quando decodificação não disponível"""
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.new('RGB', (640, 360), color=(20, 20, 40))
            draw = ImageDraw.Draw(img)
            draw.rectangle([0, 0, 640, 360], outline=(0, 180, 255), width=3)
            ts = datetime.now().strftime('%H:%M:%S')
            draw.text((20, 30),  f'RANOR — Stream Ativo', fill=(0, 220, 255))
            draw.text((20, 70),  f'Device: {self.sim}', fill=(200, 200, 200))
            draw.text((20, 100), f'Canal: CH{self.channel}', fill=(200, 200, 200))
            draw.text((20, 130), f'Frame #{self._frame_count}', fill=(150, 150, 150))
            draw.text((20, 160), f'{ts}', fill=(100, 255, 100))
            draw.text((20, 200), f'Stream H264 recebido — aguardando', fill=(255, 200, 0))
            draw.text((20, 225), f'decodificador nativo', fill=(255, 200, 0))
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=80)
            return buf.getvalue()
        except Exception:
            return None


class JT1078Server:
    """
    Servidor TCP que recebe conexões de streaming da dashcam.
    A dashcam conecta NESTE servidor quando recebe comando 0x9101.
    """

    def __init__(self, event_bus, storage: Path, host='0.0.0.0', port=1078):
        self.event_bus = event_bus
        self.storage = storage
        self.host = host
        self.port = port
        self._server = None
        self._sessions: Dict[str, JT1078Session] = {}
        # WebSocket clients aguardando frames: {(sim, channel): set of ws}
        self._stream_clients: Dict[tuple, Set] = defaultdict(set)

    async def start(self):
        self._server = await asyncio.get_event_loop().create_server(
            lambda: JT1078Protocol(self),
            self.host, self.port
        )
        logger.info(f"JT1078 streaming server on {self.host}:{self.port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def add_stream_client(self, ws, sim: str, channel: int):
        key = (sim, channel)
        self._stream_clients[key].add(ws)
        logger.info(f"Stream client added: {sim}/CH{channel} (total: {len(self._stream_clients[key])})")

    def remove_stream_client(self, ws, sim: str, channel: int):
        key = (sim, channel)
        self._stream_clients[key].discard(ws)

    async def broadcast_jpeg(self, sim: str, channel: int, jpeg: bytes):
        """Envia JPEG para todos os clientes WebSocket desse canal"""
        import base64
        key = (sim, channel)
        clients = self._stream_clients.get(key, set())
        if not clients:
            return

        b64 = base64.b64encode(jpeg).decode()
        msg = f'{{"type":"frame","sim":"{sim}","channel":{channel},"data":"{b64}","ts":{int(time.time()*1000)}}}'

        dead = set()
        for ws in clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._stream_clients[key].discard(ws)

    def get_active_streams(self) -> list:
        return [
            {'sim': k[0], 'channel': k[1], 'clients': len(v)}
            for k, v in self._stream_clients.items()
            if v
        ]

    async def on_packet(self, pkt: dict):
        """Chamado pelo Protocol para cada pacote 1078 recebido"""
        sim = pkt['sim']
        ch  = pkt['channel']
        key = f"{sim}_{ch}"

        if key not in self._sessions:
            self._sessions[key] = JT1078Session(sim, ch, self.event_bus, self.storage)
            logger.info(f"Nova sessão de stream: {sim}/CH{ch} codec=0x{pkt['codec']:02X}")
            await self.event_bus.publish('stream_started', {
                'sim': sim, 'channel': ch,
                'codec': pkt['codec'],
                'timestamp': datetime.now().isoformat(),
            })

        session = self._sessions[key]
        jpeg = session.feed_packet(pkt)
        if jpeg:
            await self.broadcast_jpeg(sim, ch, jpeg)


class JT1078Protocol(asyncio.Protocol):
    """asyncio Protocol para uma conexão de streaming"""

    def __init__(self, server: JT1078Server):
        self.server = server
        self.transport = None
        self._buffer = bytearray()
        self._peer = None

    def connection_made(self, transport):
        self.transport = transport
        self._peer = transport.get_extra_info('peername')
        logger.info(f"JT1078 stream connection from {self._peer}")

    def connection_lost(self, exc):
        logger.info(f"JT1078 stream disconnected: {self._peer}")

    def data_received(self, data: bytes):
        self._buffer.extend(data)
        self._process_buffer()

    def _process_buffer(self):
        while len(self._buffer) >= 32:
            # Check magic
            if self._buffer[0] != 0x30 or self._buffer[1] != 0x31:
                # Sync: find next magic
                idx = self._buffer.find(b'\x30\x31', 1)
                if idx == -1:
                    self._buffer.clear()
                    return
                self._buffer = self._buffer[idx:]
                continue

            # Need at least header to know payload size
            if len(self._buffer) < 32:
                break

            data_len = struct.unpack_from('>H', self._buffer, 30)[0]
            total = 32 + data_len

            if len(self._buffer) < total:
                break  # Wait for more data

            pkt_data = bytes(self._buffer[:total])
            self._buffer = self._buffer[total:]

            pkt = parse_jt1078_header(pkt_data)
            if pkt:
                asyncio.get_event_loop().create_task(self.server.on_packet(pkt))
