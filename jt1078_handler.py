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
CODEC_H264_ALT = 0x64   # variante usada por firmware MDVR chinês (decimal 100)
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
    [4]     V(2), P(1), X(1), CC(4)
    [5]     M(1), PT(7) (Payload Type)
    [6:8]   Sequence Number
    [8:14]  SIM (BCD 6 bytes = 12 digits phone)
    [14]    Channel (Logic Channel)
    [15]    DataType (high 4=frame type, low 4=sub-packet type)
    [16:24] Timestamp (8 bytes)
    [24:26] Last I-Frame Interval (2 bytes)
    [26:28] Last Frame Interval (2 bytes)
    [28:30] DataLength (2 bytes)
    [30:]   Payload
    """
    if len(data) < 30:
        return None

    magic = struct.unpack_from('>H', data, 0)[0]
    if magic != 0x3031:
        return None

    # Validate DataType (byte 15)
    datatype = data[15]
    frame_type = (datatype >> 4) & 0x0F
    sub_type = datatype & 0x0F
    if frame_type > 6 or sub_type > 3:
        return None

    # Validate SIM bytes
    sim_raw = data[8:14]
    for b in sim_raw:
        if (b & 0x0F) > 9 or (b >> 4) > 9:
            return None

    seq      = struct.unpack_from('>H', data, 6)[0]
    ts       = struct.unpack_from('>Q', data, 16)[0]  # 8 bytes timestamp
    sim_raw  = data[8:14]
    sim      = ''.join(f'{b:02X}' for b in sim_raw).lstrip('0') or '0'
    channel  = data[14]
    datatype = data[15]
    frame_type = (datatype >> 4) & 0x0F
    sub_type   = datatype & 0x0F
    data_len   = struct.unpack_from('>H', data, 28)[0]
    payload    = data[30:30 + data_len]

    pt = data[5] & 0x7F  # codec

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
        'total_size': 30 + data_len,
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
        self._seen_iframe = False
        self._av_converter = None
        self._init_converter()

    def _init_converter(self):
        """Inicializa o conversor H264 → JPEG via PyAV"""
        try:
            import av
            self._av = av
            self._codec_ctx = av.CodecContext.create('h264', 'r')
            self._codec = True  # flag: av available
            logger.info(f"[{self.sim}/CH{self.channel}] PyAV H264 decoder OK")
        except ImportError:
            logger.warning(f"[{self.sim}/CH{self.channel}] PyAV não instalado — instale com: pip install av")
            self._av = None
            self._codec = None
            self._codec_ctx = None
        except Exception as e:
            logger.warning(f"[{self.sim}/CH{self.channel}] PyAV init failed: {e}")
            self._av = None
            self._codec = None
            self._codec_ctx = None

    def feed_packet(self, pkt: dict) -> Optional[bytes]:
        """Alimenta um pacote 1078, retorna JPEG se disponível"""
        if pkt['frame_type'] == PT_AUDIO:
            return None

        raw_frame = self.assembler.feed(pkt)
        if not raw_frame:
            return None

        # Wait for the first I-frame to sync the decoder (SPS/PPS)
        if not self._seen_iframe:
            if pkt['frame_type'] != PT_VIDEO_I:
                return None
            self._seen_iframe = True
            logger.info(f"[{self.sim}/CH{self.channel}] I-frame sincronizado, iniciando decodificador!")

        self._frame_count += 1
        now = time.time()

        jpeg_data = None
        if self._av and self._codec_ctx:
            normalized = self._normalize_h264(raw_frame)
            if pkt['frame_type'] == PT_VIDEO_I and self._frame_count == 1:
                logger.info(f"[{self.sim}/CH{self.channel}] FIRST I-FRAME HEX: {normalized[:64].hex()}")
            
            # Feed the decoder context continuously
            try:
                packets = self._codec_ctx.parse(normalized)
                for p in packets:
                    frames = self._codec_ctx.decode(p)
                    for frame in frames:
                        # Extract JPEG only at the configured interval
                        if now - self._last_jpeg_ts >= self._jpeg_interval:
                            import io as _io
                            img = frame.to_image()
                            buf = _io.BytesIO()
                            img.save(buf, format='JPEG', quality=75)
                            jpeg_data = buf.getvalue()
                            self._last_jpeg_ts = now
            except Exception as e:
                logger.debug(f"[{self.sim}/CH{self.channel}] Decode error: {e}")
                
        return jpeg_data

    def _normalize_h264(self, raw: bytes) -> bytes:
        """
        Converte payload JT1078 para H264 Annex-B que o PyAV aceita.
        Suporta 3 formatos comuns de firmware MDVR:
          1. Já é Annex-B (começa com 00 00 00 01 ou 00 00 01)
          2. AVCC: [4 bytes length][NAL data][4 bytes length][NAL data]...
          3. Com header proprietário de 8 bytes antes do NAL
        """
        if not raw or len(raw) < 4:
            return raw

        # Formato 1: já é Annex-B
        if raw[:3] == b'\x00\x00\x01' or raw[:4] == b'\x00\x00\x00\x01':
            return raw

        # Formato 3: header proprietário — procura start code nos primeiros 128 bytes
        idx4 = raw.find(b'\x00\x00\x00\x01', 0, 128)
        idx3 = raw.find(b'\x00\x00\x01', 0, 128)
        
        # Prefere o de 4 bytes se estiver na mesma posição
        if idx4 != -1 and (idx3 == -1 or idx4 <= idx3):
            return raw[idx4:]
        elif idx3 != -1:
            return raw[idx3:]

        # Formato 2: AVCC length-prefixed → Annex-B
        result = bytearray()
        i = 0
        while i + 4 <= len(raw):
            nal_len = int.from_bytes(raw[i:i+4], 'big')
            i += 4
            if nal_len == 0 or i + nal_len > len(raw):
                break
            result.extend(b'\x00\x00\x00\x01')
            result.extend(raw[i:i+nal_len])
            i += nal_len

        return bytes(result) if result else raw

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

    def __init__(self, event_bus, storage: Path, host='0.0.0.0', port=1078,
                 public_host=None, public_port=None):
        self.event_bus = event_bus
        self.storage = storage
        self.host = host
        self.port = port
        # public_host/public_port: o endereço que a câmera vai usar para conectar
        # (ex: host ngrok quando câmera está na internet via 4G)
        self.public_host = public_host   # None = detectar IP local
        self.public_port = public_port or port
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

    def _decode_sim_channel(self, sim: str, ch: int) -> tuple:
        """
        Esse firmware MDVR codifica o canal nos 2 últimos dígitos do SIM:
          XXXXXXXXXX01 = CH1 main, XX02 = CH2 main, XX03 = CH3 main
          XXXXXXXXXX11 = CH1 sub,  XX12 = CH2 sub,  XX13 = CH3 sub
        Retorna (device_id, channel_num, stream_type)
        """
        if len(sim) >= 2:
            suffix = sim[-2:]
            suffix_map = {
                '01': (1, 'main'), '02': (2, 'main'), '03': (3, 'main'),
                '11': (1, 'sub'),  '12': (2, 'sub'),  '13': (3, 'sub'),
                '30': (0, 'audio'),
            }
            if suffix in suffix_map:
                real_ch, stype = suffix_map[suffix]
                device_id = sim[:-2].lstrip('0') or sim
                return device_id, real_ch, stype
        return sim, ch, 'main'

    async def on_packet(self, pkt: dict):
        """Chamado pelo Protocol para cada pacote 1078 recebido"""
        raw_sim = pkt['sim']
        raw_ch  = pkt['channel']

        # Decodificar SIM -> device_id + channel real
        device_id, real_ch, stream_type = self._decode_sim_channel(raw_sim, raw_ch)

        # Ignorar áudio por ora
        if stream_type == 'audio':
            return

        key = f"{raw_sim}_{raw_ch}"

        if key not in self._sessions:
            self._sessions[key] = JT1078Session(
                device_id, real_ch, self.event_bus, self.storage
            )
            logger.info(
                f"Nova sessão de stream: device={device_id} CH{real_ch} "
                f"({stream_type}) codec=0x{pkt['codec']:02X} sim={raw_sim}"
            )
            await self.event_bus.publish('stream_started', {
                'sim': device_id, 'channel': real_ch,
                'stream_type': stream_type,
                'codec': pkt['codec'],
                'timestamp': datetime.now().isoformat(),
            })

        session = self._sessions[key]
        jpeg = session.feed_packet(pkt)
        if jpeg:
            await self.broadcast_jpeg(device_id, real_ch, jpeg)

    def remove_session(self, key: str):
        if key in self._sessions:
            del self._sessions[key]
            logger.info(f"Sessão de stream limpa: {key}")


class JT1078Protocol(asyncio.Protocol):
    """asyncio Protocol para uma conexão de streaming"""

    def __init__(self, server: JT1078Server):
        self.server = server
        self.transport = None
        self._buffer = bytearray()
        self._peer = None
        self._active_keys = set()

    def connection_made(self, transport):
        self.transport = transport
        self._peer = transport.get_extra_info('peername')
        logger.info(f"JT1078 stream connection from {self._peer}")

    def connection_lost(self, exc):
        logger.info(f"JT1078 stream disconnected: {self._peer}")
        for key in self._active_keys:
            self.server.remove_session(key)

    def data_received(self, data: bytes):
        self._buffer.extend(data)
        self._process_buffer()

    def _process_buffer(self):
        while len(self._buffer) >= 32:
            # Check magic 0x3031
            if self._buffer[0] != 0x30 or self._buffer[1] != 0x31:
                # Sync: find next magic
                idx = self._buffer.find(b'\x30\x31', 1)
                if idx == -1:
                    self._buffer.clear()
                    return
                self._buffer = self._buffer[idx:]
                continue

            # Need at least header to know payload size
            if len(self._buffer) < 30:
                break

            # Quickly check DataType and BCD to avoid downloading huge false payload
            datatype = self._buffer[15]
            if ((datatype >> 4) & 0x0F) > 6 or (datatype & 0x0F) > 3:
                self._buffer = self._buffer[2:]
                continue
                
            sim_raw = self._buffer[8:14]
            valid_bcd = True
            for b in sim_raw:
                if (b & 0x0F) > 9 or (b >> 4) > 9:
                    valid_bcd = False
                    break
            if not valid_bcd:
                self._buffer = self._buffer[2:]
                continue

            data_len = struct.unpack_from('>H', self._buffer, 28)[0]
            total = 30 + data_len

            if len(self._buffer) < total:
                break  # Wait for more data

            pkt_data = bytes(self._buffer[:total])

            pkt = parse_jt1078_header(pkt_data)
            if pkt:
                self._buffer = self._buffer[total:]
                key = f"{pkt['sim']}_{pkt['channel']}"
                self._active_keys.add(key)
                asyncio.get_event_loop().create_task(self.server.on_packet(pkt))
            else:
                self._buffer = self._buffer[2:]
