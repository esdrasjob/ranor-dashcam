"""
API REST + WebSocket + Dashboard Web
Acesso: http://localhost:8080
"""

import asyncio
import json
import os
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("ranor.api")

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RANOR — Dashcam Monitor</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --surface2: #22263a;
    --accent: #00d4ff; --accent2: #7c3aed; --warn: #f59e0b;
    --danger: #ef4444; --ok: #22c55e; --text: #e2e8f0; --muted: #64748b;
    --border: #2d3348; --radius: 10px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; min-height: 100vh; }
  
  header {
    background: var(--surface); border-bottom: 1px solid var(--border);
    padding: 16px 24px; display: flex; align-items: center; gap: 16px;
    position: sticky; top: 0; z-index: 100;
  }
  .logo { font-size: 20px; font-weight: 700; color: var(--accent); letter-spacing: 2px; }
  .logo span { color: var(--text); font-weight: 400; }
  .status-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--danger); margin-left: auto; }
  .status-dot.connected { background: var(--ok); box-shadow: 0 0 8px var(--ok); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.5; } }
  .status-label { font-size: 12px; color: var(--muted); }

  main { padding: 24px; max-width: 1400px; margin: 0 auto; }
  
  .grid-top { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
  @media(max-width:900px) { .grid-top { grid-template-columns: repeat(2,1fr); } }
  
  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px;
  }
  .card-title { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 8px; }
  .card-value { font-size: 28px; font-weight: 700; }
  .card-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .accent { color: var(--accent); }
  .warn-color { color: var(--warn); }
  .danger-color { color: var(--danger); }
  .ok-color { color: var(--ok); }

  .grid-main { display: grid; grid-template-columns: 1fr 380px; gap: 16px; }
  @media(max-width:1100px) { .grid-main { grid-template-columns: 1fr; } }

  .section-title {
    font-size: 13px; font-weight: 600; color: var(--muted);
    text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px;
  }
  
  /* Events feed */
  #events-feed {
    height: 480px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px;
  }
  #events-feed::-webkit-scrollbar { width: 4px; }
  #events-feed::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
  
  .event-item {
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 14px; font-size: 13px;
    border-left: 3px solid var(--accent);
    animation: slideIn .3s ease;
  }
  @keyframes slideIn { from { transform: translateX(-10px); opacity:0; } to { transform: translateX(0); opacity:1; } }
  .event-item.alarm { border-left-color: var(--danger); }
  .event-item.location { border-left-color: var(--ok); }
  .event-item.snapshot { border-left-color: var(--accent2); }
  .event-item.heartbeat { border-left-color: var(--muted); }
  .event-time { font-size: 10px; color: var(--muted); margin-bottom: 3px; }
  .event-type { font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing:.5px; }
  .event-detail { color: var(--muted); margin-top: 3px; font-size: 12px; word-break: break-all; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 10px; font-weight: 600; }
  .badge-alarm { background: rgba(239,68,68,.15); color: var(--danger); }
  .badge-ok { background: rgba(34,197,94,.1); color: var(--ok); }

  /* Devices panel */
  #devices-list { display: flex; flex-direction: column; gap: 10px; max-height: 480px; overflow-y: auto; }
  .device-card {
    background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 14px;
  }
  .device-phone { font-size: 14px; font-weight: 700; font-family: monospace; color: var(--accent); }
  .device-info { font-size: 11px; color: var(--muted); margin-top: 4px; line-height: 1.6; }
  .device-loc { font-size: 12px; margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border); }
  .speed-badge {
    display: inline-block; background: var(--accent2); color: #fff;
    padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 700;
  }

  /* Snapshots grid */
  .snap-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px,1fr)); gap: 12px; margin-top: 16px; }
  .snap-item { border-radius: 8px; overflow: hidden; border: 1px solid var(--border); background: var(--surface2); }
  .snap-item img { width: 100%; height: 130px; object-fit: cover; display: block; }
  .snap-label { font-size: 11px; color: var(--muted); padding: 6px 8px; }

  /* Map placeholder */
  #map-area {
    background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius);
    height: 300px; display: flex; align-items: center; justify-content: center; margin-top: 24px;
    position: relative; overflow: hidden;
  }
  #map-pins { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
  .map-hint { color: var(--muted); font-size: 13px; text-align: center; }

  .tabs { display: flex; gap: 4px; margin-bottom: 16px; }
  .tab { padding: 7px 16px; border-radius: 6px; font-size: 13px; cursor: pointer; border: 1px solid var(--border); background: transparent; color: var(--muted); }
  .tab.active { background: var(--accent); color: #000; font-weight: 600; border-color: var(--accent); }

  .empty { color: var(--muted); font-size: 13px; text-align: center; padding: 40px; }
</style>
</head>
<body>

<header>
  <div class="logo">RANOR <span>/ Dashcam</span></div>
  <div style="display:flex;align-items:center;gap:8px;margin-left:auto">
    <span class="status-label" id="ws-label">Desconectado</span>
    <div class="status-dot" id="ws-dot"></div>
  </div>
</header>

<main>
  <!-- Stats -->
  <div class="grid-top">
    <div class="card">
      <div class="card-title">Dispositivos Online</div>
      <div class="card-value accent" id="stat-devices">0</div>
      <div class="card-sub">conectados agora</div>
    </div>
    <div class="card">
      <div class="card-title">Eventos Hoje</div>
      <div class="card-value" id="stat-events">0</div>
      <div class="card-sub">posições + alarmes</div>
    </div>
    <div class="card">
      <div class="card-title">Alarmes</div>
      <div class="card-value danger-color" id="stat-alarms">0</div>
      <div class="card-sub">na sessão atual</div>
    </div>
    <div class="card">
      <div class="card-title">Snapshots</div>
      <div class="card-value" id="stat-snaps">0</div>
      <div class="card-sub">imagens recebidas</div>
    </div>
  </div>

  <!-- Main grid -->
  <div class="grid-main">

    <!-- Left: feed + snapshots -->
    <div>
      <div class="tabs">
        <button class="tab active" onclick="showTab('events')">Feed de Eventos</button>
        <button class="tab" onclick="showTab('snaps')">Snapshots</button>
      </div>

      <div class="card" id="tab-events">
        <div class="section-title">Eventos em tempo real</div>
        <div id="events-feed"><div class="empty">Aguardando conexão da dashcam...</div></div>
      </div>

      <div class="card" id="tab-snaps" style="display:none">
        <div class="section-title">Imagens capturadas</div>
        <div class="snap-grid" id="snap-grid"><div class="empty">Nenhuma imagem ainda</div></div>
      </div>

      <!-- Mini map -->
      <div id="map-area" style="margin-top:16px">
        <canvas id="map-canvas" width="800" height="300" style="position:absolute;top:0;left:0;width:100%;height:100%"></canvas>
        <div class="map-hint" id="map-hint">📍 Posições aparecerão aqui quando a dashcam enviar GPS</div>
      </div>
    </div>

    <!-- Right: devices -->
    <div class="card">
      <div class="section-title">Dispositivos</div>
      <div id="devices-list"><div class="empty">Nenhum dispositivo conectado</div></div>
    </div>
  </div>
</main>

<script>
let ws = null;
let stats = { devices: 0, events: 0, alarms: 0, snaps: 0 };
let positions = [];
const MAX_EVENTS = 80;

function showTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['events','snaps'][i] === name));
  document.getElementById('tab-events').style.display = name === 'events' ? '' : 'none';
  document.getElementById('tab-snaps').style.display  = name === 'snaps'  ? '' : 'none';
}

