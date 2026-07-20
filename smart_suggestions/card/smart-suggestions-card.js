// smart_suggestions/card/smart-suggestions-card.js
// Smart Suggestions card v2 — zones, activity trail, promote chips.
// Resource: /local/smart-suggestions-card.js (JavaScript Module)

const SENSORS = {
  now: "sensor.smart_suggestions_now",
  activity: "sensor.smart_suggestions_activity",
  noticed: "sensor.smart_suggestions_noticed",
  discoveries: "sensor.smart_suggestions_discoveries",
};
const ZONE_TITLES = {
  now: "Right now", activity: "Auto-pilot",
  noticed: "Noticed", discoveries: "Discovered patterns",
};
const MINER_ICONS = {
  temporal: "mdi:clock-outline", sequence: "mdi:arrow-decision",
  cross_area: "mdi:motion-sensor", waste: "mdi:alert-circle-outline",
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const timeAgo = (ts) => {
  const mins = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  return `${Math.round(mins / 60)}h ago`;
};

class SmartSuggestionsCard extends HTMLElement {
  constructor() { super(); this._expanded = new Set(); }
  setConfig(config) { this._config = config; }
  getCardSize() { return 5; }

  set hass(hass) {
    this._hass = hass;
    const key = Object.values(SENSORS)
      .map((id) => hass.states[id]?.last_updated ?? "")
      .join("|");
    if (key === this._renderedKey) return;
    this._renderedKey = key;
    this._render();
  }

  _items(zone) {
    const st = this._hass.states[SENSORS[zone]];
    return (st && st.attributes.suggestions) || [];
  }

  _fire(payload) {
    this._hass.callApi("POST", "events/smart_suggestions_action", payload);
    this._renderedKey = null; // force refresh on next hass tick
  }

  _suggestionRow(s, zone) {
    const open = this._expanded.has(s.signature);
    const pct = Math.round((s.confidence || 0) * 100);
    return `
      <div class="item" data-sig="${esc(s.signature)}">
        <ha-icon class="mi" icon="${MINER_ICONS[s.miner_type] || "mdi:lightbulb-outline"}"></ha-icon>
        <div class="text">
          <div class="title">${esc(s.title)}
            ${s.lifecycle === "autopilot" ? `<span class="badge">⚡</span>` : ""}
            <span class="chip">${s.occurrences}×</span>
          </div>
          <div class="bar"><span style="width:${pct}%"></span></div>
          ${open ? `<div class="desc">${esc(s.description)}</div>` : ""}
        </div>
        <div class="btns" data-zone="${zone}" data-sig="${esc(s.signature)}">
          ${s.can_promote ? `<button class="promote" title="Enable auto-run">⚡ Auto</button>` : ""}
          ${zone !== "discoveries" ? `<button class="run">Run</button>` : ""}
          ${s.can_automate ? `<button class="accept">Automate</button>` : ""}
          ${zone === "noticed" ? `<button class="snooze">Snooze</button>` : ""}
          <button class="dismiss">✕</button>
        </div>
      </div>`;
  }

  _activityRow(a) {
    const verb = a.act_action.includes("off") ? "Turned off" : "Turned on";
    return `
      <div class="item act ${a.undone ? "undone" : ""}">
        <ha-icon class="mi" icon="mdi:robot"></ha-icon>
        <div class="text">
          <div class="title">${verb} ${esc(a.title)}</div>
          <div class="desc">${timeAgo(a.ts)}${a.undone ? " · undone" : ""}</div>
        </div>
        ${a.undone ? "" :
          `<div class="btns"><button class="undo" data-aid="${a.activity_id}">Undo</button></div>`}
      </div>`;
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const sections = ["now", "activity", "noticed", "discoveries"]
      .map((zone) => {
        const items = this._items(zone);
        if (!items.length) return "";
        const rows = items.map((s) =>
          zone === "activity" ? this._activityRow(s) : this._suggestionRow(s, zone)
        ).join("");
        return `<div class="zone z-${zone}"><h3>${ZONE_TITLES[zone]}</h3>${rows}</div>`;
      }).join("");

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 12px 16px 14px; }
        h3 { margin: 10px 0 4px; font-size: 0.78em; text-transform: uppercase;
             letter-spacing: 0.08em; color: var(--secondary-text-color); }
        .z-now h3 { color: var(--primary-color); }
        .z-noticed h3 { color: var(--warning-color, #e6a23c); }
        .item { display: flex; align-items: flex-start; gap: 10px;
                padding: 9px 0; border-bottom: 1px solid var(--divider-color); }
        .item:last-child { border-bottom: none; }
        .mi { --mdc-icon-size: 20px; color: var(--secondary-text-color);
              margin-top: 2px; flex-shrink: 0; }
        .text { flex: 1; min-width: 0; cursor: pointer; }
        .title { font-weight: 500; line-height: 1.3; }
        .chip { font-size: 0.72em; color: var(--secondary-text-color);
                background: var(--secondary-background-color);
                border-radius: 8px; padding: 1px 6px; margin-left: 6px; }
        .badge { margin-left: 4px; }
        .bar { height: 3px; border-radius: 2px; margin-top: 5px;
               background: var(--divider-color); overflow: hidden; }
        .bar span { display: block; height: 100%; border-radius: 2px;
                    background: var(--primary-color); }
        .desc { font-size: 0.85em; color: var(--secondary-text-color);
                margin-top: 5px; }
        .btns { display: flex; gap: 4px; flex-shrink: 0; flex-wrap: wrap;
                justify-content: flex-end; max-width: 45%; }
        button { border: none; border-radius: 12px; padding: 6px 10px;
                 cursor: pointer; font: inherit; font-size: 0.78em;
                 background: var(--secondary-background-color);
                 color: var(--primary-text-color); }
        button.run, button.accept { background: var(--primary-color); color: #fff; }
        button.promote { background: #7c4dff; color: #fff; }
        button.undo { background: var(--warning-color, #e6a23c); color: #fff; }
        .act.undone { opacity: 0.55; }
        .empty { color: var(--secondary-text-color); padding: 12px 0; }
      </style>
      <ha-card header="Smart Suggestions">
        ${sections || `<div class="empty">Nothing to suggest right now — still learning your patterns.</div>`}
      </ha-card>`;

    // expand/collapse on text tap
    this.shadowRoot.querySelectorAll(".item[data-sig] .text").forEach((el) => {
      const sig = el.closest(".item").dataset.sig;
      el.addEventListener("click", () => {
        this._expanded.has(sig) ? this._expanded.delete(sig) : this._expanded.add(sig);
        this._renderedKey = null;
        this._render();
      });
    });
    // suggestion actions
    this.shadowRoot.querySelectorAll(".btns[data-sig]").forEach((btns) => {
      const sig = btns.dataset.sig;
      [["run", "run"], ["accept", "accept"], ["snooze", "snooze"],
       ["dismiss", "dismiss"], ["promote", "promote"]].forEach(([cls, action]) => {
        const b = btns.querySelector(`.${cls}`);
        if (b) b.addEventListener("click", (ev) => {
          ev.stopPropagation();
          this._fire({ action, signature: sig });
        });
      });
    });
    // activity undo
    this.shadowRoot.querySelectorAll("button.undo").forEach((b) => {
      b.addEventListener("click", () =>
        this._fire({ action: "undo", activity_id: Number(b.dataset.aid) }));
    });
  }
}

customElements.define("smart-suggestions-card", SmartSuggestionsCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "smart-suggestions-card",
  name: "Smart Suggestions Card",
  description: "One-tap actions, auto-pilot activity, discoveries, waste alerts.",
});
