# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""The TCP proxy port check in the config and options flows."""

from __future__ import annotations

import socket
import threading
from typing import TYPE_CHECKING
from unittest.mock import patch

from custom_components.meshtastic.config_flow import _is_local_port_in_use, validate_tcp_proxy_port
from custom_components.meshtastic.const import (
    CONF_OPTION_TCP_PROXY,
    CONF_OPTION_TCP_PROXY_ENABLE,
    CONF_OPTION_TCP_PROXY_PORT,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _proxy_on(port: int) -> dict[str, dict[str, object]]:
    return {CONF_OPTION_TCP_PROXY: {CONF_OPTION_TCP_PROXY_ENABLE: True, CONF_OPTION_TCP_PROXY_PORT: port}}


async def test_the_port_probe_runs_off_the_event_loop(hass: HomeAssistant) -> None:
    loop_thread = threading.get_ident()
    probe_threads = []

    def probe(_port: int) -> bool:
        probe_threads.append(threading.get_ident())
        return False

    with patch("custom_components.meshtastic.config_flow._is_local_port_in_use", probe):
        assert await validate_tcp_proxy_port(hass, None, _proxy_on(4403))

    assert probe_threads
    assert loop_thread not in probe_threads


async def test_a_port_something_listens_on_is_rejected(hass: HomeAssistant) -> None:
    with patch("custom_components.meshtastic.config_flow._is_local_port_in_use", return_value=True):
        assert not await validate_tcp_proxy_port(hass, None, _proxy_on(4403))


def test_the_probe_sees_a_listening_socket(socket_enabled: None) -> None:  # noqa: ARG001
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        assert _is_local_port_in_use(port)

    assert not _is_local_port_in_use(port)
