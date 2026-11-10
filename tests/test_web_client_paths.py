# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""The bundled web client's file and route serving."""

from __future__ import annotations

import re
from pathlib import Path

from aiohttp import web

from custom_components.meshtastic.meshtastic_web import (
    CROSS_ORIGIN_ISOLATION_HEADERS,
    FROM_RADIO_HOLD_SECONDS,
    _serve_web_client_path,
)


def test_a_bundled_file_is_served_cross_origin_isolated() -> None:
    response = _serve_web_client_path("index.html")

    assert isinstance(response, web.FileResponse)
    assert response._path.name == "index.html"
    for header, value in CROSS_ORIGIN_ISOLATION_HEADERS.items():
        assert response.headers[header] == value


def test_a_client_route_falls_back_to_index_html() -> None:
    response = _serve_web_client_path("messages/broadcast/0")

    assert isinstance(response, web.FileResponse)
    assert response._path.name == "index.html"


def test_a_missing_asset_is_not_masked_by_index_html() -> None:
    assert isinstance(_serve_web_client_path("assets/missing.js"), web.HTTPNotFound)


def test_a_path_outside_the_static_directory_is_never_served() -> None:
    response = _serve_web_client_path("../__init__.py")

    assert isinstance(response, web.HTTPNotFound)


def test_every_file_index_html_references_exists_with_its_exact_case() -> None:
    # git on macOS ignores case, so a rebuilt asset can keep a stale name that 404s on Home Assistant
    static = Path(_serve_web_client_path.__code__.co_filename).parent / "static"
    referenced = set(re.findall(r'"/meshtastic/web/([^"]+)"', (static / "index.html").read_text()))
    present = {path.name for path in static.iterdir()}

    assert referenced
    assert referenced <= present, referenced - present


def test_the_fromradio_hold_ends_before_the_client_read_timeout() -> None:
    assert FROM_RADIO_HOLD_SECONDS < 7  # READ_TIMEOUT_MS in @meshtastic/transport-http
