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
    cmd_upload_media,
)
from jt808_parser import (
    JT808Parser, ParsedMessage,
    build_response, build_register_response,
    MSG_TERMINAL_REGISTER, MSG_TERMINAL_AUTH,
    MSG_HEARTBEAT, MSG_LOCATION_REPORT,
    MSG_MEDIA_EVENT, MSG_MEDIA_UPLOAD,
    MSG_TRANSPARENT_DATA, MSG_CAMERA_CAPTURE,
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
        elif t == "location_batch":
            await self._on_location_batch(msg)
        elif t == "media_event":
            await self._on_media_event(msg)
        elif t == "media_upload":
            await self._on_media_upload(msg)
        elif t == "camera_capture":
            await self._on_camera_capture(msg)
        elif t == "transparent_data":
            await self._on_transparent_data(msg)
        else:
            # Generic ACK for unknown commands
            self.respond(msg.header.msg_serial, msg.header.msg_id)

    async def request_stream(self, channel: int = 1):
        """Envia 0x9101 para a dashcam iniciar stream no servidor JT1078"""
        if not self.stream_server:
            logger.warning(f"[{self.phone}] stream_server not set, cannot request stream")
            return
        srv = self.stream_server

        # Prioridade: public_host configurado > IP local detectado
        if srv.public_host:
            target_ip = srv.public_host
            target_port = srv.public_port
        else:
            target_ip = _get_local_ip()
            target_port = srv.port

        frame = cmd_start_stream(
            self._phone_bcd, self.next_serial(),
            server_ip=target_ip,
            server_port=target_port,
            channel=channel,
            stream_type=0,
            data_type=1,   # vídeo apenas (sem áudio para reduzir banda)
        )
        self.send(frame)
        logger.info(f"[{self.phone}] Solicitando stream CH{channel} → {target_ip}:{target_port}")

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

        # Log detalhado: mostra flags brutos para diagnóstico
        logger.debug(
            f"[{self.phone}] LOC raw: alarm=0x{loc.alarm_flags:08X} "
            f"status=0x{loc.status_flags:08X} "
            f"lat={loc.latitude:.5f} lon={loc.longitude:.5f} "
            f"spd={loc.speed:.1f}km/h"
        )
        if loc.alarm_flags:
            logger.warning(
                f"[{self.phone}] ⚠ ALARM FLAGS=0x{loc.alarm_flags:08X} → {loc.alarms}"
            )

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
            try:
                # Determine which camera triggered the alarm:
                # DSM (0x65) → driver cam (CH2), ADAS (0x64) → front cam (CH1)
                if 'dsm_channel' in loc.extras:
                    snap_ch = loc.extras['dsm_channel']
                elif 'adas_channel' in loc.extras:
                    snap_ch = loc.extras['adas_channel']
                else:
                    snap_ch = 1
                await self.capture_snapshot(channel=snap_ch)
                logger.info(f"[{self.phone}] Snapshot automático solicitado CH{snap_ch}")
            except Exception as e:
                pass

        # Persist to file
        self._save_event('location', payload)

    async def _on_location_batch(self, msg: ParsedMessage):
        """0x0704 — processa lote de posições armazenadas (modo cego / buffer)"""
        self.respond(msg.header.msg_serial, 0x0704)
        locations = msg.extra.get('locations', [])
        if not locations:
            return
        logger.info(f"[{self.phone}] Location batch: {len(locations)} posições")
        for loc in locations:
            self.last_location = loc
            payload = {
                'phone': self.phone,
                'lat': loc.latitude, 'lon': loc.longitude,
                'speed': loc.speed, 'direction': loc.direction,
                'altitude': loc.altitude,
                'timestamp': loc.timestamp.isoformat(),
                'alarms': loc.alarms, 'status': loc.status,
                'extras': loc.extras, 'source': 'batch',
            }
            await self.event_bus.publish('location', payload)
            if loc.alarms:
                logger.warning(f"[{self.phone}] BATCH ALARME: {loc.alarms}")
                await self.event_bus.publish('alarm', {**payload, 'alarm_list': loc.alarms})
                try:
                    if 'dsm_channel' in loc.extras:
                        snap_ch = loc.extras['dsm_channel']
                    elif 'adas_channel' in loc.extras:
                        snap_ch = loc.extras['adas_channel']
                    else:
                        snap_ch = 1
                    await self.capture_snapshot(channel=snap_ch)
                except Exception:
                    pass
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
        # For alarm-triggered captures the device announces via 0x0800 and
        # waits for a 0x8805 upload request before sending 0x0801 fragments.
        if ev.media_id > 0:
            self.send(cmd_upload_media(self._phone_bcd, self.next_serial(),
                                       media_id=ev.media_id, delete=0))
            logger.info(f"[{self.phone}] 0x8805 solicitado via media_event: media_id={ev.media_id}")

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
            logger.info(f"[{self.phone}] 0x0801 novo media_id={media_id} "
                        f"mtype={mtype} ch={channel} first_bytes={data[:8].hex()}")
        self._media_buffers[media_id].extend(data)

        buf = self._media_buffers[media_id]

        # JPEG markers: SOI = FFD8 at start, EOI = FFD9 anywhere in full buffer
        has_soi = len(buf) >= 2 and buf[0] == 0xFF and buf[1] == 0xD8
        # Check the entire accumulated buffer for EOI (not just current chunk —
        # the end marker may arrive in a chunk that is not the very last packet)
        has_eoi = b'\xFF\xD9' in buf

        if has_eoi and has_soi:
            logger.info(f"[{self.phone}] 0x0801 JPEG completo: media_id={media_id} "
                        f"mtype={mtype} buf_size={len(buf)}")

        # Detect JPEG by content regardless of declared mtype.
        # Some MDVR firmwares report media_type=0 (video) even when sending JPEG images.
        is_jpeg = has_soi and has_eoi

        if is_jpeg or (mtype == 'image' and has_eoi):
            raw = bytes(self._media_buffers.pop(media_id))
            logger.info(f"[{self.phone}] JPEG salvo via 0x0801: "
                        f"media_id={media_id} mtype={mtype} size={len(raw)}")
            await self._save_snapshot(raw, channel, media_id)
        elif mtype == 'video' and len(buf) > 4 * 1024 * 1024:
            raw = bytes(self._media_buffers.pop(media_id))
            await self._save_video_chunk(raw, channel, media_id)

    async def _on_camera_capture(self, msg: ParsedMessage):
        self.respond(msg.header.msg_serial, MSG_CAMERA_CAPTURE)
        ex = msg.extra
        logger.info(f"[{self.phone}] Snapshot efetuado! result={ex.get('result')} media_ids={ex.get('media_ids')}")
        
        # Pede explicitamente o envio da imagem recém capturada
        media_ids = ex.get('media_ids', [])
        for mid in media_ids:
            if mid > 0:
                self.send(cmd_upload_media(self._phone_bcd, self.next_serial(), media_id=mid, delete=0))

    async def _on_transparent_data(self, msg: ParsedMessage):
        self.respond(msg.header.msg_serial, MSG_TRANSPARENT_DATA)
        payload = {
            'phone': self.phone,
            'type': f"0x{msg.extra.get('transparent_type', 0):02X}",
            'data_hex': msg.extra.get('data_hex', ''),
            'timestamp': datetime.now().isoformat(),
        }
        await self.event_bus.publish('transparent_data', payload)
        self._save_event('transparent_data', payload)

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
