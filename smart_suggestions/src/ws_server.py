"""WebSocket server for the Lovelace card to connect to via HA Ingress."""
from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

PORT = 8099

_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Smart Suggestions</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif;
    background: #000;
    color: #fff;
    min-height: 100vh;
    padding: 20px 16px 40px;
  }
  .header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 6px;
  }
  .header-icon {
    width: 36px; height: 36px;
    background: #007AFF;
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 20px;
  }
  h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.4px; }
  .meta {
    font-size: 13px;
    color: #8E8E93;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .status-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: #34C759;
    display: inline-block;
    flex-shrink: 0;
  }
  .status-dot.updating { background: #FF9F0A; animation: pulse 1s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
  .list { display: flex; flex-direction: column; gap: 1px; background: rgba(255,255,255,0.08); border-radius: 14px; overflow: hidden; }
  .card {
    background: #1C1C1E;
    padding: 14px 16px;
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .card-icon {
    width: 40px; height: 40px;
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 20px;
    flex-shrink: 0;
    background: #2C2C2E;
  }
  .card-body { flex: 1; min-width: 0; }
  .card-name { font-size: 15px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .card-reason { font-size: 13px; color: #8E8E93; margin-top: 3px; line-height: 1.4; }
  .vote-area {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
  }
  .vote-btn {
    width: 36px; height: 36px;
    border: none;
    border-radius: 50%;
    background: rgba(255,255,255,0.08);
    color: #fff;
    font-size: 17px;
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: background 0.15s, transform 0.1s;
    -webkit-tap-highlight-color: transparent;
  }
  .vote-btn:active { transform: scale(0.88); }
  .vote-btn.up:active, .vote-btn.up.voted { background: rgba(52,199,89,0.25); }
  .vote-btn.down:active, .vote-btn.down.voted { background: rgba(255,59,48,0.25); }
  .score {
    font-size: 13px;
    font-weight: 600;
    min-width: 20px;
    text-align: center;
    color: #8E8E93;
  }
  .score.positive { color: #34C759; }
  .score.negative { color: #FF3B30; }
  .empty {
    text-align: center;
    padding: 60px 20px;
    color: #8E8E93;
    font-size: 15px;
  }
  .empty-icon { font-size: 44px; margin-bottom: 12px; opacity: 0.3; }
  .toast {
    position: fixed;
    bottom: 24px;
    left: 50%;
    transform: translateX(-50%) translateY(80px);
    background: #2C2C2E;
    color: #fff;
    padding: 10px 18px;
    border-radius: 20px;
    font-size: 14px;
    font-weight: 500;
    transition: transform 0.3s cubic-bezier(0.34,1.56,0.64,1);
    pointer-events: none;
    white-space: nowrap;
    box-shadow: 0 4px 20px rgba(0,0,0,0.5);
  }
  .toast.show { transform: translateX(-50%) translateY(0); }
</style>
</head>
<body>
<div class="header">
  <div class="header-icon">✨</div>
  <h1>Smart Suggestions</h1>
</div>
<div class="meta">
  <span class="status-dot" id="status-dot"></span>
  <span id="meta-text">Connecting…</span>
</div>
<div id="list-container"></div>
<div class="toast" id="toast"></div>

<script>
const FEEDBACK_ENDPOINT = location.pathname.replace(/\\/ws$/, '').replace(/\\/$/, '') + '/feedback';

let _feedback = __FEEDBACK__;
let _suggestions = __SUGGESTIONS__;
let _status = 'idle';

function net(eid) {
  const f = _feedback[eid];
  if (!f) return 0;
  return (f.up || 0) - (f.down || 0);
}

function domainEmoji(eid) {
  const d = (eid || '').split('.')[0];
  const map = {
    light: '💡', switch: '🔌', climate: '🌡️', media_player: '📺',
    cover: '🪟', fan: '💨', lock: '🔒', vacuum: '🤖',
    scene: '🎨', automation: '🤖', script: '📜', input_boolean: '🔘',
  };
  return map[d] || '⚙️';
}

function render() {
  const container = document.getElementById('list-container');
  const dot = document.getElementById('status-dot');
  const metaEl = document.getElementById('meta-text');

  dot.className = 'status-dot' + (_status === 'updating' ? ' updating' : '');
  const statusLabel = _status === 'updating' ? 'Updating…' : _status === 'ready' ? 'Ready' : 'Idle';
  metaEl.textContent = statusLabel + ' · ' + new Date().toLocaleTimeString();

  if (!_suggestions.length) {
    container.innerHTML = '<div class="empty"><div class="empty-icon">✨</div>No suggestions yet. Waiting for next refresh…</div>';
    return;
  }

  const rows = _suggestions.map((s, i) => {
    const score = net(s.entity_id);
    const scoreClass = score > 0 ? 'positive' : score < 0 ? 'negative' : '';
    return `
      <div class="card" data-index="${i}">
        <div class="card-icon">${domainEmoji(s.entity_id)}</div>
        <div class="card-body">
          <div class="card-name">${s.name || s.entity_id || ''}</div>
          <div class="card-reason">${s.reason || ''}</div>
        </div>
        <div class="vote-area">
          <button class="vote-btn up" data-eid="${s.entity_id}" data-vote="up" title="Thumbs up">👍</button>
          <span class="score ${scoreClass}">${score > 0 ? '+' + score : score}</span>
          <button class="vote-btn down" data-eid="${s.entity_id}" data-vote="down" title="Thumbs down">👎</button>
        </div>
      </div>
    `;
  }).join('');

  container.innerHTML = '<div class="list">' + rows + '</div>';

  container.querySelectorAll('.vote-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const eid = btn.dataset.eid;
      const vote = btn.dataset.vote;
      btn.classList.add('voted');
      try {
        await fetch(FEEDBACK_ENDPOINT, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ entity_id: eid, vote }),
        });
        if (!_feedback[eid]) _feedback[eid] = { up: 0, down: 0 };
        _feedback[eid][vote] = (_feedback[eid][vote] || 0) + 1;
        showToast(vote === 'up' ? '👍 Upvoted' : '👎 Downvoted');
        render();
      } catch (e) {
        showToast('⚠️ Vote failed');
      }
    });
  });
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsPath = location.pathname.replace(/\\/$/, '') + '/ws';
  const ws = new WebSocket(proto + '//' + location.host + wsPath);

  ws.addEventListener('open', () => {
    document.getElementById('meta-text').textContent = 'Connected';
  });

  ws.addEventListener('message', evt => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }
    if (msg.type === 'suggestions') {
      _suggestions = Array.isArray(msg.data) ? msg.data : [];
      _status = 'ready';
      render();
    } else if (msg.type === 'status') {
      _status = msg.state || 'idle';
      render();
    }
  });

  ws.addEventListener('close', () => {
    document.getElementById('status-dot').className = 'status-dot';
    document.getElementById('meta-text').textContent = 'Disconnected — reconnecting…';
    setTimeout(connectWS, 5000);
  });
}

