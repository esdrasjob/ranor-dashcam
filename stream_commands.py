"""
Comandos JT808 para controle de streaming JT1078
O servidor envia esses comandos para a dashcam iniciar/parar o stream.
"""

import struct
import logging

logger = logging.getLogger("ranor.stream_cmd")


def build_frame(msg_id: int, phone_bcd: bytes, serial: int, body: bytes) -> bytes:
    """Build JT808 frame"""
    def escape(data):
        r = bytearray()
        for b in data:
            if b == 0x7D: r.extend([0x7D, 0x01])
            elif b == 0x7E: r.extend([0x7D, 0x02])
            else: r.append(b)
        return bytes(r)

    def checksum(data):
        cs = 0
        for b in data: cs ^= b
        return cs

    props = len(body) & 0x03FF
    header = struct.pack('>HH', msg_id, props) + phone_bcd + struct.pack('>H', serial)
    payload = header + body
    cs = checksum(payload)
    return b'\x7E' + escape(payload + bytes([cs])) + b'\x7E'


def cmd_start_stream(phone_bcd: bytes, serial: int,
                     server_ip: str, server_port: int,
                     channel: int = 1,
                     stream_type: int = 0,      # 0=main, 1=sub
                     data_type: int = 0) -> bytes:  # 0=audio+video, 1=video, 2=duplex
    """
    0x9101 — 实时音视频传输请求 (Real-time A/V stream request)
    Manda a dashcam conectar no servidor JT1078 e iniciar stream.

    Params:
        server_ip:    IP do servidor 1078 (string, ex: "192.168.1.10")
        server_port:  Porta 1078
        channel:      Canal da câmera (1=frontal, 2=motorista, 3=traseira)
        stream_type:  0=main stream, 1=sub stream
        data_type:    0=áudio+vídeo, 1=só vídeo, 2=duplex
    """
    ip_bytes = server_ip.encode('ascii')
    ip_len = len(ip_bytes)

    body = bytes([ip_len]) + ip_bytes
    body += struct.pack('>H', server_port)   # TCP port
    body += struct.pack('>H', 0)             # UDP port (not used, but required by JT1078 spec)
    body += bytes([channel, data_type, stream_type])

    logger.info(f"CMD 0x9101: canal={channel} stream_type={stream_type} "
                f"→ {server_ip}:{server_port}")
    return build_frame(0x9101, phone_bcd, serial, body)


def cmd_stop_stream(phone_bcd: bytes, serial: int, channel: int = 1) -> bytes:
    """
    0x9102 — 实时音视频传输控制 (Stream control)
    Para o stream de um canal.
    control: 0=关闭(stop), 1=暂停(pause), 2=恢复(resume), 3=关闭所有(stop all)
    """
    body = bytes([channel, 0])  # channel, control=stop
    logger.info(f"CMD 0x9102 STOP: canal={channel}")
    return build_frame(0x9102, phone_bcd, serial, body)


def cmd_capture_snapshot(phone_bcd: bytes, serial: int,
                         channel: int = 1,
                         count: int = 1,
                         interval: int = 0,
                         save_flag: int = 0, # 0=Realtime Upload, 1=Save Local
                         resolution: int = 1, 
                         quality: int = 5) -> bytes:
    """
    0x8801 — Comando de captura de foto (Camera Capture Command)
    """
    # 1:Channel, 2:Count, 2:Interval, 1:SaveFlag, 1:Res, 1:Quality, 1:Brightness, 1:Contrast, 1:Saturation, 1:Chroma = 12 bytes
    body = struct.pack(
        '>BHHBBBBBBB',
        channel, count, interval, save_flag, resolution, quality, 0, 0, 0, 0
    )
    logger.info(f"CMD 0x8801 CAPTURE: canal={channel}")
    return build_frame(0x8801, phone_bcd, serial, body)


def cmd_query_stream_params(phone_bcd: bytes, serial: int) -> bytes:
    """0x9003 — Query terminal A/V properties"""
    return build_frame(0x9003, phone_bcd, serial, b'')

def cmd_upload_media(phone_bcd: bytes, serial: int, media_id: int, delete: int = 0) -> bytes:
    """
    0x8805 — 单条存储多媒体数据检索上传命令 (Single Media Upload Command)
    Pede para a câmera enviar uma mídia salva através do media_id.
    """
    body = struct.pack('>IB', media_id, delete)
    logger.info(f"CMD 0x8805 UPLOAD: media_id={media_id}")
    return build_frame(0x8805, phone_bcd, serial, body)
