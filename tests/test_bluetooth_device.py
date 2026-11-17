# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""Which Bluetooth device a connect uses."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.meshtastic.aiomeshtastic.connection import bluetooth
from custom_components.meshtastic.aiomeshtastic.connection.bluetooth import (
    BluetoothConnection,
    BluetoothDeviceNotFoundError,
)
from custom_components.meshtastic.aiomeshtastic.connection.errors import ClientApiConnectionError

ADDRESS = "AA:BB:CC:DD:EE:FF"


def _client() -> SimpleNamespace:
    service = MagicMock()
    return SimpleNamespace(services={BluetoothConnection.BTM_SERVICE_UUID: service}, is_connected=True)


async def test_every_connect_asks_for_the_current_device() -> None:
    devices = [object(), object()]
    provided = iter(devices)
    used: list[Any] = []

    async def establish(**kwargs: Any) -> SimpleNamespace:
        used.append(kwargs["device"])
        return _client()

    connection = BluetoothConnection(ADDRESS, ble_device_provider=lambda: next(provided))
    with patch.object(bluetooth, "establish_connection", establish):
        await connection.connect()
        await connection.connect()

    assert used == devices


async def test_a_radio_out_of_reach_fails_the_connect_without_a_raw_client() -> None:
    connection = BluetoothConnection(ADDRESS, ble_device_provider=lambda: None)

    with (
        patch.object(bluetooth, "BleakClient") as raw_client,
        pytest.raises(BluetoothDeviceNotFoundError) as raised,
    ):
        await connection.connect()

    raw_client.assert_not_called()
    assert isinstance(raised.value, ClientApiConnectionError)
