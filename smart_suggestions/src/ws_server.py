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
    font-family: var(--paper-font-body1_-_font-family, -apple-system, Roboto, system-ui, sans-serif);
    background: var(--primary-background-color, #111111);
    color: var(--primary-text-color, #fff);
    min-height: 100vh; padding: 0 0 72px;
  }

  /* ── Header ── */
  .header {
    position: sticky; top: 0; z-index: 100;
    background: var(--primary-background-color, #111111);
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    padding: 16px 16px 0;
    border-bottom: 0.5px solid rgba(var(--rgb-primary-text-color, 255,255,255), 0.08);
  }
  .topbar { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
  .header-icon {
    width: 36px; height: 36px;
    background: var(--primary-color, #03a9f4);
    border-radius: 10px; display: flex; align-items: center; justify-content: center;
    color: #fff; flex-shrink: 0;
  }
  h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.4px; flex: 1; }
  .meta { font-size: 12px; color: var(--secondary-text-color, #8e8e93); padding-bottom: 10px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--success-color, #4caf50); display: inline-block; flex-shrink: 0; }
  .status-dot.updating { background: var(--warning-color, #ff9800); }
  .cycle-chip { font-size: 11px; padding: 2px 8px; border-radius: 8px; background: rgba(var(--rgb-primary-text-color,255,255,255),0.07); white-space: nowrap; }
  .cycle-chip.mining { background: rgba(var(--rgb-primary-color,3,169,244),0.15); color: var(--primary-color,#03a9f4); }

  @media (prefers-reduced-motion: no-preference) {
    .status-dot.updating { animation: pulse 1s ease-in-out infinite; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
    .tab-page.active { animation: fadeIn 0.2s ease; }
    @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
    .pipeline-new { animation: revealIn 0.4s ease-out; }
    @keyframes revealIn { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: none; } }
  }

  /* ── Tab bar ── */
  .tab-bar { display: flex; gap: 0; }
  .tab-btn {
    flex: 1; background: none; border: none; border-bottom: 2.5px solid transparent;
    color: var(--disabled-text-color, #636366); padding: 10px 8px; font-size: 13px; font-weight: 600;
    cursor: pointer; transition: color 0.2s, border-color 0.2s; text-align: center;
    min-height: 36px; -webkit-tap-highlight-color: transparent;
  }
  .tab-btn.active { color: var(--primary-color, #03a9f4); border-bottom-color: var(--primary-color, #03a9f4); }

  /* ── Tab pages ── */
  .tab-page { display: none; padding: 16px; max-width: 980px; margin: 0 auto; }
  .tab-page.active { display: block; }
  .header > * { max-width: 980px; margin-left: auto; margin-right: auto; }

  /* ── Action buttons ── */
  .action-row { display: flex; gap: 8px; margin-bottom: 16px; }
  .action-btn {
    flex: 1; background: rgba(var(--rgb-primary-text-color,255,255,255),0.08);
    color: var(--primary-text-color, #fff); border: none;
    border-radius: 10px; padding: 12px 14px; font-size: 14px; font-weight: 600; min-height: 44px;
    cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px;
    transition: opacity 0.15s, transform 0.15s; -webkit-tap-highlight-color: transparent;
  }
  .action-btn:active { transform: scale(0.97); opacity: 0.8; }
  .action-btn.primary { background: var(--primary-color, #03a9f4); color: #fff; }
  .action-btn.loading { opacity: 0.55; pointer-events: none; }

  /* ── Section headers ── */
  .sh { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--disabled-text-color, #636366); padding: 16px 0 8px; }
  .sh:first-child { padding-top: 0; }

  /* ── Suggestion cards ── */
  .sug-list {
    display: flex; flex-direction: column; gap: 1px;
    background: rgba(var(--rgb-primary-text-color,255,255,255),0.06);
    border-radius: var(--ha-card-border-radius, 12px); overflow: hidden; margin-bottom: 12px;
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,0.16));
  }
  .sug {
    background: var(--card-background-color, #1c1c1e);
    padding: 10px 14px; display: flex; gap: 12px; align-items: center;
  }
  .sug-icon { width: 42px; height: 42px; border-radius: var(--ha-card-border-radius, 12px); display: flex; align-items: center; justify-content: center; font-size: 22px; flex-shrink: 0; background: rgba(var(--rgb-primary-text-color,255,255,255),0.06); }
  .sug-body { flex: 1; min-width: 0; }
  .sug-name { font-size: 16px; font-weight: 600; margin-bottom: 2px; }
  .sug-eid { font-size: 11px; color: var(--secondary-text-color, #48484a); font-family: 'Menlo','Consolas',monospace; margin-bottom: 4px; }
  .sug-transition { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; flex-wrap: wrap; }
  .state-chip { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; color: #fff; text-transform: uppercase; letter-spacing: 0.5px; }
  .state-arrow { color: var(--secondary-text-color, #8e8e93); font-size: 14px; }
  .action-chip { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; background: var(--primary-color, #03a9f4); color: #fff; text-transform: uppercase; letter-spacing: 0.5px; }
  .sug-reason { font-size: 14px; color: var(--secondary-text-color, #8e8e93); line-height: 1.45; }
  .sug-actions { display: flex; flex-direction: row; gap: 4px; flex-shrink: 0; align-items: center; }
  .vote-btn {
    width: 36px; height: 36px; border: none; border-radius: 8px;
    background: rgba(var(--rgb-primary-text-color,255,255,255),0.06);
    color: var(--disabled-text-color, #636366);
    cursor: pointer; display: flex; align-items: center; justify-content: center;
    transition: background 0.15s, color 0.15s; -webkit-tap-highlight-color: transparent;
  }
  .vote-btn:active { transform: scale(0.88); }
  .vote-btn.up.voted { background: rgba(76,175,80,0.2); color: var(--success-color, #4caf50); }
  .vote-btn.down.voted { background: rgba(244,67,54,0.2); color: var(--error-color, #f44336); }
  .deny-btn {
    width: 36px; height: 36px; border: none; border-radius: 8px; background: none;
    color: var(--secondary-text-color, #48484a); font-size: 13px;
    cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background 0.15s, color 0.15s;
  }
  .deny-btn:hover { background: rgba(244,67,54,0.12); color: var(--error-color, #f44336); }

  .empty { text-align: center; padding: 60px 20px; color: var(--disabled-text-color, #636366); font-size: 15px; line-height: 1.6; }
  .empty svg { opacity: 0.25; margin-bottom: 12px; display: block; margin-left: auto; margin-right: auto; }
  .empty-hint { font-size: 13px; margin-top: 8px; color: var(--secondary-text-color, #8e8e93); }
  .empty-hint b { color: var(--primary-color, #03a9f4); }

  /* ── Pipeline tab ── */
  .pipeline-section { margin-bottom: 20px; }
  .pipeline-card {
    background: var(--card-background-color, #1c1c1e);
    border-radius: var(--ha-card-border-radius, 12px);
    overflow: hidden; margin-bottom: 10px;
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,0.16));
  }
  .pipeline-row { padding: 14px 16px; border-bottom: 0.5px solid rgba(var(--rgb-primary-text-color,255,255,255),0.05); display: flex; align-items: center; gap: 12px; }
  .pipeline-row:last-child { border-bottom: none; }
  .pipeline-row-label { font-size: 13px; font-weight: 600; flex: 1; }
  .pipeline-row-meta { font-size: 12px; color: var(--secondary-text-color, #8e8e93); }
  .pipeline-row-value {
    font-size: 13px; font-weight: 700;
    background: rgba(var(--rgb-primary-color,3,169,244),0.12);
    color: var(--primary-color, #03a9f4);
    padding: 3px 10px; border-radius: 10px; flex-shrink: 0; min-width: 44px; text-align: center;
  }
  .pipeline-row-desc { font-size: 12px; color: var(--secondary-text-color, #8e8e93); line-height: 1.4; }
  .pipeline-stat { display: flex; gap: 12px; padding: 14px 16px; border-bottom: 0.5px solid rgba(var(--rgb-primary-text-color,255,255,255),0.05); }
  .pipeline-stat:last-child { border-bottom: none; }
  .pipeline-stat-icon { font-size: 20px; flex-shrink: 0; }
  .pipeline-stat-body { flex: 1; min-width: 0; }
  .pipeline-stat-title { font-size: 14px; font-weight: 600; margin-bottom: 2px; }
  .pipeline-stat-val { font-size: 12px; color: var(--secondary-text-color, #8e8e93); }
  .pipeline-settings-row { padding: 12px 16px; border-bottom: 0.5px solid rgba(var(--rgb-primary-text-color,255,255,255),0.05); display: flex; align-items: flex-start; gap: 10px; }
  .pipeline-settings-row:last-child { border-bottom: none; }
  .pipeline-settings-info { flex: 1; min-width: 0; }
  .pipeline-settings-name { font-size: 13px; font-weight: 600; margin-bottom: 2px; }
  .pipeline-settings-desc { font-size: 12px; color: var(--secondary-text-color, #8e8e93); line-height: 1.4; }
  .ha-settings-link {
    display: inline-flex; align-items: center; gap: 6px; margin-top: 14px;
    padding: 10px 16px; border-radius: 10px; font-size: 13px; font-weight: 600; min-height: 36px;
    background: rgba(var(--rgb-primary-text-color,255,255,255),0.06);
    color: var(--primary-text-color, #fff); text-decoration: none;
    border: none; cursor: pointer; -webkit-tap-highlight-color: transparent;
  }
  .ha-settings-link:hover { background: rgba(var(--rgb-primary-text-color,255,255,255),0.12); }

  /* ── System tab ── */
  .sys-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 16px; }
  .sys-card {
    background: var(--card-background-color, #1c1c1e);
    border-radius: var(--ha-card-border-radius, 12px);
    padding: 14px;
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,0.16));
  }
  .sys-label { font-size: 11px; color: var(--disabled-text-color, #636366); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
  .sys-val { font-size: 15px; font-weight: 600; }
  .sys-card.full { grid-column: 1 / -1; }
  .inject-search {
    width: 100%; background: rgba(var(--rgb-primary-text-color,255,255,255),0.08);
    border: 1px solid rgba(var(--rgb-primary-text-color,255,255,255),0.1);
    border-radius: 10px;
    color: var(--primary-text-color, #fff); padding: 10px 14px; font-size: 15px; outline: none; margin-bottom: 8px;
    font-family: inherit;
  }
  .inject-search::placeholder { color: var(--secondary-text-color, #48484a); }
  .entity-list {
    background: var(--card-background-color, #1c1c1e);
    border-radius: var(--ha-card-border-radius, 12px);
    overflow: hidden; max-height: 320px; overflow-y: auto;
  }
  .entity-row { display: flex; align-items: center; padding: 10px 14px; gap: 10px; border-top: 0.5px solid rgba(var(--rgb-primary-text-color,255,255,255),0.05); min-height: 44px; }
  .entity-row:first-child { border-top: none; }
  .entity-info { flex: 1; min-width: 0; }
  .entity-name { font-size: 14px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .entity-id { font-size: 11px; color: var(--secondary-text-color, #48484a); margin-top: 1px; }
  .add-btn {
    background: rgba(var(--rgb-primary-color,3,169,244),0.15); color: var(--primary-color, #03a9f4);
    border: none; border-radius: 8px; padding: 6px 14px; font-size: 13px; font-weight: 600; cursor: pointer; flex-shrink: 0; min-height: 36px;
  }
  .no-results { padding: 20px; text-align: center; color: var(--secondary-text-color, #48484a); font-size: 14px; }
  .deny-row { display: flex; align-items: center; padding: 10px 14px; gap: 10px; border-top: 0.5px solid rgba(var(--rgb-primary-text-color,255,255,255),0.05); min-height: 44px; }
  .deny-row:first-child { border-top: none; }
  .deny-eid { flex: 1; font-size: 13px; color: var(--secondary-text-color, #8e8e93); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .undeny-btn {
    background: rgba(76,175,80,0.15); color: var(--success-color, #4caf50);
    border: none; border-radius: 8px; padding: 6px 14px; font-size: 13px; font-weight: 600; cursor: pointer; flex-shrink: 0; min-height: 36px;
  }
  .badge { font-size: 11px; font-weight: 700; padding: 3px 9px; border-radius: 20px; }
  .badge-new { background: rgba(191,122,240,0.2); color: #BF7AF0; }

  /* ── Log ── */
  .log-box {
    background: var(--code-editor-background-color, #0a0a0a);
    border-radius: var(--ha-card-border-radius, 12px); padding: 10px 12px;
    font-family: 'Menlo', 'Consolas', monospace;
    font-size: 11px; line-height: 1.65; max-height: 400px; overflow-y: auto;
    border: 1px solid rgba(var(--rgb-primary-text-color,255,255,255),0.06);
  }
  .log-line { display: flex; gap: 8px; align-items: baseline; min-width: 0; }
  .log-ts { color: var(--disabled-text-color, #636366); flex-shrink: 0; opacity: 0.6; }
  .log-lvl { flex-shrink: 0; font-weight: 700; min-width: 46px; }
  .log-lvl.DEBUG    { color: var(--disabled-text-color, #636366); }
  .log-lvl.INFO     { color: var(--success-color, #4caf50); }
  .log-lvl.WARNING  { color: var(--warning-color, #ff9800); }
  .log-lvl.ERROR    { color: var(--error-color, #f44336); }
  .log-lvl.CRITICAL { color: var(--error-color, #f44336); }
  .log-msg { color: var(--secondary-text-color, #999); word-break: break-all; }
  .log-empty { color: var(--disabled-text-color, #636366); font-style: italic; }

  /* ── Modal ── */
  .modal {
    position: fixed; inset: 0; background: rgba(0,0,0,0.6);
    z-index: 300; display: flex; align-items: center; justify-content: center; padding: 16px;
  }
  .modal-card {
    background: var(--card-background-color, #1c1c1e);
    border-radius: var(--ha-card-border-radius, 16px);
    padding: 20px; width: 100%; max-width: 480px;
    box-shadow: 0 8px 40px rgba(0,0,0,0.6);
    display: flex; flex-direction: column; gap: 8px;
  }
  .modal-card h3 { font-size: 18px; font-weight: 700; margin-bottom: 4px; }
  .modal-desc { font-size: 13px; color: var(--secondary-text-color, #8e8e93); margin-bottom: 4px; }
  .modal-card label { font-size: 12px; font-weight: 600; color: var(--secondary-text-color, #8e8e93); text-transform: uppercase; letter-spacing: 0.06em; margin-top: 6px; }
  .modal-card select {
    width: 100%; background: rgba(var(--rgb-primary-text-color,255,255,255),0.08);
    border: 1px solid rgba(var(--rgb-primary-text-color,255,255,255),0.1);
    border-radius: 10px; color: var(--primary-text-color, #fff);
    padding: 10px 14px; font-size: 15px; outline: none; margin-bottom: 4px; font-family: inherit;
  }
  .modal-actions { display: flex; gap: 8px; margin-top: 12px; }
  .modal-actions .action-btn { flex: 1; }

  /* ── Outcomes ── */
  .outcome-row { padding: 12px 16px; border-bottom: 0.5px solid rgba(var(--rgb-primary-text-color,255,255,255),0.05); display: flex; align-items: center; gap: 12px; }
  .outcome-row:last-child { border-bottom: none; }
  .outcome-miner { font-size: 13px; font-weight: 600; flex: 1; text-transform: capitalize; }
  .outcome-rate { font-size: 12px; color: var(--secondary-text-color, #8e8e93); }

  /* ── User patterns list ── */
  .pattern-row { padding: 12px 16px; border-bottom: 0.5px solid rgba(var(--rgb-primary-text-color,255,255,255),0.05); display: flex; align-items: center; gap: 10px; }
  .pattern-row:last-child { border-bottom: none; }
  .pattern-body { flex: 1; min-width: 0; }
  .pattern-title { font-size: 13px; font-weight: 600; }
  .pattern-detail { font-size: 11px; color: var(--secondary-text-color, #8e8e93); margin-top: 2px; }
  .pattern-del-btn {
    background: rgba(244,67,54,0.12); color: var(--error-color, #f44336);
    border: none; border-radius: 8px; padding: 6px 12px; font-size: 12px; font-weight: 600; cursor: pointer; flex-shrink: 0; min-height: 32px;
  }
  .badge-confirmed { background: rgba(76,175,80,0.18); color: var(--success-color, #4caf50); }

  /* ── Toast ── */
  .toast {
    position: fixed; bottom: 80px; left: 50%;
    transform: translateX(-50%) translateY(80px);
    background: var(--card-background-color, #2c2c2e);
    color: var(--primary-text-color, #fff);
    padding: 10px 20px; border-radius: 22px; font-size: 14px; font-weight: 500;
    transition: transform 0.3s cubic-bezier(0.34,1.56,0.64,1);
    pointer-events: none; white-space: nowrap;
    box-shadow: 0 4px 24px rgba(0,0,0,0.5); z-index: 200;
    border: 1px solid rgba(var(--rgb-primary-text-color,255,255,255),0.08);
  }
  .toast.show { transform: translateX(-50%) translateY(0); }
</style>
</head>
<body>

<div class="header">
  <div class="topbar">
    <div class="header-icon">
      <svg viewBox="0 0 24 24" fill="currentColor" width="20" height="20"><path d="M12 2A7 7 0 0 0 5 9C5 11.38 6.19 13.47 8 14.74V17A1 1 0 0 0 9 18H15A1 1 0 0 0 16 17V14.74C17.81 13.47 19 11.38 19 9A7 7 0 0 0 12 2M9 21A1 1 0 0 0 10 22H14A1 1 0 0 0 15 21V20H9V21M11 14H13V8H15L12 4L9 8H11V14Z"/></svg>
    </div>
    <h1>Smart Suggestions</h1>
  </div>
  <div class="meta">
    <span class="status-dot" id="status-dot"></span>
    <span id="meta-text">Connecting…</span>
    <span class="cycle-chip" id="cycle-chip" style="display:none"></span>
  </div>
  <div class="tab-bar">
    <button class="tab-btn active" data-tab="suggestions">Suggestions</button>
    <button class="tab-btn" data-tab="pipeline">Pipeline</button>
    <button class="tab-btn" data-tab="system">System</button>
  </div>
</div>

<!-- ══ TAB: Suggestions ══ -->
<div class="tab-page active" id="page-suggestions">
  <div class="action-row">
    <button class="action-btn primary" id="mine-now-btn">
      <svg viewBox="0 0 24 24" fill="currentColor" width="16" height="16"><path d="M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z"/></svg>
      Mine Now
    </button>
  </div>
  <div id="list-container"></div>
</div>

<!-- ══ TAB: Pipeline ══ -->
<div class="tab-page" id="page-pipeline">

  <!-- Status overview -->
  <div class="sh">Status</div>
  <div class="pipeline-card" id="pipeline-status-card">
    <div class="pipeline-stat">
      <div class="pipeline-stat-icon">
        <svg viewBox="0 0 24 24" fill="currentColor" width="20" height="20"><path d="M12 2A7 7 0 0 0 5 9C5 11.38 6.19 13.47 8 14.74V17A1 1 0 0 0 9 18H15A1 1 0 0 0 16 17V14.74C17.81 13.47 19 11.38 19 9A7 7 0 0 0 12 2M9 21A1 1 0 0 0 10 22H14A1 1 0 0 0 15 21V20H9V21M11 14H13V8H15L12 4L9 8H11V14Z"/></svg>
      </div>
      <div class="pipeline-stat-body">
        <div class="pipeline-stat-title">Last Hourly Cycle</div>
        <div class="pipeline-stat-val" id="ps-hourly-val">Never</div>
      </div>
      <div class="pipeline-row-value" id="ps-sug-count">—</div>
    </div>
    <div class="pipeline-stat">
      <div class="pipeline-stat-icon">
        <svg viewBox="0 0 24 24" fill="currentColor" width="20" height="20"><path d="M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2M12,4A8,8 0 0,1 20,12A8,8 0 0,1 12,20A8,8 0 0,1 4,12A8,8 0 0,1 12,4M11,7V13L15.25,15.58L16,14.33L12.5,12.25V7H11Z"/></svg>
      </div>
      <div class="pipeline-stat-body">
        <div class="pipeline-stat-title">Last Waste Check</div>
        <div class="pipeline-stat-val" id="ps-waste-val">Never</div>
      </div>
      <div class="pipeline-row-value" id="ps-noticed-count">—</div>
    </div>
  </div>
  <div class="action-row">
    <button class="action-btn primary" id="mine-now-btn-2">
      <svg viewBox="0 0 24 24" fill="currentColor" width="16" height="16"><path d="M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z"/></svg>
      Mine Now
    </button>
  </div>

  <!-- Mining settings -->
  <div class="sh">Mining Settings</div>
  <div class="pipeline-card" id="pipeline-settings-card">
    <div class="pipeline-settings-row">
      <div class="pipeline-settings-info">
        <div class="pipeline-settings-name">Minimum repetitions</div>
        <div class="pipeline-settings-desc">How many times a pattern must repeat in 30d to be considered.</div>
      </div>
      <div class="pipeline-row-value" id="ps-min-occ">—</div>
    </div>
    <div class="pipeline-settings-row">
      <div class="pipeline-settings-info">
        <div class="pipeline-settings-name">Minimum confidence</div>
        <div class="pipeline-settings-desc">Minimum P(action|trigger) — higher = stricter.</div>
      </div>
      <div class="pipeline-row-value" id="ps-min-conf">—</div>
    </div>
    <div class="pipeline-settings-row">
      <div class="pipeline-settings-info">
        <div class="pipeline-settings-name">History window</div>
        <div class="pipeline-settings-desc">How many days of history to mine each cycle.</div>
      </div>
      <div class="pipeline-row-value" id="ps-hist-days">—</div>
    </div>
    <div class="pipeline-settings-row">
      <div class="pipeline-settings-info">
        <div class="pipeline-settings-name">Mining interval</div>
        <div class="pipeline-settings-desc">How often the main miners run.</div>
      </div>
      <div class="pipeline-row-value" id="ps-mine-int">—</div>
    </div>
    <div class="pipeline-settings-row">
      <div class="pipeline-settings-info">
        <div class="pipeline-settings-name">Waste check interval</div>
        <div class="pipeline-settings-desc">How often the waste detector checks current state.</div>
      </div>
      <div class="pipeline-row-value" id="ps-waste-int">—</div>
    </div>
  </div>
  <a class="ha-settings-link" href="/hassio/addon/smart_suggestions/config" target="_top">
    <svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14"><path d="M12,15.5A3.5,3.5 0 0,1 8.5,12A3.5,3.5 0 0,1 12,8.5A3.5,3.5 0 0,1 15.5,12A3.5,3.5 0 0,1 12,15.5M19.43,12.97C19.47,12.65 19.5,12.33 19.5,12C19.5,11.67 19.47,11.34 19.43,11L21.54,9.37C21.73,9.22 21.78,8.95 21.66,8.73L19.66,5.27C19.54,5.05 19.27,4.96 19.05,5.05L16.56,6.05C16.04,5.66 15.5,5.32 14.87,5.07L14.5,2.42C14.46,2.18 14.25,2 14,2H10C9.75,2 9.54,2.18 9.5,2.42L9.13,5.07C8.5,5.32 7.96,5.66 7.44,6.05L4.95,5.05C4.73,4.96 4.46,5.05 4.34,5.27L2.34,8.73C2.21,8.95 2.27,9.22 2.46,9.37L4.57,11C4.53,11.34 4.5,11.67 4.5,12C4.5,12.33 4.53,12.65 4.57,12.97L2.46,14.63C2.27,14.78 2.21,15.05 2.34,15.27L4.34,18.73C4.46,18.95 4.73,19.03 4.95,18.95L7.44,17.94C7.96,18.34 8.5,18.68 9.13,18.93L9.5,21.58C9.54,21.82 9.75,22 10,22H14C14.25,22 14.46,21.82 14.5,21.58L14.87,18.93C15.5,18.67 16.04,18.34 16.56,17.94L19.05,18.95C19.27,19.03 19.54,18.95 19.66,18.73L21.66,15.27C21.78,15.05 21.73,14.78 21.54,14.63L19.43,12.97Z"/></svg>
    Edit in HA Settings
  </a>

  <!-- Your Patterns -->
  <div class="sh">Your Patterns <button class="action-btn" id="teach-pattern-btn" style="float:right;font-size:13px;padding:6px 12px;min-height:auto">+ Teach</button></div>
  <div class="pipeline-card" id="user-patterns-list"><div class="empty-hint" style="padding:14px 16px">No patterns taught yet.</div></div>

  <!-- Outcomes -->
  <div class="sh">Outcomes (last 7 days)</div>
  <div class="pipeline-card" id="pipeline-outcomes-card">
    <div class="empty-hint" style="padding:14px 16px">Tracking your follow-through. Build up some history first.</div>
  </div>

</div>

<!-- Teach a Pattern Modal -->
<div class="modal" id="teach-modal" style="display:none">
  <div class="modal-card">
    <h3>Teach a Pattern</h3>
    <p class="modal-desc">When this happens → that follows.</p>
    <label>When entity:</label>
    <input class="inject-search" id="teach-trigger-eid" type="search" placeholder="entity_id" autocomplete="off" list="entity-datalist" style="margin-bottom:4px">
    <select id="teach-trigger-action"><option value="turn_on">turn_on</option><option value="turn_off">turn_off</option></select>
    <label>Then entity:</label>
    <input class="inject-search" id="teach-target-eid" type="search" placeholder="entity_id" autocomplete="off" list="entity-datalist" style="margin-bottom:4px">
    <select id="teach-target-action"><option value="turn_on">turn_on</option><option value="turn_off">turn_off</option></select>
    <label>Within (seconds, optional):</label>
    <input class="inject-search" id="teach-latency" type="number" min="0" max="3600" placeholder="0" style="margin-bottom:4px">
    <label>Label (optional):</label>
    <input class="inject-search" id="teach-label" type="text" placeholder="Movie night routine" style="margin-bottom:4px">
    <div class="modal-actions">
      <button class="action-btn" id="teach-cancel">Cancel</button>
      <button class="action-btn primary" id="teach-save">Save</button>
    </div>
  </div>
</div>
<datalist id="entity-datalist"></datalist>

<!-- (end pipeline tab + modals) -->

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
  <div class="sh" style="padding-top:20px;">Live Logs <button id="log-clear" style="background:none;border:none;color:var(--disabled-text-color,#636366);font-size:11px;cursor:pointer;float:right;">Clear</button></div>
  <div class="log-box" id="log-box"><span class="log-empty">No logs yet.</span></div>
</div>

<div class="toast" id="toast"></div>

<script>
const BASE = location.pathname.replace(/\\/+$/, '');
let _feedback = __FEEDBACK__;
let _suggestions = __SUGGESTIONS__;
let _entities = [];
let _status = 'idle';
let _systemStatus = null;
let _denyList = new Set();
let _logs = [];
const MAX_LOG_LINES = 200;
let _entitiesLoaded = false;
let _miningInFlight = false;

function escHtml(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function pct(v) { return Math.round((v||0)*100)+'%'; }
function domainEmoji(eid) {
  const d=(eid||'').split('.')[0];
  return {light:'💡',switch:'🔌',climate:'🌡️',media_player:'📺',cover:'🪟',fan:'💨',lock:'🔒',vacuum:'🤖',scene:'🎨',automation:'🤖',script:'📜',input_boolean:'🔘'}[d]||'⚙️';
}

// ── Relative time helper ──
function relTime(iso) {
  if (!iso) return null;
  const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 5) return 'just now';
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff/60) + ' min ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return Math.floor(diff/86400) + 'd ago';
}

// ── Tab navigation ──
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-page').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('page-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'pipeline') { refreshPipelineStatus(); loadUserPatterns(); refreshOutcomes(); }
    if (btn.dataset.tab === 'system') { refreshSystemStatus(); if (!_entitiesLoaded) { loadEntities().then(() => renderEntities('')); } renderLogs(); }
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

const THUMB_UP_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14"><path d="M5,9V21H1V9H5M9,21A2,2 0 0,1 7,19V9C7,8.45 7.22,7.95 7.59,7.59L14.17,1L15.23,2.06C15.5,2.33 15.67,2.7 15.67,3.11L15.64,3.43L14.69,8H21C22.11,8 23,8.89 23,10V12C23,12.26 22.95,12.5 22.86,12.73L19.84,19.78C19.54,20.5 18.83,21 18,21H9Z"/></svg>';
const THUMB_DOWN_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14"><path d="M19,15H23V3H19M15,3H6C5.17,3 4.46,3.5 4.16,4.22L1.14,11.27C1.05,11.5 1,11.74 1,12V14A2,2 0 0,0 3,16H9.31L8.36,20.57C8.34,20.67 8.33,20.77 8.33,20.88C8.33,21.3 8.5,21.67 8.77,21.94L9.83,23L16.41,16.41C16.78,16.05 17,15.55 17,15V5C17,3.89 16.1,3 15,3Z"/></svg>';

function makeRow(s) {
  const cur = s.current_state || '?';
  const act = s.action || '';
  const confirmedBadge = s.user_confirmed ? `<span class="badge badge-confirmed" style="margin-left:6px;vertical-align:middle">Confirmed</span>` : '';
  return `<div class="sug">
    <div class="sug-icon">${domainEmoji(s.entity_id)}</div>
    <div class="sug-body">
      <div class="sug-name">${escHtml(s.name || s.entity_id || '')}${confirmedBadge}</div>
      <div class="sug-eid">${(s.entity_id||'').split('.')[0]} · ${s.entity_id||''}</div>
      <div class="sug-transition">
        <span class="state-chip" style="background:${stateColor(cur)}">${escHtml(cur)}</span>
        <span class="state-arrow">→</span>
        <span class="action-chip">${escHtml(actionLabel(act))}</span>
      </div>
      <div class="sug-reason">${escHtml(s.reason || s.description || '')}</div>
    </div>
    <div class="sug-actions">
      <button class="vote-btn up" data-eid="${escHtml(s.entity_id)}" data-vote="up"
        data-signature="${escHtml(s.signature||'')}" data-miner="${escHtml(s.miner_type||'')}">${THUMB_UP_SVG}</button>
      <button class="vote-btn down" data-eid="${escHtml(s.entity_id)}" data-vote="down"
        data-signature="${escHtml(s.signature||'')}" data-miner="${escHtml(s.miner_type||'')}">${THUMB_DOWN_SVG}</button>
      <button class="deny-btn" data-eid="${escHtml(s.entity_id)}" title="Block">🚫</button>
    </div>
  </div>`;
}

function render() {
  const dot = document.getElementById('status-dot');
  dot.className = 'status-dot' + (_status === 'updating' || _miningInFlight ? ' updating' : '');
  const metaEl = document.getElementById('meta-text');
  metaEl.textContent = _miningInFlight ? 'Mining…' : (_status === 'updating' ? 'Updating…' : _status === 'ready' ? 'Ready' : 'Idle');
  const container = document.getElementById('list-container');
  if (!_suggestions.length) {
    const hasCycled = _systemStatus && _systemStatus.last_hourly_completed_at;
    if (hasCycled) {
      container.innerHTML = '<div class="empty"><svg viewBox="0 0 24 24" width="44" height="44"><path fill="currentColor" d="M12 2A7 7 0 0 0 5 9C5 11.38 6.19 13.47 8 14.74V17A1 1 0 0 0 9 18H15A1 1 0 0 0 16 17V14.74C17.81 13.47 19 11.38 19 9A7 7 0 0 0 12 2M9 21A1 1 0 0 0 10 22H14A1 1 0 0 0 15 21V20H9V21M11 14H13V8H15L12 4L9 8H11V14Z"/></svg>No suggestions found.<div class="empty-hint">Try lowering thresholds in <b>Pipeline → Mining Settings</b>.</div></div>';
    } else {
      container.innerHTML = '<div class="empty"><svg viewBox="0 0 24 24" width="44" height="44"><path fill="currentColor" d="M12 2A7 7 0 0 0 5 9C5 11.38 6.19 13.47 8 14.74V17A1 1 0 0 0 9 18H15A1 1 0 0 0 16 17V14.74C17.81 13.47 19 11.38 19 9A7 7 0 0 0 12 2M9 21A1 1 0 0 0 10 22H14A1 1 0 0 0 15 21V20H9V21M11 14H13V8H15L12 4L9 8H11V14Z"/></svg>Waiting for first mining cycle (runs hourly).<div class="empty-hint">Click <b>Mine Now</b> to trigger immediately.</div></div>';
    }
    return;
  }
  const buckets = { suggestion:[], noticed:[], suggested:[], scene:[], stretch:[] };
  _suggestions.forEach(s => {
    const zone = s.zone || s.section || 'suggested';
    const key = buckets[zone] ? zone : (((s.entity_id||'').split('.')[0]) === 'scene' ? 'scene' : 'suggested');
    buckets[key].push(s);
  });
  container.innerHTML = [
    {key:'suggestion',label:'Suggested for You'},
    {key:'suggested',label:'Suggested for You'},
    {key:'noticed',label:'Noticed'},
    {key:'scene',label:'Scenes'},
    {key:'stretch',label:'Worth Trying'},
  ].filter(({key})=>buckets[key]&&buckets[key].length).map(({key,label})=>
    `<div class="sh">${label}</div><div class="sug-list">${buckets[key].map(s=>makeRow(s)).join('')}</div>`
  ).join('');
  // Wire vote + deny buttons
  container.querySelectorAll('.vote-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const eid=btn.dataset.eid, vote=btn.dataset.vote;
      const sig=btn.dataset.signature, miner=btn.dataset.miner;
      btn.classList.add('voted');
      try {
        // Legacy feedback REST for non-pipeline suggestions
        await fetch(BASE+'/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entity_id:eid,vote})});
        if(!_feedback[eid])_feedback[eid]={up:0,down:0};
        _feedback[eid][vote]=(_feedback[eid][vote]||0)+1;
        // New-pipeline signal via WS (has signature + miner_type)
        if (sig && miner && _ws && _ws.readyState === WebSocket.OPEN) {
          _ws.send(JSON.stringify({type:'signal', signature:sig, miner_type:miner, signal:vote==='up'?'up':'down'}));
        }
        showToast(vote==='up'?'👍 Upvoted':'👎 Downvoted');
        render();
      } catch { showToast('Vote failed'); }
    });
  });
  container.querySelectorAll('.deny-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const eid=btn.dataset.eid;
      try {
        await fetch(BASE+'/deny',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entity_id:eid})});
        _denyList.add(eid); showToast('Blocked'); renderDenyPanel();
      } catch { showToast('Block failed'); }
    });
  });
}

// ── Mine Now ──
async function triggerMineNow(btn) {
  if (_miningInFlight) return;
  _miningInFlight = true;
  const orig = btn.innerHTML;
  btn.classList.add('loading');
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14"><path d="M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z"/></svg> Mining…';
  // Update header chip
  const chip = document.getElementById('cycle-chip');
  chip.textContent = 'Mining…'; chip.className = 'cycle-chip mining'; chip.style.display = '';
  render();
  try {
    await fetch(BASE+'/refresh_all', {method:'POST'});
    showToast('Mining started — results appear when done');
  } catch { showToast('Failed to trigger mining'); }
  setTimeout(() => {
    _miningInFlight = false;
    btn.classList.remove('loading');
    btn.innerHTML = orig;
    chip.className = 'cycle-chip'; chip.style.display = 'none';
    render();
    refreshPipelineStatus();
  }, 8000);
}

const mineBtn1 = document.getElementById('mine-now-btn');
const mineBtn2 = document.getElementById('mine-now-btn-2');
mineBtn1.addEventListener('click', function(){ triggerMineNow(this); });
mineBtn2.addEventListener('click', function(){ triggerMineNow(this); });

// ── Pipeline status ──
async function refreshPipelineStatus() {
  try {
    const r = await fetch(BASE+'/status');
    const s = await r.json();
    _systemStatus = s;
    // Hourly cycle
    const hourlyEl = document.getElementById('ps-hourly-val');
    const rt = relTime(s.last_hourly_completed_at);
    if (hourlyEl) hourlyEl.textContent = rt || 'Never';
    // Waste check
    const wasteEl = document.getElementById('ps-waste-val');
    const wrt = relTime(s.last_waste_check_at);
    if (wasteEl) wasteEl.textContent = wrt || 'Never';
    // Counts
    const sugCount = document.getElementById('ps-sug-count');
    if (sugCount) sugCount.textContent = (s.suggestion_zone_count !== undefined ? s.suggestion_zone_count : '—') + ' 💡';
    const noticeCount = document.getElementById('ps-noticed-count');
    if (noticeCount) noticeCount.textContent = (s.noticed_zone_count !== undefined ? s.noticed_zone_count : '—') + ' ⚠️';
    // Update header chip with last cycle time
    const chip = document.getElementById('cycle-chip');
    if (rt && !_miningInFlight) {
      chip.textContent = 'Last cycle: ' + rt;
      chip.className = 'cycle-chip';
      chip.style.display = '';
    }
    // Mining settings
    const ms = s.mining_settings || {};
    const setVal = (id, key, suffix) => {
      const el = document.getElementById(id);
      if (el && ms[key]) el.textContent = ms[key].value + (suffix||'');
    };
    setVal('ps-min-occ', 'min_pattern_occurrences', ' ×');
    setVal('ps-min-conf', 'min_pattern_confidence', '');
    setVal('ps-hist-days', 'mining_history_days', 'd');
    setVal('ps-mine-int', 'mining_interval_hours', 'h');
    setVal('ps-waste-int', 'waste_check_interval_minutes', 'm');
  } catch (e) {
    _LOGGER_JS_WARN('refreshPipelineStatus failed: ' + e);
  }
}
function _LOGGER_JS_WARN(msg) { /* swallow */ }

// ── System tab ──
function renderStatusGrid(s) {
  const el=document.getElementById('sys-status-grid');
  if(!el||!s) return;
  const ok=v=>v?'🟢':'🔴';
  el.innerHTML=`
    <div class="sys-card"><div class="sys-label">HA</div><div class="sys-val">${ok(s.ha_connected)} ${s.ha_connected?'Connected':'Down'}</div></div>
    <div class="sys-card"><div class="sys-label">AI / Ollama</div><div class="sys-val">${ok(s.ollama_connected)} ${s.ollama_connected?'Connected':'Down'}</div></div>
    <div class="sys-card"><div class="sys-label">Entities</div><div class="sys-val">${s.entity_count||0}</div></div>
    <div class="sys-card"><div class="sys-label">Suggestions</div><div class="sys-val">${(s.suggestion_zone_count||0)+(s.noticed_zone_count||0)}</div></div>
    <div class="sys-card"><div class="sys-label">Last Refresh</div><div class="sys-val">${s.last_refresh||'Never'}</div></div>
    <div class="sys-card"><div class="sys-label">Last Mining</div><div class="sys-val">${relTime(s.last_hourly_completed_at)||'Never'}</div></div>
  `;
}

async function refreshSystemStatus() {
  try {
    const r=await fetch(BASE+'/status');
    const s=await r.json();
    _systemStatus = s;
    renderStatusGrid(s);
  } catch {}
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
      <div class="entity-info"><div class="entity-name">${domainEmoji(e.entity_id)} ${escHtml(e.name||e.entity_id)}</div><div class="entity-id">${escHtml(e.entity_id)} · ${escHtml(e.current_state||'')}</div></div>
      <button class="add-btn" data-eid="${escHtml(e.entity_id)}" data-name="${escHtml((e.name||e.entity_id))}" data-domain="${escHtml(e.domain||'')}">Add</button>
    </div>`).join('');
  el.querySelectorAll('.add-btn').forEach(btn=>{
    btn.addEventListener('click', async ()=>{
      btn.textContent='✓'; btn.disabled=true;
      try { await fetch(BASE+'/inject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entity_id:btn.dataset.eid,name:btn.dataset.name,domain:btn.dataset.domain})}); showToast('Added'); }
      catch { showToast('Failed'); btn.textContent='Add'; btn.disabled=false; }
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
    <div class="deny-row"><span>${domainEmoji(eid)}</span><span class="deny-eid">${escHtml(eid)}</span>
    <button class="undeny-btn" data-eid="${escHtml(eid)}">Unblock</button></div>`).join('');
  el.querySelectorAll('.undeny-btn').forEach(btn=>{
    btn.addEventListener('click', async ()=>{
      try { await fetch(BASE+'/deny',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({entity_id:btn.dataset.eid})}); _denyList.delete(btn.dataset.eid); showToast('Unblocked'); renderDenyPanel(); }
      catch { showToast('Failed'); }
    });
  });
}

// ── Logs ──
document.getElementById('log-clear').addEventListener('click',()=>{_logs=[];renderLogs();});
function makeLogLine(e){return `<div class="log-line"><span class="log-ts">${escHtml(e.ts)}</span><span class="log-lvl ${escHtml(e.level)}">${escHtml(e.level)}</span><span class="log-msg">${escHtml(e.msg)}</span></div>`;}
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

// ── Teach a Pattern modal ──
function populateEntityDatalist() {
  const dl = document.getElementById('entity-datalist');
  if (!dl) return;
  dl.innerHTML = _entities.map(e => `<option value="${escHtml(e.entity_id)}">`).join('');
}

document.getElementById('teach-pattern-btn').addEventListener('click', async () => {
  if (!_entitiesLoaded) await loadEntities();
  populateEntityDatalist();
  document.getElementById('teach-modal').style.display = 'flex';
});
document.getElementById('teach-cancel').addEventListener('click', () => {
  document.getElementById('teach-modal').style.display = 'none';
});
document.getElementById('teach-save').addEventListener('click', async () => {
  const te = document.getElementById('teach-trigger-eid').value.trim();
  const ta = document.getElementById('teach-trigger-action').value;
  const ge = document.getElementById('teach-target-eid').value.trim();
  const ga = document.getElementById('teach-target-action').value;
  const lat = parseInt(document.getElementById('teach-latency').value || '0', 10) || 0;
  const lbl = document.getElementById('teach-label').value.trim();
  if (!te || !ge) { showToast('Trigger and target entity required'); return; }
  try {
    const r = await fetch(BASE+'/user_patterns', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({trigger_entity:te, trigger_action:ta, target_entity:ge, target_action:ga, latency_seconds:lat, label:lbl})
    });
    if (!r.ok) throw new Error(await r.text());
    document.getElementById('teach-modal').style.display = 'none';
    // Reset form
    document.getElementById('teach-trigger-eid').value = '';
    document.getElementById('teach-target-eid').value = '';
    document.getElementById('teach-latency').value = '';
    document.getElementById('teach-label').value = '';
    showToast('Pattern saved');
    loadUserPatterns();
  } catch(e) { showToast('Failed: '+e.message); }
});

async function loadUserPatterns() {
  try {
    const r = await fetch(BASE+'/user_patterns');
    const patterns = await r.json();
    renderUserPatterns(patterns);
  } catch {}
}

function renderUserPatterns(patterns) {
  const el = document.getElementById('user-patterns-list');
  if (!el) return;
  if (!patterns.length) {
    el.innerHTML = '<div class="empty-hint" style="padding:14px 16px">No patterns taught yet.</div>';
    return;
  }
  el.innerHTML = patterns.map(p => {
    const lbl = p.label ? escHtml(p.label) : `${escHtml(p.trigger_entity)} → ${escHtml(p.target_entity)}`;
    const detail = `${escHtml(p.trigger_entity)} ${escHtml(p.trigger_action)} → ${escHtml(p.target_entity)} ${escHtml(p.target_action)}` +
      (p.latency_seconds ? ` within ${p.latency_seconds}s` : '');
    return `<div class="pattern-row">
      <div class="pattern-body">
        <div class="pattern-title">${lbl}</div>
        <div class="pattern-detail">${detail}</div>
      </div>
      <button class="pattern-del-btn" data-pid="${p.id}">Remove</button>
    </div>`;
  }).join('');
  el.querySelectorAll('.pattern-del-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        await fetch(BASE+'/user_patterns', {method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id:parseInt(btn.dataset.pid)})});
        showToast('Pattern removed');
        loadUserPatterns();
      } catch { showToast('Failed'); }
    });
  });
}

// ── Outcomes ──
async function refreshOutcomes() {
  try {
    const r = await fetch(BASE+'/outcomes');
    const data = await r.json();
    renderOutcomes(data);
  } catch {}
}

function renderOutcomes(stats) {
  const el = document.getElementById('pipeline-outcomes-card');
  if (!el) return;
  const entries = Object.entries(stats);
  if (!entries.length) {
    el.innerHTML = '<div class="empty-hint" style="padding:14px 16px">Tracking your follow-through. Build up some history first.</div>';
    return;
  }
  el.innerHTML = entries.map(([mt, s]) =>
    `<div class="outcome-row">
      <span class="outcome-miner">${escHtml(mt.replace('_',' '))}</span>
      <span class="outcome-rate">${s.acted}/${s.suggested} acted on (${Math.round((s.acted/Math.max(1,s.suggested))*100)}%)</span>
    </div>`
  ).join('');
}

// ── WebSocket ──
let _ws = null;
function connectWS(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  _ws=new WebSocket(proto+'//'+location.host+BASE+'/ws');
  _ws.addEventListener('open',()=>{
    document.getElementById('meta-text').textContent='Connected';
    refreshPipelineStatus();
    loadUserPatterns();
    refreshOutcomes();
  });
  _ws.addEventListener('message',evt=>{
    let msg; try{msg=JSON.parse(evt.data);}catch{return;}
    if(msg.type==='suggestions'){_suggestions=Array.isArray(msg.data)?msg.data:[];_status='ready';render();}
    else if(msg.type==='status'){_status=msg.state||'idle';render();}
    else if(msg.type==='log'){appendLog(msg);}
    else if(msg.type==='system_status'){_systemStatus=msg.data;renderStatusGrid(msg.data);}
    else if(msg.type==='deny_list'){_denyList=new Set(msg.data||[]);renderDenyPanel();}
    else if(msg.type==='add_user_pattern_ack'){loadUserPatterns();showToast('Pattern saved');}
    else if(msg.type==='delete_user_pattern_ack'){loadUserPatterns();}
  });
  _ws.addEventListener('close',()=>{
    _ws=null;
    document.getElementById('status-dot').className='status-dot';
    document.getElementById('meta-text').textContent='Reconnecting…';
    setTimeout(connectWS,5000);
  });
}

// ── Init ──
render();
renderDenyPanel();
connectWS();
fetch(BASE+'/logs').then(r=>r.json()).then(d=>{_logs=d;}).catch(()=>{});
</script>
</body>
</html>"""


class WSServer:
    """Manages connected card WebSocket clients and broadcasts events."""

    def __init__(self, signal_store=None, user_pattern_store=None, outcome_tracker=None,
                 dismissal_store=None) -> None:
        # Accept either signal_store= or legacy dismissal_store=
        self._signal_store = signal_store or dismissal_store
        self._user_pattern_store = user_pattern_store
        self._outcome_tracker = outcome_tracker
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
        self._app.router.add_get("/outcomes", self._outcomes_handler)
        self._app.router.add_get("/user_patterns", self._user_patterns_handler)
        self._app.router.add_post("/user_patterns", self._add_user_pattern_handler)
        self._app.router.add_delete("/user_patterns", self._delete_user_pattern_handler)
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
        self._pipeline_state: dict = {}
        self._addon_opts: dict = {}

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

    def set_pipeline_state(self, state: dict, opts: dict) -> None:
        """Store a reference to the live pipeline state dict and addon options."""
        self._pipeline_state = state
        self._addon_opts = opts

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
            if sig and miner_type_str and self._signal_store is not None:
                try:
                    from candidate import MinerType
                    from signal_store import SignalType
                    from datetime import datetime, timezone
                    # Support both new SignalStore and legacy DismissalStore
                    if hasattr(self._signal_store, 'add_signal'):
                        await self._signal_store.add_signal(
                            sig, MinerType(miner_type_str), SignalType.DISMISS,
                            datetime.now(timezone.utc)
                        )
                    else:
                        await self._signal_store.add_dismissal(
                            sig, MinerType(miner_type_str), datetime.now(timezone.utc)
                        )
                    await ws.send_json({"type": "dismiss_ack", "signature": sig})
                except Exception:
                    _LOGGER.exception("dismiss handler failed for signature=%s miner_type=%s", sig, miner_type_str)
                    await ws.send_json({"type": "dismiss_error", "signature": sig})

        elif msg_type == "signal":
            sig = msg.get("signature")
            miner_type_str = msg.get("miner_type")
            sig_type_str = msg.get("signal")  # "up", "down", or "dismiss"
            if sig and miner_type_str and sig_type_str and self._signal_store is not None:
                try:
                    from candidate import MinerType
                    from signal_store import SignalType
                    from datetime import datetime, timezone
                    await self._signal_store.add_signal(
                        sig, MinerType(miner_type_str), SignalType(sig_type_str),
                        datetime.now(timezone.utc)
                    )
                    await ws.send_json({"type": "signal_ack", "signature": sig, "signal": sig_type_str})
                except Exception:
                    _LOGGER.exception("signal handler failed")
                    await ws.send_json({"type": "signal_error", "signature": sig})

        elif msg_type == "add_user_pattern":
            if self._user_pattern_store is not None:
                try:
                    pid = await self._user_pattern_store.add(
                        trigger_entity=msg.get("trigger_entity", ""),
                        trigger_action=msg.get("trigger_action", "turn_on"),
                        target_entity=msg.get("target_entity", ""),
                        target_action=msg.get("target_action", "turn_on"),
                        latency_seconds=int(msg.get("latency_seconds", 0) or 0),
                        label=msg.get("label", ""),
                    )
                    if ws:
                        await ws.send_json({"type": "add_user_pattern_ack", "id": pid})
                except Exception:
                    _LOGGER.exception("add_user_pattern handler failed")
                    if ws:
                        await ws.send_json({"type": "add_user_pattern_error"})

        elif msg_type == "list_user_patterns":
            if self._user_pattern_store is not None and ws:
                try:
                    patterns = await self._user_pattern_store.list_all()
                    await ws.send_json({
                        "type": "user_patterns",
                        "data": [
                            {"id": p.pattern_id, "trigger_entity": p.trigger_entity,
                             "trigger_action": p.trigger_action, "target_entity": p.target_entity,
                             "target_action": p.target_action, "latency_seconds": p.latency_seconds,
                             "label": p.label}
                            for p in patterns
                        ]
                    })
                except Exception:
                    _LOGGER.exception("list_user_patterns handler failed")

        elif msg_type == "delete_user_pattern":
            if self._user_pattern_store is not None:
                try:
                    pid = int(msg.get("id", 0))
                    await self._user_pattern_store.delete(pid)
                    if ws:
                        await ws.send_json({"type": "delete_user_pattern_ack", "id": pid})
                except Exception:
                    _LOGGER.exception("delete_user_pattern handler failed")
                    if ws:
                        await ws.send_json({"type": "delete_user_pattern_error"})

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
        ps = self._pipeline_state
        opts = self._addon_opts
        payload = dict(self._system_status)
        payload["last_hourly_completed_at"] = ps.get("last_hourly_at")
        payload["last_waste_check_at"] = ps.get("last_waste_at")
        payload["suggestion_zone_count"] = len(ps.get("last_suggestion_zone", []))
        payload["noticed_zone_count"] = len(ps.get("last_noticed_zone", []))
        payload["mining_settings"] = {
            "min_pattern_occurrences": {
                "value": opts.get("min_pattern_occurrences", 5),
                "desc": "How many times a pattern must repeat in 30d to be considered.",
            },
            "min_pattern_confidence": {
                "value": opts.get("min_pattern_confidence", 0.7),
                "desc": "Minimum P(action|trigger) — higher = stricter.",
            },
            "mining_history_days": {
                "value": opts.get("mining_history_days", 30),
                "desc": "How many days of history to mine each cycle.",
            },
            "mining_interval_hours": {
                "value": opts.get("mining_interval_hours", 1),
                "desc": "How often the main miners run.",
            },
            "waste_check_interval_minutes": {
                "value": opts.get("waste_check_interval_minutes", 5),
                "desc": "How often the waste detector checks current state.",
            },
        }
        return web.json_response(payload)

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

    async def _outcomes_handler(self, request: web.Request) -> web.Response:
        if not self._outcome_tracker:
            return web.json_response({})
        try:
            from datetime import timedelta
            stats = await self._outcome_tracker.stats_per_miner(timedelta(days=7))
            return web.json_response({
                mt: {"miner_type": s.miner_type, "suggested": s.suggested, "acted": s.acted_on}
                for mt, s in stats.items()
            })
        except Exception:
            _LOGGER.exception("outcomes handler failed")
            return web.json_response({})

    async def _user_patterns_handler(self, request: web.Request) -> web.Response:
        if not self._user_pattern_store:
            return web.json_response([])
        try:
            patterns = await self._user_pattern_store.list_all()
            return web.json_response([
                {"id": p.pattern_id, "trigger_entity": p.trigger_entity,
                 "trigger_action": p.trigger_action, "target_entity": p.target_entity,
                 "target_action": p.target_action, "latency_seconds": p.latency_seconds,
                 "label": p.label}
                for p in patterns
            ])
        except Exception:
            _LOGGER.exception("user_patterns handler failed")
            return web.json_response([])

    async def _add_user_pattern_handler(self, request: web.Request) -> web.Response:
        if not self._user_pattern_store:
            return web.json_response({"ok": False, "error": "not configured"}, status=503)
        try:
            data = await request.json()
            pid = await self._user_pattern_store.add(
                trigger_entity=data.get("trigger_entity", ""),
                trigger_action=data.get("trigger_action", "turn_on"),
                target_entity=data.get("target_entity", ""),
                target_action=data.get("target_action", "turn_on"),
                latency_seconds=int(data.get("latency_seconds", 0) or 0),
                label=data.get("label", ""),
            )
            return web.json_response({"ok": True, "id": pid})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def _delete_user_pattern_handler(self, request: web.Request) -> web.Response:
        if not self._user_pattern_store:
            return web.json_response({"ok": False, "error": "not configured"}, status=503)
        try:
            data = await request.json()
            pid = int(data.get("id", 0))
            await self._user_pattern_store.delete(pid)
            return web.json_response({"ok": True})
        except Exception as e:
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
