# SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
#
# SPDX-License-Identifier: MIT

"""Chat: message history per gateway and a websocket API used by the chat panel."""

from __future__ import annotations

import asyncio
import base64
import binascii
import math
import secrets
import time
import uuid
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util import location as location_util
from homeassistant.util.hass_dict import HassKey

from .aiomeshtastic import MeshInterface
from .aiomeshtastic.interface import TelemetryType
from .api import (
    ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID,
    ATTR_EVENT_MESHTASTIC_API_DATA,
    EVENT_MESHTASTIC_API_TEXT_MESSAGE,
    MeshtasticApiClientError,
)
from .const import (
    CHAT_ACK_TIMEOUT,
    CHAT_CHANNEL_COUNT,
    CHAT_CHANNEL_KEY_DEFAULT,
    CHAT_CHANNEL_KEY_INPUT_MAX_LENGTH,
    CHAT_CHANNEL_KEY_KEEP,
    CHAT_CHANNEL_KEY_LENGTHS,
    CHAT_CHANNEL_KEY_NONE,
    CHAT_CHANNEL_KEY_RANDOM,
    CHAT_CHANNEL_KEY_RANDOM_128,
    CHAT_CHANNEL_NAME_INPUT_MAX_LENGTH,
    CHAT_CHANNEL_NAME_MAX_BYTES,
    CHAT_CHANNEL_ROLE_DISABLED,
    CHAT_CHANNEL_ROLE_PRIMARY,
    CHAT_CHANNEL_ROLE_SECONDARY,
    CHAT_DEDUPLICATE_WINDOW,
    CHAT_HISTORY_PAGE_SIZE,
    CHAT_HISTORY_PAGE_SIZE_MAX,
    CHAT_MAX_TEXT_BYTES,
    CHAT_POSITION_PRECISION_FULL,
    CHAT_REPLY_PREVIEW_CHARS,
    CHAT_STORAGE_SAVE_DELAY,
    CHAT_STORAGE_VERSION,
    CONF_OPTION_CHAT,
    CONF_OPTION_CHAT_CHANNEL_MANAGEMENT,
    CONF_OPTION_CHAT_CHANNEL_MANAGEMENT_ADMIN,
    CONF_OPTION_CHAT_CHANNEL_MANAGEMENT_ALL,
    CONF_OPTION_CHAT_CHANNEL_MANAGEMENT_DEFAULT,
    CONF_OPTION_CHAT_ENABLE,
    CONF_OPTION_CHAT_ENABLE_DEFAULT,
    CONF_OPTION_CHAT_HISTORY_SIZE,
    CONF_OPTION_CHAT_HISTORY_SIZE_DEFAULT,
    CONF_OPTION_CHAT_PREFIX_SENDER,
    CONF_OPTION_CHAT_PREFIX_SENDER_DEFAULT,
    CONF_OPTION_CHAT_REQUIRE_ADMIN,
    CONF_OPTION_CHAT_REQUIRE_ADMIN_DEFAULT,
    DOMAIN,
    EVENT_MESHTASTIC_CHAT_MESSAGE_SENT,
    EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG,
    LOGGER,
    MeshtasticDomainMessageLogEventData,
)
from .entity import GatewayChannelEntity, GatewayDirectMessageEntity

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.auth.models import User
    from homeassistant.config_entries import ConfigEntry

    from .data import MeshtasticConfigEntry

_LOGGER = LOGGER.getChild(__name__)

SIGNAL_CHAT_UPDATE = f"{DOMAIN}_chat_update"

DATA_CHAT: HassKey[dict[str, MeshtasticChat]] = HassKey(f"{DOMAIN}_chat")
DATA_CHAT_WEBSOCKET_REGISTERED: HassKey[bool] = HassKey(f"{DOMAIN}_chat_websocket")

CONVERSATION_CHANNEL = "channel"
CONVERSATION_NODE = "node"


class MessageStatus(StrEnum):
    RECEIVED = "received"
    SENDING = "sending"
    SENT = "sent"  # acknowledged by the mesh (e.g. rebroadcast heard), not necessarily by the recipient
    DELIVERED = "delivered"  # acknowledged by the recipient node
    NO_ACK = "no_ack"  # transmitted, but no acknowledgement arrived in time
    FAILED = "failed"


def chat_options(entry: ConfigEntry) -> Mapping[str, Any]:
    return entry.options.get(CONF_OPTION_CHAT, {})


def chat_enabled(entry: ConfigEntry) -> bool:
    return chat_options(entry).get(CONF_OPTION_CHAT_ENABLE, CONF_OPTION_CHAT_ENABLE_DEFAULT)


def chat_requires_admin(entry: ConfigEntry) -> bool:
    return chat_options(entry).get(CONF_OPTION_CHAT_REQUIRE_ADMIN, CONF_OPTION_CHAT_REQUIRE_ADMIN_DEFAULT)


def channel_conversation(index: int) -> str:
    return f"{CONVERSATION_CHANNEL}:{index}"


def node_conversation(node_id: int) -> str:
    return f"{CONVERSATION_NODE}:{node_id}"


def parse_conversation(conversation: str) -> tuple[str, int]:
    kind, _, value = conversation.partition(":")
    if kind not in (CONVERSATION_CHANNEL, CONVERSATION_NODE):
        msg = f"Invalid conversation {conversation}"
        raise vol.Invalid(msg)
    try:
        return kind, int(value)
    except ValueError as e:
        msg = f"Invalid conversation {conversation}"
        raise vol.Invalid(msg) from e


def short_user_name(name: str | None) -> str | None:
    """Format a Home Assistant user name as 'First L.'."""
    if not name:
        return None
    parts = name.split()
    if len(parts) >= 2:  # noqa: PLR2004
        return f"{parts[0]} {parts[-1][0]}."
    return parts[0]


def _psk_bytes(psk: str | None) -> int | None:
    """Return the length of a channel key in bytes; None if the key cannot be decoded."""
    if not psk:
        return 0
    try:
        return len(base64.b64decode(psk))
    except (binascii.Error, ValueError):
        return None


def _is_public_channel(psk: str | None) -> bool:
    # no key, or a one byte "well known" key (e.g. the default AQ==) means anyone can read the channel
    length = _psk_bytes(psk)
    return length is not None and length <= 1


