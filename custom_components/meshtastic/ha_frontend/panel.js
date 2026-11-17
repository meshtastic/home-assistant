// SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
//
// SPDX-License-Identifier: MIT

// Plain custom element: no imports, so the panel loads from the integration's own static path alone.

const STYLE = `
  :host {
    display: block;
  }

  h1 {
    margin-top: 8px;
    margin-left: 16px;
    margin-inline-start: 16px;
    margin-inline-end: initial;
    font-family: var(--paper-font-headline_-_font-family);
    -webkit-font-smoothing: var(--paper-font-headline_-_-webkit-font-smoothing);
    white-space: var(--paper-font-headline_-_white-space);
    overflow: var(--paper-font-headline_-_overflow);
    text-overflow: var(--paper-font-headline_-_text-overflow);
    font-size: var(--paper-font-headline_-_font-size);
    font-weight: var(--paper-font-headline_-_font-weight);
    line-height: var(--paper-font-headline_-_line-height);
  }

  .container {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 8px;
    padding: 8px 16px 16px;
  }

  .card-content {
    display: flex;
    justify-content: center;
    flex-direction: column;
    align-items: center;
  }

  .card-content h2 {
    font-size: 16px;
    font-weight: 400;
    margin-top: 8px;
    margin-bottom: 0px;
    max-width: 100%;
  }

  .card-content ha-icon {
    --mdc-icon-size: 40px;
    color: var(--primary-text-color);
  }

  .card-actions {
    padding: 8px 0 16px;
    justify-content: center;
    display: flex;
  }
`;

class MeshtasticPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = undefined;
    this._narrow = false;
    this._signature = undefined;
    this.attachShadow({ mode: "open" });
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  get hass() {
    return this._hass;
  }

  set narrow(narrow) {
    this._narrow = narrow;
    this._signature = undefined;
    this._render();
  }

  get narrow() {
    return this._narrow;
  }

  _gateways() {
    return Object.values(this._hass.entities)
      .filter((e) => e.platform === "meshtastic" && e.entity_id.startsWith("meshtastic."))
      .map((e) => ({ entity: e.entity_id, state: this._hass.states[e.entity_id] }))
      .filter(({ state }) => state && state.attributes["device_class"] === "gateway")
      .map(({ entity, state }) => ({
        entity,
        name: state.attributes["friendly_name"] || entity,
        unavailable: state.state === "unavailable",
      }));
  }

  _render() {
    if (!this._hass) {
      return;
    }
    const gateways = this._gateways();
    // hass changes on every state update, so rebuild only when the cards would differ
    const signature = JSON.stringify([this._narrow, gateways]);
    if (signature === this._signature) {
      return;
    }
    this._signature = signature;

    const root = this.shadowRoot;
    root.replaceChildren();

    const style = document.createElement("style");
    style.textContent = STYLE;

    const bar = document.createElement("ha-top-app-bar-fixed");
    const menu = document.createElement("ha-menu-button");
    menu.slot = "navigationIcon";
    menu.hass = this._hass;
    menu.narrow = this._narrow;
    const title = document.createElement("div");
    title.slot = "title";
    title.textContent = "Meshtastic";

    const heading = document.createElement("h1");
    heading.textContent = "Web Client";
    const container = document.createElement("div");
    container.className = "container";
    for (const gateway of gateways) {
      container.append(this._card(gateway));
    }

    const body = document.createElement("div");
    body.append(heading, container);
    bar.append(menu, title, body);
    root.append(style, bar);
  }

  _card(gateway) {
    const card = document.createElement("ha-card");
    card.setAttribute("outlined", "");

    const content = document.createElement("div");
    content.className = "card-content";
    const icon = document.createElement("ha-icon");
    icon.setAttribute("icon", "mdi:radio-handheld");
    const name = document.createElement("h2");
    name.textContent = gateway.name;
    content.append(icon, name);

    const actions = document.createElement("div");
    actions.className = "card-actions";
    const button = document.createElement("ha-button");
    button.setAttribute("unelevated", "");
    button.textContent = "Open";
    if (gateway.unavailable) {
      button.setAttribute("disabled", "");
    } else {
      button.addEventListener("click", () => this._openWebClient(gateway.entity));
    }
    actions.append(button);

    card.append(content, actions);
    return card;
  }

  async _openWebClient(entityId) {
    // Opened before the await so the browser treats it as part of the click, not a popup.
    const tab = window.open("about:blank", "_blank");
    try {
      const { url } = await this._hass.callWS({ type: "meshtastic/web_client/open", entity_id: entityId });
      tab.location.href = url;
    } catch (err) {
      tab.close();
      alert(`Could not open the web client: ${err.message || err.code}`);
    }
  }
}

if (!customElements.get("meshtastic-frontend")) {
  customElements.define("meshtastic-frontend", MeshtasticPanel);
}