function updateStats() {
  document.getElementById('stat-events').textContent  = stats.events;
  document.getElementById('stat-alarms').textContent  = stats.alarms;
  document.getElementById('stat-snaps').textContent   = stats.snaps;
}

function formatTime(iso) {
  return new Date(iso).toLocaleTimeString('pt-BR');
}

function addEvent(event) {
  const feed = document.getElementById('events-feed');
  const empty = feed.querySelector('.empty');
  if (empty) empty.remove();

  const t = event.type;
  const d = event.data;
  stats.events++;

  let cls = t;
  let detail = '';
  let badge = '';

  if (t === 'location') {
    detail = `📍 ${d.lat?.toFixed(5)}, ${d.lon?.toFixed(5)} | 🚗 ${d.speed} km/h | ↗ ${d.direction}°`;
    if (d.alarms && d.alarms.length) {
      badge = `<span class="badge badge-alarm">⚠ ${d.alarms.join(', ')}</span>`;
      stats.alarms++;
      cls = 'alarm';
    }
    // Add to map
    if (d.lat && d.lon) {
      positions.push({ lat: d.lat, lon: d.lon, phone: d.phone });
      drawMap();
    }
  } else if (t === 'alarm') {
    detail = `🚨 ${d.alarm_list?.join(' | ')}`;
    badge = `<span class="badge badge-alarm">ALARME</span>`;
    stats.alarms++;
  } else if (t === 'snapshot' || t === 'file_received') {
    detail = `📷 ${d.filename} (${(d.size/1024).toFixed(1)} KB) CH${d.channel||'?'}`;
    stats.snaps++;
    if (d.type === 'snapshot' || d.filename?.match(/[.]jpe?g$/i)) {
      addSnapshot(d);
    }
  } else if (t === 'device_connected') {
    detail = `✅ Registrado — ${d.device_info?.model || ''} placa ${d.device_info?.plate || '?'}`;
    badge = `<span class="badge badge-ok">ONLINE</span>`;
    loadDevices();
  } else if (t === 'device_disconnected') {
    detail = `❌ Desconectado`;
    loadDevices();
  } else if (t === 'heartbeat') {
    detail = `💓 Heartbeat`;
  } else if (t === 'media_event') {
    detail = `🎬 ${d.type} / ${d.event} — canal ${d.channel}`;
  } else {
    detail = JSON.stringify(d).substring(0, 100);
  }

  const div = document.createElement('div');
  div.className = `event-item ${cls}`;
  div.innerHTML = `
    <div class="event-time">${formatTime(event.ts)} ${badge}</div>
    <div class="event-type">${t.replace('_',' ')}</div>
    ${detail ? `<div class="event-detail">${detail}</div>` : ''}
  `;

  feed.insertBefore(div, feed.firstChild);
  while (feed.children.length > MAX_EVENTS) feed.removeChild(feed.lastChild);
  updateStats();
}

