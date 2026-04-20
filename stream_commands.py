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
                     data_type: int = 0,         # 0=audio+video, 1=video, 2=duplex
                     codec_type: int = 0) -> bytes:
    """
    0x9101 — 实时音视频传输请求 (Real-time A/V stream request)
    Manda a dashcam conectar no servidor JT1078 e iniciar stream.

    Params:
        server_ip:    IP do servidor 1078 (string, ex: "192.168.1.10")
        server_port:  Porta 1078
        channel:      Canal da câmera (1=frontal, 2=motorista, 3=traseira)
        stream_type:  0=main stream, 1=sub stream
        data_type:    0=áudio+vídeo, 1=só vídeo, 2=duplex
        codec_type:   0=不指定, 1=G.711A, 2=G.711U, 3=G.726, 4=G.729A, 96=H264
    """
    ip_bytes = server_ip.encode('ascii')
    ip_len = len(ip_bytes)

    body = bytes([ip_len]) + ip_bytes
    body += struct.pack('>H', server_port)
    body += bytes([channel, data_type, stream_type, codec_type])

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
                         channel: int = 0xFF,   # 0xFF = todos os canais
                         resolution: int = 0,    # 0=最高, 1=1080p, 2=720p, 3=D1
                         interval: int = 0,
                         quality: int = 1) -> bytes:
    """
    0x8801 — 存储多媒体数据检索 / 拍摄命令
    Solicita captura de snapshot em todos os canais.
    """
    body = bytes([channel, resolution, quality, interval & 0xFF, (interval >> 8) & 0xFF])
    logger.info(f"CMD 0x8801 CAPTURE: canal={channel}")
    return build_frame(0x8801, phone_bcd, serial, body)


def cmd_query_stream_params(phone_bcd: bytes, serial: int) -> bytes:
    """0x9003 — Query terminal A/V properties"""
    return build_frame(0x9003, phone_bcd, serial, b'')
