from __future__ import annotations

import typing
from abc import ABC, abstractmethod
from enum import StrEnum

import voluptuous as vol
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MeshtasticDataUpdateCoordinator

if typing.TYPE_CHECKING:
    from homeassistant.helpers.entity import EntityDescription


class MeshtasticDeviceClass(StrEnum):
    GATEWAY = "gateway"
    CHANNEL = "channel"


class MeshtasticEntity(Entity):
    _attr_device_class: MeshtasticDeviceClass

    def __init__(
        self,
        config_entry_id: str,
        node: int,
        meshtastic_class: MeshtasticDeviceClass,
        meshtastic_id: str | int | None = None,
    ) -> None:
        self._attr_meshtastic_class = meshtastic_class
        self._attr_unique_id = f"{config_entry_id}_{meshtastic_class}_{node}"
        if meshtastic_id is not None:
            self._attr_unique_id += f"_{meshtastic_id}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, str(node))})
        self._attr_device_class = meshtastic_class

    @property
    def suggested_object_id(self) -> str | None:
        suggested_id = super().suggested_object_id
        if suggested_id:
            return f"{self._attr_device_class} {suggested_id}"
        return f"{self._attr_device_class}"


MESHTASTIC_CLASS_SCHEMA = vol.All(vol.Lower, vol.Coerce(MeshtasticDeviceClass))


class GatewayEntity(MeshtasticEntity):
    _attr_icon = "mdi:radio-handheld"

    def __init__(  # noqa: PLR0913
        self,
        config_entry_id: str,
        node: int,
        long_name: str,  # noqa: ARG002
        short_name: str,
        local_config: dict,
        module_config: dict,
    ) -> None:
        super().__init__(config_entry_id, node, MeshtasticDeviceClass.GATEWAY, None)
        self._local_config = local_config
        self._module_config = module_config
        self._short_name = short_name

        self._attr_name = "Node"
        self._attr_has_entity_name = True

        def flatten(
            dictionary: dict[str, typing.Any], parent_key: str = "", separator: str = "_"
        ) -> dict[str, typing.Any]:
            items = []
            for key, value in dictionary.items():
                new_key = parent_key + separator + key if parent_key else key
                if isinstance(value, typing.MutableMapping):
                    items.extend(flatten(value, new_key, separator=separator).items())
                else:
                    items.append((new_key, value))
            return dict(items)

        attributes = {"config": local_config, "module": module_config}

        self._attr_extra_state_attributes = flatten(attributes)
        if self._attr_available:
            self._attr_state = "Connected"
        else:
            self._attr_state = "Disconnected"

    @property
    def suggested_object_id(self) -> str | None:
        return f"{self.device_class} {self._short_name}"


class GatewayChannelEntity(MeshtasticEntity):
    _attr_icon = "mdi:forum"

    def __init__(  # noqa: PLR0913
        self,
        config_entry_id: str,
        gateway_node: int,
        gateway_entity: GatewayEntity,
        index: int,
        name: str,
        settings: dict,
        primary: bool = False,  # noqa: FBT001, FBT002
        secondary: bool = False,  # noqa: FBT001, FBT002
    ) -> None:
        super().__init__(config_entry_id, gateway_node, MeshtasticDeviceClass.CHANNEL, index)

        self._index = index
        self._attr_messages = []
        self._settings = settings
        self._gateway_suggested_id = gateway_entity.suggested_object_id

        self._attr_unique_id = f"{config_entry_id}_{self.device_class}_{gateway_node}_{index}"

        if name:
            self._attr_has_entity_name = True
            self._attr_name = name
        elif primary:
            self._attr_has_entity_name = True
            self._attr_name = "Primary"
        elif secondary:
            self._attr_has_entity_name = True
            self._attr_name = "Secondary"

        self._attr_name = "Channel " + self._attr_name

        self._attr_state = f"Channel #{index}"
        self._attr_should_poll = False
        self._attr_extra_state_attributes = {
            "node": gateway_node,
            "primary": primary,
            "secondary": secondary,
            "psk": self._settings["psk"],
            "uplink_enabled": self._settings["uplinkEnabled"],
            "downlink_enabled": self._settings["downlinkEnabled"],
        }

    @property
    def suggested_object_id(self) -> str | None:
        return f"{self._gateway_suggested_id} {self.name}"


class MeshtasticCoordinatorEntity(CoordinatorEntity[MeshtasticDataUpdateCoordinator]):
    def __init__(self, coordinator: MeshtasticDataUpdateCoordinator) -> None:
        super().__init__(coordinator)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._async_update_attrs()
        super()._handle_coordinator_update()

    def update(self) -> None:
        self._async_update_attrs()
        self.async_write_ha_state()

    @abstractmethod
    def _async_update_attrs(self) -> None:
        pass


class MeshtasticNodeEntity(MeshtasticCoordinatorEntity, ABC):
    def __init__(
        self,
        coordinator: MeshtasticDataUpdateCoordinator,
        gateway: typing.Mapping[str, typing.Any],
        node_id: int,
        platform: str,
        entity_description: EntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        self.entity_description = entity_description

        gateway_short_name = gateway.get("user", {}).get("shortName", None)
        gateway_prefix = "" if gateway_short_name is None else f"{gateway_short_name.lower()}_"

        self.entity_id = f"{platform}.{DOMAIN}_{gateway_prefix}{self.node_id}_{self.entity_description.key}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(self.node_id))},
        )
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{platform}_" f"{self.node_id}_{self.entity_description.key}"
        )

    @property
    def node_id(self) -> int:
        return self._node_id

    @property
    def available(self) -> bool:
        return super().available and self.node_id in self.coordinator.data
