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
    min-height: 100vh; padding: 0 0 72px;
  }

  /* ── Header ── */
  .header { position: sticky; top: 0; z-index: 100; background: rgba(0,0,0,0.85); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); padding: 16px 16px 0; border-bottom: 0.5px solid rgba(255,255,255,0.08); }
  .topbar { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
  .header-icon { width: 36px; height: 36px; background: #007AFF; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 20px; flex-shrink: 0; }
  h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.4px; flex: 1; }
  .meta { font-size: 12px; color: #8E8E93; padding-bottom: 10px; display: flex; align-items: center; gap: 8px; }
  .status-dot { width: 7px; height: 7px; border-radius: 50%; background: #34C759; display: inline-block; flex-shrink: 0; }
  .status-dot.updating { background: #FF9F0A; animation: pulse 1s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }

  /* ── Tab bar ── */
  .tab-bar { display: flex; gap: 0; }
  .tab-btn {
    flex: 1; background: none; border: none; border-bottom: 2.5px solid transparent;
    color: #636366; padding: 10px 8px; font-size: 13px; font-weight: 600;
    cursor: pointer; transition: all 0.2s; text-align: center;
    -webkit-tap-highlight-color: transparent;
  }
  .tab-btn.active { color: #007AFF; border-bottom-color: #007AFF; }
  .tab-btn .tab-count { font-size: 10px; background: rgba(255,255,255,0.12); padding: 1px 6px; border-radius: 8px; margin-left: 4px; vertical-align: middle; }
  .tab-btn.active .tab-count { background: rgba(0,122,255,0.2); color: #007AFF; }

  /* ── Tab pages ── */
  .tab-page { display: none; padding: 16px; animation: fadeIn 0.2s ease; }
  .tab-page.active { display: block; }
  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

  /* ── Action buttons ── */
  .action-row { display: flex; gap: 8px; margin-bottom: 16px; }
  .action-btn {
    flex: 1; background: rgba(255,255,255,0.08); color: #fff; border: none;
    border-radius: 12px; padding: 12px 14px; font-size: 14px; font-weight: 600;
    cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px;
    transition: all 0.15s; -webkit-tap-highlight-color: transparent;
  }
  .action-btn:active { transform: scale(0.97); opacity: 0.8; }
  .action-btn.primary { background: #007AFF; }
  .action-btn.loading { opacity: 0.55; pointer-events: none; }

  /* ── Section headers ── */
  .sh { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: #636366; padding: 16px 0 8px; }
  .sh:first-child { padding-top: 0; }

  /* ── Suggestion cards ── */
  .sug-list { display: flex; flex-direction: column; gap: 1px; background: rgba(255,255,255,0.06); border-radius: 14px; overflow: hidden; margin-bottom: 12px; }
  .sug { background: #1C1C1E; padding: 14px 16px; display: flex; gap: 14px; align-items: flex-start; }
  .sug-icon { width: 42px; height: 42px; border-radius: 12px; display: flex; align-items: center; justify-content: center; font-size: 22px; flex-shrink: 0; background: #2C2C2E; }
  .sug-body { flex: 1; min-width: 0; }
  .sug-name { font-size: 16px; font-weight: 600; margin-bottom: 2px; }
  .sug-eid { font-size: 11px; color: #48484A; font-family: 'SF Mono','Menlo',monospace; margin-bottom: 4px; }
  .sug-transition { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
  .state-chip { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; color: #fff; text-transform: uppercase; letter-spacing: 0.5px; }
  .state-arrow { color: #8E8E93; font-size: 14px; }
  .action-chip { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; background: #007AFF; color: #fff; text-transform: uppercase; letter-spacing: 0.5px; }
  .sug-reason { font-size: 14px; color: #8E8E93; line-height: 1.45; }
  .sug-actions { display: flex; flex-direction: column; gap: 4px; flex-shrink: 0; align-items: center; padding-top: 2px; }
  .vote-btn { width: 32px; height: 32px; border: none; border-radius: 8px; background: rgba(255,255,255,0.06); color: #636366; font-size: 15px; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: all 0.15s; -webkit-tap-highlight-color: transparent; }
  .vote-btn:active { transform: scale(0.88); }
  .vote-btn.up.voted { background: rgba(52,199,89,0.2); color: #34C759; }
  .vote-btn.down.voted { background: rgba(255,59,48,0.2); color: #FF3B30; }
  .deny-btn { width: 32px; height: 32px; border: none; border-radius: 8px; background: none; color: #48484A; font-size: 13px; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: all 0.15s; }
  .deny-btn:hover { background: rgba(255,59,48,0.12); color: #FF3B30; }

  .empty { text-align: center; padding: 60px 20px; color: #636366; font-size: 15px; }
  .empty-icon { font-size: 44px; margin-bottom: 12px; opacity: 0.3; }

  /* ── Discoveries ── */
  .disc-card { background: #1C1C1E; border-radius: 14px; padding: 16px; margin-bottom: 10px; border-left: 3px solid transparent; }
  .disc-card.routine { border-left-color: #007AFF; }
  .disc-card.correlation { border-left-color: #34C759; }
  .disc-card.anomaly { border-left-color: #FF3B30; }
  .disc-card.new { background: linear-gradient(135deg, rgba(175,82,222,0.08), rgba(0,122,255,0.05)); border: 1px solid rgba(175,82,222,0.2); animation: revealIn 0.4s ease-out; }
  @keyframes revealIn { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: none; } }
  .disc-top { display: flex; align-items: flex-start; gap: 12px; }
  .disc-icon { font-size: 24px; flex-shrink: 0; }
  .disc-info { flex: 1; min-width: 0; }
  .disc-name { font-size: 16px; font-weight: 600; margin-bottom: 2px; }
  .disc-desc { font-size: 14px; color: #8E8E93; line-height: 1.45; margin-bottom: 8px; }
  .disc-eid { font-size: 11px; color: #48484A; font-family: 'SF Mono','Menlo',monospace; margin-bottom: 6px; }
  .disc-meta { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
  .badge { font-size: 11px; font-weight: 700; padding: 3px 9px; border-radius: 20px; }
  .badge-new { background: rgba(175,82,222,0.2); color: #BF7AF0; }
  .badge-time { background: rgba(0,122,255,0.15); color: #4DA6FF; }
  .badge-days { background: rgba(52,199,89,0.12); color: #34C759; }
  .badge-conf { background: rgba(255,159,10,0.12); color: #FF9F0A; }
  .badge-inst { background: rgba(175,82,222,0.15); color: #BF7AF0; }
  .badge-sev-low { background: rgba(255,159,10,0.12); color: #FF9F0A; }
  .badge-sev-medium { background: rgba(255,149,0,0.15); color: #FF9500; }
  .badge-sev-high { background: rgba(255,59,48,0.15); color: #FF3B30; }
  .disc-actions { display: flex; gap: 8px; }
  .disc-btn { flex: 1; padding: 10px 12px; border: none; border-radius: 10px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.15s; -webkit-tap-highlight-color: transparent; text-align: center; }
  .disc-btn:active { transform: scale(0.97); opacity: 0.8; }
  .disc-btn-auto { background: rgba(0,122,255,0.15); color: #007AFF; }
  .disc-btn-nah { background: rgba(255,255,255,0.06); color: #636366; }
  .disc-empty { text-align: center; padding: 40px 20px; color: #48484A; font-size: 14px; }

  /* ── Trends ── */
  .trend-card { background: #1C1C1E; border-radius: 14px; overflow: hidden; margin-bottom: 10px; }
  .trend-row { padding: 14px 16px; border-bottom: 0.5px solid rgba(255,255,255,0.05); }
  .trend-row:last-child { border-bottom: none; }
  .trend-label { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
  .trend-value { font-size: 13px; color: #8E8E93; line-height: 1.5; }
  .bar-row { display: flex; align-items: center; gap: 8px; margin-top: 3px; }
  .bar-label { width: 30px; font-size: 11px; color: #636366; text-align: right; flex-shrink: 0; }
  .bar-track { flex: 1; height: 10px; background: rgba(255,255,255,0.05); border-radius: 5px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 5px; transition: width 0.4s ease; }
  .bar-count { width: 20px; font-size: 11px; color: #48484A; }

  /* ── System tab ── */
  .sys-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 16px; }
  .sys-card { background: #1C1C1E; border-radius: 12px; padding: 14px; }
  .sys-label { font-size: 11px; color: #636366; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
  .sys-val { font-size: 15px; font-weight: 600; }
  .sys-card.full { grid-column: 1 / -1; }
  .inject-search {
    width: 100%; background: rgba(255,255,255,0.08);
    border: 1px solid rgba(255,255,255,0.1); border-radius: 10px;
    color: #fff; padding: 10px 14px; font-size: 15px; outline: none; margin-bottom: 8px;
  }
  .inject-search::placeholder { color: #48484A; }
  .entity-list { background: #1C1C1E; border-radius: 12px; overflow: hidden; max-height: 320px; overflow-y: auto; }
  .entity-row { display: flex; align-items: center; padding: 10px 14px; gap: 10px; border-top: 0.5px solid rgba(255,255,255,0.05); }
  .entity-row:first-child { border-top: none; }
  .entity-info { flex: 1; min-width: 0; }
  .entity-name { font-size: 14px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .entity-id { font-size: 11px; color: #48484A; margin-top: 1px; }
  .add-btn { background: rgba(0,122,255,0.15); color: #007AFF; border: none; border-radius: 8px; padding: 6px 14px; font-size: 13px; font-weight: 600; cursor: pointer; flex-shrink: 0; }
  .no-results { padding: 20px; text-align: center; color: #48484A; font-size: 14px; }
  .deny-row { display: flex; align-items: center; padding: 10px 14px; gap: 10px; border-top: 0.5px solid rgba(255,255,255,0.05); }
  .deny-row:first-child { border-top: none; }
  .deny-eid { flex: 1; font-size: 13px; color: #8E8E93; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .undeny-btn { background: rgba(52,199,89,0.15); color: #34C759; border: none; border-radius: 8px; padding: 6px 14px; font-size: 13px; font-weight: 600; cursor: pointer; flex-shrink: 0; }

  /* ── Log ── */
  .log-box {
    background: #0a0a0a; border-radius: 12px; padding: 10px 12px;
    font-family: 'SF Mono', 'Menlo', 'Consolas', monospace;
    font-size: 11px; line-height: 1.65; max-height: 400px; overflow-y: auto;
    border: 1px solid rgba(255,255,255,0.06);
  }
  .log-line { display: flex; gap: 8px; align-items: baseline; min-width: 0; }
  .log-ts { color: #333; flex-shrink: 0; }
  .log-lvl { flex-shrink: 0; font-weight: 700; min-width: 46px; }
  .log-lvl.DEBUG    { color: #636366; }
  .log-lvl.INFO     { color: #34C759; }
  .log-lvl.WARNING  { color: #FF9F0A; }
  .log-lvl.ERROR    { color: #FF3B30; }
  .log-lvl.CRITICAL { color: #FF3B30; }
  .log-msg { color: #999; word-break: break-all; }
  .log-empty { color: #333; font-style: italic; }

  /* ── Toast ── */
  .toast { position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%) translateY(80px); background: #2C2C2E; color: #fff; padding: 10px 20px; border-radius: 22px; font-size: 14px; font-weight: 500; transition: transform 0.3s cubic-bezier(0.34,1.56,0.64,1); pointer-events: none; white-space: nowrap; box-shadow: 0 4px 24px rgba(0,0,0,0.6); z-index: 200; }
  .toast.show { transform: translateX(-50%) translateY(0); }
</style>
</head>
<body>

<div class="header">
  <div class="topbar">
    <div class="header-icon">✨</div>
    <h1>Smart Suggestions</h1>
  </div>
  <div class="meta">
    <span class="status-dot" id="status-dot"></span>
    <span id="meta-text">Connecting…</span>
  </div>
  <div class="tab-bar">
    <button class="tab-btn active" data-tab="suggestions">Suggestions</button>
    <button class="tab-btn" data-tab="discoveries">Discoveries <span class="tab-count" id="disc-count" style="display:none"></span></button>
    <button class="tab-btn" data-tab="system">System</button>
  </div>
</div>

<!-- ══ TAB: Suggestions ══ -->
<div class="tab-page active" id="page-suggestions">
  <div class="action-row">
    <button class="action-btn primary" id="refresh-btn">⟳ Refresh</button>
    <button class="action-btn" id="analyze-btn">🧠 Analyze</button>
  </div>
  <div id="first-run-banner" style="display:none;background:linear-gradient(135deg,rgba(0,122,255,0.1),rgba(52,199,89,0.06));border:1px solid rgba(0,122,255,0.15);border-radius:14px;padding:20px;margin-bottom:16px;text-align:center;">
    <div style="font-size:16px;font-weight:600;margin-bottom:8px;">🧠 No pattern analysis yet</div>
    <div style="font-size:14px;color:#8E8E93;margin-bottom:14px;">Run a deep analysis to discover routines, correlations, and anomalies.</div>
    <button class="action-btn primary" id="first-run-analyze" style="max-width:200px;margin:0 auto;">Analyze Now</button>
  </div>
  <div class="revelations" id="revelations"></div>
  <div id="list-container"></div>
</div>

<!-- ══ TAB: Discoveries ══ -->
<div class="tab-page" id="page-discoveries">
  <div class="action-row">
    <button class="action-btn" id="analyze-btn-2">🧠 Run Analysis</button>
  </div>
  <div id="disc-trends"></div>
  <div id="disc-patterns"></div>
</div>

<!-- ══ TAB: System ══ -->
<div class="tab-page" id="page-system">
  <div id="sys-status-grid" class="sys-grid"></div>
  <div class="sh">Add Entity</div>
  <input class="inject-search" id="inject-search" type="search" placeholder="Search entities…" autocomplete="off">
  <div class="entity-list" id="entity-list"></div>
  <div id="deny-section" style="display:none">
    <div class="sh" style="padding-top:20px;">Blocked Entities</div>
    <div class="entity-list" id="deny-content"></div>
  </div>
  <div class="sh" style="padding-top:20px;">Live Logs <button id="log-clear" style="background:none;border:none;color:#636366;font-size:11px;cursor:pointer;float:right;">Clear</button></div>
  <div class="log-box" id="log-box"><span class="log-empty">No logs yet.</span></div>
</div>

<div class="toast" id="toast"></div>

<script>
const BASE = location.pathname.replace(/\/+$/, '');
let _feedback = __FEEDBACK__;
let _suggestions = __SUGGESTIONS__;
let _entities = [];
let _status = 'idle';
let _patterns = null;
let _denyList = new Set();
let _newPatternKeys = new Set();
let _seenRevelations = new Set(JSON.parse(localStorage.getItem('seenRevelations') || '[]'));
let _dismissedPatterns = new Set(JSON.parse(sessionStorage.getItem('dismissedPatterns') || '[]'));
let _logs = [];
const MAX_LOG_LINES = 200;
let _entitiesLoaded = false;

function escHtml(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function net(eid) { const f=_feedback[eid]; return f?(f.up||0)-(f.down||0):0; }
function pct(v) { return Math.round((v||0)*100)+'%'; }
function domainEmoji(eid) {
  const d=(eid||'').split('.')[0];
  return {light:'💡',switch:'🔌',climate:'🌡️',media_player:'📺',cover:'🪟',fan:'💨',lock:'🔒',vacuum:'🤖',scene:'🎨',automation:'🤖',script:'📜',input_boolean:'🔘'}[d]||'⚙️';
}

// ── Tab navigation ──
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-page').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('page-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'discoveries') { renderDiscoveries(); renderTrends(); }
    if (btn.dataset.tab === 'system') { refreshStatus(); if (!_entitiesLoaded) { loadEntities().then(() => renderEntities('')); } renderLogs(); }
  });
});

// ── Suggestion rendering ──
function actionLabel(action) {
  const map = {activate:'Activate',trigger:'Trigger',turn_on:'Turn On',turn_off:'Turn Off',lock:'Lock',unlock:'Unlock',open_cover:'Open',close_cover:'Close'};
  return map[action] || action || '?';
}
function stateColor(state) {
  if (!state) return '#999';
  const s = state.toLowerCase();
  if (['on','open','unlocked','playing','home'].includes(s)) return '#4CAF50';
  if (['off','closed','locked','idle','paused'].includes(s)) return '#78909C';
  if (['error','unavailable'].includes(s)) return '#f44336';
  return '#FFB300';
}
function makeRow(s, i) {
  const cur = s.current_state || '?';
  const act = s.action || '';
  return `<div class="sug">
    <div class="sug-icon">${domainEmoji(s.entity_id)}</div>
    <div class="sug-body">
      <div class="sug-name">${escHtml(s.name || s.entity_id || '')}</div>
      <div class="sug-eid">${(s.entity_id||'').split('.')[0]} · ${s.entity_id||''}</div>
      <div class="sug-transition">
        <span class="state-chip" style="background:${stateColor(cur)}">${escHtml(cur)}</span>
        <span class="state-arrow">→</span>
        <span class="action-chip">${escHtml(actionLabel(act))}</span>
      </div>
      <div class="sug-reason">${escHtml(s.reason || '')}</div>
    </div>
    <div class="sug-actions">
      <button class="vote-btn up" data-eid="${s.entity_id}" data-vote="up">👍</button>
      <button class="vote-btn down" data-eid="${s.entity_id}" data-vote="down">👎</button>
      <button class="deny-btn" data-eid="${s.entity_id}" title="Block">🚫</button>
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
    container.innerHTML = '<div class="empty"><div class="empty-icon">✨</div>No suggestions yet.</div>';
    return;
  }
  const buckets = { suggested:[], scene:[], stretch:[] };
  _suggestions.forEach((s,i) => {
    const d=(s.entity_id||'').split('.')[0];
    const key=(s.section&&buckets[s.section])?s.section:d==='scene'?'scene':'suggested';
    buckets[key].push({s,i});
  });
  container.innerHTML = [
    {key:'suggested',label:'Suggested for You'},
    {key:'scene',label:'Scenes'},
    {key:'stretch',label:'Worth Trying'},
  ].filter(({key})=>buckets[key].length).map(({key,label})=>
    `<div class="sh">${label}</div><div class="sug-list">${buckets[key].map(({s,i})=>makeRow(s,i)).join('')}</div>`
  ).join('');
  // Wire vote + deny buttons
  container.querySelectorAll('.vote-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const eid=btn.dataset.eid, vote=btn.dataset.vote;
      btn.classList.add('voted');
      try {
        await fetch(BASE+'/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entity_id:eid,vote})});
        if(!_feedback[eid])_feedback[eid]={up:0,down:0};
        _feedback[eid][vote]=(_feedback[eid][vote]||0)+1;
        showToast(vote==='up'?'👍 Upvoted':'👎 Downvoted');
        render();
      } catch { showToast('⚠️ Vote failed'); }
    });
  });
  container.querySelectorAll('.deny-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const eid=btn.dataset.eid;
      try {
        await fetch(BASE+'/deny',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entity_id:eid})});
        _denyList.add(eid); showToast('🚫 Blocked'); renderDenyPanel();
      } catch { showToast('⚠️ Block failed'); }
    });
  });
}

// ── Action buttons ──
async function triggerAnalyze(btn) {
  const orig=btn.textContent;
  btn.classList.add('loading'); btn.textContent='⏳ Analyzing…';
  try { await fetch(BASE+'/analyze',{method:'POST'}); showToast('🧠 Analysis started'); }
  catch { showToast('⚠️ Failed'); }
  setTimeout(()=>{ btn.classList.remove('loading'); btn.textContent=orig; },3000);
}
document.getElementById('refresh-btn').addEventListener('click', async function() {
  this.classList.add('loading'); this.textContent='…';
  try { await fetch(BASE+'/refresh',{method:'POST'}); showToast('🔄 Refreshing'); }
  catch { showToast('⚠️ Failed'); }
  setTimeout(()=>{ this.classList.remove('loading'); this.textContent='⟳ Refresh'; },2000);
});
document.getElementById('analyze-btn').addEventListener('click', function(){ triggerAnalyze(this); });
document.getElementById('analyze-btn-2').addEventListener('click', function(){ triggerAnalyze(this); });
document.getElementById('first-run-analyze').addEventListener('click', function(){ triggerAnalyze(this); });

function updateFirstRunBanner(s) {
  const b=document.getElementById('first-run-banner');
  if(b) b.style.display=(s&&s.last_analysis&&s.last_analysis!=='Never')?'none':'';
}

// ── Discoveries tab ──
function renderDiscoveries() {
  const el = document.getElementById('disc-patterns');
  if (!_patterns) { el.innerHTML = '<div class="disc-empty">No pattern analysis yet. Hit Analyze to start.</div>'; return; }
  const routines = (_patterns.routines||[]).filter(r=>!_dismissedPatterns.has('r_'+r.entity_id+'_'+(r.name||'')));
  const correls = (_patterns.correlations||[]).filter(c=>!_dismissedPatterns.has('c_'+c.entity_a+'_'+c.entity_b));
  const anomalies = (_patterns.anomalies||[]).filter(a=>!_dismissedPatterns.has('a_'+a.entity_id));
  let html = '';
  // Update tab count
  const total = routines.length + correls.length + anomalies.length;
  const countEl = document.getElementById('disc-count');
  if (total > 0) { countEl.style.display = ''; countEl.textContent = total; } else { countEl.style.display = 'none'; }

  if (routines.length) {
    html += '<div class="sh">Routines</div>';
    html += routines.map(r => {
      const key = 'r_'+r.entity_id+'_'+(r.name||'');
      const isNew = _newPatternKeys.has(key) && !_seenRevelations.has(key);
      const days = Array.isArray(r.days)?r.days.join(' '):'';
      return `<div class="disc-card routine${isNew?' new':''}" id="dc-${key}">
        <div class="disc-top"><div class="disc-icon">🔁</div><div class="disc-info">
          <div class="disc-name">${escHtml(r.name||r.entity_id)}</div>
          <div class="disc-eid">${escHtml(r.entity_id)}</div>
          <div class="disc-meta">${isNew?'<span class="badge badge-new">NEW</span>':''}${r.typical_time?`<span class="badge badge-time">⏰ ${r.typical_time}</span>`:''}${days?`<span class="badge badge-days">${days}</span>`:''}
            <span class="badge badge-conf">${pct(r.confidence)}</span>${r.instances?`<span class="badge badge-inst">×${r.instances}</span>`:''}</div>
        </div></div>
        <div class="disc-actions">
          <button class="disc-btn disc-btn-auto" data-key="${key}" data-eid="${escHtml(r.entity_id)}" data-name="${escHtml(r.name||r.entity_id)}" data-type="routine">Automate</button>
          <button class="disc-btn disc-btn-nah" data-key="${key}" data-dismiss="1">Dismiss</button>
        </div>
      </div>`;
    }).join('');
  }
  if (correls.length) {
    html += '<div class="sh">Correlations</div>';
    html += correls.map(c => {
      const key = 'c_'+c.entity_a+'_'+c.entity_b;
      const isNew = _newPatternKeys.has(key) && !_seenRevelations.has(key);
      return `<div class="disc-card correlation${isNew?' new':''}" id="dc-${key}">
        <div class="disc-top"><div class="disc-icon">🔗</div><div class="disc-info">
          <div class="disc-name">${escHtml(c.entity_a)} → ${escHtml(c.entity_b)}</div>
          <div class="disc-desc">${escHtml(c.pattern||'')}</div>
          <div class="disc-meta">${isNew?'<span class="badge badge-new">NEW</span>':''}${c.window_minutes?`<span class="badge badge-time">within ${c.window_minutes}m</span>`:''}
            <span class="badge badge-conf">${pct(c.confidence)}</span>${c.instances?`<span class="badge badge-inst">×${c.instances}</span>`:''}</div>
        </div></div>
        <div class="disc-actions">
          <button class="disc-btn disc-btn-auto" data-key="${key}" data-eid="${escHtml(c.entity_b)}" data-eid-a="${escHtml(c.entity_a)}" data-name="${escHtml(c.pattern||c.entity_b)}" data-type="correlation">Automate</button>
          <button class="disc-btn disc-btn-nah" data-key="${key}" data-dismiss="1">Dismiss</button>
        </div>
      </div>`;
    }).join('');
  }
  if (anomalies.length) {
    html += '<div class="sh">Anomalies</div>';
    html += anomalies.map(a => {
      const key = 'a_'+a.entity_id;
      const isNew = _newPatternKeys.has(key) && !_seenRevelations.has(key);
      const sevClass = 'badge-sev-'+(a.severity||'low');
      return `<div class="disc-card anomaly${isNew?' new':''}" id="dc-${key}">
        <div class="disc-top"><div class="disc-icon">🚨</div><div class="disc-info">
          <div class="disc-name">${escHtml(a.entity_id)}</div>
          <div class="disc-desc">${escHtml(a.description||'')}</div>
          <div class="disc-meta">${isNew?'<span class="badge badge-new">NEW</span>':''}<span class="badge ${sevClass}">⚠️ ${a.severity||'low'}</span></div>
        </div></div>
        <div class="disc-actions">
          <button class="disc-btn disc-btn-nah" data-key="${key}" data-dismiss="1">Dismiss</button>
        </div>
      </div>`;
    }).join('');
  }
  if (!html) html = '<div class="disc-empty">All patterns reviewed!</div>';
  el.innerHTML = html;
  // Wire buttons
  el.querySelectorAll('.disc-btn-nah[data-dismiss]').forEach(btn => {
    btn.addEventListener('click', () => {
      const key=btn.dataset.key;
      _dismissedPatterns.add(key);
      sessionStorage.setItem('dismissedPatterns', JSON.stringify([..._dismissedPatterns]));
      const card=document.getElementById('dc-'+key);
      if(card){card.style.transition='opacity 0.25s';card.style.opacity='0';}
      setTimeout(()=>renderDiscoveries(),260);
    });
  });
  el.querySelectorAll('.disc-btn-auto').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.textContent='⏳'; btn.disabled=true;
      try {
        const suggestion={entity_id:btn.dataset.eid,name:btn.dataset.name,automation_context:{trigger_type:btn.dataset.type,entity_a:btn.dataset.eidA}};
        await fetch(BASE+'/save_automation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({suggestion})});
        showToast('🚀 Queued'); btn.textContent='✅';
      } catch { showToast('⚠️ Failed'); btn.textContent='Automate'; btn.disabled=false; }
    });
  });
}

// ── Trends ──
function renderTrends() {
  if(!_patterns) return;
  const routines=_patterns.routines||[], correlations=_patterns.correlations||[], anomalies=_patterns.anomalies||[];
  const el=document.getElementById('disc-trends');
  if(!routines.length&&!correlations.length&&!anomalies.length){ el.innerHTML=''; return; }
  let html='<div class="sh">Trends & Insights</div><div class="trend-card">';
  // Peak activity
  const hourBuckets={};
  routines.forEach(r=>{const h=parseInt((r.typical_time||'').split(':')[0],10);if(!isNaN(h))hourBuckets[h]=(hourBuckets[h]||0)+1;});
  if(Object.keys(hourBuckets).length>1){
    const peak=Object.entries(hourBuckets).sort((a,b)=>b[1]-a[1])[0];
    const hr=parseInt(peak[0]);
    const label=hr===0?'12 AM':hr<12?hr+' AM':hr===12?'12 PM':(hr-12)+' PM';
    html+=`<div class="trend-row"><div class="trend-label" style="color:#4DA6FF;">⏰ Peak Activity</div><div class="trend-value">Most routines around <b style="color:#fff">${label}</b> (${peak[1]})</div></div>`;
  }
  // Top entities
  const mentions={};
  routines.forEach(r=>{mentions[r.entity_id]=(mentions[r.entity_id]||0)+1;});
  correlations.forEach(c=>{mentions[c.entity_a]=(mentions[c.entity_a]||0)+1;mentions[c.entity_b]=(mentions[c.entity_b]||0)+1;});
  const top5=Object.entries(mentions).sort((a,b)=>b[1]-a[1]).slice(0,5);
  if(top5.length){
    html+=`<div class="trend-row"><div class="trend-label" style="color:#34C759;">🏠 Most Active</div><div class="trend-value">${top5.map(([eid,c])=>`${domainEmoji(eid)} ${eid} — ${c}`).join('<br>')}</div></div>`;
  }
  // Weekly bars
  const dayCounts={};
  routines.forEach(r=>{(r.days||[]).forEach(d=>{dayCounts[d]=(dayCounts[d]||0)+1;});});
  if(Object.keys(dayCounts).length){
    const dayOrder=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    const maxC=Math.max(...Object.values(dayCounts),1);
    const bars=dayOrder.map(d=>{
      const c=dayCounts[d]||0; const p=Math.round(c/maxC*100);
      return `<div class="bar-row"><span class="bar-label">${d}</span><div class="bar-track"><div class="bar-fill" style="width:${p}%;background:rgba(0,122,255,0.6)"></div></div><span class="bar-count">${c}</span></div>`;
    }).join('');
    html+=`<div class="trend-row"><div class="trend-label" style="color:#FF9F0A;">📊 Weekly Distribution</div>${bars}</div>`;
  }
  if(anomalies.length){
    const hi=anomalies.filter(a=>a.severity==='high').length;
    html+=`<div class="trend-row"><div class="trend-label" style="color:#FF3B30;">⚠️ Anomalies</div><div class="trend-value">${anomalies.length} detected${hi?` (${hi} high)`:''}</div></div>`;
  }
  html+='</div>';
  el.innerHTML=html;
}

// ── Revelations (suggestions tab) ──
function renderRevelations() {
  const el=document.getElementById('revelations');
  if(!_patterns||!_newPatternKeys.size){el.innerHTML='';return;}
  const unseen=[..._newPatternKeys].filter(k=>!_seenRevelations.has(k));
  if(!unseen.length){el.innerHTML='';return;}
  const items=unseen.map(key=>{
    if(key.startsWith('r_')){const r=(_patterns.routines||[]).find(r=>'r_'+r.entity_id+'_'+(r.name||'')===key);if(r)return `<div style="padding:4px 0;font-size:14px;color:#ddd;">🔁 <b>${escHtml(r.name||r.entity_id)}</b> at ${r.typical_time||'?'}</div>`;}
    else if(key.startsWith('c_')){const c=(_patterns.correlations||[]).find(c=>'c_'+c.entity_a+'_'+c.entity_b===key);if(c)return `<div style="padding:4px 0;font-size:14px;color:#ddd;">🔗 ${escHtml(c.pattern||c.entity_a+' → '+c.entity_b)}</div>`;}
    else if(key.startsWith('a_')){const a=(_patterns.anomalies||[]).find(a=>'a_'+a.entity_id===key);if(a)return `<div style="padding:4px 0;font-size:14px;color:#ddd;">🚨 <b>${escHtml(a.entity_id)}</b> — ${escHtml(a.description||'')}</div>`;}
    return '';
  }).filter(Boolean);
  if(!items.length){el.innerHTML='';return;}
  el.innerHTML=`<div style="background:linear-gradient(135deg,rgba(175,82,222,0.1),rgba(0,122,255,0.06));border:1px solid rgba(175,82,222,0.2);border-radius:14px;padding:16px;margin-bottom:16px;animation:revealIn 0.4s ease-out;">
    <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#BF7AF0;margin-bottom:8px;">✨ New Discoveries</div>
    ${items.join('')}
    <button id="dismiss-revelations" style="background:none;border:none;color:#636366;font-size:12px;cursor:pointer;margin-top:8px;padding:4px 0;">Mark as seen</button>
  </div>`;
  document.getElementById('dismiss-revelations').addEventListener('click',()=>{
    unseen.forEach(k=>_seenRevelations.add(k));
    localStorage.setItem('seenRevelations',JSON.stringify([..._seenRevelations]));
    el.innerHTML='';
  });
}

// ── System tab ──
function renderStatusGrid(s) {
  const el=document.getElementById('sys-status-grid');
  if(!el||!s) return;
  const ok=v=>v?'🟢':'🔴';
  el.innerHTML=`
    <div class="sys-card"><div class="sys-label">HA</div><div class="sys-val">${ok(s.ha_connected)} ${s.ha_connected?'Connected':'Down'}</div></div>
    <div class="sys-card"><div class="sys-label">AI / Ollama</div><div class="sys-val">${ok(s.ollama_connected)} ${s.ollama_connected?'Connected':'Down'}</div></div>
    <div class="sys-card"><div class="sys-label">Entities</div><div class="sys-val">${s.entity_count||0}</div></div>
    <div class="sys-card"><div class="sys-label">Patterns</div><div class="sys-val">${s.patterns_loaded?`${s.pattern_routines} routines`:'None'}</div></div>
    <div class="sys-card"><div class="sys-label">Last Refresh</div><div class="sys-val">${s.last_refresh||'Never'}</div></div>
    <div class="sys-card"><div class="sys-label">Last Analysis</div><div class="sys-val">${s.last_analysis||'Never'}</div></div>
  `;
}

async function refreshStatus() {
  try { const r=await fetch(BASE+'/status'); const s=await r.json(); renderStatusGrid(s); updateFirstRunBanner(s); } catch {}
}

// ── Entity inject ──
async function loadEntities() {
  try { const r=await fetch(BASE+'/entities'); _entities=await r.json(); _entitiesLoaded=true; } catch { _entities=[]; }
}
document.getElementById('inject-search').addEventListener('input', e=>renderEntities(e.target.value));
document.getElementById('inject-search').addEventListener('focus', async ()=>{ if(!_entitiesLoaded) { await loadEntities(); renderEntities(''); }});

function renderEntities(filter) {
  const q=filter.toLowerCase();
  const matches=_entities.filter(e=>!q||e.entity_id.includes(q)||(e.name||'').toLowerCase().includes(q)).slice(0,100);
  const el=document.getElementById('entity-list');
  if(!matches.length){el.innerHTML='<div class="no-results">No entities found</div>';return;}
  el.innerHTML=matches.map(e=>`
    <div class="entity-row">
      <div class="entity-info"><div class="entity-name">${domainEmoji(e.entity_id)} ${e.name||e.entity_id}</div><div class="entity-id">${e.entity_id} · ${e.current_state||''}</div></div>
      <button class="add-btn" data-eid="${e.entity_id}" data-name="${(e.name||e.entity_id).replace(/"/g,'&quot;')}" data-domain="${e.domain||''}">Add</button>
    </div>`).join('');
  el.querySelectorAll('.add-btn').forEach(btn=>{
    btn.addEventListener('click', async ()=>{
      btn.textContent='✓'; btn.disabled=true;
      try { await fetch(BASE+'/inject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entity_id:btn.dataset.eid,name:btn.dataset.name,domain:btn.dataset.domain})}); showToast('✅ Added'); }
      catch { showToast('⚠️ Failed'); btn.textContent='Add'; btn.disabled=false; }
    });
  });
}

// ── Deny list ──
function renderDenyPanel() {
  const sec=document.getElementById('deny-section');
  if(!_denyList.size){sec.style.display='none';return;}
  sec.style.display='';
  const el=document.getElementById('deny-content');
  el.innerHTML=[..._denyList].sort().map(eid=>`
    <div class="deny-row"><span>${domainEmoji(eid)}</span><span class="deny-eid">${eid}</span>
    <button class="undeny-btn" data-eid="${eid}">Unblock</button></div>`).join('');
  el.querySelectorAll('.undeny-btn').forEach(btn=>{
    btn.addEventListener('click', async ()=>{
      try { await fetch(BASE+'/deny',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({entity_id:btn.dataset.eid})}); _denyList.delete(btn.dataset.eid); showToast('✅ Unblocked'); renderDenyPanel(); }
      catch { showToast('⚠️ Failed'); }
    });
  });
}

// ── Logs ──
document.getElementById('log-clear').addEventListener('click',()=>{_logs=[];renderLogs();});
function makeLogLine(e){return `<div class="log-line"><span class="log-ts">${e.ts}</span><span class="log-lvl ${e.level}">${e.level}</span><span class="log-msg">${escHtml(e.msg)}</span></div>`;}
function renderLogs(){
  const box=document.getElementById('log-box');
  if(!_logs.length){box.innerHTML='<span class="log-empty">No logs yet.</span>';return;}
  box.innerHTML=_logs.map(makeLogLine).join('');
  box.scrollTop=box.scrollHeight;
}
function appendLog(entry){
  _logs.push(entry); if(_logs.length>MAX_LOG_LINES)_logs.shift();
  const box=document.getElementById('log-box');
  const empty=box.querySelector('.log-empty'); if(empty)empty.remove();
  const atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<60;
  box.insertAdjacentHTML('beforeend',makeLogLine(entry));
  while(box.children.length>MAX_LOG_LINES)box.removeChild(box.firstChild);
  if(atBottom)box.scrollTop=box.scrollHeight;
}

function showToast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2400);}

// ── WebSocket ──
function connectWS(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  const ws=new WebSocket(proto+'//'+location.host+BASE+'/ws');
  ws.addEventListener('open',()=>{document.getElementById('meta-text').textContent='Connected';});
  ws.addEventListener('message',evt=>{
    let msg; try{msg=JSON.parse(evt.data);}catch{return;}
    if(msg.type==='suggestions'){_suggestions=Array.isArray(msg.data)?msg.data:[];_status='ready';render();}
    else if(msg.type==='status'){_status=msg.state||'idle';render();}
    else if(msg.type==='log'){appendLog(msg);}
    else if(msg.type==='system_status'){renderStatusGrid(msg.data);updateFirstRunBanner(msg.data);}
    else if(msg.type==='patterns'){
      _patterns=msg.data;
      if(msg.new_keys&&msg.new_keys.length){msg.new_keys.forEach(k=>_newPatternKeys.add(k));renderRevelations();}
      // Update discoveries tab if visible
      if(document.getElementById('page-discoveries').classList.contains('active')){renderDiscoveries();renderTrends();}
      // Update tab count
      const total=(_patterns.routines||[]).length+(_patterns.correlations||[]).length+(_patterns.anomalies||[]).length;
      const ct=document.getElementById('disc-count');
      if(total>0){ct.style.display='';ct.textContent=total;}else{ct.style.display='none';}
    }
    else if(msg.type==='deny_list'){_denyList=new Set(msg.data||[]);renderDenyPanel();}
  });
  ws.addEventListener('close',()=>{
    document.getElementById('status-dot').className='status-dot';
    document.getElementById('meta-text').textContent='Reconnecting…';
    setTimeout(connectWS,5000);
  });
}

// ── Init ──
render();
renderDenyPanel();
connectWS();
// Load logs in background
fetch(BASE+'/logs').then(r=>r.json()).then(d=>{_logs=d;}).catch(()=>{});
</script>
</body>
</html>"""


class WSServer:
    """Manages connected card WebSocket clients and broadcasts events."""

    def __init__(self, dismissal_store=None) -> None:
        self._dismissal_store = dismissal_store
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
        self._app.router.add_post("/refresh_all", self._refresh_all_handler)
        self._app.router.add_post("/save_automation", self._handle_post_save_automation)
        self._app.router.add_post("/deny", self._deny_handler)
        self._app.router.add_delete("/deny", self._undeny_handler)
        self._app.router.add_get("/deny_list", self._deny_list_handler)
        self._runner: web.AppRunner | None = None
        self._last_suggestions: list = []
        self._last_status: str = "idle"
        self._feedback: dict = {}
        self._known_entities: list = []
        self._feedback_cb = None
        self._refresh_cb = None
        self._analyze_cb = None
        self._refresh_all_cb = None
        self._automation_handler = None
        self._deny_cb = None
        self._undeny_cb = None
        self._deny_set: set[str] = set()
        self._log_buffer: deque = deque(maxlen=_LOG_BUFFER_SIZE)
        self._system_status: dict = {}
        self._patterns: dict = {}
        self._prev_pattern_keys: set[str] = set()
        self._usage_log = None
        self._automation_builder = None
        self._ha_client = None

    def register_feedback_handler(self, cb) -> None:
        self._feedback_cb = cb

    def register_refresh_handler(self, cb) -> None:
        self._refresh_cb = cb

    def register_analyze_handler(self, cb) -> None:
        self._analyze_cb = cb

    def register_refresh_all_handler(self, cb) -> None:
        self._refresh_all_cb = cb

    def register_automation_handler(self, handler) -> None:
        self._automation_handler = handler

    def register_deny_handler(self, cb) -> None:
        self._deny_cb = cb

    def register_undeny_handler(self, cb) -> None:
        self._undeny_cb = cb

    def set_deny_list(self, deny_set: set[str]) -> None:
        self._deny_set = deny_set

    def set_usage_log(self, usage_log) -> None:
        self._usage_log = usage_log

    def set_automation_builder(self, builder) -> None:
        self._automation_builder = builder

    def set_ha_client(self, ha_client) -> None:
        self._ha_client = ha_client

    async def _handle_client_message(self, msg: dict, ws=None) -> None:
        msg_type = msg.get("type")

        if msg_type == "outcome":
            if self._usage_log:
                await self._usage_log.log(
                    msg.get("entity_id", ""),
                    msg.get("action", ""),
                    msg.get("outcome", "shown"),
                    float(msg.get("confidence", 0.0)),
                )

        elif msg_type == "build_yaml":
            if ws and self._automation_builder:
                entity_id = msg.get("entity_id", "")
                action = msg.get("action", "")
                try:
                    ctx = {
                        "entity_id": entity_id,
                        "name": msg.get("name", entity_id),
                        "typical_time": None,
                        "days": [],
                    }
                    result = await self._automation_builder.build(
                        ctx, self._ha_client
                    )
                    await ws.send_str(json.dumps({
                        "type": "yaml_result",
                        "entity_id": entity_id,
                        "action": action,
                        "yaml": result.get("yaml"),
                    }))
                except Exception as e:
                    await ws.send_str(json.dumps({
                        "type": "yaml_result",
                        "entity_id": entity_id,
                        "action": action,
                        "yaml": None,
                        "error": str(e),
                    }))

        elif msg_type == "refresh_all":
            # Full pipeline: analysis + correlation + refresh
            if self._refresh_all_cb:
                asyncio.create_task(self._refresh_all_cb())
            elif self._refresh_cb:
                await self._refresh_cb()

        elif msg_type == "save_automation":
            suggestion = msg.get("suggestion", {})
            asyncio.create_task(self._handle_save_automation(suggestion))

        elif msg_type == "dismiss":
            sig = msg.get("signature")
            miner_type_str = msg.get("miner_type")
            if sig and miner_type_str and self._dismissal_store is not None:
                try:
                    from candidate import MinerType
                    from datetime import datetime, timezone
                    await self._dismissal_store.add_dismissal(
                        sig, MinerType(miner_type_str), datetime.now(timezone.utc)
                    )
                    await ws.send_json({"type": "dismiss_ack", "signature": sig})
                except Exception:
                    _LOGGER.exception("dismiss handler failed for signature=%s miner_type=%s", sig, miner_type_str)
                    await ws.send_json({"type": "dismiss_error", "signature": sig})

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
        # Compute which pattern keys are new since last push
        new_keys = self._compute_pattern_keys(patterns)
        new_revelations = new_keys - self._prev_pattern_keys
        self._prev_pattern_keys = new_keys
        self._patterns = patterns
        try:
            import asyncio  # noqa: PLC0415
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast({
                "type": "patterns", "data": patterns,
                "new_keys": list(new_revelations) if new_revelations else [],
            }))
        except RuntimeError:
            pass

    @staticmethod
    def _compute_pattern_keys(patterns: dict) -> set[str]:
        keys = set()
        for r in patterns.get("routines", []):
            keys.add("r_" + r.get("entity_id", "") + "_" + r.get("name", ""))
        for c in patterns.get("correlations", []):
            keys.add("c_" + c.get("entity_a", "") + "_" + c.get("entity_b", ""))
        for a in patterns.get("anomalies", []):
            keys.add("a_" + a.get("entity_id", ""))
        return keys

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

    async def _refresh_all_handler(self, request: web.Request) -> web.Response:
        if self._refresh_all_cb:
            asyncio.get_running_loop().create_task(self._refresh_all_cb())
        elif self._refresh_cb:
            await self._refresh_cb()
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

    async def _deny_handler(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            entity_id = data.get("entity_id", "")
            if not entity_id:
                return web.json_response({"ok": False}, status=400)
            if self._deny_cb:
                await self._deny_cb(entity_id)
            self._deny_set.add(entity_id)
            await self.broadcast({"type": "deny_list", "data": sorted(self._deny_set)})
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def _undeny_handler(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            entity_id = data.get("entity_id", "")
            if not entity_id:
                return web.json_response({"ok": False}, status=400)
            if self._undeny_cb:
                await self._undeny_cb(entity_id)
            self._deny_set.discard(entity_id)
            await self.broadcast({"type": "deny_list", "data": sorted(self._deny_set)})
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def _deny_list_handler(self, request: web.Request) -> web.Response:
        return web.json_response(sorted(self._deny_set))

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
            await self._send(ws, {"type": "patterns", "data": self._patterns, "new_keys": []})
        if self._deny_set:
            await self._send(ws, {"type": "deny_list", "data": sorted(self._deny_set)})

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_client_message(data, ws=ws)
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
