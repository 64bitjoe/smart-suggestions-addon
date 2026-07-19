// smart_suggestions/card/smart-suggestions-card.js
// Lovelace card for the Smart Suggestions add-on.
// Resource: /local/smart-suggestions-card.js  (type: JavaScript Module)
// Card config: - type: custom:smart-suggestions-card

const SENSORS = {
  now: "sensor.smart_suggestions_now",
  discoveries: "sensor.smart_suggestions_discoveries",
  noticed: "sensor.smart_suggestions_noticed",
};
const ZONE_TITLES = { now: "Right now", discoveries: "Discovered patterns", noticed: "Noticed" };

class SmartSuggestionsCard extends HTMLElement {
  setConfig(config) { this._config = config; }
  getCardSize() { return 4; }

  set hass(hass) {
    this._hass = hass;
    const key = Object.values(SENSORS)
      .map((id) => hass.states[id]?.last_updated ?? "")
      .join("|");
    if (key === this._renderedKey) return;
    this._renderedKey = key;
    this._render();
  }

  _suggestions(zone) {
    const st = this._hass.states[SENSORS[zone]];
    return (st && st.attributes.suggestions) || [];
  }

  _fire(action, signature) {
    this._hass.callApi("POST", "events/smart_suggestions_action", {
      action, signature,
    });
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const zones = ["now", "noticed", "discoveries"];
    const sections = zones
      .map((zone) => {
        const items = this._suggestions(zone);
        if (!items.length) return "";
        const rows = items
          .map(
            (s, i) => `
          <div class="item">
            <div class="text">
              <div class="title">${s.title}</div>
              <div class="desc">${s.description}</div>
            </div>
            <div class="btns" data-zone="${zone}" data-i="${i}">
              ${zone !== "discoveries" ? `<button class="run">Run</button>` : ""}
              ${s.can_automate ? `<button class="accept">Automate</button>` : ""}
              ${zone === "noticed" ? `<button class="snooze">Snooze</button>` : ""}
              <button class="dismiss">✕</button>
            </div>
          </div>`
          )
          .join("");
        return `<div class="zone"><h3>${ZONE_TITLES[zone]}</h3>${rows}</div>`;
      })
      .join("");

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 12px 16px; }
        h3 { margin: 8px 0 4px; font-size: 0.85em; text-transform: uppercase;
             letter-spacing: 0.06em; color: var(--secondary-text-color); }
        .item { display: flex; align-items: center; gap: 8px;
                padding: 8px 0; border-bottom: 1px solid var(--divider-color); }
        .item:last-child { border-bottom: none; }
        .text { flex: 1; min-width: 0; }
        .title { font-weight: 500; }
        .desc { font-size: 0.85em; color: var(--secondary-text-color); }
        .btns { display: flex; gap: 4px; flex-shrink: 0; }
        button { border: none; border-radius: 12px; padding: 6px 10px;
                 cursor: pointer; font: inherit; font-size: 0.8em;
                 background: var(--secondary-background-color);
                 color: var(--primary-text-color); }
        button.run, button.accept { background: var(--primary-color); color: #fff; }
        .empty { color: var(--secondary-text-color); padding: 12px 0; }
      </style>
      <ha-card header="Smart Suggestions">
        ${sections || `<div class="empty">Nothing to suggest right now — still learning your patterns.</div>`}
      </ha-card>`;

    this.shadowRoot.querySelectorAll(".btns").forEach((btns) => {
      const zone = btns.dataset.zone;
      const item = this._suggestions(zone)[Number(btns.dataset.i)];
      if (!item) return;
      const map = { run: "run", accept: "accept", snooze: "snooze", dismiss: "dismiss" };
      Object.entries(map).forEach(([cls, action]) => {
        const b = btns.querySelector(`.${cls}`);
        if (b) b.addEventListener("click", () => this._fire(action, item.signature));
      });
    });
  }
}

customElements.define("smart-suggestions-card", SmartSuggestionsCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "smart-suggestions-card",
  name: "Smart Suggestions Card",
  description: "One-tap contextual actions, discoveries, and waste alerts.",
});