render();
connectWS();
</script>
</body>
</html>"""


class WSServer:
    """Manages connected card WebSocket clients and broadcasts events."""

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        self._app = web.Application()
        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/", self._ui_handler)
        self._app.router.add_post("/feedback", self._feedback_handler)
        self._runner: web.AppRunner | None = None
        self._last_suggestions: list = []
        self._last_status: str = "idle"
        self._feedback: dict = {}
        self._feedback_cb = None

    def register_feedback_handler(self, cb) -> None:
        self._feedback_cb = cb

    def set_feedback(self, feedback: dict) -> None:
        self._feedback = feedback

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", PORT)
        await site.start()
        _LOGGER.info("WebSocket server listening on port %d", PORT)

    async def stop(self) -> None:
        for ws in list(self._clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    async def _ui_handler(self, request: web.Request) -> web.Response:
        html = (
            _UI_HTML
            .replace("__FEEDBACK__", json.dumps(self._feedback))
            .replace("__SUGGESTIONS__", json.dumps(self._last_suggestions))
        )
        return web.Response(text=html, content_type="text/html")

    async def _feedback_handler(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            entity_id = data.get("entity_id", "")
            vote = data.get("vote", "")
            if entity_id and vote in ("up", "down") and self._feedback_cb:
                await self._feedback_cb(entity_id, vote)
            return web.json_response({"ok": True})
        except Exception as e:
            _LOGGER.error("Feedback handler error: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        _LOGGER.info("Card connected (%d total)", len(self._clients))

        # Send current state immediately on connect
        await self._send(ws, {"type": "status", "state": self._last_status})
        if self._last_suggestions:
            await self._send(ws, {"type": "suggestions", "data": self._last_suggestions})

        try:
            async for msg in ws:
                pass  # card sends no messages; just keep alive
        except Exception:
            pass
        finally:
            self._clients.discard(ws)
            _LOGGER.info("Card disconnected (%d remaining)", len(self._clients))

        return ws

    async def _send(self, ws: web.WebSocketResponse, payload: dict) -> None:
        try:
            await ws.send_str(json.dumps(payload))
        except Exception as e:
            _LOGGER.debug("Send failed to client: %s", e)

    async def broadcast(self, payload: dict) -> None:
        """Broadcast a message to all connected clients."""
        dead = set()
        for ws in list(self._clients):
            try:
                await ws.send_str(json.dumps(payload))
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def broadcast_token(self, token: str) -> None:
        await self.broadcast({"type": "streaming", "token": token})

    async def broadcast_suggestions(self, suggestions: list) -> None:
        self._last_suggestions = suggestions
        self._last_status = "ready"
        await self.broadcast({"type": "suggestions", "data": suggestions})
        await self.broadcast({"type": "status", "state": "ready"})

    async def broadcast_status(self, state: str) -> None:
        self._last_status = state
        await self.broadcast({"type": "status", "state": state})