def precision_to_meters(precision_bits: int | None) -> int | None:
    """Convert the number of position bits a node (or channel) shares to a precision in meters."""
    # https://github.com/meshtastic/Meshtastic-Android/issues/893: 2 bits ~ 5976 km, each extra bit halves it
    if not precision_bits or precision_bits < 2:  # noqa: PLR2004
        return None
    # 32 bits is the full precision of the device, reported as exact (0 m)
    return round(5976446 / 2 ** (precision_bits - 2))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute the initial bearing in degrees from the first to the second coordinate."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta = math.radians(lon2 - lon1)
    x = math.sin(delta) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _position_summary(position: Mapping[str, Any], origin: Mapping[str, Any]) -> dict[str, Any] | None:
    """Summarise a position with the distance and bearing from the gateway (``origin``) when both are known."""
    latitude = position.get("latitude")
    longitude = position.get("longitude")
    if latitude is None or longitude is None:
        return None
    summary: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "altitude": position.get("altitude"),
        "time": position.get("time"),
        "precision_bits": position.get("precisionBits"),
        "accuracy": precision_to_meters(position.get("precisionBits")),
        "distance": None,
        "bearing": None,
    }
    origin_latitude = origin.get("latitude")
    origin_longitude = origin.get("longitude")
    if origin_latitude is not None and origin_longitude is not None:
        summary["distance"] = location_util.distance(origin_latitude, origin_longitude, latitude, longitude)
        summary["bearing"] = round(bearing(origin_latitude, origin_longitude, latitude, longitude))
    return summary


NODE_REQUEST_TELEMETRY = "telemetry"
NODE_REQUEST_POSITION = "position"
NODE_REQUEST_TRACEROUTE = "traceroute"
NODE_REQUESTS = (NODE_REQUEST_TELEMETRY, NODE_REQUEST_POSITION, NODE_REQUEST_TRACEROUTE)

_REQUEST_ERRORS = (MeshtasticApiClientError, ValueError, TimeoutError)


def _error_text(error: Exception) -> str:
    # errors raised with a translation key carry the key as text; the panel shows what went wrong
    shown: BaseException = error
    if getattr(error, "translation_key", None) and error.__cause__ is not None:
        shown = error.__cause__
    return str(shown) or type(shown).__name__


def _user_fields(node_id: int, user: Mapping[str, Any]) -> dict[str, Any]:
    """Describe a node by the user part of its node database entry, the way the panel shows it."""
    user_id = user.get("id") or f"!{node_id:08x}"
    return {
        "user_id": user_id,
        "long_name": user.get("longName") or user_id,
        "short_name": user.get("shortName") or user_id[-4:],
    }


def _channel_name(channel: Mapping[str, Any]) -> str:
    index = int(channel.get("index", 0))
    return channel.get("settings", {}).get("name") or (
        "Primary" if channel.get("role") == CHAT_CHANNEL_ROLE_PRIMARY else f"Channel {index}"
    )


def _channel_key(choice: str, current: str | None) -> bytes:
    """Turn the key choice of the panel into the PSK bytes to store."""
    if choice == CHAT_CHANNEL_KEY_KEEP:
        return base64.b64decode(current) if current else b""
    if choice == CHAT_CHANNEL_KEY_NONE:
        return b""
    if choice == CHAT_CHANNEL_KEY_DEFAULT:
        return b"\x01"
    if choice == CHAT_CHANNEL_KEY_RANDOM:
        return secrets.token_bytes(32)
    if choice == CHAT_CHANNEL_KEY_RANDOM_128:
        return secrets.token_bytes(16)
    try:
        key = base64.b64decode(choice, validate=True)
    except (binascii.Error, ValueError) as e:
        msg = "The key is not valid base64"
        raise vol.Invalid(msg) from e
    if len(key) not in CHAT_CHANNEL_KEY_LENGTHS:
        msg = "A key must be empty, 1 byte (default keys), 16 bytes (AES-128) or 32 bytes (AES-256)"
        raise vol.Invalid(msg)
    return key


