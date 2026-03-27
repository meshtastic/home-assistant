# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
# SPDX-FileCopyrightText: 2025 Hendrik @novag
#
# SPDX-License-Identifier: MIT

import asyncio
import struct
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import bleak
from bleak import BaseBleakClient, BleakClient, BleakGATTCharacteristic
from bleak_retry_connector import establish_connection
from google.protobuf import message

from ..protobuf import mesh_pb2  # noqa: TID252
from . import ClientApiConnection
from .errors import (
    ClientApiConnectionError,
    ClientApiNotConnectedError,
)

if TYPE_CHECKING:
    from bleak.backends.service import BleakGATTService


class BluetoothConnectionError(ClientApiConnectionError):
    pass


class BluetoothConnectionServiceNotFoundError:
    def __init__(self) -> None:
        super().__init__("Bluetooth meshtastic service not found")


class BluetoothConnection(ClientApiConnection):
    BTM_SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
    BTM_CHARACTERISTIC_FROM_RADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
    BTM_CHARACTERISTIC_TO_RADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
    BTM_CHARACTERISTIC_FROM_NUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"
    BTM_CHARACTERISTIC_LOG_UUID = "5a3d6e49-06e6-4423-9944-e9de8cdf9547"

    def __init__(
        self,
        ble_address: str,
        ble_device: Any | None = None,
        bleak_client_backend: type[BaseBleakClient] | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        super().__init__()
        self._ble_address = ble_address
        self._ble_device = ble_device
        self._bleak_client_backend = bleak_client_backend
        self._connect_timeout = connect_timeout
        self._ble_meshtastic_service: BleakGATTService | None = None
        self._ble_from_radio: BleakGATTCharacteristic | None
        self._ble_to_radio: BleakGATTCharacteristic | None
        self._ble_from_num: BleakGATTCharacteristic | None
        self._ble_log: BleakGATTCharacteristic | None
        self._write_lock = asyncio.Lock()
        self._last_packet_number = None
        self._force_read_event = asyncio.Event()

    async def _connect(self) -> None:
        if self._ble_device is not None:
            self._bleak_client = await establish_connection(
                client_class=BleakClient,
                device=self._ble_device,
                name=self._ble_address,
                max_attempts=3,
            )
        else:
            self._bleak_client = BleakClient(
                self._ble_address, timeout=self._connect_timeout, backend=self._bleak_client_backend
            )
            await self._bleak_client.connect()

        self._ble_meshtastic_service = self._bleak_client.services[BluetoothConnection.BTM_SERVICE_UUID]

        if self._ble_meshtastic_service is None:
            raise BluetoothConnectionServiceNotFoundError

        self._ble_from_radio = self._ble_meshtastic_service.get_characteristic(
            BluetoothConnection.BTM_CHARACTERISTIC_FROM_RADIO_UUID
        )
        self._ble_to_radio = self._ble_meshtastic_service.get_characteristic(
            BluetoothConnection.BTM_CHARACTERISTIC_TO_RADIO_UUID
        )
        self._ble_from_num = self._ble_meshtastic_service.get_characteristic(
            BluetoothConnection.BTM_CHARACTERISTIC_FROM_NUM_UUID
        )
        self._ble_log = self._ble_meshtastic_service.get_characteristic(BluetoothConnection.BTM_CHARACTERISTIC_LOG_UUID)

    async def _disconnect(self) -> None:
        try:
            await self._bleak_client.disconnect()
        except:  # noqa: E722
            self._logger.debug("Disconnecting failed", exc_info=True)

    @property
    def is_connected(self) -> bool:
        return self._bleak_client.is_connected

    async def _handle_notify_wait(  # noqa: PLR0913
        self,
        packet_num_queue: asyncio.Queue,
        force_read_event: asyncio.Event,
        notify_timeout_duration: int,
        notify_timeout_count: int,
        max_notify_timeouts_before_restart: int,
        restart_notify_func: callable,
    ) -> tuple[bool, int]:
        """Wait for packet notification or force read event."""
        wait_notify = asyncio.create_task(packet_num_queue.get(), name="wait_notify")
        wait_force_read = asyncio.create_task(force_read_event.wait(), name="wait_force_read")

        done, pending = await asyncio.wait(
            {wait_notify, wait_force_read},
            timeout=notify_timeout_duration,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Ensure pending tasks are cancelled before proceeding
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        continue_active_read = False
        if wait_force_read in done:
            self._logger.debug("Force read event received. Continuing loop for active read.")
            force_read_event.clear()
            notify_timeout_count = 0  # Reset timeout counter
            continue_active_read = True
        elif wait_notify in done:
            self._logger.debug("Packet notification received. Will attempt read.")
            _ = wait_notify.result()
            notify_timeout_count = 0  # Reset timeout counter
        else:  # Timeout occurred
            notify_timeout_count += 1
            if notify_timeout_count > max_notify_timeouts_before_restart:
                self._logger.debug(
                    "No bluetooth notification for %d times after %ds timeout, restarting notifications",
                    notify_timeout_count,
                    max_notify_timeouts_before_restart,
                )
                notify_timeout_count = 0
                await restart_notify_func()
            # continue with active read
            continue_active_read = True

        return continue_active_read, notify_timeout_count

    async def _packet_stream(self) -> AsyncGenerator[mesh_pb2.FromRadio, Any]:  # noqa: PLR0915
        if not self.is_connected:
            return
        packet_num_queue = asyncio.Queue()
        force_read_event = self._force_read_event

        def notification_handler(_: BleakGATTCharacteristic, data: bytearray) -> None:
            nums = struct.unpack("<I", data)
            num = nums[0]

            if num != self._last_packet_number:
                self._last_packet_number = num
                self._logger.debug("New packet available: %s", num)
                packet_num_queue.put_nowait(num)
            else:
                self._logger.debug("Duplicate packet notification: %s", num)

        try:

            async def start_notify() -> None:
                await asyncio.wait_for(
                    self._bleak_client.start_notify(self._ble_from_num, notification_handler), timeout=30
                )

            async def stop_notify() -> None:
                await asyncio.wait_for(self._bleak_client.stop_notify(self._ble_from_num), timeout=30)

            async def restart_notify() -> None:
                try:
                    with suppress(Exception):
                        await stop_notify()
                    await start_notify()
                except:  # noqa: E722
                    self._logger.debug("Restart notify failed", exc_info=True)

            await start_notify()

            notify_timeout_count = 0
            notify_timeout_duration = 300
            max_notify_timeouts_before_restart = 2
            while True:
                packet = await self._bleak_client.read_gatt_char(self._ble_from_radio)
                if not isinstance(packet, bytes):
                    packet = bytes(packet)
                if packet == b"":
                    # no more packets available, waiting for notification or force_read event.
                    # if we do not receive bluetooth notifications for an extended period of time, this could be an
                    # indication of issue with bluetooth stack, so we try to do an active read. This will either trigger
                    # an error or help resume sending of data by the firmware. If this happens too often, we try to
                    # re-start notifications.
                    continue_active_read, notify_timeout_count = await self._handle_notify_wait(
                        packet_num_queue,
                        force_read_event,
                        notify_timeout_duration,
                        notify_timeout_count,
                        max_notify_timeouts_before_restart,
                        restart_notify,
                    )
                    if continue_active_read:
                        continue

                elif notify_timeout_count > 0:
                    self._logger.debug(
                        "Read returned packet after ble notify timeout, maybe notifications from device have stopped"
                    )

                from_radio = mesh_pb2.FromRadio()
                try:
                    from_radio.ParseFromString(packet)
                    self._logger.debug("Parsed packet: %s", self._protobuf_log(from_radio))
                    yield from_radio
                except message.DecodeError:
                    self._logger.warning("Error while parsing FromRadio bytes %s", packet, exc_info=True)
        except bleak.BleakError as e:
            raise BluetoothConnectionError from e
        finally:
            with suppress(bleak.BleakError):
                await self._bleak_client.stop_notify(self._ble_from_num)

    async def _send_packet(self, data: bytes) -> bool:
        if not self._bleak_client.is_connected:
            raise ClientApiNotConnectedError

        # Check if this packet requires a forced read
        try:
            to_radio = mesh_pb2.ToRadio()
            to_radio.ParseFromString(data)
            if to_radio.HasField("want_config_id"):
                self._logger.debug("want_config_id detected, setting force read event.")
                self._force_read_event.set()
        except message.DecodeError:
            self._logger.warning("Could not parse ToRadio packet in _send_packet to check for want_config_id.")

        async with self._write_lock:
            try:
                await self._bleak_client.write_gatt_char(self._ble_to_radio, data)
            except bleak.BleakError:
                self._logger.debug("Failed to send data", exc_info=True)
                return False
            else:
                return True
