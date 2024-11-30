import asyncio
import struct
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import bleak
from bleak import BaseBleakClient, BleakClient, BleakGATTCharacteristic
from google.protobuf import message

from meshtastic.protobuf import mesh_pb2

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
        self, ble_address: str, bleak_client_backend: type[BaseBleakClient] | None = None, connect_timeout: float = 10.0
    ) -> None:
        super().__init__()
        self._ble_address = ble_address
        self._bleak_client_backend = bleak_client_backend
        self._connect_timeout = connect_timeout
        self._ble_meshtastic_service: BleakGATTService | None = None
        self._ble_from_radio: BleakGATTCharacteristic | None
        self._ble_to_radio: BleakGATTCharacteristic | None
        self._ble_from_num: BleakGATTCharacteristic | None
        self._ble_log: BleakGATTCharacteristic | None
        self._write_lock = asyncio.Lock()
        self._last_packet_number = None

    async def _connect(self) -> None:
        self._bleak_client = BleakClient(
            self._ble_address, timeout=self._connect_timeout, backend=self._bleak_client_backend
        )
        await self._bleak_client.connect()

        # attempt pairing, we don't know if it is required. Should not harm if
        # not needed. if pairing is required, external input is necessary as we are not
        # able to fully pair with bleak see https://github.com/hbldh/bleak/issues/1434.
        # possible workaround: https://technotes.kynetics.com/2018/pairing_agents_bluez/
        try:
            await self._bleak_client.pair()
        except:  # noqa: E722
            self._logger.debug("Pairing failed", exc_info=True)

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
        await self._bleak_client.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._bleak_client.is_connected

    async def _packet_stream(self) -> AsyncGenerator[mesh_pb2.FromRadio, Any]:
        if not self.is_connected:
            return
        packet_num_queue = asyncio.Queue()

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
            await self._bleak_client.start_notify(self._ble_from_num, notification_handler)
            while True:
                packet = await self._bleak_client.read_gatt_char(self._ble_from_radio)
                if not isinstance(packet, bytes):
                    packet = bytes(packet)
                if packet == b"":
                    # no more packets available, waiting for notification
                    await packet_num_queue.get()
                    continue

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

        async with self._write_lock:
            try:
                await self._bleak_client.write_gatt_char(self._ble_to_radio, data)
            except bleak.BleakError:
                self._logger.debug("Failed to send data", exc_info=True)
                return False
            else:
                return True
