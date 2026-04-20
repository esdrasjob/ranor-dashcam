"""
Servidor TCP JT/T 808-2013
Aceita conexões da dashcam, processa telemetria, eventos e média
"""

import asyncio
import struct
import logging
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from stream_commands import (
    cmd_start_stream, cmd_stop_stream,
    cmd_capture_snapshot, cmd_query_stream_params,
)
from jt808_parser import (
    JT808Parser, ParsedMessage,
    build_response, build_register_response,
    MSG_TERMINAL_REGISTER, MSG_TERMINAL_AUTH,
    MSG_HEARTBEAT, MSG_LOCATION_REPORT,
    MSG_MEDIA_EVENT, MSG_MEDIA_UPLOAD,
)

logger = logging.getLogger("ranor.jt808")


class DeviceSession:
    """Represents one connected dashcam device"""

    def __init__(self, phone: str, transport: asyncio.Transport,
                 storage: Path, event_bus):
        self.phone = phone
        self.transport = transport
        self.storage = storage
        self.event_bus = event_bus
        self.parser = JT808Parser()
        self.serial = 0
        self.registered = False
        self.last_location = None
        self.last_seen = datetime.now()
        self.device_info = {}
        self._media_buffers: Dict[int, bytearray] = {}
        self.stream_server = None   # injected by JT808Server after init

        # Phone as BCD bytes (pad to 12 digits)
        padded = phone.zfill(12)
        self._phone_bcd = bytes(int(padded[i:i+2], 16) for i in range(0, 12, 2))

    def next_serial(self) -> int:
        self.serial = (self.serial + 1) % 0xFFFF
        return self.serial

    def send(self, frame: bytes):
        self.transport.write(frame)

    def respond(self, resp_serial: int, resp_msg_id: int, result: int = 0):
        frame = build_response(
            self._phone_bcd, self.next_serial(),
            resp_serial, resp_msg_id, result
        )
        self.send(frame)

    async def handle_message(self, msg: ParsedMessage):
        self.last_seen = datetime.now()
        t = msg.msg_type
        logger.debug(f"[{self.phone}] MSG={t}")

        if t == "register":
            await self._on_register(msg)
        elif t == "auth":
            await self._on_auth(msg)
        elif t == "heartbeat":
            await self._on_heartbeat(msg)
        elif t == "location":
            await self._on_location(msg)
        elif t == "media_event":
            await self._on_media_event(msg)
        elif t == "media_upload":
            await self._on_media_upload(msg)
        else:
            # Generic ACK for unknown commands
            self.respond(msg.header.msg_serial, msg.header.msg_id)

    async def request_stream(self, channel: int = 1):
        """Envia 0x9101 para a dashcam iniciar stream no servidor JT1078"""
        if not self.stream_server:
            logger.warning(f"[{self.phone}] stream_server not set, cannot request stream")
            return
        srv = self.stream_server
        frame = cmd_start_stream(
            self._phone_bcd, self.next_serial(),
            server_ip=srv.host if srv.host != '0.0.0.0' else _get_local_ip(),
            server_port=srv.port,
            channel=channel,
            stream_type=0,
            data_type=1,   # vídeo apenas (sem áudio para reduzir banda)
        )
        self.send(frame)
        logger.info(f"[{self.phone}] Solicitando stream CH{channel} → {srv.host}:{srv.port}")

    async def stop_stream(self, channel: int = 1):
        """Envia 0x9102 para parar o stream"""
        self.send(cmd_stop_stream(self._phone_bcd, self.next_serial(), channel))

    async def capture_snapshot(self, channel: int = 0xFF):
        """Envia 0x8801 para capturar snapshot de todos os canais"""
        self.send(cmd_capture_snapshot(self._phone_bcd, self.next_serial(), channel))

    async def _on_register(self, msg: ParsedMessage):
        self.device_info = msg.extra
        self.registered = True
        logger.info(f"[{self.phone}] Registered: {msg.extra}")
        frame = build_register_response(
            self._phone_bcd, self.next_serial(),
            msg.header.msg_serial, result=0
        )
        self.send(frame)
        await self.event_bus.publish('device_connected', {
            'phone': self.phone,
            'device_info': msg.extra,
            'timestamp': datetime.now().isoformat(),
        })

    async def _on_auth(self, msg: ParsedMessage):
        self.registered = True
        self.respond(msg.header.msg_serial, MSG_TERMINAL_AUTH)
        logger.info(f"[{self.phone}] Auth OK, token={msg.extra.get('token','')}")

    async def _on_heartbeat(self, msg: ParsedMessage):
        self.respond(msg.header.msg_serial, MSG_HEARTBEAT)
        await self.event_bus.publish('heartbeat', {
            'phone': self.phone,
            'timestamp': datetime.now().isoformat(),
        })

    async def _on_location(self, msg: ParsedMessage):
        if not msg.location:
            return
        loc = msg.location
        self.last_location = loc
        self.respond(msg.header.msg_serial, MSG_LOCATION_REPORT)

        payload = {
            'phone': self.phone,
            'lat': loc.latitude,
            'lon': loc.longitude,
            'speed': loc.speed,
            'direction': loc.direction,
            'altitude': loc.altitude,
            'timestamp': loc.timestamp.isoformat(),
            'alarms': loc.alarms,
            'status': loc.status,
            'extras': loc.extras,
        }
        await self.event_bus.publish('location', payload)

        if loc.alarms:
            logger.warning(f"[{self.phone}] ALARME: {loc.alarms}")
            await self.event_bus.publish('alarm', {**payload, 'alarm_list': loc.alarms})

        # Persist to file
        self._save_event('location', payload)

    async def _on_media_event(self, msg: ParsedMessage):
        self.respond(msg.header.msg_serial, MSG_MEDIA_EVENT)
        ev = msg.media_event
        if not ev:
            return
        logger.info(f"[{self.phone}] Media event: {msg.extra}")
        payload = {
            'phone': self.phone,
            'media_id': ev.media_id,
            'type': msg.extra.get('type_label'),
            'event': msg.extra.get('event_label'),
            'channel': ev.channel_id,
            'timestamp': datetime.now().isoformat(),
        }
        await self.event_bus.publish('media_event', payload)
        self._save_event('media_event', payload)

    async def _on_media_upload(self, msg: ParsedMessage):
        """0x0801 - image/video chunk from dashcam"""
        self.respond(msg.header.msg_serial, MSG_MEDIA_UPLOAD)
        ex = msg.extra
        media_id  = ex.get('media_id', 0)
        mtype     = ex.get('media_type', 'unknown')
        channel   = ex.get('channel', 0)
        data      = ex.get('data', b'')

        if not data:
            return

        # Buffer fragments by media_id
        if media_id not in self._media_buffers:
            self._media_buffers[media_id] = bytearray()
        self._media_buffers[media_id].extend(data)

        # Detect JPEG end marker (FFD9) -> save
        if mtype == 'image' and b'\xFF\xD9' in data:
            raw = bytes(self._media_buffers.pop(media_id))
            await self._save_snapshot(raw, channel, media_id)
        elif mtype == 'video' and len(self._media_buffers[media_id]) > 4 * 1024 * 1024:
            # Save video chunk at 4MB
            raw = bytes(self._media_buffers.pop(media_id))
            await self._save_video_chunk(raw, channel, media_id)

    async def _save_snapshot(self, data: bytes, channel: int, media_id: int):
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        fname = f"CH{channel}IMG{ts}-{media_id}.jpg"
        dest = self.storage / 'snapshots' / fname
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        logger.info(f"[{self.phone}] Snapshot salvo: {fname} ({len(data)} bytes)")
        await self.event_bus.publish('snapshot', {
            'phone': self.phone,
            'filename': fname,
            'path': str(dest),
            'channel': channel,
            'size': len(data),
            'timestamp': datetime.now().isoformat(),
        })

    async def _save_video_chunk(self, data: bytes, channel: int, media_id: int):
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        fname = f"CH{channel}VID{ts}-{media_id}.mp4"
        dest = self.storage / 'videos' / fname
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        logger.info(f"[{self.phone}] Video chunk salvo: {fname}")
        await self.event_bus.publish('video_chunk', {
            'phone': self.phone,
            'filename': fname,
            'channel': channel,
            'size': len(data),
            'timestamp': datetime.now().isoformat(),
        })

    def _save_event(self, etype: str, payload: dict):
        log_dir = self.storage / 'events'
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime('%Y%m%d')
        log_file = log_dir / f"{self.phone}_{today}.jsonl"
        with open(log_file, 'a') as f:
            f.write(json.dumps({'type': etype, **payload}) + '\n')


