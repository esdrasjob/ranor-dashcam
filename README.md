# RANOR Dashcam Integration Server

Servidor de integração para dashcams MDVR com protocolo **JT/T 808-2013** e upload FTP de snapshots/vídeos.

## Arquitetura

```
Dashcam MDVR
   │
   ├── TCP :808  ─── JT808 Parser ─── Eventos / Posição / Alarmes ──→ API REST / WebSocket
   │
   └── FTP :9999 ─── FTP Server ──── Snapshots (JPG) / Vídeos (MP4) ──→ Storage
                                                              │
                                                  Dashboard http://host:8080
```

## Início rápido

### Python direto

```bash
pip install -r requirements.txt
python main.py
```

Dashboard: http://localhost:8888

### Docker

```bash
docker compose up --build
```

Dashboard: http://localhost:8080 (porta 8080 se usar docker-compose padrão)

## Configuração da dashcam

Editar `/config/jtconfig2013_9/jtconfig.json` e `/config/jtconfig2013_10/jtconfig.json` na dashcam:

```json
{
  "server": "IP_DO_SEU_SERVIDOR",
  "port": 808
}
```

Para o FTP (snapshots de alarmes), configurar no painel da dashcam:
- Host: `IP_DO_SEU_SERVIDOR`
- Port: `9999`
- User: `ranor` (qualquer usuário é aceito)
- Pass: `ranor`

## Portas

| Porta | Protocolo | Descrição |
|-------|-----------|-----------|
| 808   | TCP       | JT/T 808-2013 (telemetria + eventos) |
| 9999  | TCP/FTP   | Upload de snapshots e vídeos |
| 8080  | HTTP/WS   | Dashboard + API REST |

## API REST

| Endpoint | Descrição |
|----------|-----------|
| `GET /` | Dashboard HTML |
| `GET /api/devices` | Dispositivos conectados e última posição |
| `GET /api/events` | Histórico de eventos (posição, alarmes) |
| `GET /api/snapshots` | Lista de snapshots recebidos |
| `GET /api/snapshots/{filename}` | Download/visualização do snapshot |
| `GET /api/videos` | Lista de vídeos |
| `GET /api/stats` | Estatísticas gerais |
| `WS  /ws` | WebSocket para eventos em tempo real |

## Produção (EC2 / VPS)

Para usar a porta 808 sem root:

```bash
# Linux — conceder cap ao Python
sudo setcap 'cap_net_bind_service=+ep' $(which python3)

# Ou usar iptables redirect
sudo iptables -t nat -A PREROUTING -p tcp --dport 808 -j REDIRECT --to-port 8080
```

## Eventos suportados

| Tipo | Descrição |
|------|-----------|
| `device_connected` | Dashcam registrou |
| `device_disconnected` | Dashcam desconectou |
| `heartbeat` | Sinal de vida (keepalive) |
| `location` | Posição GPS + velocidade + direção |
| `alarm` | Alarme ativo (colisão, câmera obstruída, etc.) |
| `media_event` | Evento de mídia (início de gravação) |
| `snapshot` | Snapshot recebido via JT808 0x0801 |
| `file_received` | Arquivo recebido via FTP (JPG ou MP4) |
| `video_chunk` | Chunk de vídeo recebido |

## Estrutura de pastas

```
storage/
  snapshots/   ← JPGs da câmera (alarmes, capturas manuais)
  videos/      ← MP4 dos canais CH1/CH2/CH3
  events/      ← JSONL com histórico por dispositivo/dia
logs/
  server.log   ← Log do servidor
```