function addSnapshot(d) {
  const grid = document.getElementById('snap-grid');
  const empty = grid.querySelector('.empty');
  if (empty) empty.remove();

  const url = `/api/snapshots/${d.filename}`;
  const div = document.createElement('div');
  div.className = 'snap-item';
  div.innerHTML = `
    <a href="${url}" target="_blank"><img src="${url}" alt="${d.filename}" onerror="this.src='/static/no-img.png'"></a>
    <div class="snap-label">${d.filename}<br>${new Date().toLocaleTimeString('pt-BR')}</div>
  `;
  grid.insertBefore(div, grid.firstChild);
  if (grid.children.length > 30) grid.removeChild(grid.lastChild);
}

async function loadDevices() {
  try {
    const r = await fetch('/api/devices');
    const devs = await r.json();
    const list = document.getElementById('devices-list');
    const keys = Object.keys(devs);
    stats.devices = keys.length;
    document.getElementById('stat-devices').textContent = keys.length;

    if (!keys.length) {
      list.innerHTML = '<div class="empty">Nenhum dispositivo conectado</div>';
      return;
    }
    list.innerHTML = '';
    for (const [phone, d] of Object.entries(devs)) {
      const streamUrl = `/stream/${phone}`;
      const loc = d.last_location;
      list.innerHTML += `
        <div class="device-card">
          <div class="device-phone">${phone}</div>
          <div class="device-info">
            ${d.device_info?.model ? `Modelo: ${d.device_info.model}<br>` : ''}
            ${d.device_info?.plate ? `Placa: ${d.device_info.plate}<br>` : ''}
            Última vez: ${new Date(d.last_seen).toLocaleTimeString('pt-BR')}
          </div>
          <div style="display:flex;gap:6px;margin-top:10px">
            <a href="${streamUrl}?channel=1" target="_blank" style="flex:1;text-align:center;padding:6px;background:#7c3aed;color:#fff;border-radius:6px;font-size:12px;font-weight:600;text-decoration:none">📹 CH1</a>
            <a href="${streamUrl}?channel=2" target="_blank" style="flex:1;text-align:center;padding:6px;background:#7c3aed;color:#fff;border-radius:6px;font-size:12px;font-weight:600;text-decoration:none">📹 CH2</a>
            <a href="${streamUrl}?channel=3" target="_blank" style="flex:1;text-align:center;padding:6px;background:#7c3aed;color:#fff;border-radius:6px;font-size:12px;font-weight:600;text-decoration:none">📹 CH3</a>
          </div>
          ${loc ? `<div class="device-loc">
            📍 ${loc.lat?.toFixed(5)}, ${loc.lon?.toFixed(5)}<br>
            <span class="speed-badge">${loc.speed} km/h</span>
            ${loc.alarms?.length ? `<span class="badge badge-alarm">${loc.alarms[0]}</span>` : '<span class="badge badge-ok">Normal</span>'}
          </div>` : ''}
        </div>`;
    }
  } catch(e) { console.error(e); }
}

