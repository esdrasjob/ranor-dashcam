"""
JT/T 808-2013 Protocol Parser
Decodifica mensagens binárias da dashcam MDVR
"""

import struct
import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

logger = logging.getLogger("jt808.parser")

# Message IDs
MSG_TERMINAL_REGISTER     = 0x0100
MSG_TERMINAL_AUTH         = 0x0102
MSG_TERMINAL_GENERAL_RESP = 0x0001
MSG_HEARTBEAT             = 0x0002
MSG_LOCATION_REPORT       = 0x0200
MSG_LOCATION_BATCH        = 0x0704
MSG_MEDIA_EVENT           = 0x0800
MSG_MEDIA_UPLOAD          = 0x0801
MSG_CAMERA_CAPTURE        = 0x0805
MSG_TRANSPARENT_DATA      = 0x0900
MSG_MEDIA_DATA            = 0x0A00  # JT1078 real-time stream
MSG_PLATFORM_GENERAL_RESP = 0x8001
MSG_REGISTER_RESP         = 0x8100
MSG_SET_PARAMS            = 0x8103
MSG_QUERY_PARAMS          = 0x8104
MSG_TERMINAL_CTRL         = 0x8105
MSG_CAPTURE_CMD           = 0x8801  # Platform -> device: capture photo

# Alarm flags (0x0200 bit mask)
ALARM_FLAGS = {
    0:  "Emergência",
    1:  "Excesso de velocidade",
    2:  "Fadiga ao volante",
    3:  "Risco iminente de colisão",
    4:  "Mudança de faixa",
    5:  "Desvio de rota",
    6:  "Sensor de combustível anormal",
    7:  "Roubo de veículo",
    8:  "Acidente de trânsito",
    9:  "Falha em componente",
    10: "Temperatura anormal",
    11: "Colisão (acelerômetro)",
    18: "Câmera obstruída",
    19: "Motorista anormal (IA)",
    20: "Excesso de velocidade em área",
    21: "Entrada em área proibida",
    22: "Saída de área",
}

# Status flags
STATUS_FLAGS = {
    0: "ACC ligado",
    1: "Posicionado",
    2: "Latitude Sul",
    3: "Longitude Oeste",
    5: "Em movimento",
    8: "Porta aberta",
}


def unescape(data: bytes) -> bytes:
    """JT808 byte stuffing removal: 0x7D 0x01 -> 0x7D, 0x7D 0x02 -> 0x7E"""
    result = bytearray()
    i = 0
    while i < len(data):
        if data[i] == 0x7D and i + 1 < len(data):
            if data[i + 1] == 0x01:
                result.append(0x7D)
                i += 2
                continue
            elif data[i + 1] == 0x02:
                result.append(0x7E)
                i += 2
                continue
        result.append(data[i])
        i += 1
    return bytes(result)


def checksum(data: bytes) -> int:
    """XOR checksum over all bytes"""
    cs = 0
    for b in data:
        cs ^= b
    return cs


def bcd_to_str(data: bytes) -> str:
    """BCD encoded phone number to string"""
    return ''.join(f'{b:02X}' for b in data).lstrip('0') or '0'


@dataclass
class JT808Header:
    msg_id: int
    msg_len: int
    phone: str          # 6 bytes BCD
    msg_serial: int
    fragment_total: int = 1
    fragment_index: int = 0
    is_fragmented: bool = False


@dataclass
class LocationInfo:
    alarm_flags: int
    status_flags: int
    latitude: float
    longitude: float
    altitude: int       # meters
    speed: float        # km/h
    direction: int      # degrees 0-359
    timestamp: datetime
    alarms: list = field(default_factory=list)
    status: list = field(default_factory=list)
    extras: dict = field(default_factory=dict)


@dataclass
class MediaEventInfo:
    media_id: int
    media_type: int     # 0=video, 1=audio, 2=image
    media_encoding: int
    event_code: int
    channel_id: int


