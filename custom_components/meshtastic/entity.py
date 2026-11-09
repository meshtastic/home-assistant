# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import typing
from abc import ABC, abstractmethod
from enum import StrEnum

import voluptuous as vol
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, STATE_ATTRIBUTE_CHANNEL_INDEX, STATE_ATTRIBUTE_CHANNEL_NODE
from .coordinator import MeshtasticDataUpdateCoordinator

if typing.TYPE_CHECKING:
    from homeassistant.helpers.entity import EntityDescription
    from homeassistant.helpers.typing import UndefinedType


class MeshtasticDeviceClass(StrEnum):
    GATEWAY = "gateway"
    CHANNEL = "channel"
    MESSAGES = "messages"


class MeshtasticEntity(Entity):
    _attr_device_class: MeshtasticDeviceClass

    def __init__(
        self,
        config_entry_id: str,
        node: int,
        meshtastic_class: MeshtasticDeviceClass,
        meshtastic_id: str | int | None = None,
    ) -> None:
        super().__init__()
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


def _gateway_short_prefix(short_name: str, long_name: str) -> str:
    ascii_only = short_name.encode("ascii", "ignore")
    if len(ascii_only) == 0 or len(ascii_only) == 1:
        return long_name[0:3].lower()

    return ascii_only.decode("ascii", "ignore").lower()


class GatewayEntity(MeshtasticEntity):
    _attr_icon = "mdi:radio-handheld"

    def __init__(  # noqa: PLR0913
        self,
        config_entry_id: str,
        node: int,
        long_name: str,
        short_name: str,
        local_config: dict,
        module_config: dict,
    ) -> None:
        super().__init__(config_entry_id, node, MeshtasticDeviceClass.GATEWAY, None)
        self._local_config = local_config
        self._module_config = module_config
        self._long_name = long_name
        self._short_name = short_name

        self._attr_name = None
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
        name = _gateway_short_prefix(self._short_name, self._long_name)
        return f"{self.device_class} {name}"


class GatewayChannelEntity(MeshtasticEntity):
    _attr_icon = "mdi:forum"

    @staticmethod
    def build_unique_id(
        config_entry_id: str,
        gateway_node: int,
        index: int,
        device_class: MeshtasticDeviceClass = MeshtasticDeviceClass.CHANNEL,
    ) -> str:
        return f"{config_entry_id}_{device_class}_{gateway_node}_{index}"

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
        has_logbook: bool = True,  # noqa: FBT001, FBT002
    ) -> None:
        super().__init__(config_entry_id, gateway_node, MeshtasticDeviceClass.CHANNEL, index)

        self._index = index
        self._attr_messages = []
        self._settings = settings
        self._gateway_suggested_id = gateway_entity.suggested_object_id
        self._attr_unique_id = self.build_unique_id(config_entry_id, gateway_node, index)
        self._attr_translation_key = "channel"

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

        if has_logbook:
            self._attr_state = "logging"
        else:
            self._attr_state = "logbook_missing"

        self._attr_should_poll = False
        self._attr_extra_state_attributes = {
            STATE_ATTRIBUTE_CHANNEL_INDEX: index,
            STATE_ATTRIBUTE_CHANNEL_NODE: gateway_node,
            "primary": primary,
            "secondary": secondary,
            "psk": self._settings["psk"],
            "uplink_enabled": self._settings["uplinkEnabled"],
            "downlink_enabled": self._settings["downlinkEnabled"],
        }

    @property
    def suggested_object_id(self) -> str | None:
        return f"{self._gateway_suggested_id} {self.name}"


class GatewayDirectMessageEntity(MeshtasticEntity):
    _attr_icon = "mdi:message-lock"
    _attr_name = "Direct Messages"
    _attr_translation_key = "direct_messages"
    _attr_has_entity_name = True
    _attr_should_poll = False

    @staticmethod
    def build_unique_id(
        config_entry_id: str, gateway_node: int, device_class: MeshtasticDeviceClass = MeshtasticDeviceClass.MESSAGES
    ) -> str:
        return f"{config_entry_id}_{device_class}_{gateway_node}_dm"

    def __init__(
        self,
        config_entry_id: str,
        gateway_node: int,
        gateway_entity: GatewayEntity,
        has_logbook: bool = True,  # noqa: FBT001, FBT002
    ) -> None:
        super().__init__(config_entry_id, gateway_node, MeshtasticDeviceClass.MESSAGES, "dm")
        self._gateway_suggested_id = gateway_entity.suggested_object_id
        self._attr_unique_id = self.build_unique_id(config_entry_id, gateway_node)

        if has_logbook:
            self._attr_state = "logging"
        else:
            self._attr_state = "logbook_missing"

    @property
    def suggested_object_id(self) -> str | None:
        return f"{self._gateway_suggested_id} DM"

    @property
    def name(self) -> str | UndefinedType | None:
        return super().name

    def _name_internal(
        self, device_class_name: str | None, platform_translations: dict[str, str]
    ) -> str | UndefinedType | None:
        return super()._name_internal(device_class_name, platform_translations)


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

    @property
    def name(self) -> str | UndefinedType | None:
        return super().name

    def _name_internal(
        self, device_class_name: str | None, platform_translations: dict[str, str]
    ) -> str | UndefinedType | None:
        return super()._name_internal(device_class_name, platform_translations)


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

        gateway_short_name = gateway.get("user", {}).get("shortName", "")
        gateway_long_name = gateway.get("user", {}).get("longName", "")
        gateway_short_prefix = _gateway_short_prefix(gateway_short_name, gateway_long_name)
        gateway_prefix = f"{gateway_short_prefix}_"

        self.entity_id = f"{platform}.{DOMAIN}_{gateway_prefix}{self.node_id}_{self.entity_description.key}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(self.node_id))},
        )
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{platform}_{self.node_id}_{self.entity_description.key}"
        )
        self._attr_has_entity_name = True

    @property
    def node_id(self) -> int:
        return self._node_id

    @property
    def available(self) -> bool:
        return super().available and self.node_id in self.coordinator.data