function drawMap() {
  if (!positions.length) return;
  const canvas = document.getElementById('map-canvas');
  const ctx = canvas.getContext('2d');
  const hint = document.getElementById('map-hint');
  hint.style.display = 'none';
  ctx.clearRect(0,0,canvas.width,canvas.height);

  const lats = positions.map(p=>p.lat), lons = positions.map(p=>p.lon);
  const minLat = Math.min(...lats), maxLat = Math.max(...lats);
  const minLon = Math.min(...lons), maxLon = Math.max(...lons);
  const pad = 40;
  const W = canvas.width - 2*pad, H = canvas.height - 2*pad;
  const latRange = maxLat - minLat || 0.001;
  const lonRange = maxLon - minLon || 0.001;

  const toXY = (lat, lon) => ({
    x: pad + (lon - minLon) / lonRange * W,
    y: pad + (1 - (lat - minLat) / latRange) * H,
  });

  // Draw trail
  ctx.beginPath();
  ctx.strokeStyle = '#00d4ff40';
  ctx.lineWidth = 2;
  positions.forEach((p, i) => {
    const {x,y} = toXY(p.lat, p.lon);
    i === 0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y);
  });
  ctx.stroke();

  // Draw points
  positions.forEach((p, i) => {
    const {x,y} = toXY(p.lat, p.lon);
    const isLast = i === positions.length - 1;
    ctx.beginPath();
    ctx.arc(x, y, isLast ? 7 : 3, 0, Math.PI*2);
    ctx.fillStyle = isLast ? '#00d4ff' : '#7c3aed80';
    ctx.fill();
  });
}

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  
  ws.onopen = () => {
    document.getElementById('ws-dot').classList.add('connected');
    document.getElementById('ws-label').textContent = 'Conectado';
    loadDevices();
  };
  ws.onmessage = e => {
    try { addEvent(JSON.parse(e.data)); } catch {}
  };
  ws.onclose = () => {
    document.getElementById('ws-dot').classList.remove('connected');
    document.getElementById('ws-label').textContent = 'Reconectando...';
    setTimeout(connectWS, 3000);
  };
}

// Load recent events on start
async function loadHistory() {
  try {
    const r = await fetch('/api/events?limit=30');
    const events = await r.json();
    events.reverse().forEach(addEvent);
  } catch {}
}

