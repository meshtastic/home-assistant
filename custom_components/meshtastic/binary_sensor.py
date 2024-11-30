from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
)
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)

from . import helpers
from .entity import MeshtasticNodeEntity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import MeshtasticDataUpdateCoordinator
    from .data import MeshtasticConfigEntry, MeshtasticData


def _build_binary_sensors(
    nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData
) -> Iterable[MeshtasticBinarySensor]:
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()
    entities = []
    entities += [
        MeshtasticBinarySensor(
            coordinator=coordinator,
            entity_description=MeshtasticBinarySensorEntityDescription(
                key="device_powered",
                name="Powered",
                icon="mdi:power-plug",
                device_class=BinarySensorDeviceClass.POWER,
                exists_fn=lambda device: device.coordinator.data[device.node_id]
                .get("deviceMetrics", {})
                .get("batteryLevel", None)
                is not None,
                value_fn=lambda device: device.coordinator.data[device.node_id]
                .get("deviceMetrics", {})
                .get("batteryLevel", 0)
                > 100,  # noqa: PLR2004
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]

    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary_sensor platform."""
    await helpers.setup_platform_entry(hass, entry, async_add_entities, _build_binary_sensors)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
) -> bool:
    return await helpers.async_unload_entry(hass, entry)


@dataclass(kw_only=True)
class MeshtasticBinarySensorEntityDescription(BinarySensorEntityDescription):
    value_fn: Callable[[MeshtasticBinarySensor], bool]
    exists_fn: Callable[[MeshtasticBinarySensor], bool]


class MeshtasticBinarySensor(MeshtasticNodeEntity, BinarySensorEntity):
    entity_description: MeshtasticBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: MeshtasticDataUpdateCoordinator,
        entity_description: MeshtasticBinarySensorEntityDescription,
        gateway: typing.Mapping[str, typing.Any],
        node_id: int,
    ) -> None:
        """Initialize the binary_sensor class."""
        super().__init__(coordinator, gateway, node_id, BINARY_SENSOR_DOMAIN, entity_description)

    def _async_update_attrs(self) -> None:
        self._attr_available = self.entity_description.exists_fn(self)
        self._attr_is_on = self.entity_description.value_fn(self)
