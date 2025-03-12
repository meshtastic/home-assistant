"""Starting setup task: Frontend."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from homeassistant.components.frontend import (
    async_register_built_in_panel,
    async_remove_panel,
)
from homeassistant.components.http import StaticPathConfig

from .const import DOMAIN, URL_BASE
from .ha_frontend import locate_dir

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_DEBUG = False


async def async_register_frontend(hass: HomeAssistant) -> None:
    # Add to sidepanel if needed
    if DOMAIN not in hass.data.get("frontend_panels", {}):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(f"{URL_BASE}/frontend", locate_dir(), cache_headers=not _DEBUG)]
        )

        async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title="Meshtastic",
            sidebar_icon="mdi:radio-handheld",
            frontend_url_path=DOMAIN,
            config={
                "_panel_custom": {
                    "name": "meshtastic-frontend",
                    "embed_iframe": False,
                    "trust_external": False,
                    "module_url": f"{URL_BASE}/frontend/panel.js" + (f"?{time.time()}" if _DEBUG else ""),
                }
            },
            require_admin=True,
        )


async def async_unregister_frontend(hass: HomeAssistant) -> None:
    if DOMAIN in hass.data.get("frontend_panels", {}):
        async_remove_panel(
            hass,
            frontend_url_path=DOMAIN,
        )
