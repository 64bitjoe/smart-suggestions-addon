"""WebSocket server for the Lovelace card to connect to via HA Ingress."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

PORT = 8099
_LOG_BUFFER_SIZE = 200

_DOMAIN_ICONS = {
    "light": "mdi:lightbulb",
    "switch": "mdi:toggle-switch",
    "climate": "mdi:thermostat",
    "media_player": "mdi:cast",
    "cover": "mdi:window-shutter",
    "fan": "mdi:fan",
    "lock": "mdi:lock",
    "vacuum": "mdi:robot-vacuum",
    "scene": "mdi:palette",
    "automation": "mdi:robot",
    "script": "mdi:script-text",
    "input_boolean": "mdi:toggle-switch",
}

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
    background: #000; color: #fff;
    min-height: 100vh; padding: 20px 16px 48px;
  }
  .topbar { display: flex; align-items: center; gap: 12px; margin-bottom: 4px; }
  .header-icon { width: 36px; height: 36px; background: #007AFF; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 20px; flex-shrink: 0; }
  h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.4px; flex: 1; }
  .refresh-btn {
    background: #007AFF; color: #fff; border: none; border-radius: 20px;
    padding: 8px 16px; font-size: 14px; font-weight: 600; cursor: pointer;
    display: flex; align-items: center; gap: 5px; flex-shrink: 0;
    transition: opacity 0.15s; -webkit-tap-highlight-color: transparent;
  }
  .refresh-btn:active { opacity: 0.75; }
  .refresh-btn.loading { opacity: 0.55; pointer-events: none; }
  .meta { font-size: 13px; color: #8E8E93; margin-bottom: 20px; display: flex; align-items: center; gap: 8px; }
  .status-dot { width: 7px; height: 7px; border-radius: 50%; background: #34C759; display: inline-block; flex-shrink: 0; }
  .status-dot.updating { background: #FF9F0A; animation: pulse 1s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }

  /* ── Section labels ── */
  .section-label { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: #8E8E93; padding: 16px 4px 8px; }
  .section-label:first-child { padding-top: 0; }

  /* ── Suggestion cards ── */
  .list { display: flex; flex-direction: column; gap: 1px; background: rgba(255,255,255,0.08); border-radius: 14px; overflow: hidden; margin-bottom: 4px; }
  .card { background: #1C1C1E; padding: 14px 16px; display: flex; align-items: center; gap: 14px; }
  .card-icon { width: 40px; height: 40px; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 20px; flex-shrink: 0; background: #2C2C2E; }
  .card-body { flex: 1; min-width: 0; }
  .card-name { font-size: 15px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .card-reason { font-size: 13px; color: #8E8E93; margin-top: 3px; line-height: 1.4; }
  .vote-area { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  .vote-btn { width: 36px; height: 36px; border: none; border-radius: 50%; background: rgba(255,255,255,0.08); color: #fff; font-size: 17px; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background 0.15s, transform 0.1s; -webkit-tap-highlight-color: transparent; }
  .vote-btn:active { transform: scale(0.88); }
  .vote-btn.up.voted { background: rgba(52,199,89,0.25); }
  .vote-btn.down.voted { background: rgba(255,59,48,0.25); }
  .score { font-size: 13px; font-weight: 600; min-width: 20px; text-align: center; color: #8E8E93; }
  .score.positive { color: #34C759; }
  .score.negative { color: #FF3B30; }

  .empty { text-align: center; padding: 60px 20px; color: #8E8E93; font-size: 15px; }
  .empty-icon { font-size: 44px; margin-bottom: 12px; opacity: 0.3; }

  /* ── Collapsible panels (shared) ── */
  .panel-wrap { margin-top: 16px; }
  .panel-toggle {
    width: 100%; background: rgba(255,255,255,0.08); border: none;
    border-radius: 12px; color: #fff; padding: 13px 16px;
    font-size: 15px; font-weight: 500; cursor: pointer;
    display: flex; align-items: center; justify-content: space-between;
    -webkit-tap-highlight-color: transparent;
  }
  .panel-toggle:active { background: rgba(255,255,255,0.12); }
  .panel-body { margin-top: 10px; display: none; }
  .panel-body.open { display: block; }

  /* ── Inject panel ── */
  .inject-search {
    width: 100%; background: rgba(255,255,255,0.08);
    border: 1px solid rgba(255,255,255,0.12); border-radius: 10px;
    color: #fff; padding: 10px 14px; font-size: 15px; outline: none; margin-bottom: 8px;
  }
  .inject-search::placeholder { color: #8E8E93; }
  .entity-list { background: rgba(255,255,255,0.08); border-radius: 12px; overflow: hidden; max-height: 320px; overflow-y: auto; }
  .entity-row { background: #1C1C1E; display: flex; align-items: center; padding: 10px 14px; gap: 10px; border-top: 0.5px solid rgba(255,255,255,0.07); }
  .entity-row:first-child { border-top: none; }
  .entity-info { flex: 1; min-width: 0; }
  .entity-name { font-size: 14px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .entity-id { font-size: 11px; color: #8E8E93; margin-top: 1px; }
  .add-btn { background: rgba(0,122,255,0.18); color: #007AFF; border: none; border-radius: 8px; padding: 6px 13px; font-size: 13px; font-weight: 600; cursor: pointer; flex-shrink: 0; transition: background 0.15s; }
  .add-btn:active { background: rgba(0,122,255,0.35); }
  .no-results { padding: 20px; text-align: center; color: #8E8E93; font-size: 14px; }

  /* ── Log panel ── */
  .log-toolbar { display: flex; justify-content: flex-end; margin-bottom: 6px; }
  .log-clear-btn { background: none; border: none; color: #8E8E93; font-size: 12px; cursor: pointer; padding: 0; }
  .log-clear-btn:hover { color: #fff; }
  .log-box {
    background: #0a0a0a; border-radius: 12px; padding: 10px 12px;
    font-family: 'SF Mono', 'Menlo', 'Consolas', monospace;
    font-size: 11px; line-height: 1.65; max-height: 380px; overflow-y: auto;
    border: 1px solid rgba(255,255,255,0.08);
  }
  .log-line { display: flex; gap: 8px; align-items: baseline; min-width: 0; }
  .log-ts { color: #444; flex-shrink: 0; }
  .log-lvl { flex-shrink: 0; font-weight: 700; min-width: 46px; }
  .log-lvl.DEBUG    { color: #8E8E93; }
  .log-lvl.INFO     { color: #34C759; }
  .log-lvl.WARNING  { color: #FF9F0A; }
  .log-lvl.ERROR    { color: #FF3B30; }
  .log-lvl.CRITICAL { color: #FF3B30; }
  .log-msg { color: #bbb; word-break: break-all; }
  .log-empty { color: #444; font-style: italic; }

  /* ── Patterns panel ── */
  .pattern-section-header { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: #8E8E93; padding: 12px 4px 6px; }
  .pattern-section-header:first-child { padding-top: 0; }
  .pattern-card { background: #1C1C1E; border-radius: 12px; padding: 14px 16px; margin-bottom: 8px; }
  .pattern-card:last-child { margin-bottom: 0; }
  .pattern-top { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 8px; }
  .pattern-icon { font-size: 22px; flex-shrink: 0; padding-top: 1px; }
  .pattern-info { flex: 1; min-width: 0; }
  .pattern-name { font-size: 15px; font-weight: 600; margin-bottom: 3px; }
  .pattern-desc { font-size: 13px; color: #8E8E93; line-height: 1.4; }
  .pattern-meta { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
  .badge { font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 20px; }
  .badge-time { background: rgba(0,122,255,0.18); color: #4DA6FF; }
  .badge-days { background: rgba(52,199,89,0.15); color: #34C759; }
  .badge-conf { background: rgba(255,159,10,0.15); color: #FF9F0A; }
  .badge-inst { background: rgba(175,82,222,0.18); color: #BF7AF0; }
  .badge-sev-low { background: rgba(255,159,10,0.15); color: #FF9F0A; }
  .badge-sev-medium { background: rgba(255,149,0,0.2); color: #FF9500; }
  .badge-sev-high { background: rgba(255,59,48,0.2); color: #FF3B30; }
  .pattern-actions { display: flex; gap: 8px; }
  .pattern-btn { flex: 1; padding: 9px 12px; border: none; border-radius: 10px; font-size: 13px; font-weight: 600; cursor: pointer; transition: opacity 0.15s, transform 0.1s; -webkit-tap-highlight-color: transparent; }
  .pattern-btn:active { transform: scale(0.96); opacity: 0.8; }
  .pattern-btn-auto { background: rgba(0,122,255,0.2); color: #007AFF; }
  .pattern-btn-nah { background: rgba(255,255,255,0.08); color: #8E8E93; }
  .pattern-empty { text-align: center; padding: 20px; color: #636366; font-size: 14px; }

  /* ── Toast ── */
  .toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(80px); background: #2C2C2E; color: #fff; padding: 10px 18px; border-radius: 20px; font-size: 14px; font-weight: 500; transition: transform 0.3s cubic-bezier(0.34,1.56,0.64,1); pointer-events: none; white-space: nowrap; box-shadow: 0 4px 20px rgba(0,0,0,0.5); }
  .toast.show { transform: translateX(-50%) translateY(0); }
</style>
</head>
<body>
<div class="topbar">
  <div class="header-icon">✨</div>
  <h1>Smart Suggestions</h1>
  <button class="refresh-btn" id="refresh-btn">⟳ Refresh</button>
</div>
<div class="meta">
  <span class="status-dot" id="status-dot"></span>
  <span id="meta-text">Connecting…</span>
</div>

<div id="list-container"></div>

<div class="panel-wrap">
  <button class="panel-toggle" id="status-toggle">
    <span>🔍 System Status</span>
    <span id="status-arrow">▶</span>
  </button>
  <div class="panel-body" id="status-panel">
    <div id="status-content" style="background:rgba(255,255,255,0.05);border-radius:12px;padding:14px 16px;font-size:13px;line-height:1.7;font-family:'SF Mono','Menlo',monospace;">Loading…</div>
    <button class="refresh-btn" id="analyze-btn" style="margin-top:10px;width:100%;justify-content:center;">🧠 Run Pattern Analysis Now</button>
  </div>
</div>

<div class="panel-wrap" id="patterns-panel-wrap" style="display:none">
  <button class="panel-toggle" id="patterns-toggle">
    <span>🧠 What I Learned <span id="patterns-badge" style="background:rgba(175,82,222,0.25);color:#BF7AF0;font-size:11px;font-weight:700;padding:2px 7px;border-radius:10px;margin-left:6px;vertical-align:middle;"></span></span>
    <span id="patterns-arrow">▶</span>
  </button>
  <div class="panel-body" id="patterns-panel">
    <div id="patterns-content"></div>
  </div>
</div>

<div class="panel-wrap">
  <button class="panel-toggle" id="inject-toggle">
    <span>+ Add entity to suggestions</span>
    <span id="inject-arrow">▶</span>
  </button>
  <div class="panel-body" id="inject-panel">
    <input class="inject-search" id="inject-search" type="search" placeholder="Search entities…" autocomplete="off">
    <div class="entity-list" id="entity-list"></div>
  </div>
</div>

<div class="panel-wrap">
  <button class="panel-toggle" id="log-toggle">
    <span>📋 Live Logs</span>
    <span id="log-arrow">▶</span>
  </button>
  <div class="panel-body" id="log-panel">
    <div class="log-toolbar">
      <button class="log-clear-btn" id="log-clear">Clear</button>
    </div>
    <div class="log-box" id="log-box"><span class="log-empty">No logs yet.</span></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const BASE = location.pathname.replace(/\/+$/, '');

let _feedback = __FEEDBACK__;
let _suggestions = __SUGGESTIONS__;
let _entities = [];
let _status = 'idle';
let _patterns = null;
let _dismissedPatterns = new Set(JSON.parse(sessionStorage.getItem('dismissedPatterns') || '[]'));

function net(eid) {
  const f = _feedback[eid];
  return f ? (f.up || 0) - (f.down || 0) : 0;
}

function domainEmoji(eid) {
  const d = (eid || '').split('.')[0];
  const map = { light:'💡', switch:'🔌', climate:'🌡️', media_player:'📺', cover:'🪟', fan:'💨', lock:'🔒', vacuum:'🤖', scene:'🎨', automation:'🤖', script:'📜', input_boolean:'🔘' };
  return map[d] || '⚙️';
}

function makeRow(s, i) {
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
    </div>`;
}

function render() {
  const dot = document.getElementById('status-dot');
  const metaEl = document.getElementById('meta-text');
  dot.className = 'status-dot' + (_status === 'updating' ? ' updating' : '');
  metaEl.textContent = (_status === 'updating' ? 'Updating…' : _status === 'ready' ? 'Ready' : 'Idle') + ' · ' + new Date().toLocaleTimeString();

  const container = document.getElementById('list-container');
  if (!_suggestions.length) {
    container.innerHTML = '<div class="empty"><div class="empty-icon">✨</div>No suggestions yet. Waiting for next refresh…</div>';
    return;
  }

  const buckets = { suggested: [], scene: [], stretch: [] };
  _suggestions.forEach((s, i) => {
    const domain = (s.entity_id || '').split('.')[0];
    const key = (s.section && buckets[s.section]) ? s.section : domain === 'scene' ? 'scene' : 'suggested';
    buckets[key].push({ s, i });
  });

  const sectionDefs = [
    { key: 'suggested', label: 'Suggested for You' },
    { key: 'scene',     label: 'Scenes' },
    { key: 'stretch',   label: '✨ Worth Trying' },
  ];

  container.innerHTML = sectionDefs
    .filter(({ key }) => buckets[key].length)
    .map(({ key, label }) =>
      `<div class="section-label">${label}</div>` +
      `<div class="list">${buckets[key].map(({ s, i }) => makeRow(s, i)).join('')}</div>`
    ).join('');

  container.querySelectorAll('.vote-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const eid = btn.dataset.eid;
      const vote = btn.dataset.vote;
      btn.classList.add('voted');
      try {
        await fetch(BASE + '/feedback', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ entity_id: eid, vote }),
        });
        if (!_feedback[eid]) _feedback[eid] = { up: 0, down: 0 };
        _feedback[eid][vote] = (_feedback[eid][vote] || 0) + 1;
        showToast(vote === 'up' ? '👍 Upvoted — refreshing…' : '👎 Downvoted — refreshing…');
        render();
      } catch { showToast('⚠️ Vote failed'); }
    });
  });
}

// ── Refresh button ──
document.getElementById('refresh-btn').addEventListener('click', async () => {
  const btn = document.getElementById('refresh-btn');
  btn.classList.add('loading');
  btn.textContent = '…';
  try {
    await fetch(BASE + '/refresh', { method: 'POST' });
    showToast('🔄 Refresh triggered');
  } catch { showToast('⚠️ Refresh failed'); }
  setTimeout(() => { btn.classList.remove('loading'); btn.textContent = '⟳ Refresh'; }, 2000);
});

// ── Inject panel ──
let _injectOpen = false;
document.getElementById('inject-toggle').addEventListener('click', async () => {
  _injectOpen = !_injectOpen;
  document.getElementById('inject-panel').classList.toggle('open', _injectOpen);
  document.getElementById('inject-arrow').textContent = _injectOpen ? '▼' : '▶';
  if (_injectOpen) {
    await loadEntities();
    renderEntities('');
  }
});

document.getElementById('inject-search').addEventListener('input', e => renderEntities(e.target.value));

async function loadEntities() {
  try {
    const r = await fetch(BASE + '/entities');
    _entities = await r.json();
  } catch { _entities = []; }
}

function renderEntities(filter) {
  const q = filter.toLowerCase();
  const matches = _entities
    .filter(e => !q || e.entity_id.includes(q) || (e.name || '').toLowerCase().includes(q))
    .slice(0, 150);
  const el = document.getElementById('entity-list');
  if (!matches.length) {
    el.innerHTML = '<div class="no-results">No entities found</div>';
    return;
  }
  el.innerHTML = matches.map(e => `
    <div class="entity-row">
      <div class="entity-info">
        <div class="entity-name">${e.name || e.entity_id}</div>
        <div class="entity-id">${e.entity_id} · ${e.current_state || ''}</div>
      </div>
      <button class="add-btn" data-eid="${e.entity_id}" data-name="${(e.name || e.entity_id).replace(/"/g, '&quot;')}" data-domain="${e.domain || ''}">Add</button>
    </div>`).join('');

  el.querySelectorAll('.add-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.textContent = '✓';
      btn.disabled = true;
      try {
        await fetch(BASE + '/inject', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ entity_id: btn.dataset.eid, name: btn.dataset.name, domain: btn.dataset.domain }),
        });
        showToast('✅ Added to suggestions');
      } catch { showToast('⚠️ Failed to add entity'); btn.textContent = 'Add'; btn.disabled = false; }
    });
  });
}

// ── Log panel ──
let _logOpen = false;
let _logs = [];
const MAX_LOG_LINES = 200;

document.getElementById('log-toggle').addEventListener('click', async () => {
  _logOpen = !_logOpen;
  document.getElementById('log-panel').classList.toggle('open', _logOpen);
  document.getElementById('log-arrow').textContent = _logOpen ? '▼' : '▶';
  if (_logOpen && _logs.length === 0) {
    await loadLogs();
  }
  if (_logOpen) renderLogs();
});

document.getElementById('log-clear').addEventListener('click', () => {
  _logs = [];
  renderLogs();
});

async function loadLogs() {
  try {
    const r = await fetch(BASE + '/logs');
    _logs = await r.json();
  } catch { _logs = []; }
}

function escHtml(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function makeLogLine(entry) {
  return `<div class="log-line"><span class="log-ts">${entry.ts}</span><span class="log-lvl ${entry.level}">${entry.level}</span><span class="log-msg">${escHtml(entry.msg)}</span></div>`;
}

function renderLogs() {
  const box = document.getElementById('log-box');
  if (!_logs.length) {
    box.innerHTML = '<span class="log-empty">No logs yet.</span>';
    return;
  }
  box.innerHTML = _logs.map(makeLogLine).join('');
  box.scrollTop = box.scrollHeight;
}

function appendLog(entry) {
  _logs.push(entry);
  if (_logs.length > MAX_LOG_LINES) _logs.shift();
  if (!_logOpen) return;
  const box = document.getElementById('log-box');
  // Remove placeholder if present
  const empty = box.querySelector('.log-empty');
  if (empty) empty.remove();
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.insertAdjacentHTML('beforeend', makeLogLine(entry));
  while (box.children.length > MAX_LOG_LINES) box.removeChild(box.firstChild);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2400);
}

// ── WebSocket ──
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(proto + '//' + location.host + BASE + '/ws');
  ws.addEventListener('open', () => { document.getElementById('meta-text').textContent = 'Connected'; });
  ws.addEventListener('message', evt => {
    let msg; try { msg = JSON.parse(evt.data); } catch { return; }
    if (msg.type === 'suggestions') { _suggestions = Array.isArray(msg.data) ? msg.data : []; _status = 'ready'; render(); }
    else if (msg.type === 'status') { _status = msg.state || 'idle'; render(); }
    else if (msg.type === 'log') { appendLog(msg); }
    else if (msg.type === 'system_status') { renderStatusData(msg.data); }
    else if (msg.type === 'patterns') { _patterns = msg.data; updatePatternsBadge(); if (_patternsOpen) renderPatterns(); }
  });
  ws.addEventListener('close', () => {
    document.getElementById('status-dot').className = 'status-dot';
    document.getElementById('meta-text').textContent = 'Disconnected — reconnecting…';
    setTimeout(connectWS, 5000);
  });
}

// ── System Status panel ──
let _statusOpen = false;
document.getElementById('status-toggle').addEventListener('click', async () => {
  _statusOpen = !_statusOpen;
  document.getElementById('status-panel').classList.toggle('open', _statusOpen);
  document.getElementById('status-arrow').textContent = _statusOpen ? '▼' : '▶';
  if (_statusOpen) await refreshStatus();
});

function renderStatusData(s) {
  const el = document.getElementById('status-content');
  if (!el) return;
  const ok = (v) => v ? '🟢' : '🔴';
  const na = (v) => (v !== undefined && v !== null && v !== '') ? v : '—';
  const rows = [
    ['HA connection',     ok(s.ha_connected)    + ' ' + (s.ha_connected ? 'Connected' : 'Disconnected')],
    ['Ollama connection', ok(s.ollama_connected) + ' ' + (s.ollama_connected ? 'Reachable' : 'Unreachable')],
    ['Ollama URL',        na(s.ollama_url)],
    ['Model',             na(s.ollama_model)],
    ['Entity count',      na(s.entity_count)],
    ['Last refresh',      na(s.last_refresh)],
    ['Last analysis',     na(s.last_analysis)],
    ['Patterns loaded',   ok(s.patterns_loaded)  + ' ' + (s.patterns_loaded ? `${s.pattern_routines} routines, ${s.pattern_anomalies} anomalies` : 'None yet')],
    ['Feedback entries',  na(s.feedback_count)],
  ];
  el.innerHTML = rows.map(([k, v]) =>
    `<div style="display:flex;justify-content:space-between;gap:12px;border-bottom:0.5px solid rgba(255,255,255,0.06);padding:3px 0"><span style="color:#8E8E93">${k}</span><span style="text-align:right;color:#fff">${v}</span></div>`
  ).join('');
}

async function refreshStatus() {
  const el = document.getElementById('status-content');
  try {
    const r = await fetch(BASE + '/status');
    const s = await r.json();
    renderStatusData(s);
  } catch (e) {
    if (el) el.textContent = '⚠️ Could not load status: ' + e;
  }
}

// ── Analyze button ──
document.getElementById('analyze-btn').addEventListener('click', async () => {
  const btn = document.getElementById('analyze-btn');
  btn.classList.add('loading');
  btn.textContent = '⏳ Analyzing…';
  try {
    await fetch(BASE + '/analyze', { method: 'POST' });
    showToast('🧠 Pattern analysis started — check logs for progress');
  } catch { showToast('⚠️ Could not start analysis'); }
  setTimeout(() => {
    btn.classList.remove('loading');
    btn.textContent = '🧠 Run Pattern Analysis Now';
  }, 3000);
});

// ── Patterns panel ──
let _patternsOpen = false;
document.getElementById('patterns-toggle').addEventListener('click', () => {
  _patternsOpen = !_patternsOpen;
  document.getElementById('patterns-panel').classList.toggle('open', _patternsOpen);
  document.getElementById('patterns-arrow').textContent = _patternsOpen ? '▼' : '▶';
  if (_patternsOpen && _patterns) renderPatterns();
});

function pct(v) { return Math.round((v || 0) * 100) + '%'; }

function makePatternRoutineCard(r, key) {
  if (_dismissedPatterns.has(key)) return '';
  const days = Array.isArray(r.days) ? r.days.join(' ') : (r.days || '');
  const inst = r.instances ? `<span class="badge badge-inst">×${r.instances}</span>` : '';
  const time = r.typical_time ? `<span class="badge badge-time">⏰ ${r.typical_time}</span>` : '';
  const daysB = days ? `<span class="badge badge-days">${days}</span>` : '';
  const conf = `<span class="badge badge-conf">${pct(r.confidence)} conf</span>`;
  return `<div class="pattern-card" id="pc-${key}">
    <div class="pattern-top">
      <div class="pattern-icon">🔁</div>
      <div class="pattern-info">
        <div class="pattern-name">${escHtml(r.name || r.entity_id)}</div>
        <div class="pattern-desc">${escHtml(r.entity_id)}</div>
      </div>
    </div>
    <div class="pattern-meta">${time}${daysB}${conf}${inst}</div>
    <div class="pattern-actions">
      <button class="pattern-btn pattern-btn-auto" data-key="${key}" data-eid="${escHtml(r.entity_id)}" data-name="${escHtml(r.name||r.entity_id)}" data-type="routine">Automate it! 🚀</button>
      <button class="pattern-btn pattern-btn-nah" data-key="${key}" data-dismiss="1">Nah</button>
    </div>
  </div>`;
}

function makePatternCorrelCard(c, key) {
  if (_dismissedPatterns.has(key)) return '';
  const inst = c.instances ? `<span class="badge badge-inst">×${c.instances}</span>` : '';
  const conf = `<span class="badge badge-conf">${pct(c.confidence)} conf</span>`;
  const win = c.window_minutes ? `<span class="badge badge-time">within ${c.window_minutes}m</span>` : '';
  return `<div class="pattern-card" id="pc-${key}">
    <div class="pattern-top">
      <div class="pattern-icon">🔗</div>
      <div class="pattern-info">
        <div class="pattern-name">${escHtml(c.entity_a)} → ${escHtml(c.entity_b)}</div>
        <div class="pattern-desc">${escHtml(c.pattern || '')}</div>
      </div>
    </div>
    <div class="pattern-meta">${win}${conf}${inst}</div>
    <div class="pattern-actions">
      <button class="pattern-btn pattern-btn-auto" data-key="${key}" data-eid="${escHtml(c.entity_b)}" data-eid-a="${escHtml(c.entity_a)}" data-name="${escHtml(c.pattern||c.entity_b)}" data-type="correlation">Automate it! 🚀</button>
      <button class="pattern-btn pattern-btn-nah" data-key="${key}" data-dismiss="1">Nah</button>
    </div>
  </div>`;
}

function makePatternAnomalyCard(a, key) {
  if (_dismissedPatterns.has(key)) return '';
  const sevClass = 'badge-sev-' + (a.severity || 'low');
  const sev = `<span class="badge ${sevClass}">⚠️ ${a.severity || 'low'}</span>`;
  return `<div class="pattern-card" id="pc-${key}">
    <div class="pattern-top">
      <div class="pattern-icon">🚨</div>
      <div class="pattern-info">
        <div class="pattern-name">${escHtml(a.entity_id)}</div>
        <div class="pattern-desc">${escHtml(a.description || '')}</div>
      </div>
    </div>
    <div class="pattern-meta">${sev}</div>
    <div class="pattern-actions">
      <button class="pattern-btn pattern-btn-nah" data-key="${key}" data-dismiss="1">Dismiss</button>
    </div>
  </div>`;
}

function renderPatterns() {
  const el = document.getElementById('patterns-content');
  if (!_patterns) { el.innerHTML = '<div class="pattern-empty">No pattern analysis yet. Hit "Run Pattern Analysis Now" in System Status.</div>'; return; }
  const routines = (_patterns.routines || []).filter(r => !_dismissedPatterns.has('r_' + r.entity_id + '_' + (r.name||'')));
  const correls = (_patterns.correlations || []).filter(c => !_dismissedPatterns.has('c_' + c.entity_a + '_' + c.entity_b));
  const anomalies = (_patterns.anomalies || []).filter(a => !_dismissedPatterns.has('a_' + a.entity_id));
  let html = '';
  if (routines.length) {
    html += '<div class="pattern-section-header">🔁 Daily Routines</div>';
    html += routines.map(r => makePatternRoutineCard(r, 'r_' + r.entity_id + '_' + (r.name||''))).join('');
  }
  if (correls.length) {
    html += '<div class="pattern-section-header">🔗 Entity Correlations</div>';
    html += correls.map(c => makePatternCorrelCard(c, 'c_' + c.entity_a + '_' + c.entity_b)).join('');
  }
  if (anomalies.length) {
    html += '<div class="pattern-section-header">🚨 Anomalies</div>';
    html += anomalies.map(a => makePatternAnomalyCard(a, 'a_' + a.entity_id)).join('');
  }
  if (!html) html = '<div class="pattern-empty">All patterns reviewed — nice work! 🎉</div>';
  el.innerHTML = html;

  // Wire up buttons
  el.querySelectorAll('.pattern-btn-nah[data-dismiss]').forEach(btn => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.key;
      _dismissedPatterns.add(key);
      sessionStorage.setItem('dismissedPatterns', JSON.stringify([..._dismissedPatterns]));
      const card = document.getElementById('pc-' + key);
      if (card) card.style.transition = 'opacity 0.25s'; if (card) card.style.opacity = '0';
      setTimeout(() => { renderPatterns(); updatePatternsBadge(); }, 260);
    });
  });
  el.querySelectorAll('.pattern-btn-auto').forEach(btn => {
    btn.addEventListener('click', async () => {
      const eid = btn.dataset.eid;
      const name = btn.dataset.name;
      const type = btn.dataset.type;
      btn.textContent = '⏳ Building…'; btn.disabled = true;
      try {
        const suggestion = { entity_id: eid, name: name, automation_context: { trigger_type: type, entity_a: btn.dataset.eidA } };
        const resp = await fetch(BASE + '/save_automation', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ suggestion }) });
        showToast('🚀 Automation queued — check logs!');
        btn.textContent = '✅ Queued';
      } catch { showToast('⚠️ Failed to queue'); btn.textContent = 'Automate it! 🚀'; btn.disabled = false; }
    });
  });
}

function updatePatternsBadge() {
  if (!_patterns) return;
  const total = (_patterns.routines||[]).length + (_patterns.correlations||[]).length + (_patterns.anomalies||[]).length;
  const remaining = total - [..._dismissedPatterns].filter(k => {
    return (_patterns.routines||[]).some(r => 'r_'+r.entity_id+'_'+(r.name||'') === k) ||
           (_patterns.correlations||[]).some(c => 'c_'+c.entity_a+'_'+c.entity_b === k) ||
           (_patterns.anomalies||[]).some(a => 'a_'+a.entity_id === k);
  }).length;
  const badge = document.getElementById('patterns-badge');
  if (badge) badge.textContent = remaining > 0 ? remaining + ' patterns' : 'reviewed ✓';
  const wrap = document.getElementById('patterns-panel-wrap');
  if (wrap) wrap.style.display = '';
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
        self._app.router.add_post("/refresh", self._refresh_handler)
        self._app.router.add_get("/entities", self._entities_handler)
        self._app.router.add_post("/inject", self._inject_handler)
        self._app.router.add_get("/logs", self._logs_handler)
        self._app.router.add_get("/status", self._status_handler)
        self._app.router.add_post("/analyze", self._analyze_handler)
        self._app.router.add_post("/save_automation", self._handle_post_save_automation)
        self._runner: web.AppRunner | None = None
        self._last_suggestions: list = []
        self._last_status: str = "idle"
        self._feedback: dict = {}
        self._known_entities: list = []
        self._feedback_cb = None
        self._refresh_cb = None
        self._analyze_cb = None
        self._automation_handler = None
        self._log_buffer: deque = deque(maxlen=_LOG_BUFFER_SIZE)
        self._system_status: dict = {}
        self._patterns: dict = {}

    def register_feedback_handler(self, cb) -> None:
        self._feedback_cb = cb

    def register_refresh_handler(self, cb) -> None:
        self._refresh_cb = cb

    def register_analyze_handler(self, cb) -> None:
        self._analyze_cb = cb

    def register_automation_handler(self, handler) -> None:
        self._automation_handler = handler

    async def broadcast_automation_result(self, result: dict) -> None:
        await self.broadcast({"type": "automation_result", **result})

    async def _handle_save_automation(self, suggestion: dict) -> None:
        if self._automation_handler:
            await self._automation_handler(suggestion)
        else:
            _LOGGER.warning("save_automation received but no handler registered")

    async def _handle_post_save_automation(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        asyncio.create_task(self._handle_save_automation(data.get("suggestion", {})))
        return web.json_response({"status": "queued"})

    def set_feedback(self, feedback: dict) -> None:
        self._feedback = feedback

    def set_known_entities(self, entities: list) -> None:
        self._known_entities = entities

    def set_patterns(self, patterns: dict) -> None:
        self._patterns = patterns
        try:
            import asyncio  # noqa: PLC0415
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast({"type": "patterns", "data": patterns}))
        except RuntimeError:
            pass

    def set_system_status(self, status: dict) -> None:
        self._system_status = status
        # Schedule a live push to all connected clients
        try:
            import asyncio  # noqa: PLC0415
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast({"type": "system_status", "data": status}))
        except RuntimeError:
            pass  # No running loop yet (called during startup before ws server started)

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
            .replace("__FEEDBACK__", json.dumps({}))
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

    async def _refresh_handler(self, request: web.Request) -> web.Response:
        if self._refresh_cb:
            await self._refresh_cb()
        return web.json_response({"ok": True})

    async def _entities_handler(self, request: web.Request) -> web.Response:
        return web.json_response(self._known_entities)

    async def _logs_handler(self, request: web.Request) -> web.Response:
        return web.json_response(list(self._log_buffer))

    async def _status_handler(self, request: web.Request) -> web.Response:
        return web.json_response(self._system_status)

    async def _analyze_handler(self, request: web.Request) -> web.Response:
        if self._analyze_cb:
            asyncio.get_running_loop().create_task(self._analyze_cb())
        return web.json_response({"ok": True})

    async def _inject_handler(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            entity_id = data.get("entity_id", "")
            if not entity_id:
                return web.json_response({"ok": False}, status=400)
            name = data.get("name", entity_id)
            domain = data.get("domain") or entity_id.split(".")[0]
            suggestion = {
                "entity_id": entity_id,
                "name": name,
                "action": "toggle",
                "reason": "Manually added from web UI",
                "section": "scene" if domain == "scene" else "suggested",
                "icon": _DOMAIN_ICONS.get(domain, "mdi:star-circle"),
                "type": "entity",
            }
            # Insert at top, remove any existing entry for the same entity
            self._last_suggestions = [
                s for s in self._last_suggestions if s.get("entity_id") != entity_id
            ]
            self._last_suggestions.insert(0, suggestion)
            await self.broadcast({"type": "suggestions", "data": self._last_suggestions})
            _LOGGER.info("Injected entity: %s", entity_id)
            return web.json_response({"ok": True})
        except Exception as e:
            _LOGGER.error("Inject handler error: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        _LOGGER.info("Card connected (%d total)", len(self._clients))

        await self._send(ws, {"type": "status", "state": self._last_status})
        if self._last_suggestions:
            await self._send(ws, {"type": "suggestions", "data": self._last_suggestions})
        if self._system_status:
            await self._send(ws, {"type": "system_status", "data": self._system_status})
        if self._patterns:
            await self._send(ws, {"type": "patterns", "data": self._patterns})

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        msg_type = data.get("type", "")
                        if msg_type == "save_automation":
                            suggestion = data.get("suggestion", {})
                            asyncio.create_task(self._handle_save_automation(suggestion))
                    except Exception as e:
                        _LOGGER.debug("WS message parse error: %s", e)
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
        dead = set()
        for ws in list(self._clients):
            try:
                await ws.send_str(json.dumps(payload))
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def broadcast_suggestions(self, suggestions: list) -> None:
        self._last_suggestions = suggestions
        self._last_status = "ready"
        await self.broadcast({"type": "suggestions", "data": suggestions})
        await self.broadcast({"type": "status", "state": "ready"})

    async def broadcast_status(self, state: str) -> None:
        self._last_status = state
        await self.broadcast({"type": "status", "state": state})

    async def broadcast_log(self, level: str, msg: str) -> None:
        entry = {"type": "log", "ts": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg}
        self._log_buffer.append(entry)
        await self.broadcast(entry)
