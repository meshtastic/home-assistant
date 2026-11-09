// SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
//
// SPDX-License-Identifier: MIT

import {
  LitElement,
  html,
  css,
} from "https://unpkg.com/lit-element@2.4.0/lit-element.js?module";

class MeshtasticPanel extends LitElement {
  static get properties() {
    return {
      hass: { type: Object },
      narrow: { type: Boolean },
      route: { type: Object },
      panel: { type: Object },
    };
  }

  render() {
    if (!this.hass) {
      return html``;
    }

    const mesh_entities = Object.values(this.hass.entities).filter(e => e.platform === "meshtastic" && e.entity_id.startsWith("meshtastic."));
    const gateway_entities = mesh_entities.filter(e => this.hass.states[e.entity_id].attributes["device_class"] === "gateway");

    const gateway_info = gateway_entities.map(e => ({
      "entity": e.entity_id,
      "name": this.hass.states[e.entity_id].attributes["friendly_name"],
      "unavailable": this.hass.states[e.entity_id].state === "unavailable"
    }));

    return html`
      <ha-top-app-bar-fixed>
        <ha-menu-button
            slot="navigationIcon"
            .hass=${this.hass}
            .narrow=${this.narrow}
        ></ha-menu-button>
        <div slot="title">Meshtastic</div>
        
        <div class="">
          <h1>Web Client</h1>
          <div class="container">
            ${gateway_info.map((gateway) => html`
              <ha-card outlined>
                <div class="card-content">
                  <img alt="" crossorigin="anonymous" referrerpolicy="no-referrer"
                       src="https://brands.home-assistant.io/meshtastic/dark_icon.png"
                       style="visibility: initial;">
                  <h2>${gateway.name || gateway.entity}</h2>
                  <h3>&nbsp;</h3>
                  <div class="card-actions">
                    ${gateway.unavailable ? html`
                      <ha-button unelevated disabled="true">Open</ha-button>` : html`
                      <ha-button unelevated @click=${() => this._openWebClient(gateway.entity)}>Open</ha-button>`}
                  </div>
              </ha-card>
            `)}
          </div>
      </ha-top-app-bar-fixed>
    `;
  }

  async _openWebClient(entityId) {
    // Opened before the await so the browser treats it as part of the click, not a popup.
    const tab = window.open("about:blank", "_blank");
    try {
      const { url } = await this.hass.callWS({ type: "meshtastic/web_client/open", entity_id: entityId });
      tab.location.href = url;
    } catch (err) {
      tab.close();
      alert(`Could not open the web client: ${err.message || err.code}`);
    }
  }

  static get styles() {
    return css`
      :host {
        display: block;
      }

      :host([virtualize]) {
        height: 100%;
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

      a {
        text-decoration: none;
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
      
      .card-content h3 {
        font-size: 14px;
        margin: 0px;
        max-width: 100%;
        text-align: center;
        font-weight: normal;
      }

      .card-content img {
        width: 40px;
        height: 40px;
      }

      .card-actions {
        border-top: none;
        padding-top: 0px;
        padding-bottom: 16px;
        justify-content: center;
        display: flex;
      }
    `;
  }
}

if (!customElements.get("meshtastic-frontend")) {
  customElements.define("meshtastic-frontend", MeshtasticPanel);
}