from __future__ import annotations

import datetime
import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config import callback
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfTime,
)

from . import LOGGER, helpers
from .entity import MeshtasticNodeEntity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.typing import StateType

    from .coordinator import MeshtasticDataUpdateCoordinator
    from .data import MeshtasticConfigEntry, MeshtasticData


def _build_sensors(nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData) -> Iterable[MeshtasticSensor]:
    entities = []
    entities += _build_node_sensors(nodes, runtime_data)
    entities += _build_device_sensors(nodes, runtime_data)
    entities += _build_local_stats_sensors(nodes, runtime_data)
    entities += _build_power_metrics_sensors(nodes, runtime_data)
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    await helpers.setup_platform_entry(hass, entry, async_add_entities, _build_sensors)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
) -> bool:
    return await helpers.async_unload_entry(hass, entry)


@dataclass(kw_only=True)
class MeshtasticSensorEntityDescription(SensorEntityDescription):
    exists_fn: Callable[[MeshtasticSensor], bool] = lambda _: True
    value_fn: Callable[[MeshtasticSensor], StateType]


class MeshtasticSensor(MeshtasticNodeEntity, SensorEntity):
    entity_description: MeshtasticSensorEntityDescription

    def __init__(
        self,
        coordinator: MeshtasticDataUpdateCoordinator,
        entity_description: MeshtasticSensorEntityDescription,
        gateway: typing.Mapping[str, typing.Any],
        node_id: int,
    ) -> None:
        super().__init__(coordinator, gateway, node_id, SENSOR_DOMAIN, entity_description)

    @callback
    def _async_update_attrs(self) -> None:
        LOGGER.debug("Updating sensor attributes: %s", self)
        self._attr_native_value = self.entity_description.value_fn(self)
        self._attr_available = self._attr_native_value is not None


def _build_node_sensors(
    nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData
) -> Iterable[MeshtasticSensor]:
    entities = []
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()

    def last_heard(device: MeshtasticNodeEntity) -> datetime.datetime | None:
        last_heard_int = device.coordinator.data[device.node_id].get("lastHeard")
        if last_heard_int is None:
            return None
        return datetime.datetime.fromtimestamp(last_heard_int, tz=datetime.UTC)

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="node_last_heard",
                name="Last Heard",
                icon="mdi:timeline-clock",
                device_class=SensorDeviceClass.TIMESTAMP,
                value_fn=last_heard,
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
        if node_id != runtime_data.gateway_node["num"]
    ]

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="node_snr",
                name="Signal to Noise Ratio",
                icon="mdi:signal",
                native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS,
                device_class=SensorDeviceClass.SIGNAL_STRENGTH,
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=lambda device: device.coordinator.data[device.node_id].get("snr", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
        if node_id != runtime_data.gateway_node["num"]
    ]

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="node_hops_away",
                name="Hops away",
                icon="mdi:rabbit",
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=lambda device: device.coordinator.data[device.node_id].get("hopsAway", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
        if node_id != runtime_data.gateway_node["num"]
    ]

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="node_role",
                name="Role",
                icon="mdi:card-account-details",
                value_fn=lambda device: device.coordinator.data[device.node_id].get("user", {}).get("role", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
        if node_id != runtime_data.gateway_node["num"]
    ]
    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="node_short_name",
                name="Short Name",
                icon="mdi:card-account-details",
                value_fn=lambda device: device.coordinator.data[device.node_id].get("user", {}).get("shortName", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="node_long_name",
                name="Long Name",
                icon="mdi:card-account-details",
                value_fn=lambda device: device.coordinator.data[device.node_id].get("user", {}).get("longName", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]

    return entities


def _build_device_sensors(
    nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData
) -> Iterable[MeshtasticSensor]:
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()
    entities = []

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="device_uptime",
                name="Uptime",
                icon="mdi:progress-clock",
                native_unit_of_measurement=UnitOfTime.SECONDS,
                device_class=SensorDeviceClass.DURATION,
                state_class=SensorStateClass.TOTAL_INCREASING,
                value_fn=lambda device: device.coordinator.data[device.node_id]
                .get("deviceMetrics", {})
                .get("uptimeSeconds", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]

    def battery_level(device: MeshtasticSensor) -> int | None:
        level = device.coordinator.data[device.node_id].get("deviceMetrics", {}).get("batteryLevel", None)
        if level is not None:
            return max(0, min(100, level))
        return level

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="device_battery_level",
                name="Battery Level",
                icon="mdi:battery",
                native_unit_of_measurement=PERCENTAGE,
                device_class=SensorDeviceClass.BATTERY,
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=battery_level,
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="device_voltage",
                name="Voltage",
                icon="mdi:lightning-bolt",
                native_unit_of_measurement=UnitOfElectricPotential.VOLT,
                device_class=SensorDeviceClass.VOLTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=lambda device: device.coordinator.data[device.node_id]
                .get("deviceMetrics", {})
                .get("voltage", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]
    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="device_channel_utilization",
                name="Channel Utilization",
                icon="mdi:signal-distance-variant",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=lambda device: device.coordinator.data[device.node_id]
                .get("deviceMetrics", {})
                .get("channelUtilization", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]
    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="device_airtime",
                name="Airtime",
                icon="mdi:timer",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=lambda device: device.coordinator.data[device.node_id]
                .get("deviceMetrics", {})
                .get("airUtilTx", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]

    return entities


def _build_local_stats_sensors(
    nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData
) -> Iterable[MeshtasticSensor]:
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()
    nodes_with_loca_stats = {node_id: node_info for node_id, node_info in nodes.items() if "localStats" in node_info}

    entities = []
    try:
        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_packets_tx",
                    name="Packets sent",
                    icon="mdi:call-made",
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    value_fn=lambda device: device.coordinator.data[device.node_id]
                    .get("localStats", {})
                    .get("numPacketsTx", None),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_loca_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_packets_rx",
                    name="Packets received",
                    icon="mdi:call-received",
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    value_fn=lambda device: device.coordinator.data[device.node_id]
                    .get("localStats", {})
                    .get("numPacketsRx", None),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_loca_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_packets_rx_bad",
                    name="Malformed Packets received",
                    icon="mdi:call-missed",
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    value_fn=lambda device: device.coordinator.data[device.node_id]
                    .get("localStats", {})
                    .get("numPacketsRxBad", None),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_loca_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_packets_rx_duplicate",
                    name="Duplicate Packets received",
                    icon="mdi:call-split",
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    value_fn=lambda device: device.coordinator.data[device.node_id]
                    .get("localStats", {})
                    .get("numRxDupe", None),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_loca_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_packets_tx_relayed",
                    name="Packets relayed",
                    icon="mdi:call-missed",
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    value_fn=lambda device: device.coordinator.data[device.node_id]
                    .get("localStats", {})
                    .get("numTxRelay", None),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_loca_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_packets_tx_relay_cancelled",
                    name="Packets relay canceled",
                    icon="mdi:call-missed",
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    value_fn=lambda device: device.coordinator.data[device.node_id]
                    .get("localStats", {})
                    .get("numTxRelayCanceled", None),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_loca_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_nodes_online",
                    name="Online Nodes",
                    icon="mdi:radio-handheld",
                    state_class=SensorStateClass.TOTAL,
                    value_fn=lambda device: device.coordinator.data[device.node_id]
                    .get("localStats", {})
                    .get("numOnlineNodes", None),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_loca_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_nodes_total",
                    name="Total Nodes",
                    icon="mdi:radio-handheld",
                    state_class=SensorStateClass.TOTAL,
                    value_fn=lambda device: device.coordinator.data[device.node_id]
                    .get("localStats", {})
                    .get("numTotalNodes", None),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_loca_stats.items()
        ]
    except:  # noqa: E722
        LOGGER.warning("Failed to create local stats entities", exc_info=True)

    return entities