connectWS();
loadHistory();
setInterval(loadDevices, 15000);
</script>
</body>
</html>
"""


def create_api(storage: Path, event_bus, jt808_server, jt1078_server=None) -> FastAPI:
    app = FastAPI(title="RANOR Dashcam API", version="1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        return DASHBOARD_HTML

    @app.get("/api/devices")
    async def get_devices():
        return jt808_server.get_sessions()

    @app.get("/api/events")
    async def get_events(event_type: Optional[str] = None, limit: int = 100):
        return event_bus.get_history(event_type, limit)

    @app.get("/api/stats")
    async def get_stats():
        return {
            'devices_online': len(jt808_server.sessions),
            'event_counts': event_bus.get_stats(),
            'storage': _storage_info(storage),
        }

    @app.get("/api/snapshots")
    async def list_snapshots():
        snap_dir = storage / 'snapshots'
        if not snap_dir.exists():
            return []
        files = sorted(snap_dir.glob('*.jpg'), key=lambda f: f.stat().st_mtime, reverse=True)
        return [
            {
                'filename': f.name,
                'size': f.stat().st_size,
                'modified': datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                'url': f'/api/snapshots/{f.name}',
            }
            for f in files[:100]
        ]

    @app.get("/api/snapshots/{filename}")
    async def get_snapshot(filename: str):
        path = storage / 'snapshots' / filename
        if not path.exists():
            raise HTTPException(404, "Snapshot not found")
        return FileResponse(str(path), media_type='image/jpeg')

    @app.get("/api/videos")
    async def list_videos():
        vid_dir = storage / 'videos'
        if not vid_dir.exists():
            return []
        files = sorted(vid_dir.glob('*.mp4'), key=lambda f: f.stat().st_mtime, reverse=True)
        return [
            {
                'filename': f.name,
                'size': f.stat().st_size,
                'modified': datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                'url': f'/api/videos/{f.name}',
            }
            for f in files[:50]
        ]

    @app.get("/api/videos/{filename}")
    async def get_video(filename: str):
        path = storage / 'videos' / filename
        if not path.exists():
            raise HTTPException(404, "Video not found")
        return FileResponse(str(path), media_type='video/mp4')

    @app.post("/api/stream/start/{phone}/{channel}")
    async def start_stream(phone: str, channel: int = 1):
        session = jt808_server.sessions.get(phone)
        if not session:
            raise HTTPException(404, f"Dispositivo {phone} não conectado")
        if jt1078_server:
            session.stream_server = jt1078_server
        await session.request_stream(channel)
        return {"status": "ok", "message": f"Stream CH{channel} solicitado para {phone}"}

    @app.post("/api/stream/stop/{phone}/{channel}")
    async def stop_stream(phone: str, channel: int = 1):
        session = jt808_server.sessions.get(phone)
        if not session:
            raise HTTPException(404, f"Dispositivo {phone} não conectado")
        await session.stop_stream(channel)
        return {"status": "ok", "message": f"Stream CH{channel} parado"}

    @app.post("/api/snapshot/{phone}")
    async def request_snapshot(phone: str, channel: int = 255):
        session = jt808_server.sessions.get(phone)
        if not session:
            raise HTTPException(404, f"Dispositivo {phone} não conectado")
        await session.capture_snapshot(channel)
        return {"status": "ok", "message": "Snapshot solicitado"}

    @app.get("/api/streams")
    async def get_streams():
        if jt1078_server:
            return jt1078_server.get_active_streams()
        return []

    @app.websocket("/ws/stream/{sim}/{channel}")
    async def stream_ws(ws: WebSocket, sim: str, channel: int):
        """WebSocket que recebe frames JPEG do stream JT1078"""
        await ws.accept()
        if jt1078_server:
            jt1078_server.add_stream_client(ws, sim, channel)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            if jt1078_server:
                jt1078_server.remove_stream_client(ws, sim, channel)

    @app.get("/stream/{phone}", response_class=HTMLResponse)
    async def stream_viewer(phone: str, channel: int = 1):
        return _stream_page(phone, channel)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        event_bus.add_ws_client(ws)
        # Send current devices on connect
        try:
            await ws.send_text(json.dumps({
                'type': 'init',
                'data': {'devices': jt808_server.get_sessions()},
                'ts': datetime.now().isoformat(),
            }))
            while True:
                await ws.receive_text()  # keep alive / ping
        except WebSocketDisconnect:
            pass
        finally:
            event_bus.remove_ws_client(ws)

    return app


def _storage_info(storage: Path) -> dict:
    info = {}
    for folder in ['snapshots', 'videos', 'events']:
        d = storage / folder
        if d.exists():
            files = list(d.iterdir())
            total = sum(f.stat().st_size for f in files if f.is_file())
            info[folder] = {'count': len(files), 'size_mb': round(total / 1024 / 1024, 2)}
        else:
            info[folder] = {'count': 0, 'size_mb': 0}
    return info


def _stream_page(phone: str, channel: int) -> str:
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RANOR — Live CH{channel} | {phone}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0a0a0f; color: #e2e8f0; font-family: 'Segoe UI', system-ui, sans-serif;
         display: flex; flex-direction: column; height: 100vh; }}
  header {{
    background: #12141e; border-bottom: 1px solid #1e2235;
    padding: 12px 20px; display: flex; align-items: center; gap: 12px;
  }}
  .logo {{ color: #00d4ff; font-weight: 700; font-size: 16px; letter-spacing: 2px; }}
  .device {{ font-size: 13px; color: #94a3b8; font-family: monospace; }}
  .badge {{ padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 700; }}
  .live {{ background: #ef4444; color: #fff; animation: blink 1s infinite; }}
  .wait {{ background: #374151; color: #9ca3af; }}
  @keyframes blink {{ 0%,100%{{opacity:1}} 50%{{opacity:.4}} }}
  .status-bar {{
    background: #12141e; padding: 8px 20px; font-size: 12px; color: #64748b;
    display: flex; gap: 20px; border-bottom: 1px solid #1e2235;
  }}
  .stat {{ display: flex; gap: 6px; }}
  .stat-val {{ color: #00d4ff; font-weight: 600; }}

  .viewer {{ flex: 1; display: flex; align-items: center; justify-content: center;
             background: #050508; position: relative; overflow: hidden; }}
  #video-canvas {{
    max-width: 100%; max-height: 100%; object-fit: contain;
    border: 1px solid #1e2235;
  }}
  .overlay {{
    position: absolute; top: 12px; left: 12px;
    background: rgba(0,0,0,.7); padding: 6px 12px; border-radius: 6px;
    font-size: 11px; font-family: monospace; color: #00ff88;
    pointer-events: none;
  }}
  .no-signal {{
    position: absolute; display: flex; flex-direction: column;
    align-items: center; gap: 16px; color: #374151;
  }}
  .no-signal svg {{ width: 64px; height: 64px; opacity: .4; }}
  .no-signal p {{ font-size: 14px; }}

  .controls {{
    background: #12141e; border-top: 1px solid #1e2235;
    padding: 12px 20px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  }}
  button {{
    padding: 8px 18px; border-radius: 8px; border: none; cursor: pointer;
    font-size: 13px; font-weight: 600; transition: all .2s;
  }}
  .btn-primary {{ background: #00d4ff; color: #000; }}
  .btn-primary:hover {{ background: #00b8e0; }}
  .btn-danger {{ background: #ef4444; color: #fff; }}
  .btn-danger:hover {{ background: #dc2626; }}
  .btn-secondary {{ background: #1e2235; color: #e2e8f0; border: 1px solid #2d3348; }}
  .btn-secondary:hover {{ background: #2d3348; }}
  .ch-btns {{ display: flex; gap: 6px; margin-left: auto; }}
  .ch-btn {{
    width: 36px; height: 36px; border-radius: 6px; font-size: 12px; font-weight: 700;
    background: #1e2235; color: #64748b; border: 1px solid #2d3348;
  }}
  .ch-btn.active {{ background: #7c3aed; color: #fff; border-color: #7c3aed; }}
</style>
</head>
<body>

<header>
  <div class="logo">RANOR</div>
  <div class="device">/ {phone} / Canal CH<span id="ch-label">{channel}</span></div>
  <span class="badge wait" id="live-badge">● AGUARDANDO</span>
</header>

<div class="status-bar">
  <div class="stat">FPS <span class="stat-val" id="fps">0</span></div>
  <div class="stat">Frames <span class="stat-val" id="frames">0</span></div>
  <div class="stat">Latência <span class="stat-val" id="latency">—</span> ms</div>
  <div class="stat">Resolução <span class="stat-val" id="resolution">—</span></div>
  <div class="stat">Status <span class="stat-val" id="ws-status">Conectando...</span></div>
</div>

<div class="viewer">
  <img id="video-canvas" alt="Stream" style="display:none">
  <div class="no-signal" id="no-signal">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
      <path d="M15.75 10.5l4.72-4.72M9 3.75L12 .75l3 3M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
      <path d="M9 9l6 6m0-6l-6 6"/>
    </svg>
    <p>Clique em <strong>▶ Iniciar Stream</strong> para ver a câmera ao vivo</p>
    <p style="font-size:12px;color:#4b5563;">O servidor envia o comando 0x9101 para a dashcam</p>
  </div>
  <div class="overlay" id="overlay" style="display:none">
    CH{channel} | <span id="ts-overlay">--:--:--</span>
  </div>
</div>

<div class="controls">
  <button class="btn-primary" onclick="startStream()">▶ Iniciar Stream</button>
  <button class="btn-danger" onclick="stopStream()">■ Parar</button>
  <button class="btn-secondary" onclick="requestSnapshot()">📷 Snapshot</button>
  <div class="ch-btns">
    <button class="ch-btn {'active' if channel==1 else ''}" onclick="switchCh(1)">CH1</button>
    <button class="ch-btn {'active' if channel==2 else ''}" onclick="switchCh(2)">CH2</button>
    <button class="ch-btn {'active' if channel==3 else ''}" onclick="switchCh(3)">CH3</button>
  </div>
</div>

<script>
const PHONE = '{phone}';
let currentChannel = {channel};
let ws = null;
let frameCount = 0, fps = 0, lastFpsTime = Date.now();
let fpsInterval = null;

function updateFps() {{
  const now = Date.now();
  const elapsed = (now - lastFpsTime) / 1000;
  fps = Math.round(frameCount / elapsed);
  frameCount = 0;
  lastFpsTime = now;
  document.getElementById('fps').textContent = fps;
}}

function connectWS(channel) {{
  if (ws) {{ ws.close(); ws = null; }}
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${{proto}}://${{location.host}}/ws/stream/${{PHONE}}/${{channel}}`);
  document.getElementById('ws-status').textContent = 'Conectando WS...';

  ws.onopen = () => {{
    document.getElementById('ws-status').textContent = 'WS conectado';
    fpsInterval = setInterval(updateFps, 1000);
  }};

  ws.onmessage = (e) => {{
    try {{
      const msg = JSON.parse(e.data);
      if (msg.type === 'frame' && msg.data) {{
        const img = document.getElementById('video-canvas');
        const noSig = document.getElementById('no-signal');
        const overlay = document.getElementById('overlay');
        const badge = document.getElementById('live-badge');

        img.src = 'data:image/jpeg;base64,' + msg.data;
        img.style.display = 'block';
        noSig.style.display = 'none';
        overlay.style.display = 'block';
        badge.textContent = '● AO VIVO';
        badge.className = 'badge live';

        // Stats
        frameCount++;
        const total = parseInt(document.getElementById('frames').textContent || '0') + 1;
        document.getElementById('frames').textContent = total;
        const lat = Date.now() - msg.ts;
        document.getElementById('latency').textContent = lat;
        document.getElementById('ts-overlay').textContent = new Date().toLocaleTimeString('pt-BR');

        // Resolution from image natural size
        img.onload = () => {{
          if (img.naturalWidth)
            document.getElementById('resolution').textContent =
              img.naturalWidth + 'x' + img.naturalHeight;
        }};
      }}
    }} catch(err) {{ console.error(err); }}
  }};

  ws.onclose = () => {{
    document.getElementById('ws-status').textContent = 'WS desconectado';
    const badge = document.getElementById('live-badge');
    badge.textContent = '● AGUARDANDO';
    badge.className = 'badge wait';
    clearInterval(fpsInterval);
  }};
}}

async function startStream() {{
  connectWS(currentChannel);
  try {{
    const r = await fetch(`/api/stream/start/${{PHONE}}/${{currentChannel}}`, {{method:'POST'}});
    const d = await r.json();
    document.getElementById('ws-status').textContent = d.message || 'Stream iniciado';
  }} catch(e) {{ console.error(e); }}
}}

async function stopStream() {{
  if (ws) {{ ws.close(); ws = null; }}
  await fetch(`/api/stream/stop/${{PHONE}}/${{currentChannel}}`, {{method:'POST'}});
  document.getElementById('video-canvas').style.display = 'none';
  document.getElementById('no-signal').style.display = 'flex';
  document.getElementById('overlay').style.display = 'none';
  const badge = document.getElementById('live-badge');
  badge.textContent = '● AGUARDANDO';
  badge.className = 'badge wait';
  document.getElementById('ws-status').textContent = 'Parado';
}}

async function requestSnapshot() {{
  await fetch(`/api/snapshot/${{PHONE}}?channel=${{currentChannel}}`, {{method:'POST'}});
}}

function switchCh(ch) {{
  currentChannel = ch;
  document.getElementById('ch-label').textContent = ch;
  document.querySelectorAll('.ch-btn').forEach((b,i) =>
    b.classList.toggle('active', i+1 === ch));
  if (ws && ws.readyState === WebSocket.OPEN) {{
    stopStream().then(() => startStream());
  }}
}}

// Auto-conectar WS ao carregar
connectWS(currentChannel);
</script>
</body>
</html>"""
