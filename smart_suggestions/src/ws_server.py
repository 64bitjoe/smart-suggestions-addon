# smart_suggestions/src/ws_server.py
"""Slim ingress panel: zones view + live logs + action relay."""
from __future__ import annotations
import json
import logging

from aiohttp import web, WSMsgType

_LOGGER = logging.getLogger(__name__)

_UI_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smart Suggestions</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font-family:-apple-system,system-ui,sans-serif;
         background:#111418; color:#e8eaed; padding:16px; max-width:900px; margin:auto; }
  h1 { font-size:1.25em; margin:4px 0 2px; }
  .sub { color:#9aa0a6; font-size:.8em; margin-bottom:12px; }
  .stats { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }
  .stat { background:#1b1f24; border-radius:12px; padding:10px 14px; min-width:74px; }
  .stat b { display:block; font-size:1.3em; }
  .stat span { font-size:.72em; color:#9aa0a6; text-transform:uppercase; letter-spacing:.05em; }
  h2 { font-size:.78em; text-transform:uppercase; letter-spacing:.08em;
       color:#9aa0a6; margin:20px 0 6px; }
  h2.now { color:#2f6fed; } h2.noticed { color:#e6a23c; } h2.auto { color:#9b6bff; }
  .filters { display:flex; gap:6px; margin:6px 0 10px; }
  .fchip { border:0; border-radius:10px; padding:5px 11px; cursor:pointer;
           font-size:.78em; background:#1b1f24; color:#9aa0a6; }
  .fchip.on { background:#2f6fed; color:#fff; }
  .item { display:flex; align-items:flex-start; gap:10px; background:#1b1f24;
          border-radius:12px; padding:10px 12px; margin-bottom:6px; }
  .t { flex:1; min-width:0; } .title { font-weight:600; line-height:1.3; }
  .desc, .meta { font-size:.8em; color:#9aa0a6; margin-top:2px; }
  .bar { height:3px; border-radius:2px; margin-top:6px; background:#2b3138; overflow:hidden; }
  .bar span { display:block; height:100%; background:#2f6fed; border-radius:2px; }
  .btns { display:flex; gap:4px; flex-wrap:wrap; justify-content:flex-end; }
  button { border:0; border-radius:9px; padding:7px 11px; cursor:pointer;
           font:inherit; font-size:.78em; background:#2b3138; color:#e8eaed; }
  button.pri { background:#2f6fed; color:#fff; }
  button.promote { background:#7c4dff; color:#fff; }
  button.undo { background:#e6a23c; color:#fff; }
  .undone { opacity:.5; }
  .empty { color:#9aa0a6; font-size:.85em; padding:4px 0 10px; }
  #logs { background:#0b0d10; border-radius:12px; padding:10px; font:11px/1.5 monospace;
          height:170px; overflow-y:auto; white-space:pre-wrap; }
  code { background:#1b1f24; padding:2px 5px; border-radius:5px; }
</style></head><body>
<h1>Smart Suggestions</h1>
<div class="sub" id="sub"></div>
<div class="stats" id="stats"></div>
<div id="zones"></div>
<h2>Logs</h2><div id="logs"></div>
<script>
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ZONES = [["now","Right now","now"],["activity","Auto-pilot activity","auto"],
  ["autopilot","Auto-pilot patterns","auto"],["noticed","Noticed","noticed"],
  ["discoveries","Discovered patterns",""],["emerging","Emerging (still learning)",""]];
let zones = {}, stats = {}, filter = "all";
function act(payload) {
  fetch("action", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(payload)});
}
function timeAgo(ts) {
  const m = Math.max(0, Math.round(Date.now()/1000/60 - ts/60));
  return m < 1 ? "just now" : m < 60 ? m + "m ago" : Math.round(m/60) + "h ago";
}
function sugRow(s, key) {
  const pct = Math.round((s.confidence||0)*100);
  return `<div class="item"><div class="t">
    <div class="title">${esc(s.title)}${s.lifecycle==="autopilot"?" ⚡":""}</div>
    <div class="desc">${esc(s.description)}</div>
    <div class="meta">${esc(s.miner_type)} · ${pct}% · seen ${s.occurrences}×${s.accepted_runs?` · run ${s.accepted_runs}×`:""}</div>
    <div class="bar"><span style="width:${pct}%"></span></div>
  </div><div class="btns">
    ${s.can_promote?`<button class="promote" onclick='act({action:"promote",signature:"${esc(s.signature)}"})'>⚡ Auto</button>`:""}
    ${key==="autopilot"?`<button onclick='act({action:"demote",signature:"${esc(s.signature)}"})'>Demote</button>`:""}
    ${key==="now"||key==="noticed"?`<button class="pri" onclick='act({action:"run",signature:"${esc(s.signature)}"})'>Run</button>`:""}
    ${s.can_automate&&key!=="emerging"?`<button class="pri" onclick='act({action:"accept",signature:"${esc(s.signature)}"})'>Automate</button>`:""}
    ${key==="noticed"?`<button onclick='act({action:"snooze",signature:"${esc(s.signature)}"})'>Snooze</button>`:""}
    ${key!=="emerging"?`<button onclick='act({action:"dismiss",signature:"${esc(s.signature)}"})'>✕</button>`:""}
  </div></div>`;
}
function actRow(a) {
  const verb = a.act_action.includes("off") ? "Turned off" : "Turned on";
  return `<div class="item ${a.undone?"undone":""}"><div class="t">
    <div class="title">🤖 ${verb} ${esc(a.title)}</div>
    <div class="meta">${timeAgo(a.ts)}${a.success?"":" · failed"}${a.undone?" · undone":""}</div></div>
    ${a.undone?"":`<div class="btns"><button class="undo" onclick='act({action:"undo",activity_id:${Number(a.activity_id)}})'>Undo</button></div>`}
  </div>`;
}
function render() {
  const s = stats.counts || {};
  document.getElementById("sub").textContent =
    `v${stats.version || "?"} · last mining: ${stats.last_mining || "…"}`;
  document.getElementById("stats").innerHTML =
    [["emerging","Emerging"],["confirmed","Confirmed"],["autopilot","Auto-pilot"],
     ["automated","Automated"],["dismissed","Dismissed"]]
    .map(([k,l]) => `<div class="stat"><b>${s[k]||0}</b><span>${l}</span></div>`).join("");
  document.getElementById("zones").innerHTML = ZONES.map(([key,label,cls]) => {
    let items = zones[key] || [];
    const isFilterable = key === "discoveries" || key === "emerging";
    if (isFilterable && filter !== "all")
      items = items.filter((s) => s.miner_type === filter);
    const chips = isFilterable && key === "discoveries" ?
      `<div class="filters">${["all","temporal","sequence","cross_area"].map((f) =>
        `<button class="fchip ${filter===f?"on":""}" onclick="setFilter('${f}')">${f}</button>`).join("")}</div>` : "";
    const body = items.length
      ? items.map((x) => key === "activity" ? actRow(x) : sugRow(x, key)).join("")
      : `<div class="empty">Nothing here yet.</div>`;
    return `<h2 class="${cls}">${label}</h2>${chips}${body}`;
  }).join("");
}
function setFilter(f) { filter = f; render(); }
function addLog(line) {
  const el = document.getElementById("logs");
  el.textContent += line + "\\n"; el.scrollTop = el.scrollHeight;
}
fetch("zones").then(r => r.json()).then((d) => { zones = d.zones||{}; stats = d.stats||{}; render(); });
const proto = location.protocol === "https:" ? "wss" : "ws";
const ws = new WebSocket(`${proto}://${location.host}${location.pathname.replace(/\\/$/,"")}/ws`);
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.type === "zones") { zones = msg.zones||{}; stats = msg.stats||{}; render(); }
  if (msg.type === "log") addLog(`[${msg.level}] ${msg.message}`);
};
</script></body></html>"""


class WSServer:
    def __init__(self, port: int = 8099):
        self._port = port
        self._zones: dict = {"now": [], "noticed": [], "discoveries": [], "emerging": []}
        self._stats: dict = {}
        self._action_handler = None
        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None

    def register_action_handler(self, cb) -> None:
        self._action_handler = cb

    def set_zones(self, zones: dict) -> None:
        self._zones = zones

    def set_stats(self, stats: dict) -> None:
        self._stats = stats

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._ui)
        app.router.add_get("/zones", self._zones_handler)
        app.router.add_post("/action", self._action)
        app.router.add_get("/ws", self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        _LOGGER.info("Ingress panel on :%d", self._port)

    async def stop(self) -> None:
        for ws in list(self._clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    async def _ui(self, request: web.Request) -> web.Response:
        return web.Response(text=_UI_HTML, content_type="text/html")

    async def _zones_handler(self, request: web.Request) -> web.Response:
        return web.json_response({"zones": self._zones, "stats": self._stats})

    async def _action(self, request: web.Request) -> web.Response:
        data = await request.json()
        if self._action_handler:
            await self._action_handler(data)
        return web.json_response({"ok": True})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            await ws.send_json({"type": "zones", "zones": self._zones, "stats": self._stats})
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            self._clients.discard(ws)
        return ws

    async def _send_all(self, payload: dict) -> None:
        dead = []
        for ws in self._clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def broadcast_zones(self) -> None:
        await self._send_all({"type": "zones", "zones": self._zones, "stats": self._stats})

    async def broadcast_log(self, level: str, message: str) -> None:
        await self._send_all({"type": "log", "level": level, "message": message})