@dataclass
class ParsedMessage:
    header: JT808Header
    msg_type: str
    raw_body: bytes
    location: Optional[LocationInfo] = None
    media_event: Optional[MediaEventInfo] = None
    extra: dict = field(default_factory=dict)


class JT808Parser:
    def __init__(self):
        self._buffer = bytearray()
        # Reassembly buffer for fragmented JT808 messages.
        # Key: (phone, msg_serial) — all fragments of the same message share the same serial.
        # Value: dict with accumulated body, header info, and fragment count.
        self._frag_buffers: dict = {}

    def feed(self, data: bytes) -> list:
        """Feed raw TCP bytes, returns list of parsed messages"""
        self._buffer.extend(data)
        messages = []

        while True:
            # Find frame start 0x7E
            start = self._buffer.find(0x7E)
            if start == -1:
                self._buffer.clear()
                break
            if start > 0:
                self._buffer = self._buffer[start:]

            # Find frame end 0x7E (after position 1)
            end = self._buffer.find(0x7E, 1)
            if end == -1:
                break  # incomplete frame

            # Extract frame (without delimiters)
            frame = bytes(self._buffer[1:end])
            self._buffer = self._buffer[end:]  # keep from next 0x7E

            try:
                msg = self._parse_frame(frame)
                if msg:
                    messages.append(msg)
            except Exception as e:
                logger.warning(f"Frame parse error: {e} | raw={frame.hex()[:60]}")

        return messages

    def _parse_frame(self, frame: bytes) -> Optional[ParsedMessage]:
        """Parse a single unescaped frame, reassembling fragments as needed."""
        frame = unescape(frame)

        if len(frame) < 12:
            return None

        # Verify checksum (last byte)
        cs_received = frame[-1]
        cs_computed = checksum(frame[:-1])
        if cs_received != cs_computed:
            logger.debug(f"Checksum mismatch: got {cs_received:02X} expected {cs_computed:02X}")
            # Continue anyway — some devices have quirks

        # Parse header
        msg_id = struct.unpack_from('>H', frame, 0)[0]
        props  = struct.unpack_from('>H', frame, 2)[0]
        phone  = bcd_to_str(frame[4:10])
        serial = struct.unpack_from('>H', frame, 10)[0]

        msg_len = props & 0x03FF
        is_frag = bool(props & 0x2000)

        offset = 12
        frag_total = 1
        frag_index = 0
        if is_frag:
            frag_total = struct.unpack_from('>H', frame, offset)[0]
            frag_index = struct.unpack_from('>H', frame, offset + 2)[0]
            offset += 4

        body = frame[offset:-1]  # strip checksum

        if msg_id == MSG_MEDIA_UPLOAD:
            logger.debug(f"RAW 0x0801: props=0x{props:04X} is_frag={is_frag} "
                         f"serial={serial} frag={frag_index}/{frag_total} "
                         f"body_head={body[:8].hex()}")

        header = JT808Header(
            msg_id=msg_id, msg_len=msg_len,
            phone=phone, msg_serial=serial,
            fragment_total=frag_total, fragment_index=frag_index,
            is_fragmented=is_frag,
        )

        if not is_frag:
            return self._dispatch(header, body)

        # ── Fragment reassembly ───────────────────────────────────────────────
        # 0x0801 (MSG_MEDIA_UPLOAD): firmware sends frag_index=1..N with a UNIQUE
        # serial per fragment. Only frag_index=1 carries the 36-byte media header;
        # subsequent fragments are raw continuation data. Reassemble by
        # (phone, 'media_upload') key, resetting on each new frag_index=1.
        if msg_id == MSG_MEDIA_UPLOAD:
            media_key = (phone, 'media_upload')
            if frag_index == 1:
                self._frag_buffers[media_key] = {
                    'total': frag_total,
                    'received': 1,
                    'body': bytearray(body),
                    'serial': serial,
                }
                if frag_total == 1:
                    full = bytes(self._frag_buffers.pop(media_key)['body'])
                    h = JT808Header(msg_id=msg_id, msg_len=len(full), phone=phone,
                                    msg_serial=serial, fragment_total=1,
                                    fragment_index=1, is_fragmented=True)
                    return self._dispatch(h, full)
                return None
            fb = self._frag_buffers.get(media_key)
            if fb is None or fb['total'] != frag_total:
                logger.debug(f"0x0801 orphan fragment: phone={phone} "
                             f"frag={frag_index}/{frag_total} (missed frag_index=1?)")
                return None
            fb['body'].extend(body)
            fb['received'] += 1
            if fb['received'] >= fb['total']:
                full_body = bytes(self._frag_buffers.pop(media_key)['body'])
                logger.info(f"0x0801 reassembly complete: phone={phone} "
                            f"frags={frag_total} total={len(full_body)}B")
                h = JT808Header(msg_id=msg_id, msg_len=len(full_body), phone=phone,
                                msg_serial=fb['serial'], fragment_total=frag_total,
                                fragment_index=frag_total, is_fragmented=True)
                return self._dispatch(h, full_body)
            return None

        key = (phone, serial)
        if key not in self._frag_buffers:
            self._frag_buffers[key] = {
                'msg_id': msg_id,
                'total': frag_total,
                'received': 0,
                'body': bytearray(),
            }
            logger.debug(f"Frag start: phone={phone} serial={serial} "
                         f"msg_id=0x{msg_id:04X} total={frag_total}")

        fb = self._frag_buffers[key]
        fb['body'].extend(body)
        fb['received'] += 1

        if fb['received'] < fb['total']:
            return None  # Still waiting for more fragments

        # All fragments received — assemble and dispatch
        full_body = bytes(self._frag_buffers.pop(key)['body'])
        logger.debug(f"Frag complete: phone={phone} serial={serial} "
                     f"msg_id=0x{msg_id:04X} size={len(full_body)}")
        header = JT808Header(
            msg_id=msg_id, msg_len=len(full_body),
            phone=phone, msg_serial=serial,
            fragment_total=frag_total, fragment_index=frag_index,
            is_fragmented=True,
        )
        return self._dispatch(header, full_body)

    def _dispatch(self, header: JT808Header, body: bytes) -> ParsedMessage:
        handlers = {
            MSG_TERMINAL_REGISTER:    self._parse_register,
            MSG_TERMINAL_AUTH:        self._parse_auth,
            MSG_TERMINAL_GENERAL_RESP: self._parse_terminal_resp,
            MSG_HEARTBEAT:            self._parse_heartbeat,
            MSG_LOCATION_REPORT:   self._parse_location,
            MSG_LOCATION_BATCH:    self._parse_location_batch,
            MSG_MEDIA_EVENT:       self._parse_media_event,
            MSG_MEDIA_UPLOAD:      self._parse_media_upload,
            MSG_CAMERA_CAPTURE:    self._parse_camera_capture,
            MSG_TRANSPARENT_DATA:  self._parse_transparent_data,
        }
        handler = handlers.get(header.msg_id, self._parse_unknown)
        return handler(header, body)

    # ── Specific parsers ──────────────────────────────────────────────────────

    def _parse_register(self, header, body) -> ParsedMessage:
        if len(body) < 37:
            return ParsedMessage(header=header, msg_type="register", raw_body=body)
        province   = struct.unpack_from('>H', body, 0)[0]
        city       = struct.unpack_from('>H', body, 2)[0]
        maker_id   = body[4:9].decode('ascii', errors='replace').strip('\x00')
        model      = body[9:29].decode('ascii', errors='replace').strip('\x00')
        device_id  = body[29:36].decode('ascii', errors='replace').strip('\x00')
        plate_color = body[36]
        plate = body[37:].decode('gbk', errors='replace') if len(body) > 37 else ''
        return ParsedMessage(
            header=header, msg_type="register", raw_body=body,
            extra={
                'province': province, 'city': city,
                'maker_id': maker_id, 'model': model,
                'device_id': device_id, 'plate': plate,
                'plate_color': plate_color,
            }
        )

    def _parse_auth(self, header, body) -> ParsedMessage:
        token = body.decode('ascii', errors='replace').strip('\x00')
        return ParsedMessage(header=header, msg_type="auth", raw_body=body,
                             extra={'token': token})

    def _parse_heartbeat(self, header, body) -> ParsedMessage:
        return ParsedMessage(header=header, msg_type="heartbeat", raw_body=body)

    def _parse_location(self, header, body) -> ParsedMessage:
        if len(body) < 28:
            return ParsedMessage(header=header, msg_type="location", raw_body=body)
        alarm  = struct.unpack_from('>I', body, 0)[0]
        status = struct.unpack_from('>I', body, 4)[0]
        lat    = struct.unpack_from('>I', body, 8)[0]  / 1e6
        lon    = struct.unpack_from('>I', body, 12)[0] / 1e6
        alt    = struct.unpack_from('>H', body, 16)[0]
        spd    = struct.unpack_from('>H', body, 18)[0] / 10.0
        dire   = struct.unpack_from('>H', body, 20)[0]

        # Timestamp BCD: YY MM DD HH mm SS
        ts_raw = body[22:28]
        try:
            ts = datetime(
                2000 + int(f'{ts_raw[0]:02X}'),
                int(f'{ts_raw[1]:02X}'),
                int(f'{ts_raw[2]:02X}'),
                int(f'{ts_raw[3]:02X}'),
                int(f'{ts_raw[4]:02X}'),
                int(f'{ts_raw[5]:02X}'),
            )
        except Exception:
            ts = datetime.utcnow()

        # South lat / West lon
        if status & (1 << 2):
            lat = -lat
        if status & (1 << 3):
            lon = -lon

        active_alarms = [ALARM_FLAGS[i] for i in range(32) if alarm & (1 << i) and i in ALARM_FLAGS]
        active_status = [STATUS_FLAGS[i] for i in range(32) if status & (1 << i) and i in STATUS_FLAGS]

        # Extra info elements
        extras = {}
        offset = 28
        while offset + 2 <= len(body):
            tag = body[offset]
            length = body[offset + 1]
            val = body[offset + 2:offset + 2 + length]
            offset += 2 + length
            if tag == 0x01:   extras['mileage'] = struct.unpack('>I', val)[0] / 10.0
            elif tag == 0x02: extras['fuel'] = struct.unpack('>H', val)[0] / 10.0
            elif tag == 0x30: extras['rssi'] = val[0]
            elif tag == 0x31: extras['gnss_count'] = val[0]
            elif tag == 0x64:
                # ADAS alarms → front camera = CH1
                if len(val) >= 6:
                    alarm_type = val[5]
                    types = {
                        1: "Colisão Frontal (FCW)", 2: "Desvio de Faixa (LDW)",
                        3: "Distância Perigosa (HMW)", 4: "Colisão com Pedestre",
                        5: "Mudança Freq. de Faixa", 6: "Obstáculo",
                        16: "Frenagem Brusca", 17: "Aceleração Brusca",
                        18: "Curva Brusca",
                    }
                    active_alarms.append(types.get(alarm_type, f"ADAS Tipo {alarm_type}"))
                else:
                    active_alarms.append("Alarme ADAS")
                extras['adas_channel'] = 1
                extras['adas_raw'] = val.hex()
            elif tag == 0x65:
                # DSM alarms → driver camera = CH2
                if len(val) >= 6:
                    alarm_type = val[5]
                    types = {
                        1: "Fadiga (Olhos fechados)", 2: "Uso de Celular",
                        3: "Fumando", 4: "Distração (Olhando lado)",
                        5: "Motorista Ausente", 6: "Sem Cinto",
                        7: "Bocejo", 8: "Cabeça Baixa",
                        9: "Sem Máscara", 10: "Câmera Obstruída",
                    }
                    active_alarms.append(types.get(alarm_type, f"DSM Tipo {alarm_type}"))
                else:
                    active_alarms.append("Alarme DSM (Fadiga/Distração)")
                extras['dsm_channel'] = 2
                extras['dsm_raw'] = val.hex()
            elif tag == 0x66:
                active_alarms.append("Alarme TPMS")
                extras['tpms_raw'] = val.hex()
            elif tag == 0x67:
                active_alarms.append("Alarme BSD (Ponto Cego)")
                extras['bsd_raw'] = val.hex()
            else:
                extras[f'tag_0x{tag:02X}'] = val.hex()

        loc = LocationInfo(
            alarm_flags=alarm, status_flags=status,
            latitude=lat, longitude=lon,
            altitude=alt, speed=spd, direction=dire,
            timestamp=ts, alarms=active_alarms,
            status=active_status, extras=extras
        )
        return ParsedMessage(header=header, msg_type="location", raw_body=body, location=loc)

    def _parse_location_batch(self, header, body) -> ParsedMessage:
        """0x0704 — batch of location reports, each prefixed with 2-byte length"""
        if len(body) < 3:
            return ParsedMessage(header=header, msg_type="location_batch", raw_body=body)
        count = struct.unpack_from('>H', body, 0)[0]
        item_type = body[2]  # 0=normal, 1=blind
        offset = 3
        locations = []
        for _ in range(count):
            if offset + 2 > len(body):
                break
            item_len = struct.unpack_from('>H', body, offset)[0]
            offset += 2
            if offset + item_len > len(body):
                break
            loc_body = body[offset:offset + item_len]
            offset += item_len
            # Parse each location using the standard location parser
            try:
                loc_msg = self._parse_location(header, loc_body)
                if loc_msg.location:
                    locations.append(loc_msg.location)
            except Exception:
                pass
        return ParsedMessage(header=header, msg_type="location_batch", raw_body=body,
                             extra={'count': count, 'item_type': item_type,
                                    'locations': locations})

    def _parse_media_event(self, header, body) -> ParsedMessage:
        if len(body) < 5:
            return ParsedMessage(header=header, msg_type="media_event", raw_body=body)
        media_id  = struct.unpack_from('>I', body, 0)[0]
        media_type = body[4]
        encoding   = body[5] if len(body) > 5 else 0
        event_code = body[6] if len(body) > 6 else 0
        channel    = body[7] if len(body) > 7 else 0
        ev = MediaEventInfo(
            media_id=media_id, media_type=media_type,
            media_encoding=encoding, event_code=event_code,
            channel_id=channel
        )
        MEDIA_TYPE = {0: "Vídeo", 1: "Áudio", 2: "Imagem"}
        EVENT_CODE = {0: "Plataforma", 1: "Timer", 2: "Alarme"}
        return ParsedMessage(
            header=header, msg_type="media_event", raw_body=body,
            media_event=ev,
            extra={
                'type_label': MEDIA_TYPE.get(media_type, f"Tipo {media_type}"),
                'event_label': EVENT_CODE.get(event_code, f"Evento {event_code}"),
                'channel': channel,
            }
        )

    def _parse_media_upload(self, header, body) -> ParsedMessage:
        """0x0801 - Media data upload (image/video chunk)"""
        if len(body) < 36:
            return ParsedMessage(header=header, msg_type="media_upload", raw_body=body)
        media_id   = struct.unpack_from('>I', body, 0)[0]
        media_type = body[4]   # 0=video 1=audio 2=image
        encoding   = body[5]
        channel    = body[6]
        event_code = body[7]
        # Location embedded (28 bytes)
        loc_body   = body[8:36]
        media_data = body[36:]
        MEDIA_TYPE = {0: "video", 1: "audio", 2: "image"}
        return ParsedMessage(
            header=header, msg_type="media_upload", raw_body=body,
            extra={
                'media_id': media_id,
                'media_type': MEDIA_TYPE.get(media_type, 'unknown'),
                'encoding': encoding,
                'channel': channel,
                'event_code': event_code,
                'data': media_data,
                'data_size': len(media_data),
            }
        )

    def _parse_transparent_data(self, header, body) -> ParsedMessage:
        if len(body) < 1:
            return ParsedMessage(header=header, msg_type="transparent_data", raw_body=body)
        msg_type = body[0]
        data = body[1:]
        return ParsedMessage(
            header=header, msg_type="transparent_data", raw_body=body,
            extra={
                'transparent_type': msg_type,
                'data_hex': data.hex(),
            }
        )

    def _parse_camera_capture(self, header, body) -> ParsedMessage:
        if len(body) < 3:
            return ParsedMessage(header=header, msg_type="camera_capture", raw_body=body)
        reply_serial = struct.unpack_from('>H', body, 0)[0]
        result = body[2]
        media_count = struct.unpack_from('>H', body, 3)[0] if len(body) >= 5 else 0
        media_ids = []
        if len(body) >= 5 + (media_count * 4):
            offset = 5
            for _ in range(media_count):
                media_ids.append(struct.unpack_from('>I', body, offset)[0])
                offset += 4
        return ParsedMessage(
            header=header, msg_type="camera_capture", raw_body=body,
            extra={'reply_serial': reply_serial, 'result': result, 'media_count': media_count, 'media_ids': media_ids}
        )

    def _parse_terminal_resp(self, header, body) -> ParsedMessage:
        """0x0001 - Terminal general response (device ACK of platform commands)"""
        resp_serial = struct.unpack_from('>H', body, 0)[0] if len(body) >= 2 else 0
        resp_msg_id = struct.unpack_from('>H', body, 2)[0] if len(body) >= 4 else 0
        result      = body[4] if len(body) >= 5 else 0
        RESULTS = {0: "OK", 1: "Falha", 2: "Msg inválida", 3: "Não suportado"}
        logger.debug(f"Terminal ACK: serial={resp_serial} msg=0x{resp_msg_id:04X} "
                     f"result={RESULTS.get(result, result)}")
        return ParsedMessage(header=header, msg_type="terminal_resp", raw_body=body,
                             extra={'resp_serial': resp_serial, 'resp_msg_id': resp_msg_id,
                                    'result': result})

    def _parse_unknown(self, header, body) -> ParsedMessage:
        return ParsedMessage(
            header=header,
            msg_type=f"unknown_0x{header.msg_id:04X}",
            raw_body=body
        )


