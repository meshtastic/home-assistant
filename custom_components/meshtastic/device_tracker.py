# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import typing
from types import MappingProxyType
from typing import TYPE_CHECKING

from homeassistant.components.device_tracker import DOMAIN as DEVICE_TRACKER_DOMAIN
from homeassistant.components.device_tracker import TrackerEntity, TrackerEntityDescription

from . import MeshtasticData, helpers
from .entity import MeshtasticNodeEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import MeshtasticDataUpdateCoordinator
    from .data import MeshtasticConfigEntry, MeshtasticData


def _build_device_trackers(
    nodes: typing.Mapping[int, typing.Mapping[str, typing.Any]], runtime_data: MeshtasticData
) -> typing.Iterable[MeshtasticDeviceTracker]:
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()
    nodes_with_position = {node_id: node_info for node_id, node_info in nodes.items() if "position" in node_info}

    return [
        MeshtasticDeviceTracker(
            coordinator=coordinator,
            entity_description=TrackerEntityDescription(key="node_position"),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes_with_position.items()
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    await helpers.setup_platform_entry(hass, entry, async_add_entities, _build_device_trackers)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
) -> bool:
    return await helpers.async_unload_entry(hass, entry)


_DOP_ATTRIBUTES = ("PDOP", "HDOP", "VDOP")


class MeshtasticDeviceTracker(MeshtasticNodeEntity, TrackerEntity):
    entity_description: TrackerEntityDescription

    # according to https://github.com/meshtastic/Meshtastic-Android/issues/893
    _precision_to_meters: typing.Mapping[int, int] = MappingProxyType(
        {
            2: 5976446,
            3: 2988223,
            4: 1494111,
            5: 747055,
            6: 373527,
            7: 186763,
            8: 93381,
            9: 46690,
            10: 23345,
            11: 11672,
            12: 5836,
            13: 2918,
            14: 1459,
            15: 729,
            16: 364,
            17: 182,
            18: 91,
            19: 45,
            20: 22,
            21: 11,
            22: 5,
            23: 2,
            24: 1,
        }
    )

    def __init__(
        self,
        coordinator: MeshtasticDataUpdateCoordinator,
        entity_description: TrackerEntityDescription,
        gateway: typing.Mapping[str, typing.Any],
        node_id: int,
    ) -> None:
        super().__init__(coordinator, gateway, node_id, DEVICE_TRACKER_DOMAIN, entity_description)
        self._attr_name = self.coordinator.data[self.node_id].get("user", {}).get("longName", None)
        self._attr_name = None
        self._attr_has_entity_name = True

    def _async_update_attrs(self) -> None:
        self._attr_available = (
            self.node_id in self.coordinator.data and "position" in self.coordinator.data[self.node_id]
        )
        if not self._attr_available:
            return

        position = self.coordinator.data[self.node_id].get("position", {})
        precision_bits = position.get("precisionBits", 0)

        self._attr_latitude = position.get("latitude", None)
        self._attr_longitude = position.get("longitude", None)
        self._attr_location_accuracy = self._precision_to_meters.get(precision_bits, 0)
        # PDOP/HDOP/VDOP are transmitted in 1/100 units, convert to actual DOP values
        self._attr_extra_state_attributes = {
            k: v / 100 if k in _DOP_ATTRIBUTES else v
            for k, v in position.items()
            if k in ["altitude", "groundSpeed", "groundTrack", "locationSource", "satsInView", *_DOP_ATTRIBUTES]
        }

    @property
    def battery_level(self) -> int | None:
        level = self.coordinator.data[self.node_id].get("deviceMetrics", {}).get("batteryLevel", None)
        if level is not None:
            return max(0, min(100, level))
        return level
