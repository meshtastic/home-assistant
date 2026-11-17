# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""The bundled web client's file and route serving."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from aiohttp import web

from custom_components.meshtastic.meshtastic_web import (
    CROSS_ORIGIN_ISOLATION_HEADERS,
    FROM_RADIO_HOLD_SECONDS,
    _resolve_web_client_path,
    _serve_web_client_path,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_a_bundled_file_is_served_cross_origin_isolated(hass: HomeAssistant) -> None:
    response = await _serve_web_client_path(hass, "index.html")

    assert isinstance(response, web.FileResponse)
    assert response._path.name == "index.html"
    for header, value in CROSS_ORIGIN_ISOLATION_HEADERS.items():
        assert response.headers[header] == value


async def test_a_missing_asset_is_answered_with_not_found(hass: HomeAssistant) -> None:
    assert isinstance(await _serve_web_client_path(hass, "assets/missing.js"), web.HTTPNotFound)


async def test_the_file_lookup_runs_off_the_event_loop(hass: HomeAssistant) -> None:
    loop_thread = threading.get_ident()
    lookup_threads = []

    def lookup(relative_path: str) -> Path | None:
        lookup_threads.append(threading.get_ident())
        return _resolve_web_client_path(relative_path)

    with patch("custom_components.meshtastic.meshtastic_web._resolve_web_client_path", lookup):
        await _serve_web_client_path(hass, "index.html")

    assert lookup_threads
    assert loop_thread not in lookup_threads


def test_a_client_route_falls_back_to_index_html() -> None:
    file = _resolve_web_client_path("messages/broadcast/0")

    assert file is not None
    assert file.name == "index.html"


def test_a_missing_asset_is_not_masked_by_index_html() -> None:
    assert _resolve_web_client_path("assets/missing.js") is None


def test_a_path_outside_the_static_directory_is_never_served() -> None:
    assert _resolve_web_client_path("../__init__.py") is None


def test_every_file_index_html_references_exists_with_its_exact_case() -> None:
    # git on macOS ignores case, so a rebuilt asset can keep a stale name that 404s on Home Assistant
    static = Path(_resolve_web_client_path.__code__.co_filename).parent / "static"
    referenced = set(re.findall(r'"/meshtastic/web/([^"]+)"', (static / "index.html").read_text()))
    present = {path.name for path in static.iterdir()}

    assert referenced
    assert referenced <= present, referenced - present


def test_the_fromradio_hold_ends_before_the_client_read_timeout() -> None:
    assert FROM_RADIO_HOLD_SECONDS < 7  # READ_TIMEOUT_MS in @meshtastic/transport-http
