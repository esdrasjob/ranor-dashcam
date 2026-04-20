"""
FTP Server simples para receber snapshots e vídeos da dashcam
A dashcam conecta em porta 9999 e faz upload de CH2IMG*.jpg e MP4
"""

import asyncio
import os
import logging
import time
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("ranor.ftp")


class FTPSession(asyncio.Protocol):
    """Minimal FTP server session handler"""

    def __init__(self, storage_path: Path, event_bus):
        self.storage = storage_path
        self.event_bus = event_bus
        self.transport = None
        self.data_transport = None
        self.data_protocol = None
        self.data_host = None
        self.data_port = None
        self.passive_server = None
        self.passive_port = None
        self.cwd = "/"
        self.username = None
        self.authenticated = False
        self.rename_from = None
        self._current_file = None
        self._current_filename = None
        self._recv_buffer = b""

    def connection_made(self, transport):
        self.transport = transport
        peer = transport.get_extra_info('peername')
        logger.info(f"FTP connection from {peer}")
        self._send("220 RANOR FTP Server Ready")

    def connection_lost(self, exc):
        if self._current_file:
            self._current_file.close()
        logger.debug("FTP connection closed")

    def data_received(self, data):
        self._recv_buffer += data
        while b'\r\n' in self._recv_buffer:
            line, self._recv_buffer = self._recv_buffer.split(b'\r\n', 1)
            self._handle_command(line.decode('utf-8', errors='replace').strip())

    def _send(self, msg: str):
        self.transport.write((msg + '\r\n').encode())

    def _handle_command(self, line: str):
        parts = line.split(' ', 1)
        cmd = parts[0].upper()
        arg = parts[1] if len(parts) > 1 else ''
        logger.debug(f"FTP CMD: {cmd} {arg[:40]}")

        handlers = {
            'USER': self._cmd_user,
            'PASS': self._cmd_pass,
            'SYST': self._cmd_syst,
            'TYPE': self._cmd_type,
            'PWD':  self._cmd_pwd,
            'CWD':  self._cmd_cwd,
            'PASV': self._cmd_pasv,
            'PORT': self._cmd_port,
            'LIST': self._cmd_list,
            'STOR': self._cmd_stor,
            'RETR': self._cmd_retr,
            'DELE': self._cmd_dele,
            'MKD':  self._cmd_mkd,
            'RNFR': self._cmd_rnfr,
            'RNTO': self._cmd_rnto,
            'SIZE': self._cmd_size,
            'MDTM': self._cmd_mdtm,
            'QUIT': self._cmd_quit,
            'NOOP': lambda a: self._send("200 OK"),
            'FEAT': lambda a: self._send("211 No features"),
            'OPTS': lambda a: self._send("200 OK"),
        }
        handler = handlers.get(cmd)
        if handler:
            handler(arg)
        else:
            self._send(f"502 Command {cmd} not implemented")

    def _cmd_user(self, arg):
        self.username = arg
        self._send("331 Password required")

    def _cmd_pass(self, arg):
        # Accept any credentials from dashcam
        self.authenticated = True
        logger.info(f"FTP login: user={self.username}")
        self._send("230 Login successful")

    def _cmd_syst(self, arg):
        self._send("215 UNIX Type: L8")

    def _cmd_type(self, arg):
        self._send("200 Type set")

    def _cmd_pwd(self, arg):
        self._send(f'257 "{self.cwd}" is current directory')

    def _cmd_cwd(self, arg):
        self.cwd = arg if arg.startswith('/') else f"{self.cwd}/{arg}"
        self.cwd = self.cwd.replace('//', '/')
        self._send("250 Directory changed")

    def _cmd_mkd(self, arg):
        self._send(f'257 "{arg}" created')

    def _cmd_dele(self, arg):
        self._send("250 Deleted")

    def _cmd_rnfr(self, arg):
        self.rename_from = arg
        self._send("350 Ready for RNTO")

    def _cmd_rnto(self, arg):
        self._send("250 Renamed")

    def _cmd_size(self, arg):
        self._send("213 0")

    def _cmd_mdtm(self, arg):
        self._send("213 20230101000000")

    def _cmd_quit(self, arg):
        self._send("221 Goodbye")
        self.transport.close()

    def _cmd_list(self, arg):
        self._send("150 Directory listing")
        if self.data_transport:
            self.data_transport.write(b"")
            self.data_transport.close()
            self.data_transport = None
        self._send("226 Transfer complete")

    def _cmd_pasv(self, arg):
        """Passive mode - open data port"""
        loop = asyncio.get_event_loop()
        self.passive_port = self._find_free_port()
        
        async def start_passive():
            server = await loop.create_server(
                lambda: PassiveDataProtocol(self),
                '0.0.0.0', self.passive_port
            )
            self.passive_server = server

        loop.create_task(start_passive())
        
        # Tell client our IP and port
        local_ip = self.transport.get_extra_info('sockname')[0]
        if local_ip == '::1' or ':' in local_ip:
            local_ip = '127,0,0,1'
        else:
            local_ip = local_ip.replace('.', ',')
        
        p1, p2 = self.passive_port >> 8, self.passive_port & 0xFF
        self._send(f"227 Entering Passive Mode ({local_ip},{p1},{p2})")

    def _cmd_port(self, arg):
        """Active mode"""
        parts = arg.split(',')
        if len(parts) >= 6:
            self.data_host = '.'.join(parts[:4])
            self.data_port = int(parts[4]) * 256 + int(parts[5])
        self._send("200 PORT command successful")

    def _cmd_stor(self, arg):
        """Receive file from dashcam"""
        filename = os.path.basename(arg)
        self._current_filename = filename

        # Determine storage subfolder
        if filename.lower().endswith('.jpg') or filename.lower().endswith('.jpeg'):
            dest_dir = self.storage / 'snapshots'
        elif filename.lower().endswith('.mp4') or filename.lower().endswith('.avi'):
            dest_dir = self.storage / 'videos'
        else:
            dest_dir = self.storage / 'events'

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / filename

        self._current_file = open(dest_path, 'wb')
        self._current_dest = dest_path
        self._send("150 Ready to receive data")
        logger.info(f"FTP STOR: receiving {filename} -> {dest_path}")

    def _cmd_retr(self, arg):
        self._send("550 File not available")

    def file_received_complete(self, filepath: Path, size: int):
        """Called by PassiveDataProtocol when transfer completes"""
        logger.info(f"FTP: received {filepath.name} ({size} bytes)")
        if self.event_bus:
            asyncio.get_event_loop().create_task(
                self.event_bus.publish('file_received', {
                    'filename': filepath.name,
                    'path': str(filepath),
                    'size': size,
                    'timestamp': datetime.now().isoformat(),
                    'type': 'snapshot' if filepath.suffix.lower() in ('.jpg', '.jpeg') else 'video',
                })
            )
        self._send("226 Transfer complete")

    @staticmethod
    def _find_free_port() -> int:
        import socket
        with socket.socket() as s:
            s.bind(('', 0))
            return s.getsockname()[1]


class PassiveDataProtocol(asyncio.Protocol):
    """Handles the data connection in PASV mode"""

    def __init__(self, session: FTPSession):
        self.session = session
        self.transport = None
        self._bytes_received = 0

    def connection_made(self, transport):
        self.transport = transport
        self.session.data_transport = transport

    def data_received(self, data):
        if self.session._current_file:
            self.session._current_file.write(data)
            self._bytes_received += len(data)

    def connection_lost(self, exc):
        if self.session._current_file:
            self.session._current_file.close()
            self.session._current_file = None
            filepath = self.session._current_dest
            self.session.file_received_complete(filepath, self._bytes_received)
        self.session.data_transport = None
        if self.session.passive_server:
            self.session.passive_server.close()
            self.session.passive_server = None


class FTPServer:
    def __init__(self, storage_path: Path, event_bus, host='0.0.0.0', port=9999):
        self.storage = storage_path
        self.event_bus = event_bus
        self.host = host
        self.port = port
        self._server = None

    async def start(self):
        loop = asyncio.get_event_loop()
        self._server = await loop.create_server(
            lambda: FTPSession(self.storage, self.event_bus),
            self.host, self.port
        )
        logger.info(f"FTP server listening on {self.host}:{self.port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