# ── Response builders ──────────────────────────────────────────────────────────

def escape(data: bytes) -> bytes:
    """JT808 byte stuffing: 0x7D -> 0x7D 0x01, 0x7E -> 0x7D 0x02"""
    result = bytearray()
    for b in data:
        if b == 0x7D:
            result.extend([0x7D, 0x01])
        elif b == 0x7E:
            result.extend([0x7D, 0x02])
        else:
            result.append(b)
    return bytes(result)


def build_response(phone_bcd: bytes, serial: int, resp_serial: int,
                   resp_msg_id: int, result: int = 0) -> bytes:
    """Build 0x8001 general platform response"""
    msg_id = MSG_PLATFORM_GENERAL_RESP
    body = struct.pack('>HHB', resp_serial, resp_msg_id, result)
    return _build_frame(msg_id, phone_bcd, serial, body)


def build_register_response(phone_bcd: bytes, serial: int,
                             resp_serial: int, result: int = 0,
                             token: str = "RANOR01") -> bytes:
    """Build 0x8100 register response"""
    token_bytes = token.encode('ascii')
    body = struct.pack('>HB', resp_serial, result) + token_bytes
    return _build_frame(MSG_REGISTER_RESP, phone_bcd, serial, body)


def _build_frame(msg_id: int, phone_bcd: bytes, serial: int, body: bytes) -> bytes:
    """Assemble a complete JT808 frame"""
    props = len(body) & 0x03FF
    header = struct.pack('>HH', msg_id, props) + phone_bcd + struct.pack('>H', serial)
    payload = header + body
    cs = checksum(payload)
    inner = escape(payload + bytes([cs]))
    return b'\x7E' + inner + b'\x7E'
