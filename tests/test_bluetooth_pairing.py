# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""A radio is paired only after it rejects a GATT operation for missing authentication."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from bleak.exc import BleakDBusError, BleakError, BleakGATTProtocolError

from custom_components.meshtastic.aiomeshtastic.connection import bluetooth
from custom_components.meshtastic.aiomeshtastic.connection.bluetooth import BluetoothConnection

ADDRESS = "AA:BB:CC:DD:EE:FF"
INSUFFICIENT_AUTHENTICATION = 0x05
INSUFFICIENT_ENCRYPTION = 0x0F
UNLIKELY_ERROR = 0x0E  # what BlueZ answered a pair() on a radio without a PIN
INSUFFICIENT_AUTHORIZATION = 0x08


class BluetoothGATTAPIError(Exception):
    """Same name and fields as aioesphomeapi's, which is not importable here."""

    def __init__(self, code: int) -> None:
        super().__init__(f"Bluetooth GATT Error address=AA:BB handle=44 error={code} description=x")
        self.error = SimpleNamespace(error=code)


def _proxy_error(code: int) -> BleakError:
    """Build the error bleak_esphome raises: a plain BleakError caused by the API error."""
    cause = BluetoothGATTAPIError(code)
    err = BleakError(str(cause))
    err.__cause__ = cause
    return err


class FakeClient:
    def __init__(self, failures: list[BaseException | None], *, pair_error: BaseException | None = None) -> None:
        self._failures = failures
        self._pair_error = pair_error
        self.writes = 0
        self.pairs = 0
        self.is_connected = True
        self.services = {
            BluetoothConnection.BTM_SERVICE_UUID: SimpleNamespace(get_characteristic=lambda _uuid: object())
        }

    async def write_gatt_char(self, _characteristic: Any, _data: bytes) -> None:
        self.writes += 1
        failure = self._failures.pop(0) if self._failures else None
        await asyncio.sleep(0)
        if failure is not None:
            raise failure

    async def pair(self) -> None:
        self.pairs += 1
        if self._pair_error is not None:
            raise self._pair_error


def _connection(client: FakeClient) -> BluetoothConnection:
    connection = BluetoothConnection(ADDRESS)
    connection._bleak_client = client
    connection._ble_to_radio = object()  # type: ignore[assignment]
    return connection


async def test_a_radio_that_accepts_the_write_is_never_asked_to_pair() -> None:
    client = FakeClient([None])
    connection = _connection(client)

    assert await connection._send_packet(b"") is True

    assert client.pairs == 0


async def test_connecting_does_not_pair() -> None:
    client = FakeClient([])

    async def establish(**_kwargs: Any) -> FakeClient:
        return client

    connection = BluetoothConnection(ADDRESS, ble_device_provider=object)
    with patch.object(bluetooth, "establish_connection", establish):
        await connection.connect()

    assert client.pairs == 0


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(_proxy_error(INSUFFICIENT_AUTHENTICATION), id="esphome-proxy-status-5"),
        pytest.param(_proxy_error(INSUFFICIENT_ENCRYPTION), id="esphome-proxy-status-15"),
        pytest.param(BleakGATTProtocolError(INSUFFICIENT_AUTHENTICATION), id="bleak-protocol-5"),
        pytest.param(BleakGATTProtocolError(INSUFFICIENT_ENCRYPTION), id="bleak-protocol-15"),
        pytest.param(BleakDBusError("org.bluez.Error.NotPermitted", ["Not paired"]), id="bluez-not-paired"),
    ],
)
async def test_an_authentication_error_pairs_once_and_retries_the_write(error: BleakError) -> None:
    client = FakeClient([error, None])
    connection = _connection(client)

    assert await connection._send_packet(b"") is True

    assert client.pairs == 1
    assert client.writes == 2


async def test_concurrent_operations_failing_together_pair_once() -> None:
    client = FakeClient([])
    connection = _connection(client)
    calls: dict[str, int] = {"a": 0, "b": 0}

    def operation(name: str) -> Any:
        async def run() -> str:
            calls[name] += 1
            if calls[name] == 1:
                raise _proxy_error(INSUFFICIENT_AUTHENTICATION)
            return name

        return run

    results = await asyncio.gather(connection._gatt(operation("a")), connection._gatt(operation("b")))

    assert results == ["a", "b"]
    assert client.pairs == 1
    assert calls == {"a": 2, "b": 2}


async def test_a_failed_pairing_raises_a_pairing_error_and_does_not_retry() -> None:
    client = FakeClient([_proxy_error(INSUFFICIENT_AUTHENTICATION)], pair_error=BleakError("Pairing failed"))
    connection = _connection(client)

    with pytest.raises(bluetooth.BluetoothPairingRequiredError, match="pairing"):
        await connection._send_packet(b"")

    assert client.pairs == 1
    assert client.writes == 1


async def test_a_pairing_that_is_not_a_bleak_error_is_also_reported_as_pairing() -> None:
    client = FakeClient([_proxy_error(INSUFFICIENT_AUTHENTICATION)], pair_error=NotImplementedError("old proxy"))
    connection = _connection(client)

    with pytest.raises(bluetooth.BluetoothPairingRequiredError):
        await connection._send_packet(b"")


async def test_an_authentication_error_after_pairing_raises_without_pairing_again() -> None:
    auth = _proxy_error(INSUFFICIENT_AUTHENTICATION)
    client = FakeClient([auth, auth, auth, auth])
    connection = _connection(client)

    with pytest.raises(bluetooth.BluetoothPairingRequiredError):
        await connection._send_packet(b"")
    with pytest.raises(bluetooth.BluetoothPairingRequiredError):
        await connection._send_packet(b"")

    assert client.pairs == 1
    assert client.writes == 4


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(BleakGATTProtocolError(UNLIKELY_ERROR), id="unlikely-error-0x0e"),
        pytest.param(BleakGATTProtocolError(INSUFFICIENT_AUTHORIZATION), id="insufficient-authorization"),
        pytest.param(_proxy_error(0x03), id="esphome-write-not-permitted"),
        pytest.param(BleakDBusError("org.bluez.Error.NotPermitted", ["Write not permitted"]), id="bluez-write-denied"),
        pytest.param(BleakError("Disconnected"), id="disconnected"),
    ],
)
async def test_other_errors_are_not_treated_as_authentication_errors(error: BleakError) -> None:
    client = FakeClient([error])
    connection = _connection(client)

    assert await connection._send_packet(b"") is False

    assert client.pairs == 0
    assert client.writes == 1


async def test_pairing_that_drops_the_link_reconnects_before_the_retry() -> None:
    client = FakeClient([_proxy_error(INSUFFICIENT_AUTHENTICATION), None])

    async def pair_and_drop() -> None:
        client.pairs += 1
        client.is_connected = False

    client.pair = pair_and_drop  # type: ignore[method-assign]
    reopened = FakeClient([None])
    connection = _connection(client)

    async def reopen() -> None:
        connection._bleak_client = reopened

    with patch.object(connection, "_open_client", reopen):
        assert await connection._send_packet(b"") is True

    assert client.pairs == 1
    assert reopened.writes == 1