def _build_power_metrics_sensors(
    nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData
) -> Iterable[MeshtasticSensor]:
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()
    nodes_with_power_metrics = {
        node_id: node_info for node_id, node_info in nodes.items() if "powerMetrics" in node_info
    }
    if not nodes_with_power_metrics:
        return []

    entities = []
    try:
        for node_id, node_info in nodes_with_power_metrics.items():
            power_metrics = node_info["powerMetrics"]
            for channel in range(1, 3):
                voltage_key = f"ch{channel}Voltage"
                current_key = f"ch{channel}Current"

                def power_metrics_value_fn(key: str) -> Callable[[MeshtasticSensor], str | None]:
                    return lambda device: device.coordinator.data[device.node_id].get("powerMetrics", {}).get(key, None)

                if voltage_key in power_metrics:
                    entities.append(
                        MeshtasticSensor(
                            coordinator=coordinator,
                            entity_description=MeshtasticSensorEntityDescription(
                                key=f"power_ch{channel}_voltage",
                                name=f"Channel {channel} Voltage",
                                icon="mdi:lightning-bolt",
                                native_unit_of_measurement=UnitOfElectricPotential.VOLT,
                                device_class=SensorDeviceClass.VOLTAGE,
                                state_class=SensorStateClass.MEASUREMENT,
                                value_fn=power_metrics_value_fn(voltage_key),
                            ),
                            gateway=gateway,
                            node_id=node_id,
                        )
                    )
                if current_key in power_metrics:
                    entities.append(
                        MeshtasticSensor(
                            coordinator=coordinator,
                            entity_description=MeshtasticSensorEntityDescription(
                                key=f"power_ch{channel}_current",
                                name=f"Channel {channel} Current",
                                icon="mdi:current-dc",
                                native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
                                device_class=SensorDeviceClass.CURRENT,
                                state_class=SensorStateClass.MEASUREMENT,
                                value_fn=power_metrics_value_fn(current_key),
                            ),
                            gateway=gateway,
                            node_id=node_id,
                        )
                    )

    except:  # noqa: E722
        LOGGER.warning("Failed to create power metrics entities", exc_info=True)

    return entities