class JT808Server:
    def __init__(self, storage: Path, event_bus, host='0.0.0.0', port=808):
        self.storage = storage
        self.event_bus = event_bus
        self.host = host
        self.port = port
        self.sessions: Dict[str, DeviceSession] = {}
        self._server = None

    async def start(self):
        self._server = await asyncio.get_event_loop().create_server(
            lambda: JT808Protocol(self),
            self.host, self.port
        )
        logger.info(f"JT808 server listening on {self.host}:{self.port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def get_sessions(self) -> dict:
        return {
            phone: {
                'phone': s.phone,
                'registered': s.registered,
                'last_seen': s.last_seen.isoformat(),
                'device_info': s.device_info,
                'last_location': {
                    'lat': s.last_location.latitude,
                    'lon': s.last_location.longitude,
                    'speed': s.last_location.speed,
                    'alarms': s.last_location.alarms,
                    'timestamp': s.last_location.timestamp.isoformat(),
                } if s.last_location else None,
            }
            for phone, s in self.sessions.items()
        }


class JT808Protocol(asyncio.Protocol):
    """asyncio Protocol for one TCP connection"""

    def __init__(self, server: JT808Server):
        self.server = server
        self.transport = None
        self.session: Optional[DeviceSession] = None
        self._parser = JT808Parser()
        self._peer = None

    def connection_made(self, transport):
        self.transport = transport
        self._peer = transport.get_extra_info('peername')
        logger.info(f"JT808 connection from {self._peer}")

    def connection_lost(self, exc):
        if self.session:
            phone = self.session.phone
            self.server.sessions.pop(phone, None)
            asyncio.get_event_loop().create_task(
                self.server.event_bus.publish('device_disconnected', {
                    'phone': phone,
                    'timestamp': datetime.now().isoformat(),
                })
            )
            logger.info(f"JT808 disconnected: {phone}")

    def data_received(self, data: bytes):
        messages = self._parser.feed(data)
        for msg in messages:
            asyncio.get_event_loop().create_task(self._handle(msg))

    async def _handle(self, msg: ParsedMessage):
        phone = msg.header.phone

        # Create session on first message
        if phone not in self.server.sessions:
            session = DeviceSession(
                phone=phone,
                transport=self.transport,
                storage=self.server.storage,
                event_bus=self.server.event_bus,
            )
            self.server.sessions[phone] = session
            self.session = session
        else:
            self.session = self.server.sessions[phone]
            # Update transport if reconnected
            self.session.transport = self.transport

        await self.session.handle_message(msg)


def _get_local_ip() -> str:
    """Retorna o IP local da máquina para enviar para a dashcam"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'
