# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

"""Starting setup task: Frontend."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
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


def _panel_digest() -> str:
    # panel.js is served with long cache headers, so its URL changes with its content
    return hashlib.sha256((Path(locate_dir()) / "panel.js").read_bytes()).hexdigest()[:12]


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
                    "module_url": f"{URL_BASE}/frontend/panel.js?"
                    + (str(time.time()) if _DEBUG else await hass.async_add_executor_job(_panel_digest)),
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