class MeshtasticChat:
    """Keeps the message history of one gateway and sends messages on behalf of the chat panel."""

    def __init__(self, hass: HomeAssistant, entry: MeshtasticConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, CHAT_STORAGE_VERSION, f"{DOMAIN}.chat.{entry.entry_id}", private=True
        )
        self._conversations: dict[str, list[dict[str, Any]]] = {}
        self._history_size = int(
            chat_options(entry).get(CONF_OPTION_CHAT_HISTORY_SIZE, CONF_OPTION_CHAT_HISTORY_SIZE_DEFAULT)
        )
        self._unsubscribe: CALLBACK_TYPE | None = None
        self._pending: set[asyncio.Task] = set()

    @property
    def requires_admin(self) -> bool:
        return chat_requires_admin(self.entry)

    @property
    def channel_management(self) -> str:
        return chat_options(self.entry).get(
            CONF_OPTION_CHAT_CHANNEL_MANAGEMENT, CONF_OPTION_CHAT_CHANNEL_MANAGEMENT_DEFAULT
        )

    def can_manage_channels(self, user: User | None) -> bool:
        level = self.channel_management
        if level == CONF_OPTION_CHAT_CHANNEL_MANAGEMENT_ALL:
            return True
        return level == CONF_OPTION_CHAT_CHANNEL_MANAGEMENT_ADMIN and user is not None and user.is_admin

    @property
    def prefix_sender(self) -> bool:
        return chat_options(self.entry).get(CONF_OPTION_CHAT_PREFIX_SENDER, CONF_OPTION_CHAT_PREFIX_SENDER_DEFAULT)

    @property
    def gateway_node(self) -> Mapping[str, Any]:
        return self.entry.runtime_data.gateway_node or {}

    @property
    def gateway_id(self) -> int | None:
        return self.gateway_node.get("num")

    async def async_setup(self) -> None:
        data = await self._store.async_load()
        if data:
            self._conversations = {k: list(v) for k, v in data.get("conversations", {}).items()}
            # messages that were still in flight when Home Assistant stopped will never be acknowledged
            for messages in self._conversations.values():
                for message in messages:
                    if message.get("status") == MessageStatus.SENDING:
                        message["status"] = MessageStatus.NO_ACK
        self._unsubscribe = self.hass.bus.async_listen(EVENT_MESHTASTIC_API_TEXT_MESSAGE, self._on_text_message)

    async def async_unload(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        for task in list(self._pending):
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        await self._store.async_save(self._data_to_save())

    @staticmethod
    async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, CHAT_STORAGE_VERSION, f"{DOMAIN}.chat.{entry_id}", private=True).async_remove()

    def _data_to_save(self) -> dict[str, Any]:
        return {"conversations": self._conversations}

    def _node_names(self, node_id: int) -> tuple[str, str, str]:
        node = self.entry.runtime_data.client.get_node_info(node_id)
        user_id = f"!{node_id:08x}"
        if node is None:
            return f"Meshtastic {user_id[-4:]}", user_id[-4:], user_id
        return node.long_name or user_id, node.short_name or user_id[-4:], node.user_id or user_id

    def _publish(self, kind: str, conversation: str, message: dict[str, Any]) -> None:
        self._store.async_delay_save(self._data_to_save, CHAT_STORAGE_SAVE_DELAY)
        async_dispatcher_send(
            self.hass,
            SIGNAL_CHAT_UPDATE,
            {"entry_id": self.entry.entry_id, "kind": kind, "conversation": conversation, "message": dict(message)},
        )

    def _append(self, conversation: str, message: dict[str, Any]) -> None:
        messages = self._conversations.setdefault(conversation, [])
        messages.append(message)
        if len(messages) > self._history_size:
            del messages[: len(messages) - self._history_size]
        self._publish("message", conversation, message)

    @callback
    def _on_text_message(self, event: Event) -> None:
        if event.data.get(ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID) != self.entry.entry_id:
            return
        data = event.data.get(ATTR_EVENT_MESHTASTIC_API_DATA)
        if not data:
            return

        from_id = data.get("from")
        to = data.get("to") or {}
        if from_id is None:
            return

        if to.get("node") is None and to.get("channel") is not None:
            conversation = channel_conversation(to["channel"])
        elif to.get("node") is not None:
            if self.gateway_id is not None and to["node"] != self.gateway_id:
                return
            conversation = node_conversation(from_id)
        else:
            return

        packet_id = event.data.get("message_id")
        if packet_id is not None:
            recent = self._conversations.get(conversation, [])[-CHAT_DEDUPLICATE_WINDOW:]
            if any(m.get("packet_id") == packet_id and m.get("from") == from_id for m in recent):
                return

        long_name, short_name, user_id = self._node_names(from_id)
        reply_id = data.get("reply_id")
        quoted = self._find_message(conversation, packet_id=reply_id) if reply_id else None
        self._append(
            conversation,
            {
                "id": uuid.uuid4().hex,
                "packet_id": packet_id,
                "reply_to": reply_id,
                "reply_preview": self._reply_preview(quoted),
                "ts": time.time(),
                "direction": "in",
                "from": from_id,
                "from_user_id": user_id,
                "from_name": long_name,
                "from_short": short_name,
                "text": data.get("message", ""),
                "status": MessageStatus.RECEIVED,
                # the event reports 0 for a packet without signal info (e.g. relayed through MQTT)
                "snr": data.get("rx_snr") or None,
                "rssi": data.get("rx_rssi") or None,
                "hops": data.get("hops_away"),
            },
        )

    def _find_message(
        self, conversation: str, *, message_id: str | None = None, packet_id: int | None = None
    ) -> dict[str, Any] | None:
        for message in reversed(self._conversations.get(conversation, [])):
            if (message_id is not None and message["id"] == message_id) or (
                packet_id is not None and message.get("packet_id") == packet_id
            ):
                return message
        return None

    @staticmethod
    def _reply_preview(message: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """Keep who said what with a reply, so the quote is still there when the original is long gone."""
        if message is None:
            return None
        return {
            "id": message["id"],
            "from": message["from"],
            "name": message["from_name"],
            "text": message["text"][:CHAT_REPLY_PREVIEW_CHARS],
        }

    def history(
        self, conversation: str, before: float | None = None, limit: int = CHAT_HISTORY_PAGE_SIZE
    ) -> list[dict[str, Any]]:
        messages = self._conversations.get(conversation, [])
        if before is not None:
            messages = [m for m in messages if m["ts"] < before]
        return [dict(m) for m in messages[-limit:]]

    def clear(self, conversation: str) -> None:
        if self._conversations.pop(conversation, None) is not None:
            self._store.async_delay_save(self._data_to_save, CHAT_STORAGE_SAVE_DELAY)
            async_dispatcher_send(
                self.hass,
                SIGNAL_CHAT_UPDATE,
                {"entry_id": self.entry.entry_id, "kind": "cleared", "conversation": conversation, "message": None},
            )

    def summaries(self) -> dict[str, dict[str, Any]]:
        result = {}
        for conversation, messages in self._conversations.items():
            if not messages:
                continue
            last = messages[-1]
            result[conversation] = {
                "ts": last["ts"],
                "text": last["text"],
                "direction": last["direction"],
                "from_short": last.get("from_short"),
                "count": len(messages),
            }
        return result

    async def async_targets(self) -> dict[str, list[dict[str, Any]]]:
        client = self.entry.runtime_data.client
        channels = []
        used_indexes = set()
        for channel in await client.async_get_channels():
            role = channel.get("role")
            if role == CHAT_CHANNEL_ROLE_DISABLED:
                continue
            index = int(channel.get("index", 0))
            used_indexes.add(index)
            channels.append(
                {
                    "index": index,
                    "name": _channel_name(channel),
                    "primary": role == CHAT_CHANNEL_ROLE_PRIMARY,
                    "public": _is_public_channel(channel.get("settings", {}).get("psk")),
                    "conversation": channel_conversation(index),
                }
            )

        nodes = []
        for node_id, node in (await client.async_get_all_nodes()).items():
            if node_id == self.gateway_id:
                continue
            user = node.get("user", {})
            nodes.append(
                {
                    "num": node_id,
                    **_user_fields(node_id, user),
                    "hw_model": user.get("hwModel"),
                    "role": user.get("role"),
                    "last_heard": node.get("lastHeard"),
                    "hops_away": node.get("hopsAway"),
                    "snr": node.get("snr"),
                    "via_mqtt": bool(node.get("viaMqtt", False)),
                    "battery_level": node.get("deviceMetrics", {}).get("batteryLevel"),
                    "has_position": "latitude" in node.get("position", {}),
                    "favorite": bool(node.get("isFavorite", False)),
                    "has_public_key": bool(user.get("publicKey")),
                    "conversation": node_conversation(node_id),
                }
            )
        free_indexes = [i for i in range(1, CHAT_CHANNEL_COUNT) if i not in used_indexes]
        targets: dict[str, Any] = {"channels": channels, "nodes": nodes, "free_channel_indexes": free_indexes}
        return targets

    def _device_id(self, node_id: int) -> str | None:
        device = dr.async_get(self.hass).async_get_device(identifiers={(DOMAIN, str(node_id))})
        return device.id if device else None

    async def _async_get_channel(self, index: int) -> Mapping[str, Any] | None:
        channels = await self.entry.runtime_data.client.async_get_channels()
        return next((c for c in channels if int(c.get("index", 0)) == index), None)

    async def _gateway_position_accuracy(self) -> int | None:
        """
        Say how precisely the gateway shares its own position with the mesh.

        The node database holds the gateway's exact position without a precision. What the other nodes see is
        limited by the position precision of the channel it broadcasts on, normally the primary one.
        """
        for channel in await self.entry.runtime_data.client.async_get_channels():
            if channel.get("role") == CHAT_CHANNEL_ROLE_PRIMARY:
                bits = channel.get("settings", {}).get("moduleSettings", {}).get("positionPrecision")
                return precision_to_meters(bits)
        return None

    async def async_node_details(self, node_id: int) -> dict[str, Any]:
        """Describe a node with everything the gateway knows about it, for the details view of the panel."""
        nodes = await self.entry.runtime_data.client.async_get_all_nodes()
        node = nodes.get(node_id)
        if node is None:
            msg = f"Unknown node {node_id}"
            raise vol.Invalid(msg)
        user = node.get("user", {})
        metrics = node.get("deviceMetrics", {})
        gateway_node = nodes.get(self.gateway_id or 0) or {}
        gateway_position = gateway_node.get("position", {})
        gateway_info = await self._gateway_info(len(nodes)) if node_id == self.gateway_id else None
        gateway_marker = None
        gateway_accuracy = None
        if gateway_position.get("latitude") is not None and gateway_position.get("longitude") is not None:
            gateway_fields = _user_fields(self.gateway_id or 0, gateway_node.get("user", {}))
            gateway_accuracy = precision_to_meters(gateway_position.get("precisionBits"))
            if gateway_accuracy is None:
                gateway_accuracy = await self._gateway_position_accuracy()
            gateway_marker = {
                "num": self.gateway_id,
                "short_name": gateway_fields["short_name"],
                "long_name": gateway_fields["long_name"],
                "latitude": gateway_position["latitude"],
                "longitude": gateway_position["longitude"],
                "accuracy": gateway_accuracy,
            }
        position = _position_summary(node.get("position", {}), gateway_position)
        if position is not None and node_id == self.gateway_id and position["accuracy"] is None:
            position["accuracy"] = gateway_accuracy
        return {
            "num": node_id,
            **_user_fields(node_id, user),
            "hw_model": user.get("hwModel"),
            "role": user.get("role"),
            "is_licensed": bool(user.get("isLicensed", False)),
            "has_public_key": bool(user.get("publicKey")),
            "favorite": bool(node.get("isFavorite", False)),
            "ignored": bool(node.get("isIgnored", False)),
            "last_heard": node.get("lastHeard"),
            "hops_away": node.get("hopsAway"),
            "snr": node.get("snr"),
            "rssi": node.get("rssi"),
            "via_mqtt": bool(node.get("viaMqtt", False)),
            "channel": node.get("channel"),
            "battery_level": metrics.get("batteryLevel"),
            "voltage": metrics.get("voltage"),
            "channel_utilization": metrics.get("channelUtilization"),
            "air_util_tx": metrics.get("airUtilTx"),
            "uptime_seconds": metrics.get("uptimeSeconds"),
            "position": position,
            "device_id": self._device_id(node_id),
            "gateway_position": gateway_marker,
            "is_gateway": node_id == self.gateway_id,
            "gateway_info": gateway_info,
            "activity": self.node_activity(node_id) if node_id != self.gateway_id else None,
            "conversation": node_conversation(node_id),
        }

    async def _gateway_info(self, nodes_known: int) -> dict[str, Any]:
        """Facts about the connected node itself: firmware, radio settings, what it knows."""
        client = self.entry.runtime_data.client
        metadata = client.metadata
        local_config = await client.async_get_node_local_config()
        module_config = await client.async_get_node_module_config()
        lora = local_config.get("lora", {})
        mqtt = module_config.get("mqtt", {})
        channels = [c for c in await client.async_get_channels() if c.get("role") != CHAT_CHANNEL_ROLE_DISABLED]
        return {
            "firmware_version": metadata.get("firmwareVersion"),
            "hw_model": metadata.get("hwModel"),
            "role": metadata.get("role") or local_config.get("device", {}).get("role"),
            "region": lora.get("region"),
            "modem_preset": lora.get("modemPreset"),
            "hop_limit": lora.get("hopLimit"),
            "tx_power": lora.get("txPower"),
            "tx_enabled": lora.get("txEnabled", True),
            "mqtt_enabled": bool(mqtt.get("enabled", False)),
            "channels": len(channels),
            "nodes_known": nodes_known,
            "messages_today": self.messages_since(dt_util.start_of_local_day().timestamp()),
            "messages_total": sum(len(m) for m in self._conversations.values()),
            "title": self.entry.title,
        }

    async def async_map_nodes(self) -> dict[str, Any]:
        """Every node with a position, for the map view: trackers to show and nodes that have none."""
        nodes = await self.entry.runtime_data.client.async_get_all_nodes()
        gateway_position = (nodes.get(self.gateway_id or 0) or {}).get("position", {})
        result = []
        gateway_accuracy = None
        for node_id, node in nodes.items():
            position = _position_summary(node.get("position", {}), gateway_position)
            if position is None:
                continue
            if node_id == self.gateway_id and position["accuracy"] is None:
                if gateway_accuracy is None:
                    gateway_accuracy = await self._gateway_position_accuracy()
                position["accuracy"] = gateway_accuracy
            result.append(
                {
                    "num": node_id,
                    **_user_fields(node_id, node.get("user", {})),
                    "last_heard": node.get("lastHeard"),
                    "position": position,
                    "is_gateway": node_id == self.gateway_id,
                    "conversation": node_conversation(node_id),
                }
            )
        result.sort(key=lambda n: (not n["is_gateway"], n["position"]["distance"] or 0))
        return {"nodes": result, "nodes_known": len(nodes)}

    async def async_node_request(
        self, node_id: int, request: str, telemetry_type: str | None = None
    ) -> Mapping[str, Any]:
        """Ask a node for telemetry, its position or a traceroute and return the decoded response."""
        client = self.entry.runtime_data.client
        if request == NODE_REQUEST_TELEMETRY:
            metric = TelemetryType(telemetry_type or TelemetryType.DEVICE_METRICS)
            return await client.request_telemetry(node_id, metric)
        if request == NODE_REQUEST_POSITION:
            response = dict(await client.request_position(node_id))
            if "latitudeI" in response:
                response["latitude"] = float(response["latitudeI"] * 10**-7)
            if "longitudeI" in response:
                response["longitude"] = float(response["longitudeI"] * 10**-7)
            return response
        if request == NODE_REQUEST_TRACEROUTE:
            response = dict(await client.request_traceroute(node_id))
            hops = {*response.get("route", []), *response.get("routeBack", []), node_id}
            if self.gateway_id is not None:
                hops.add(self.gateway_id)
            response["nodes"] = {}
            for hop in hops:
                long_name, short_name, user_id = self._node_names(hop)
                response["nodes"][str(hop)] = {"long_name": long_name, "short_name": short_name, "user_id": user_id}
            return response
        msg = f"Unknown request {request}"
        raise vol.Invalid(msg)

    async def async_set_favorite(self, node_id: int, *, favorite: bool) -> None:
        await self.entry.runtime_data.client.async_set_favorite(node_id, favorite=favorite)

    def node_activity(self, node_id: int) -> dict[str, Any]:
        """Count the stored messages from a node, in its direct messages and on the channels."""
        direct = 0
        on_channels = 0
        last_ts = None
        for conversation, messages in self._conversations.items():
            for message in messages:
                if message["direction"] != "in" or message.get("from") != node_id:
                    continue
                if conversation.startswith(CONVERSATION_NODE):
                    direct += 1
                else:
                    on_channels += 1
                if last_ts is None or message["ts"] > last_ts:
                    last_ts = message["ts"]
        return {"direct": direct, "channels": on_channels, "total": direct + on_channels, "last_ts": last_ts}

    def messages_since(self, since: float) -> int:
        return sum(1 for messages in self._conversations.values() for m in messages if m["ts"] >= since)

    def channel_activity(self, index: int) -> dict[str, Any]:
        """Summarise the stored history of a channel: message counts and the nodes heard on it."""
        messages = self._conversations.get(channel_conversation(index), [])
        senders: dict[int, dict[str, Any]] = {}
        received = 0
        for message in messages:
            if message["direction"] != "in":
                continue
            received += 1
            sender = senders.setdefault(
                message["from"],
                {
                    "num": message["from"],
                    "user_id": message.get("from_user_id"),
                    "long_name": message.get("from_name"),
                    "short_name": message.get("from_short"),
                    "count": 0,
                    "last_ts": 0,
                    "conversation": node_conversation(message["from"]),
                },
            )
            sender["count"] += 1
            sender["last_ts"] = max(sender["last_ts"], message["ts"])
        return {
            "messages": len(messages),
            "received": received,
            "sent": len(messages) - received,
            "first_ts": messages[0]["ts"] if messages else None,
            "last_ts": messages[-1]["ts"] if messages else None,
            "nodes": sorted(senders.values(), key=lambda s: -s["last_ts"]),
        }

    async def async_channel_details(self, index: int, *, share_urls: bool = False) -> dict[str, Any]:
        """
        Describe a channel: its settings on the gateway plus the activity seen in the chat history.

        The share links carry the channel key, so they are only included when ``share_urls`` is set, which the
        callers do for users allowed to manage channels.
        """
        client = self.entry.runtime_data.client
        channel = await self._async_get_channel(index)
        if channel is None or channel.get("role") == CHAT_CHANNEL_ROLE_DISABLED:
            msg = f"Unknown channel {index}"
            raise vol.Invalid(msg)
        settings = channel.get("settings", {})
        module_settings = settings.get("moduleSettings", {})
        lora = (await client.async_get_node_local_config()).get("lora", {})
        module_config = await client.async_get_node_module_config()
        mqtt = module_config.get("mqtt", {})
        role = channel.get("role")
        precision_bits = module_settings.get("positionPrecision")
        return {
            "index": index,
            "name": _channel_name(channel),
            "settings_name": settings.get("name", ""),
            "role": role,
            "public": _is_public_channel(settings.get("psk")),
            "key_bytes": _psk_bytes(settings.get("psk")),
            "uplink_enabled": bool(settings.get("uplinkEnabled", False)),
            "downlink_enabled": bool(settings.get("downlinkEnabled", False)),
            "position_precision_bits": precision_bits,
            "position_precision": precision_to_meters(precision_bits),
            "client_muted": bool(module_settings.get("isClientMuted", False)),
            "gateway": {
                "region": lora.get("region"),
                "modem_preset": lora.get("modemPreset"),
                "hop_limit": lora.get("hopLimit"),
                "mqtt_enabled": bool(mqtt.get("enabled", False)),
                "mqtt_address": mqtt.get("address") if mqtt.get("enabled") else None,
                "mqtt_encryption_enabled": bool(mqtt.get("encryptionEnabled", False)),
                "store_forward_enabled": bool(module_config.get("storeForward", {}).get("enabled", False)),
            },
            "activity": self.channel_activity(index),
            "conversation": channel_conversation(index),
            "url": client.channel_url([index], add=True) if share_urls else None,
            "url_all": client.channel_url() if share_urls else None,
        }

    async def async_set_channel(  # noqa: PLR0913
        self,
        index: int,
        *,
        role: str | None = None,
        name: str | None = None,
        key: str = CHAT_CHANNEL_KEY_KEEP,
        uplink_enabled: bool | None = None,
        downlink_enabled: bool | None = None,
        position_precision: int | None = None,
        user: User | None = None,
    ) -> dict[str, Any]:
        """
        Create or change a channel on the gateway; fields left out keep their current value.

        A disabled slot becomes a secondary channel with a fresh AES-256 key unless another key is chosen. The
        primary channel keeps its role and key: changing those would move the gateway to another mesh, which is
        left to the apps on purpose.
        """
        if not 0 <= index < CHAT_CHANNEL_COUNT:
            msg = f"Channel index must be between 0 and {CHAT_CHANNEL_COUNT - 1}"
            raise vol.Invalid(msg)
        client = self.entry.runtime_data.client
        current = await self._async_get_channel(index)
        current_role = (current or {}).get("role", CHAT_CHANNEL_ROLE_DISABLED)
        settings = (current or {}).get("settings", {})
        creating = current_role == CHAT_CHANNEL_ROLE_DISABLED

        if current_role == CHAT_CHANNEL_ROLE_PRIMARY:
            if role not in (None, CHAT_CHANNEL_ROLE_PRIMARY):
                msg = "The primary channel cannot be disabled from the chat"
                raise vol.Invalid(msg)
            if key != CHAT_CHANNEL_KEY_KEEP:
                msg = "The key of the primary channel cannot be changed from the chat; use the Meshtastic app"
                raise vol.Invalid(msg)
            new_role = CHAT_CHANNEL_ROLE_PRIMARY
        else:
            if role == CHAT_CHANNEL_ROLE_PRIMARY:
                msg = "Only channel 0 can be the primary channel"
                raise vol.Invalid(msg)
            new_role = role or CHAT_CHANNEL_ROLE_SECONDARY
            if creating and key == CHAT_CHANNEL_KEY_KEEP:
                key = CHAT_CHANNEL_KEY_RANDOM

        new_name = settings.get("name", "") if name is None else name.strip()
        if len(new_name.encode("utf-8")) > CHAT_CHANNEL_NAME_MAX_BYTES:
            msg = f"Channel names are limited to {CHAT_CHANNEL_NAME_MAX_BYTES} bytes"
            raise vol.Invalid(msg)
        precision = settings.get("moduleSettings", {}).get("positionPrecision", 0)
        if position_precision is not None:
            precision = position_precision
        if not 0 <= precision <= CHAT_POSITION_PRECISION_FULL:
            msg = "Position precision must be between 0 (off) and 32 (full)"
            raise vol.Invalid(msg)

        stored = await client.async_write_channel(
            index,
            role=new_role,
            name=new_name,
            psk=_channel_key(key, settings.get("psk")),
            uplink_enabled=settings.get("uplinkEnabled", False) if uplink_enabled is None else uplink_enabled,
            downlink_enabled=settings.get("downlinkEnabled", False) if downlink_enabled is None else downlink_enabled,
            position_precision=precision,
        )
        _LOGGER.info(
            "Channel %s %s by %s: role %s, name %r",
            index,
            "created" if creating else "changed",
            user.name if user else "an automation",
            stored.get("role"),
            stored.get("settings", {}).get("name"),
        )
        if stored.get("role") == CHAT_CHANNEL_ROLE_DISABLED:
            return {"index": index, "role": CHAT_CHANNEL_ROLE_DISABLED, "conversation": channel_conversation(index)}
        # whoever may change a channel may also share it
        return await self.async_channel_details(index, share_urls=True)

    def prefix_for(self, user: User | None) -> str | None:
        if not self.prefix_sender or user is None:
            return None
        return short_user_name(user.name)

    def compose(self, text: str, user: User | None) -> str:
        text = text.strip()
        if prefix := self.prefix_for(user):
            text = f"{prefix}: {text}"
        return text

    async def async_send(  # noqa: PLR0913
        self,
        conversation: str,
        text: str,
        user: User | None = None,
        *,
        sent_by: str | None = None,
        channel_index: int | None = None,
        reply_to: str | None = None,
        want_ack: bool = True,
    ) -> dict[str, Any]:
        """
        Send a message and keep it in the history.

        ``user`` is the Home Assistant user sending from the chat panel (used for the optional prefix).
        ``sent_by`` labels messages sent on behalf of someone else, e.g. by an automation or a bridge.
        ``channel_index`` optionally selects the channel a direct message is sent on; by default the client
        decides (the node's PKI channel or the primary channel).
        ``reply_to`` is the id of a message of the same conversation this one answers: it is sent as a Meshtastic
        reply (the apps show it as a quote) and kept with the quoted text.
        ``want_ack`` requests an acknowledgement and tracks the delivery of the message; without it the message is
        only handed to the mesh and shown as sent.
        """
        kind, target = parse_conversation(conversation)
        prefix = self.prefix_for(user)
        full_text = self.compose(text, user)
        if not full_text:
            msg = "Message is empty"
            raise vol.Invalid(msg)
        if len(full_text.encode("utf-8")) > CHAT_MAX_TEXT_BYTES:
            msg = f"Message is longer than {CHAT_MAX_TEXT_BYTES} bytes"
            raise vol.Invalid(msg)

        quoted = None
        if reply_to is not None:
            quoted = self._find_message(conversation, message_id=reply_to)
            if quoted is None or quoted.get("packet_id") is None:
                msg = "The message to reply to is not available"
                raise vol.Invalid(msg)

        if kind == CONVERSATION_CHANNEL:
            destination = MeshInterface.BROADCAST_NUM
            channel_index = target
        else:
            destination = target

        gateway_id = self.gateway_id or 0
        long_name, short_name, user_id = self._node_names(gateway_id)
        message = {
            "id": uuid.uuid4().hex,
            # chosen here so that a reply can point at this message before the mesh has acknowledged it
            "packet_id": self.entry.runtime_data.client.new_packet_id(),
            "reply_to": quoted["packet_id"] if quoted else None,
            "reply_preview": self._reply_preview(quoted),
            "ts": time.time(),
            "direction": "out",
            "from": gateway_id,
            "from_user_id": user_id,
            "from_name": long_name,
            "from_short": short_name,
            "to": target if kind == CONVERSATION_NODE else None,
            "text": full_text,
            "status": MessageStatus.SENDING,
            "sent_by": sent_by if sent_by is not None else (user.name if user else None),
            "prefix": prefix,
        }
        self._append(conversation, message)

        task = self.hass.async_create_background_task(
            self._async_deliver(conversation, message, destination, channel_index, want_ack=want_ack),
            name=f"{DOMAIN}_chat_send_{message['id']}",
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return dict(message)

    async def _async_deliver(  # noqa: PLR0912
        self,
        conversation: str,
        message: dict[str, Any],
        destination: int,
        channel_index: int | None,
        *,
        want_ack: bool = True,
    ) -> None:
        client = self.entry.runtime_data.client
        try:
            if want_ack:
                ack_from, error = await client.send_text_with_ack(
                    message["text"],
                    destination,
                    channel_index=channel_index,
                    ack_timeout=CHAT_ACK_TIMEOUT,
                    packet_id=message["packet_id"],
                    reply_id=message["reply_to"],
                )
            else:
                sent = await client.send_text(
                    message["text"],
                    destination,
                    want_ack=False,
                    channel_index=channel_index,
                    packet_id=message["packet_id"],
                    reply_id=message["reply_to"],
                )
        except asyncio.CancelledError:
            message["status"] = MessageStatus.NO_ACK
            self._publish("update", conversation, message)
            raise
        except (MeshtasticApiClientError, ValueError) as e:
            _LOGGER.debug("Failed to send chat message", exc_info=True)
            message["status"] = MessageStatus.FAILED
            message["error"] = _error_text(e)
            self._publish("update", conversation, message)
            return

        if not want_ack:
            # no delivery tracking: the message counts as sent once the mesh has taken it
            if sent:
                message["status"] = MessageStatus.SENT
            else:
                message["status"] = MessageStatus.FAILED
                message["error"] = "Timed out while sending"
        elif error is None:
            message["status"] = MessageStatus.NO_ACK
        elif error != "NONE":
            message["status"] = MessageStatus.FAILED
            message["error"] = error
        elif channel_index is None and ack_from == destination:
            message["status"] = MessageStatus.DELIVERED
        else:
            message["status"] = MessageStatus.SENT
        self._publish("update", conversation, message)

        if message["status"] != MessageStatus.FAILED:
            if want_ack:
                # without ack the client's own text_message_out event already put the message in the logbook
                self._log_sent_message(message, destination, channel_index)
            self.hass.bus.async_fire(
                EVENT_MESHTASTIC_CHAT_MESSAGE_SENT,
                {
                    ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID: self.entry.entry_id,
                    "gateway": self.gateway_id,
                    "message_id": message["id"],
                    "to": {
                        "node": destination if channel_index is None else None,
                        "channel": channel_index,
                    },
                    "message": message["text"],
                    "prefix": message.get("prefix"),
                    "sent_by": message.get("sent_by"),
                    "status": str(message["status"]),
                },
            )

    def _log_sent_message(self, message: dict[str, Any], destination: int, channel_index: int | None) -> None:
        """Record messages sent from the chat in the logbook, next to the received ones."""
        gateway_id = self.gateway_id
        if gateway_id is None:
            return
        entity_registry = er.async_get(self.hass)
        if channel_index is not None:
            unique_id = GatewayChannelEntity.build_unique_id(self.entry.entry_id, gateway_id, channel_index)
            from_name = f"{message['from_name']} ({message['from_user_id']})"
        else:
            unique_id = GatewayDirectMessageEntity.build_unique_id(self.entry.entry_id, gateway_id)
            to_long, _, to_user_id = self._node_names(destination)
            from_name = f"{message['from_name']} ({message['from_user_id']}) → {to_long} ({to_user_id})"
        entity_id = entity_registry.async_get_entity_id(DOMAIN, DOMAIN, unique_id)
        if entity_id is None:
            return
        device = dr.async_get(self.hass).async_get_device(identifiers={(DOMAIN, str(gateway_id))})
        log_event: MeshtasticDomainMessageLogEventData = {
            "entity_id": entity_id,
            "device_id": device.id if device else None,
            "from_name": from_name,
            "pki": channel_index is None,
            "message": message["text"],
        }
        self.hass.bus.async_fire(event_type=EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG, event_data=log_event)


async def async_setup_chat(hass: HomeAssistant, entry: MeshtasticConfigEntry) -> MeshtasticChat:
    chats = hass.data.setdefault(DATA_CHAT, {})
    if (existing := chats.pop(entry.entry_id, None)) is not None:
        await existing.async_unload()
    chat = MeshtasticChat(hass, entry)
    await chat.async_setup()
    chats[entry.entry_id] = chat
    _async_register_websocket_commands(hass)
    return chat


async def async_unload_chat(hass: HomeAssistant, entry: ConfigEntry) -> None:
    chats = hass.data.get(DATA_CHAT, {})
    if (chat := chats.pop(entry.entry_id, None)) is not None:
        await chat.async_unload()


def any_chat_loaded(hass: HomeAssistant) -> bool:
    return bool(hass.data.get(DATA_CHAT, {}))


def all_chats_require_admin(hass: HomeAssistant) -> bool:
    return all(chat.requires_admin for chat in hass.data.get(DATA_CHAT, {}).values())


def _chats_for_user(hass: HomeAssistant, user: User) -> dict[str, MeshtasticChat]:
    return {
        entry_id: chat
        for entry_id, chat in hass.data.get(DATA_CHAT, {}).items()
        if user.is_admin or not chat.requires_admin
    }


def _get_chat(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> MeshtasticChat | None:
    chat = _chats_for_user(hass, connection.user).get(msg["entry_id"])
    if chat is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Chat not available for this gateway")
    return chat


@callback
def _async_register_websocket_commands(hass: HomeAssistant) -> None:
    if hass.data.get(DATA_CHAT_WEBSOCKET_REGISTERED):
        return
    hass.data[DATA_CHAT_WEBSOCKET_REGISTERED] = True
    websocket_api.async_register_command(hass, websocket_chat_gateways)
    websocket_api.async_register_command(hass, websocket_chat_history)
    websocket_api.async_register_command(hass, websocket_chat_send)
    websocket_api.async_register_command(hass, websocket_chat_clear)
    websocket_api.async_register_command(hass, websocket_chat_subscribe)
    websocket_api.async_register_command(hass, websocket_chat_node)
    websocket_api.async_register_command(hass, websocket_chat_node_request)
    websocket_api.async_register_command(hass, websocket_chat_favorite)
    websocket_api.async_register_command(hass, websocket_chat_channel)
    websocket_api.async_register_command(hass, websocket_chat_channel_set)
    websocket_api.async_register_command(hass, websocket_chat_map)


@websocket_api.websocket_command({vol.Required("type"): "meshtastic/chat/gateways"})
@websocket_api.async_response
async def websocket_chat_gateways(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    result = []
    for entry_id, chat in _chats_for_user(hass, connection.user).items():
        gateway = chat.gateway_node
        user = gateway.get("user", {})
        try:
            targets = await chat.async_targets()
            available = True
        except Exception:
            _LOGGER.debug("Failed to load chat targets", exc_info=True)
            targets = {"channels": [], "nodes": []}
            available = False
        result.append(
            {
                "entry_id": entry_id,
                "title": chat.entry.title,
                "available": available,
                "gateway": {
                    "num": chat.gateway_id,
                    "user_id": user.get("id"),
                    "long_name": user.get("longName"),
                    "short_name": user.get("shortName"),
                },
                "max_bytes": CHAT_MAX_TEXT_BYTES,
                "channel_name_max_bytes": CHAT_CHANNEL_NAME_MAX_BYTES,
                "prefix": chat.prefix_for(connection.user),
                "can_clear": connection.user.is_admin,
                "can_favorite": connection.user.is_admin,
                "can_manage_channels": chat.can_manage_channels(connection.user),
                "conversations": chat.summaries(),
                **targets,
            }
        )
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshtastic/chat/history",
        vol.Required("entry_id"): str,
        vol.Required("conversation"): str,
        vol.Optional("before"): vol.Coerce(float),
        vol.Optional("limit", default=CHAT_HISTORY_PAGE_SIZE): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=CHAT_HISTORY_PAGE_SIZE_MAX)
        ),
    }
)
@callback
def websocket_chat_history(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (chat := _get_chat(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], chat.history(msg["conversation"], msg.get("before"), msg["limit"]))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshtastic/chat/send",
        vol.Required("entry_id"): str,
        vol.Required("conversation"): str,
        vol.Required("text"): str,
        vol.Optional("reply_to"): str,
    }
)
@websocket_api.async_response
async def websocket_chat_send(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (chat := _get_chat(hass, connection, msg)) is None:
        return
    try:
        message = await chat.async_send(msg["conversation"], msg["text"], connection.user, reply_to=msg.get("reply_to"))
    except vol.Invalid as e:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(e))
        return
    connection.send_result(msg["id"], message)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshtastic/chat/clear",
        vol.Required("entry_id"): str,
        vol.Required("conversation"): str,
    }
)
@websocket_api.require_admin
@callback
def websocket_chat_clear(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (chat := _get_chat(hass, connection, msg)) is None:
        return
    chat.clear(msg["conversation"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "meshtastic/chat/subscribe"})
@callback
def websocket_chat_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    user = connection.user

    @callback
    def _forward(update: dict[str, Any]) -> None:
        if update["entry_id"] not in _chats_for_user(hass, user):
            return
        connection.send_message(websocket_api.event_message(msg["id"], update))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_CHAT_UPDATE, _forward)
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshtastic/chat/node",
        vol.Required("entry_id"): str,
        vol.Required("node_id"): vol.Coerce(int),
    }
)
@websocket_api.async_response
async def websocket_chat_node(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (chat := _get_chat(hass, connection, msg)) is None:
        return
    try:
        details = await chat.async_node_details(msg["node_id"])
    except vol.Invalid as e:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, str(e))
        return
    except Exception as e:
        _LOGGER.debug("Failed to load node details", exc_info=True)
        connection.send_error(msg["id"], websocket_api.ERR_UNKNOWN_ERROR, _error_text(e))
        return
    connection.send_result(msg["id"], details)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshtastic/chat/node/request",
        vol.Required("entry_id"): str,
        vol.Required("node_id"): vol.Coerce(int),
        vol.Required("request"): vol.In(NODE_REQUESTS),
        vol.Optional("telemetry_type"): vol.In([t.value for t in TelemetryType]),
    }
)
@websocket_api.async_response
async def websocket_chat_node_request(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (chat := _get_chat(hass, connection, msg)) is None:
        return
    try:
        result = await chat.async_node_request(msg["node_id"], msg["request"], msg.get("telemetry_type"))
    except _REQUEST_ERRORS as e:
        connection.send_error(msg["id"], websocket_api.ERR_UNKNOWN_ERROR, _error_text(e))
        return
    # the response usually updated the node database, send the fresh details along
    try:
        node = await chat.async_node_details(msg["node_id"])
    except Exception:  # noqa: BLE001
        node = None
    connection.send_result(msg["id"], {"request": msg["request"], "result": result, "node": node})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshtastic/chat/favorite",
        vol.Required("entry_id"): str,
        vol.Required("node_id"): vol.Coerce(int),
        vol.Required("favorite"): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_chat_favorite(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (chat := _get_chat(hass, connection, msg)) is None:
        return
    try:
        await chat.async_set_favorite(msg["node_id"], favorite=msg["favorite"])
    except _REQUEST_ERRORS as e:
        connection.send_error(msg["id"], websocket_api.ERR_UNKNOWN_ERROR, _error_text(e))
        return
    connection.send_result(msg["id"], {"node_id": msg["node_id"], "favorite": msg["favorite"]})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshtastic/chat/channel",
        vol.Required("entry_id"): str,
        vol.Required("index"): vol.Coerce(int),
    }
)
@websocket_api.async_response
async def websocket_chat_channel(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (chat := _get_chat(hass, connection, msg)) is None:
        return
    try:
        details = await chat.async_channel_details(msg["index"], share_urls=chat.can_manage_channels(connection.user))
    except vol.Invalid as e:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, str(e))
        return
    except Exception as e:
        _LOGGER.debug("Failed to load channel details", exc_info=True)
        connection.send_error(msg["id"], websocket_api.ERR_UNKNOWN_ERROR, _error_text(e))
        return
    connection.send_result(msg["id"], details)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshtastic/chat/channel/set",
        vol.Required("entry_id"): str,
        vol.Required("index"): vol.All(vol.Coerce(int), vol.Range(min=0, max=CHAT_CHANNEL_COUNT - 1)),
        vol.Optional("role"): vol.In((CHAT_CHANNEL_ROLE_SECONDARY, CHAT_CHANNEL_ROLE_DISABLED)),
        vol.Optional("name"): vol.All(str, vol.Length(max=CHAT_CHANNEL_NAME_INPUT_MAX_LENGTH)),
        vol.Optional("key", default=CHAT_CHANNEL_KEY_KEEP): vol.All(
            str, vol.Length(max=CHAT_CHANNEL_KEY_INPUT_MAX_LENGTH)
        ),
        vol.Optional("uplink_enabled"): bool,
        vol.Optional("downlink_enabled"): bool,
        vol.Optional("position_precision"): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=CHAT_POSITION_PRECISION_FULL)
        ),
    }
)
@websocket_api.async_response
async def websocket_chat_channel_set(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (chat := _get_chat(hass, connection, msg)) is None:
        return
    if not chat.can_manage_channels(connection.user):
        connection.send_error(msg["id"], websocket_api.ERR_UNAUTHORIZED, "Channel management is not enabled for you")
        return
    try:
        result = await chat.async_set_channel(
            msg["index"],
            role=msg.get("role"),
            name=msg.get("name"),
            key=msg["key"],
            uplink_enabled=msg.get("uplink_enabled"),
            downlink_enabled=msg.get("downlink_enabled"),
            position_precision=msg.get("position_precision"),
            user=connection.user,
        )
    except vol.Invalid as e:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(e))
        return
    except _REQUEST_ERRORS as e:
        connection.send_error(msg["id"], websocket_api.ERR_UNKNOWN_ERROR, _error_text(e))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "meshtastic/chat/map", vol.Required("entry_id"): str})
@websocket_api.async_response
async def websocket_chat_map(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (chat := _get_chat(hass, connection, msg)) is None:
        return
    try:
        result = await chat.async_map_nodes()
    except Exception as e:
        _LOGGER.debug("Failed to load map nodes", exc_info=True)
        connection.send_error(msg["id"], websocket_api.ERR_UNKNOWN_ERROR, _error_text(e))
        return
    connection.send_result(msg["id"], result)
