# SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.meshtastic.aiomeshtastic.connection import ClientApiConnection
from custom_components.meshtastic.aiomeshtastic.errors import MeshRoutingError, MeshtasticError
from custom_components.meshtastic.aiomeshtastic.interface import MeshInterface, MeshNode, TelemetryType
from custom_components.meshtastic.aiomeshtastic.protobuf import (
    admin_pb2,
    apponly_pb2,
    channel_pb2,
    config_pb2,
    localonly_pb2,
    mesh_pb2,
)
from custom_components.meshtastic.api import (
    EVENT_MESHTASTIC_API_TEXT_MESSAGE,
    MeshtasticApiClient,
    MeshtasticApiClientError,
)
from custom_components.meshtastic.chat import (
    DATA_CHAT,
    MeshtasticChat,
    MessageStatus,
    async_setup_chat,
    async_unload_chat,
    bearing,
    precision_to_meters,
    short_user_name,
)
from custom_components.meshtastic.const import CHAT_CHANNEL_NAME_MAX_BYTES, CHAT_MAX_TEXT_BYTES, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

GATEWAY = 0x4904
ANNA = 0xA1B2C3D4
RELAY = 0x11111111
BROADCAST = 0xFFFFFFFF


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.sent_ids: list[tuple[int | None, int | None]] = []  # (packet id, reply id) of what was sent
        self._packet_id = 1000
        self.ack: tuple[int | None, str | None] | Exception = (GATEWAY, "NONE")
        self.favorites: set[int] = {ANNA}
        self.favorite_calls: list[tuple[int, bool]] = []
        self.requests: list[tuple[str, int, str | None]] = []
        self.request_error: Exception | None = None
        self.channels: dict[int, dict[str, Any]] = {
            0: self._channel(0, "PRIMARY", "", "AQ=="),
            1: self._channel(1, "SECONDARY", "SOSPrivate", "c2VjcmV0c2VjcmV0c2VjcmV0MTI="),
            2: self._channel(2, "DISABLED", "", ""),
        }
        self.written_channels: list[dict[str, Any]] = []
        self.metadata = {"firmwareVersion": "2.7.26.abcdef", "hwModel": "HELTEC_V3", "role": "CLIENT"}

    @staticmethod
    def _channel(index: int, role: str, name: str, psk: str, **module_settings: Any) -> dict[str, Any]:
        settings = {"name": name, "psk": psk, "uplinkEnabled": False, "downlinkEnabled": False}
        if module_settings:
            settings["moduleSettings"] = module_settings
        return {"index": index, "role": role, "settings": settings}

    def get_node_info(self, node_id: int) -> MeshNode | None:
        if node_id == ANNA:
            return MeshNode(id=ANNA, user_id="!a1b2c3d4", short_name="ANNA", long_name="Anna Hiking")
        if node_id == GATEWAY:
            return MeshNode(id=GATEWAY, user_id="!00004904", short_name="4904", long_name="Home Gateway")
        return None

    def new_packet_id(self) -> int:
        self._packet_id += 1
        return self._packet_id

    async def send_text_with_ack(  # noqa: PLR0913
        self,
        text: str,
        destination_id: int,
        *,
        channel_index: int | None = None,
        ack_timeout: float = 60,
        packet_id: int | None = None,
        reply_id: int | None = None,
    ) -> tuple[int | None, str | None]:
        self.sent.append({"text": text, "to": destination_id, "channel": channel_index, "timeout": ack_timeout})
        self.sent_ids.append((packet_id, reply_id))
        if isinstance(self.ack, Exception):
            raise self.ack
        return self.ack

    async def send_text(  # noqa: PLR0913
        self,
        text: str,
        destination_id: int | str = BROADCAST,
        *,
        channel_index: int | None = None,
        want_ack: bool = False,
        packet_id: int | None = None,
        reply_id: int | None = None,
    ) -> bool:
        self.sent.append({"text": text, "to": destination_id, "channel": channel_index, "ack": want_ack})
        self.sent_ids.append((packet_id, reply_id))
        return True

    async def async_get_channels(self) -> list[dict[str, Any]]:
        return [self.channels[i] for i in sorted(self.channels)]

    async def async_write_channel(  # noqa: PLR0913
        self,
        index: int,
        *,
        role: str,
        name: str,
        psk: bytes,
        uplink_enabled: bool,
        downlink_enabled: bool,
        position_precision: int,
    ) -> dict[str, Any]:
        if isinstance(self.request_error, Exception):
            raise self.request_error
        stored = self._channel(
            index,
            role,
            name,
            base64.b64encode(psk).decode(),
            positionPrecision=position_precision,
        )
        stored["settings"]["uplinkEnabled"] = uplink_enabled
        stored["settings"]["downlinkEnabled"] = downlink_enabled
        self.channels[index] = stored
        self.written_channels.append({"index": index, "role": role, "name": name, "psk": psk})
        return stored

    def channel_url(self, indexes: list[int] | None = None, *, add: bool = False) -> str | None:
        enabled = [i for i in sorted(self.channels) if self.channels[i]["role"] != "DISABLED"]
        if indexes is not None:
            enabled = [i for i in enabled if i in indexes]
        if not enabled:
            return None
        return f"https://meshtastic.org/e/{'?add=true' if add else ''}#" + "-".join(str(i) for i in enabled)

    async def async_get_all_nodes(self) -> dict[int, dict[str, Any]]:
        return {
            GATEWAY: {
                "num": GATEWAY,
                "user": {"id": "!00004904", "longName": "Home Gateway", "shortName": "4904", "hwModel": "HELTEC_V3"},
                "position": {"latitudeI": 470000000, "longitudeI": 80000000, "latitude": 47.0, "longitude": 8.0},
            },
            ANNA: {
                "num": ANNA,
                "user": {
                    "id": "!a1b2c3d4",
                    "longName": "Anna Hiking",
                    "shortName": "ANNA",
                    "publicKey": "abc",
                    "hwModel": "T_ECHO",
                    "role": "CLIENT",
                },
                "lastHeard": 1700000000,
                "hopsAway": 1,
                "snr": 6.5,
                "isFavorite": ANNA in self.favorites,
                "deviceMetrics": {"batteryLevel": 81, "voltage": 3.9, "channelUtilization": 4.2, "uptimeSeconds": 3600},
                "position": {
                    "latitudeI": 470100000,
                    "longitudeI": 80000000,
                    "latitude": 47.01,
                    "longitude": 8.0,
                    "altitude": 500,
                    "time": 1700000000,
                    "precisionBits": 13,
                },
            },
        }

    async def async_get_node_local_config(self) -> dict[str, Any]:
        return {"lora": {"region": "EU_868", "modemPreset": "LONG_FAST", "hopLimit": 3}}

    async def async_get_node_module_config(self) -> dict[str, Any]:
        return {"mqtt": {"enabled": True, "address": "mqtt.example.org"}, "storeForward": {"enabled": False}}

    async def async_set_favorite(self, node: int, *, favorite: bool) -> None:
        self.favorite_calls.append((node, favorite))
        if isinstance(self.request_error, Exception):
            raise self.request_error
        if favorite:
            self.favorites.add(node)
        else:
            self.favorites.discard(node)

    async def request_telemetry(self, node: int, telemetry_type: Any) -> dict[str, Any]:
        self.requests.append(("telemetry", node, str(telemetry_type)))
        if isinstance(self.request_error, Exception):
            raise self.request_error
        return {"deviceMetrics": {"batteryLevel": 77, "voltage": 3.8}}

    async def request_position(self, node: int) -> dict[str, Any]:
        self.requests.append(("position", node, None))
        if isinstance(self.request_error, Exception):
            raise self.request_error
        return {"latitudeI": 470200000, "longitudeI": 80100000, "time": 1700000100}

    async def request_traceroute(self, node: int) -> dict[str, Any]:
        self.requests.append(("traceroute", node, None))
        if isinstance(self.request_error, Exception):
            raise self.request_error
        return {"route": [RELAY], "snrTowards": [24, -8], "routeBack": [], "snrBack": [-10]}


def _make_entry(hass: HomeAssistant, client: FakeClient, **chat_options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home Gateway",
        data={},
        options={"chat": {"enable": True, **chat_options}},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        client=client,
        gateway_node={"num": GATEWAY, "user": {"id": "!00004904", "longName": "Home Gateway", "shortName": "4904"}},
    )
    return entry


def _fire_text(  # noqa: PLR0913
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    to_node=None,
    channel=None,
    message_id=1,
    text="hi",
    signal: dict[str, Any] | None = None,
) -> None:
    hass.bus.async_fire(
        EVENT_MESHTASTIC_API_TEXT_MESSAGE,
        {
            "config_entry_id": entry.entry_id,
            "node": ANNA,
            "data": {
                "from": ANNA,
                "to": {"node": to_node, "channel": channel},
                "gateway": GATEWAY,
                "message": text,
                **(signal or {}),
            },
            "message_id": message_id,
        },
    )


@pytest.fixture
async def chat_setup(hass: HomeAssistant):
    client = FakeClient()
    entry = _make_entry(hass, client)
    chat = await async_setup_chat(hass, entry)
    yield entry, chat, client
    await async_unload_chat(hass, entry)


def test_short_user_name() -> None:
    assert short_user_name("Steve Smith") == "Steve S."
    assert short_user_name("Anna Maria Muster") == "Anna M."
    assert short_user_name("Steve") == "Steve"
    assert short_user_name(None) is None


async def test_receive_channel_and_direct_messages(hass: HomeAssistant, chat_setup) -> None:
    entry, chat, _ = chat_setup

    _fire_text(hass, entry, channel=1, message_id=10, text="on channel")
    _fire_text(hass, entry, channel=1, message_id=10, text="on channel")  # duplicate
    _fire_text(hass, entry, to_node=GATEWAY, message_id=11, text="direct")
    _fire_text(hass, entry, to_node=0x12345678, message_id=12, text="not for us")
    hass.bus.async_fire(
        EVENT_MESHTASTIC_API_TEXT_MESSAGE,
        {"config_entry_id": "other", "data": {"from": ANNA, "to": {"channel": 1}, "message": "x"}},
    )
    await hass.async_block_till_done()

    channel = chat.history("channel:1")
    assert [m["text"] for m in channel] == ["on channel"]
    assert channel[0]["from_name"] == "Anna Hiking"
    assert channel[0]["from_short"] == "ANNA"
    assert channel[0]["direction"] == "in"

    direct = chat.history(f"node:{ANNA}")
    assert [m["text"] for m in direct] == ["direct"]
    assert set(chat.summaries()) == {"channel:1", f"node:{ANNA}"}
    # messages without signal data (older events, bridges) store None
    assert (channel[0]["snr"], channel[0]["rssi"], channel[0]["hops"]) == (None, None, None)

    _fire_text(hass, entry, channel=1, message_id=13, text="heard", signal={"rx_snr": 1.25, "rx_rssi": -103, "hops": 1})
    await hass.async_block_till_done()
    heard = chat.history("channel:1")[-1]
    assert (heard["snr"], heard["rssi"], heard["hops"]) == (1.25, -103, 1)

    activity = chat.node_activity(ANNA)
    assert activity == {"direct": 1, "channels": 2, "total": 3, "last_ts": heard["ts"]}
    assert (await chat.async_node_details(ANNA))["activity"] == activity
    assert chat.node_activity(0x12345678) == {"direct": 0, "channels": 0, "total": 0, "last_ts": None}


@pytest.mark.parametrize(
    ("conversation", "ack", "expected_status", "expected_to", "expected_channel"),
    [
        ("channel:1", (GATEWAY, "NONE"), MessageStatus.SENT, BROADCAST, 1),
        (f"node:{ANNA}", (ANNA, "NONE"), MessageStatus.DELIVERED, ANNA, None),
        (f"node:{ANNA}", (GATEWAY, "NONE"), MessageStatus.SENT, ANNA, None),
        (f"node:{ANNA}", (GATEWAY, "MAX_RETRANSMIT"), MessageStatus.FAILED, ANNA, None),
        (f"node:{ANNA}", (None, None), MessageStatus.NO_ACK, ANNA, None),
        ("channel:1", MeshtasticApiClientError("boom"), MessageStatus.FAILED, BROADCAST, 1),
    ],
)
async def test_send_status(  # noqa: PLR0913
    hass: HomeAssistant, chat_setup, conversation, ack, expected_status, expected_to, expected_channel
) -> None:
    _, chat, client = chat_setup
    client.ack = ack

    message = await chat.async_send(conversation, "  hello  ")
    assert message["direction"] == "out"
    await hass.async_block_till_done()

    stored = chat.history(conversation)[-1]
    assert stored["id"] == message["id"]
    assert stored["status"] == expected_status
    assert client.sent[-1]["text"] == "hello"
    assert client.sent[-1]["to"] == expected_to
    assert client.sent[-1]["channel"] == expected_channel


async def test_send_on_behalf_of_someone_else(hass: HomeAssistant, chat_setup) -> None:
    entry, chat, client = chat_setup
    events = async_capture_events(hass, "meshtastic_chat_message_sent")

    await chat.async_send("channel:1", "hello from telegram", sent_by="Telegram: Alice")
    await hass.async_block_till_done()
    assert client.sent[-1]["text"] == "hello from telegram"
    stored = chat.history("channel:1")[-1]
    assert stored["sent_by"] == "Telegram: Alice"

    # sent messages are announced once their delivery is settled, so bridges can mirror them
    assert [e.data for e in events] == [
        {
            "config_entry_id": entry.entry_id,
            "gateway": GATEWAY,
            "message_id": stored["id"],
            "to": {"node": None, "channel": 1},
            "message": "hello from telegram",
            "prefix": None,
            "sent_by": "Telegram: Alice",
            "status": "sent",
        }
    ]

    client.ack = MeshtasticApiClientError("boom")
    await chat.async_send(f"node:{ANNA}", "lost")
    await hass.async_block_till_done()
    assert len(events) == 1  # failed sends are not announced

    client.ack = (ANNA, "NONE")
    await chat.async_send(f"node:{ANNA}", "direct")
    await hass.async_block_till_done()
    assert events[-1].data["to"] == {"node": ANNA, "channel": None}
    assert events[-1].data["sent_by"] is None
    assert events[-1].data["status"] == "delivered"


async def test_send_prefix_and_limits(hass: HomeAssistant) -> None:
    client = FakeClient()
    entry = _make_entry(hass, client, prefix_sender=True)
    chat = await async_setup_chat(hass, entry)
    user = SimpleNamespace(name="Steve Smith", is_admin=True)

    events = async_capture_events(hass, "meshtastic_chat_message_sent")
    await chat.async_send("channel:1", "on my way", user)
    await hass.async_block_till_done()
    assert client.sent[-1]["text"] == "Steve S.: on my way"
    assert chat.history("channel:1")[-1]["sent_by"] == "Steve Smith"
    # the applied prefix is part of the event so bridges can show the sender once
    assert events[-1].data["prefix"] == "Steve S."
    assert events[-1].data["message"] == "Steve S.: on my way"

    with pytest.raises(vol.Invalid, match="longer than"):
        await chat.async_send("channel:1", "x" * CHAT_MAX_TEXT_BYTES, user)
    with pytest.raises(vol.Invalid, match="empty"):
        await chat.async_send("channel:1", "   ")
    with pytest.raises(vol.Invalid, match="Invalid conversation"):
        await chat.async_send("bogus:1", "hello")

    await async_unload_chat(hass, entry)


async def test_history_is_persisted_and_trimmed(hass: HomeAssistant, hass_storage) -> None:
    client = FakeClient()
    entry = _make_entry(hass, client, history_size=10)
    await async_setup_chat(hass, entry)
    for i in range(15):
        _fire_text(hass, entry, channel=1, message_id=100 + i, text=f"m{i}")
    await hass.async_block_till_done()
    await async_unload_chat(hass, entry)

    stored = hass_storage[f"{DOMAIN}.chat.{entry.entry_id}"]["data"]["conversations"]["channel:1"]
    assert [m["text"] for m in stored] == [f"m{i}" for i in range(5, 15)]

    chat = await async_setup_chat(hass, entry)
    assert [m["text"] for m in chat.history("channel:1", limit=3)] == ["m12", "m13", "m14"]
    await async_unload_chat(hass, entry)

    await MeshtasticChat.async_remove_store(hass, entry.entry_id)
    assert f"{DOMAIN}.chat.{entry.entry_id}" not in hass_storage


async def test_pending_send_is_settled_on_unload(hass: HomeAssistant) -> None:
    client = FakeClient()
    entry = _make_entry(hass, client)
    chat = await async_setup_chat(hass, entry)
    started = asyncio.Event()

    async def never_acked(*args: Any, **kwargs: Any) -> tuple[int | None, str | None]:  # noqa: ARG001
        started.set()
        await asyncio.sleep(3600)
        return None, None

    client.send_text_with_ack = never_acked
    await chat.async_send("channel:1", "hello")
    await started.wait()
    await async_unload_chat(hass, entry)
    assert chat.history("channel:1")[-1]["status"] == MessageStatus.NO_ACK


async def test_websocket_api(hass: HomeAssistant, hass_ws_client, chat_setup) -> None:
    entry, _, client = chat_setup
    ws = await hass_ws_client(hass)

    await ws.send_json({"id": 1, "type": "meshtastic/chat/subscribe"})
    assert (await ws.receive_json())["success"]

    await ws.send_json({"id": 2, "type": "meshtastic/chat/gateways"})
    response = await ws.receive_json()
    assert response["success"]
    gateway = response["result"][0]
    assert gateway["entry_id"] == entry.entry_id
    assert gateway["gateway"]["long_name"] == "Home Gateway"
    assert [(c["index"], c["name"], c["public"]) for c in gateway["channels"]] == [
        (0, "Primary", True),
        (1, "SOSPrivate", False),
    ]
    assert [n["num"] for n in gateway["nodes"]] == [ANNA]
    assert gateway["nodes"][0]["favorite"] is True
    assert gateway["max_bytes"] == CHAT_MAX_TEXT_BYTES
    assert gateway["channel_name_max_bytes"] == CHAT_CHANNEL_NAME_MAX_BYTES

    _fire_text(hass, entry, channel=1, message_id=5, text="hello from anna")
    event = await ws.receive_json()
    assert event["id"] == 1
    assert event["event"]["kind"] == "message"
    assert event["event"]["conversation"] == "channel:1"
    assert event["event"]["message"]["text"] == "hello from anna"

    await ws.send_json(
        {"id": 3, "type": "meshtastic/chat/send", "entry_id": entry.entry_id, "conversation": "channel:1", "text": "hi"}
    )
    messages = [await ws.receive_json() for _ in range(3)]
    result = next(m for m in messages if m.get("id") == 3)
    assert result["success"]
    kinds = [m["event"]["kind"] for m in messages if m.get("type") == "event"]
    assert kinds == ["message", "update"]
    assert client.sent[-1]["text"] == "hi"

    await ws.send_json(
        {"id": 4, "type": "meshtastic/chat/history", "entry_id": entry.entry_id, "conversation": "channel:1"}
    )
    response = await ws.receive_json()
    assert [m["text"] for m in response["result"]] == ["hello from anna", "hi"]

    await ws.send_json(
        {"id": 5, "type": "meshtastic/chat/send", "entry_id": entry.entry_id, "conversation": "channel:1", "text": ""}
    )
    response = await ws.receive_json()
    assert not response["success"]

    await ws.send_json(
        {"id": 6, "type": "meshtastic/chat/clear", "entry_id": entry.entry_id, "conversation": "channel:1"}
    )
    messages = [await ws.receive_json() for _ in range(2)]
    assert next(m for m in messages if m.get("id") == 6)["success"]
    assert hass.data[DATA_CHAT][entry.entry_id].history("channel:1") == []


async def test_websocket_requires_admin(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token, chat_setup
) -> None:
    entry, _, _ = chat_setup
    ws = await hass_ws_client(hass, hass_read_only_access_token)

    await ws.send_json({"id": 1, "type": "meshtastic/chat/gateways"})
    assert (await ws.receive_json())["result"] == []

    await ws.send_json(
        {"id": 2, "type": "meshtastic/chat/send", "entry_id": entry.entry_id, "conversation": "channel:1", "text": "x"}
    )
    response = await ws.receive_json()
    assert not response["success"]
    assert response["error"]["code"] == "not_found"


async def test_websocket_non_admin_allowed(hass: HomeAssistant, hass_ws_client, hass_read_only_access_token) -> None:
    client = FakeClient()
    entry = _make_entry(hass, client, require_admin=False)
    await async_setup_chat(hass, entry)
    ws = await hass_ws_client(hass, hass_read_only_access_token)

    await ws.send_json({"id": 1, "type": "meshtastic/chat/gateways"})
    result = (await ws.receive_json())["result"]
    assert [g["entry_id"] for g in result] == [entry.entry_id]
    assert result[0]["can_clear"] is False

    await ws.send_json(
        {"id": 2, "type": "meshtastic/chat/clear", "entry_id": entry.entry_id, "conversation": "channel:1"}
    )
    response = await ws.receive_json()
    assert response["error"]["code"] == "unauthorized"
    await async_unload_chat(hass, entry)


def test_position_helpers() -> None:
    assert precision_to_meters(None) is None
    assert precision_to_meters(0) is None
    assert precision_to_meters(13) == 2918
    assert precision_to_meters(24) == 1
    assert precision_to_meters(32) == 0  # full precision
    assert bearing(47.0, 8.0, 47.01, 8.0) == pytest.approx(0.0)
    assert bearing(47.0, 8.0, 47.0, 8.01) == pytest.approx(90.0, abs=0.1)
    assert bearing(47.0, 8.0, 46.99, 8.0) == pytest.approx(180.0)


async def test_node_details(hass: HomeAssistant, chat_setup) -> None:
    entry, chat, _ = chat_setup
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, str(ANNA))}, name="Anna Hiking"
    )

    details = await chat.async_node_details(ANNA)
    assert details["long_name"] == "Anna Hiking"
    assert details["user_id"] == "!a1b2c3d4"
    assert details["hw_model"] == "T_ECHO"
    assert details["role"] == "CLIENT"
    assert details["favorite"] is True
    assert details["has_public_key"] is True
    assert details["battery_level"] == 81
    assert details["uptime_seconds"] == 3600
    assert details["device_id"] == device.id
    assert details["gateway_position"] == {
        "num": GATEWAY,
        "short_name": "4904",
        "long_name": "Home Gateway",
        "latitude": 47.0,
        "longitude": 8.0,
        "accuracy": None,
    }
    assert details["is_gateway"] is False
    position = details["position"]
    assert position["latitude"] == 47.01
    assert position["altitude"] == 500
    assert position["accuracy"] == 2918
    assert position["distance"] == pytest.approx(1112, abs=2)
    assert position["bearing"] == 0

    gateway = await chat.async_node_details(GATEWAY)
    assert gateway["is_gateway"] is True
    assert gateway["position"]["distance"] == 0

    with pytest.raises(vol.Invalid, match="Unknown node"):
        await chat.async_node_details(0x22222222)


async def test_node_requests(chat_setup) -> None:
    _, chat, client = chat_setup

    telemetry = await chat.async_node_request(ANNA, "telemetry")
    assert telemetry == {"deviceMetrics": {"batteryLevel": 77, "voltage": 3.8}}
    assert client.requests[-1] == ("telemetry", ANNA, "device_metrics")
    await chat.async_node_request(ANNA, "telemetry", "environment_metrics")
    assert client.requests[-1] == ("telemetry", ANNA, "environment_metrics")

    position = await chat.async_node_request(ANNA, "position")
    assert position["latitude"] == pytest.approx(47.02)
    assert position["longitude"] == pytest.approx(8.01)

    traceroute = await chat.async_node_request(ANNA, "traceroute")
    assert traceroute["route"] == [RELAY]
    # every hop gets a name so the panel can draw the route
    assert traceroute["nodes"][str(RELAY)]["long_name"] == "Meshtastic 1111"
    assert traceroute["nodes"][str(ANNA)]["short_name"] == "ANNA"
    assert traceroute["nodes"][str(GATEWAY)]["long_name"] == "Home Gateway"

    with pytest.raises(vol.Invalid, match="Unknown request"):
        await chat.async_node_request(ANNA, "reboot")


async def test_channel_details_and_activity(hass: HomeAssistant, chat_setup) -> None:
    entry, chat, _ = chat_setup
    _fire_text(hass, entry, channel=1, message_id=1, text="first")
    _fire_text(hass, entry, channel=1, message_id=2, text="second")
    await hass.async_block_till_done()
    await chat.async_send("channel:1", "reply")
    await hass.async_block_till_done()

    details = await chat.async_channel_details(1)
    assert details["name"] == "SOSPrivate"
    assert details["role"] == "SECONDARY"
    assert details["public"] is False
    assert details["key_bytes"] == 20
    assert details["uplink_enabled"] is False
    assert details["gateway"] == {
        "region": "EU_868",
        "modem_preset": "LONG_FAST",
        "hop_limit": 3,
        "mqtt_enabled": True,
        "mqtt_address": "mqtt.example.org",
        "mqtt_encryption_enabled": False,
        "store_forward_enabled": False,
    }
    activity = details["activity"]
    assert (activity["messages"], activity["received"], activity["sent"]) == (3, 2, 1)
    assert activity["first_ts"] <= activity["last_ts"]
    assert [(n["num"], n["count"], n["long_name"]) for n in activity["nodes"]] == [(ANNA, 2, "Anna Hiking")]

    primary = await chat.async_channel_details(0)
    assert primary["name"] == "Primary"
    assert primary["public"] is True
    assert primary["key_bytes"] == 1
    assert primary["activity"]["messages"] == 0
    # the share links carry the key: only on request
    assert primary["url"] is None
    assert primary["url_all"] is None
    shared = await chat.async_channel_details(0, share_urls=True)
    assert shared["url"].startswith("https://meshtastic.org/e/?add=true#")
    assert shared["url_all"].startswith("https://meshtastic.org/e/#")

    with pytest.raises(vol.Invalid, match="Unknown channel"):
        await chat.async_channel_details(2)  # disabled


async def test_websocket_details_and_favorites(hass: HomeAssistant, hass_ws_client, chat_setup) -> None:
    entry, _, client = chat_setup
    ws = await hass_ws_client(hass)

    await ws.send_json({"id": 1, "type": "meshtastic/chat/gateways"})
    gateway = (await ws.receive_json())["result"][0]
    assert gateway["can_favorite"] is True
    assert gateway["nodes"][0]["hw_model"] == "T_ECHO"
    assert gateway["nodes"][0]["battery_level"] == 81
    assert gateway["nodes"][0]["has_position"] is True

    await ws.send_json({"id": 2, "type": "meshtastic/chat/node", "entry_id": entry.entry_id, "node_id": ANNA})
    response = await ws.receive_json()
    assert response["success"]
    assert response["result"]["long_name"] == "Anna Hiking"
    assert response["result"]["position"]["bearing"] == 0

    await ws.send_json({"id": 3, "type": "meshtastic/chat/node", "entry_id": entry.entry_id, "node_id": 12345})
    response = await ws.receive_json()
    assert response["error"]["code"] == "not_found"

    await ws.send_json(
        {
            "id": 4,
            "type": "meshtastic/chat/node/request",
            "entry_id": entry.entry_id,
            "node_id": ANNA,
            "request": "traceroute",
        }
    )
    response = await ws.receive_json()
    assert response["success"]
    assert response["result"]["request"] == "traceroute"
    assert response["result"]["result"]["route"] == [RELAY]
    assert response["result"]["node"]["long_name"] == "Anna Hiking"

    client.request_error = MeshtasticApiClientError("No response for device_metrics")
    await ws.send_json(
        {
            "id": 5,
            "type": "meshtastic/chat/node/request",
            "entry_id": entry.entry_id,
            "node_id": ANNA,
            "request": "telemetry",
            "telemetry_type": "device_metrics",
        }
    )
    response = await ws.receive_json()
    assert not response["success"]
    assert response["error"]["message"] == "No response for device_metrics"
    client.request_error = None

    await ws.send_json(
        {"id": 6, "type": "meshtastic/chat/favorite", "entry_id": entry.entry_id, "node_id": ANNA, "favorite": False}
    )
    response = await ws.receive_json()
    assert response["success"]
    assert response["result"] == {"node_id": ANNA, "favorite": False}
    assert client.favorite_calls == [(ANNA, False)]

    await ws.send_json({"id": 7, "type": "meshtastic/chat/gateways"})
    gateway = (await ws.receive_json())["result"][0]
    assert gateway["nodes"][0]["favorite"] is False

    client.request_error = MeshtasticApiClientError("No acknowledgement received")
    await ws.send_json(
        {"id": 8, "type": "meshtastic/chat/favorite", "entry_id": entry.entry_id, "node_id": ANNA, "favorite": True}
    )
    response = await ws.receive_json()
    assert not response["success"]
    assert response["error"]["message"] == "No acknowledgement received"

    await ws.send_json({"id": 9, "type": "meshtastic/chat/channel", "entry_id": entry.entry_id, "index": 0})
    response = await ws.receive_json()
    assert response["success"]
    assert response["result"]["name"] == "Primary"

    await ws.send_json({"id": 10, "type": "meshtastic/chat/channel", "entry_id": entry.entry_id, "index": 7})
    response = await ws.receive_json()
    assert response["error"]["code"] == "not_found"


async def test_websocket_favorite_requires_admin(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token
) -> None:
    client = FakeClient()
    entry = _make_entry(hass, client, require_admin=False)
    await async_setup_chat(hass, entry)
    ws = await hass_ws_client(hass, hass_read_only_access_token)

    await ws.send_json({"id": 1, "type": "meshtastic/chat/gateways"})
    result = (await ws.receive_json())["result"]
    assert result[0]["can_favorite"] is False

    # details and requests follow the gateway's "require admin" option, favourites are always admin only
    await ws.send_json({"id": 2, "type": "meshtastic/chat/node", "entry_id": entry.entry_id, "node_id": ANNA})
    assert (await ws.receive_json())["success"]

    await ws.send_json(
        {"id": 3, "type": "meshtastic/chat/favorite", "entry_id": entry.entry_id, "node_id": ANNA, "favorite": False}
    )
    response = await ws.receive_json()
    assert response["error"]["code"] == "unauthorized"
    assert client.favorite_calls == []
    await async_unload_chat(hass, entry)


async def test_interface_set_favorite_node() -> None:
    interface = MeshInterface(connection=MagicMock())
    interface._connected_node_info = mesh_pb2.MyNodeInfo(my_node_num=GATEWAY)  # noqa: SLF001
    interface._node_database[ANNA] = {  # noqa: SLF001
        "num": ANNA,
        "user": {"id": "!a1b2c3d4", "longName": "Anna Hiking", "shortName": "ANNA"},
    }
    interface.send_admin_message_await_response = AsyncMock()

    await interface.set_favorite_node(ANNA, favorite=True)
    call = interface.send_admin_message_await_response.call_args
    assert call.kwargs["node"] is None
    assert call.kwargs["expect_response"] is False
    assert call.kwargs["message"].set_favorite_node == ANNA
    assert interface.nodes()[ANNA]["isFavorite"] is True

    await interface.set_favorite_node(ANNA, favorite=False)
    message = interface.send_admin_message_await_response.call_args.kwargs["message"]
    assert message.WhichOneof("payload_variant") == "remove_favorite_node"
    assert message.remove_favorite_node == ANNA
    assert interface.nodes()[ANNA]["isFavorite"] is False

    # a node the local database does not know yet (only seen through its messages) gets a record
    await interface.set_favorite_node(RELAY, favorite=True)
    assert interface.nodes()[RELAY]["isFavorite"] is True
    assert interface.nodes()[RELAY]["user"]["id"] == "!11111111"


async def test_api_client_set_favorite_wraps_errors() -> None:
    client = MeshtasticApiClient.__new__(MeshtasticApiClient)
    client._interface = SimpleNamespace(set_favorite_node=AsyncMock(side_effect=MeshtasticError("no ack")))  # noqa: SLF001
    with pytest.raises(MeshtasticApiClientError, match="no ack"):
        await client.async_set_favorite(ANNA, favorite=True)
    client._interface.set_favorite_node.assert_awaited_once_with(ANNA, favorite=True)  # noqa: SLF001


async def test_set_channel(chat_setup) -> None:
    _, chat, client = chat_setup
    user = SimpleNamespace(name="Steve Smith", is_admin=True)

    # a free slot becomes a secondary channel with a fresh 256 bit key
    created = await chat.async_set_channel(3, name="Hikers", user=user)
    assert (created["index"], created["role"], created["name"]) == (3, "SECONDARY", "Hikers")
    assert created["key_bytes"] == 32
    assert created["public"] is False
    assert created["url"].startswith("https://meshtastic.org/e/?add=true#")
    assert client.written_channels[-1]["role"] == "SECONDARY"
    first_key = client.written_channels[-1]["psk"]
    assert len(first_key) == 32

    # renaming keeps the key
    renamed = await chat.async_set_channel(3, name="Hikers2")
    assert renamed["name"] == "Hikers2"
    assert client.written_channels[-1]["psk"] == first_key

    # key choices
    assert (await chat.async_set_channel(3, key="default"))["public"] is True
    assert client.written_channels[-1]["psk"] == b"\x01"
    await chat.async_set_channel(3, key="none")
    assert client.written_channels[-1]["psk"] == b""
    await chat.async_set_channel(3, key="random128")
    assert len(client.written_channels[-1]["psk"]) == 16
    await chat.async_set_channel(3, key=base64.b64encode(b"k" * 16).decode())
    assert client.written_channels[-1]["psk"] == b"k" * 16

    # flags and position precision
    flags = await chat.async_set_channel(3, uplink_enabled=True, position_precision=13)
    assert flags["uplink_enabled"] is True
    assert flags["downlink_enabled"] is False
    assert flags["position_precision_bits"] == 13
    assert flags["position_precision"] == 2918

    # disabling frees the slot again
    disabled = await chat.async_set_channel(3, role="DISABLED")
    assert disabled == {"index": 3, "role": "DISABLED", "conversation": "channel:3"}
    targets = await chat.async_targets()
    assert targets["free_channel_indexes"] == [2, 3, 4, 5, 6, 7]

    # the primary channel can be renamed and its flags changed, but it keeps its role and key
    primary = await chat.async_set_channel(0, name="Main", uplink_enabled=True)
    assert primary["name"] == "Main"
    assert client.written_channels[-1]["role"] == "PRIMARY"
    assert client.written_channels[-1]["psk"] == b"\x01"

    with pytest.raises(vol.Invalid, match="primary channel cannot be disabled"):
        await chat.async_set_channel(0, role="DISABLED")
    with pytest.raises(vol.Invalid, match="key of the primary channel"):
        await chat.async_set_channel(0, key="random")
    with pytest.raises(vol.Invalid, match="Only channel 0"):
        await chat.async_set_channel(1, role="PRIMARY")
    with pytest.raises(vol.Invalid, match="limited to 11 bytes"):
        await chat.async_set_channel(1, name="x" * 12)
    with pytest.raises(vol.Invalid, match="not valid base64"):
        await chat.async_set_channel(1, key="!!!")
    with pytest.raises(vol.Invalid, match="A key must be"):
        await chat.async_set_channel(1, key=base64.b64encode(b"short").decode())
    with pytest.raises(vol.Invalid, match="between 0 and 7"):
        await chat.async_set_channel(8)


@pytest.mark.parametrize(
    ("level", "admin_allowed", "user_allowed"),
    [("off", False, False), ("admin", True, False), ("all", True, True)],
)
async def test_websocket_channel_set_permissions(  # noqa: PLR0913
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token, level, admin_allowed, user_allowed
) -> None:
    client = FakeClient()
    entry = _make_entry(hass, client, require_admin=False, channel_management=level)
    await async_setup_chat(hass, entry)

    for token, allowed in ((None, admin_allowed), (hass_read_only_access_token, user_allowed)):
        ws = await hass_ws_client(hass, token) if token else await hass_ws_client(hass)
        await ws.send_json({"id": 1, "type": "meshtastic/chat/gateways"})
        gateway = (await ws.receive_json())["result"][0]
        assert gateway["can_manage_channels"] is allowed
        assert gateway["free_channel_indexes"] == [2, 3, 4, 5, 6, 7]

        # the channel's share link (it contains the key) follows the same permission
        await ws.send_json({"id": 2, "type": "meshtastic/chat/channel", "entry_id": entry.entry_id, "index": 1})
        details = (await ws.receive_json())["result"]
        assert (details["url"] is not None) is allowed

        await ws.send_json(
            {"id": 3, "type": "meshtastic/chat/channel/set", "entry_id": entry.entry_id, "index": 2, "name": "New"}
        )
        response = await ws.receive_json()
        if allowed:
            assert response["success"]
            assert response["result"]["name"] == "New"
            client.channels[2] = FakeClient._channel(2, "DISABLED", "", "")  # noqa: SLF001
        else:
            assert response["error"]["code"] == "unauthorized"

    if admin_allowed:
        ws = await hass_ws_client(hass)
        await ws.send_json(
            {"id": 3, "type": "meshtastic/chat/channel/set", "entry_id": entry.entry_id, "index": 1, "key": "!!!"}
        )
        response = await ws.receive_json()
        assert response["error"]["code"] == "invalid_format"

        client.request_error = MeshtasticApiClientError("No acknowledgement received")
        await ws.send_json(
            {"id": 4, "type": "meshtastic/chat/channel/set", "entry_id": entry.entry_id, "index": 1, "name": "X"}
        )
        response = await ws.receive_json()
        assert response["error"]["code"] == "unknown_error"
        assert response["error"]["message"] == "No acknowledgement received"
    await async_unload_chat(hass, entry)


async def test_interface_write_and_request_channel() -> None:
    interface = MeshInterface(connection=MagicMock())
    interface._connected_node_info = mesh_pb2.MyNodeInfo(my_node_num=GATEWAY)  # noqa: SLF001
    interface._connected_node_channels = [channel_pb2.Channel(index=0, role=channel_pb2.Channel.Role.PRIMARY)]  # noqa: SLF001
    interface._connected_node_ready.set()  # noqa: SLF001
    channel = channel_pb2.Channel(index=3, role=channel_pb2.Channel.Role.SECONDARY)
    channel.settings.name = "Hikers"
    channel.settings.psk = b"k" * 32
    response = SimpleNamespace(app_payload=admin_pb2.AdminMessage())
    response.app_payload.get_channel_response.CopyFrom(channel)
    interface.send_admin_message_await_response = AsyncMock(return_value=response)

    await interface.write_channel(channel)
    call = interface.send_admin_message_await_response.call_args
    assert call.kwargs["node"] is None
    assert call.kwargs["expect_response"] is False
    assert call.kwargs["message"].set_channel.settings.name == "Hikers"
    channels = interface.connected_node_channels()
    assert [c.index for c in channels] == [0, 1, 2, 3]
    assert channels[1].role == channel_pb2.Channel.Role.DISABLED
    assert channels[3].settings.name == "Hikers"

    read = await interface.request_channel(3)
    assert interface.send_admin_message_await_response.call_args.kwargs["message"].get_channel_request == 4
    assert read.settings.psk == b"k" * 32


async def test_api_client_channel_url_and_write() -> None:
    client = MeshtasticApiClient.__new__(MeshtasticApiClient)
    client._logger = MagicMock()  # noqa: SLF001
    primary = channel_pb2.Channel(index=0, role=channel_pb2.Channel.Role.PRIMARY)
    primary.settings.psk = b"\x01"
    second = channel_pb2.Channel(index=1, role=channel_pb2.Channel.Role.SECONDARY)
    second.settings.name = "SOS"
    second.settings.psk = b"s" * 16
    local_config = localonly_pb2.LocalConfig()
    local_config.lora.region = config_pb2.Config.LoRaConfig.RegionCode.EU_868
    client._interface = SimpleNamespace(  # noqa: SLF001
        connected_node_channels=lambda: [primary, second, channel_pb2.Channel(index=2)],
        connected_node_local_config=lambda: local_config,
        write_channel=AsyncMock(),
        request_channel=AsyncMock(side_effect=MeshtasticError("timeout")),
    )

    def decode(url: str) -> apponly_pb2.ChannelSet:
        encoded = url.split("#", 1)[1]
        return apponly_pb2.ChannelSet.FromString(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))

    url = client.channel_url()
    assert url.startswith("https://meshtastic.org/e/#")
    channel_set = decode(url)
    assert [s.name for s in channel_set.settings] == ["", "SOS"]
    assert channel_set.lora_config.region == config_pb2.Config.LoRaConfig.RegionCode.EU_868

    url_add = client.channel_url([1], add=True)
    assert url_add.startswith("https://meshtastic.org/e/?add=true#")
    assert [s.name for s in decode(url_add).settings] == ["SOS"]
    assert client.channel_url([2]) is None

    stored = await client.async_write_channel(
        3,
        role="SECONDARY",
        name="New",
        psk=b"k" * 32,
        uplink_enabled=True,
        downlink_enabled=False,
        position_precision=13,
    )
    written = client._interface.write_channel.call_args.args[0]  # noqa: SLF001
    assert written.index == 3
    assert written.role == channel_pb2.Channel.Role.SECONDARY
    assert written.settings.uplink_enabled is True
    assert written.settings.module_settings.position_precision == 13
    # the read-back failed, so the written channel is reported
    assert stored["settings"]["name"] == "New"
    assert stored["settings"]["psk"] == base64.b64encode(b"k" * 32).decode()


async def test_gateway_details_and_map(hass: HomeAssistant, chat_setup) -> None:
    entry, chat, _ = chat_setup

    details = await chat.async_node_details(GATEWAY)
    assert details["is_gateway"] is True
    assert details["gateway_info"] == {
        "firmware_version": "2.7.26.abcdef",
        "hw_model": "HELTEC_V3",
        "role": "CLIENT",
        "region": "EU_868",
        "modem_preset": "LONG_FAST",
        "hop_limit": 3,
        "tx_power": None,
        "tx_enabled": True,
        "mqtt_enabled": True,
        "channels": 2,
        "nodes_known": 2,
        "messages_today": 0,
        "messages_total": 0,
        "title": "Home Gateway",
    }
    assert details["activity"] is None
    _fire_text(hass, entry, channel=1, message_id=1, text="today")
    await hass.async_block_till_done()
    info = (await chat.async_node_details(GATEWAY))["gateway_info"]
    assert (info["messages_today"], info["messages_total"]) == (1, 1)
    assert (await chat.async_node_details(ANNA))["gateway_info"] is None

    nodes = await chat.async_map_nodes()
    assert nodes["nodes_known"] == 2
    assert [(n["num"], n["is_gateway"]) for n in nodes["nodes"]] == [(GATEWAY, True), (ANNA, False)]
    assert nodes["nodes"][1]["position"]["distance"] == pytest.approx(1112, abs=2)
    # the map draws the accuracy of a shared position as a circle around the marker
    assert nodes["nodes"][1]["position"]["accuracy"] == 2918


async def test_websocket_map(hass: HomeAssistant, hass_ws_client, chat_setup) -> None:
    entry, _, _ = chat_setup
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "meshtastic/chat/map", "entry_id": entry.entry_id})
    response = await ws.receive_json()
    assert response["success"]
    assert [n["short_name"] for n in response["result"]["nodes"]] == ["4904", "ANNA"]


async def test_api_text_message_event_carries_the_signal() -> None:
    client = MeshtasticApiClient.__new__(MeshtasticApiClient)
    client._hass = MagicMock()  # noqa: SLF001
    client._config_entry_id = "e1"  # noqa: SLF001
    client._interface = SimpleNamespace(connected_node=lambda: {"num": GATEWAY})  # noqa: SLF001
    node = MeshNode(id=ANNA, user_id="!a1b2c3d4", short_name="ANNA", long_name="Anna Hiking")

    def packet(reply_id: int = 0, **mesh: Any) -> SimpleNamespace:
        return SimpleNamespace(
            from_id=ANNA,
            to_id=BROADCAST,
            channel_index=1,
            app_payload="hello",
            data=SimpleNamespace(reply_id=reply_id),
            mesh_packet=SimpleNamespace(
                **{"id": 42, "rx_snr": 0.0, "rx_rssi": 0, "hop_start": 0, "hop_limit": 0, **mesh}
            ),
        )

    await client._on_text_message(node, packet(rx_snr=1.25, rx_rssi=-103, hop_start=3, hop_limit=2))  # noqa: SLF001
    event = client._hass.bus.async_fire.call_args.args[1]  # noqa: SLF001
    assert event["data"]["to"] == {"node": None, "channel": 1}
    assert (event["data"]["rx_snr"], event["data"]["rx_rssi"], event["data"]["hops"]) == (1.25, -103, 1)
    assert event["message_id"] == 42

    # a packet without signal info (e.g. relayed through MQTT) reports None rather than zeros
    await client._on_text_message(node, packet())  # noqa: SLF001
    event = client._hass.bus.async_fire.call_args.args[1]  # noqa: SLF001
    assert (event["data"]["rx_snr"], event["data"]["rx_rssi"], event["data"]["hops"]) == (None, None, None)
    assert event["data"]["reply_id"] is None

    # a reply names the packet it answers
    await client._on_text_message(node, packet(reply_id=777))  # noqa: SLF001
    event = client._hass.bus.async_fire.call_args.args[1]  # noqa: SLF001
    assert event["data"]["reply_id"] == 777


async def test_interface_sends_a_reply_with_its_own_packet_id() -> None:
    interface = MeshInterface(connection=MagicMock())
    interface._connected_node_info = SimpleNamespace(my_node_num=GATEWAY)  # noqa: SLF001
    interface._connection.send_mesh_packet = AsyncMock(return_value=None)  # noqa: SLF001

    await interface.send_text_message("agreed", ANNA, packet_id=555, reply_id=321)
    kwargs = interface._connection.send_mesh_packet.call_args.kwargs  # noqa: SLF001
    assert (kwargs["packet_id"], kwargs["reply_id"], kwargs["message"]) == (555, 321, b"agreed")

    interface._connection.generate_packet_id = lambda: 99  # noqa: SLF001
    assert interface.generate_packet_id() == 99


async def test_connection_marks_the_packet_as_a_reply() -> None:
    sent = []

    class Connection(ClientApiConnection):
        async def send_packet(self, to_radio: Any) -> bool:
            sent.append(to_radio.packet)
            return True

    connection = Connection.__new__(Connection)
    connection._current_packet_id = None  # noqa: SLF001
    await connection.send_mesh_packet(ANNA, b"hi", packet_id=555, reply_id=321)
    assert (sent[-1].id, sent[-1].decoded.reply_id) == (555, 321)

    await connection.send_mesh_packet(ANNA, b"hi")
    assert sent[-1].id != 555
    assert sent[-1].decoded.reply_id == 0


async def test_reply_is_sent_with_the_packet_id_and_kept_with_the_quote(hass: HomeAssistant, chat_setup) -> None:
    entry, chat, client = chat_setup
    _fire_text(hass, entry, channel=1, message_id=7001, text="Who is up for a hike on Saturday afternoon?")
    await hass.async_block_till_done()
    original = chat.history("channel:1")[-1]

    message = await chat.async_send("channel:1", "Count me in", reply_to=original["id"])
    await hass.async_block_till_done()
    assert message["reply_to"] == 7001
    assert message["reply_preview"] == {
        "id": original["id"],
        "from": ANNA,
        "name": "Anna Hiking",
        "text": "Who is up for a hike on Saturday afternoon?",
    }
    assert client.sent_ids[-1] == (message["packet_id"], 7001)
    assert message["packet_id"] is not None

    # replying to our own message works as soon as it is sent: its packet id was chosen up front
    again = await chat.async_send("channel:1", "Meet at 2", reply_to=message["id"])
    await hass.async_block_till_done()
    assert client.sent_ids[-1] == (again["packet_id"], message["packet_id"])
    assert again["reply_preview"]["text"] == "Count me in"

    # a message of another conversation, an unknown one or one without a packet id cannot be answered
    for reply_to in ("nope", original["id"]):
        with pytest.raises(vol.Invalid, match="not available"):
            await chat.async_send("channel:0", "x", reply_to=reply_to)
    chat._conversations["channel:1"][0]["packet_id"] = None  # noqa: SLF001
    with pytest.raises(vol.Invalid, match="not available"):
        await chat.async_send("channel:1", "x", reply_to=chat.history("channel:1")[0]["id"])


async def test_received_reply_is_linked_to_the_quoted_message(hass: HomeAssistant, chat_setup) -> None:
    entry, chat, _ = chat_setup
    _fire_text(hass, entry, channel=1, message_id=7001, text="x" * 120)
    for message_id, text, reply_id in ((7002, "agreed", 7001), (7003, "late", 1)):
        hass.bus.async_fire(
            EVENT_MESHTASTIC_API_TEXT_MESSAGE,
            {
                "config_entry_id": entry.entry_id,
                "message_id": message_id,
                "data": {"from": RELAY, "to": {"node": None, "channel": 1}, "message": text, "reply_id": reply_id},
            },
        )
    await hass.async_block_till_done()
    first, reply, unknown = chat.history("channel:1")
    assert first["reply_to"] is None
    assert first["reply_preview"] is None
    assert reply["reply_to"] == 7001
    assert reply["reply_preview"]["name"] == "Anna Hiking"
    assert reply["reply_preview"]["text"] == "x" * 80  # the quote is shortened
    # the original is not in the history any more: the reply still knows what it answers
    assert unknown["reply_to"] == 1
    assert unknown["reply_preview"] is None


async def test_websocket_send_a_reply(hass: HomeAssistant, hass_ws_client, chat_setup) -> None:
    entry, chat, _ = chat_setup
    ws = await hass_ws_client(hass)
    _fire_text(hass, entry, channel=1, message_id=7001, text="question")
    await hass.async_block_till_done()
    original = chat.history("channel:1")[-1]

    request = {
        "type": "meshtastic/chat/send",
        "entry_id": entry.entry_id,
        "conversation": "channel:1",
        "text": "answer",
    }
    await ws.send_json({"id": 1, **request, "reply_to": original["id"]})
    response = await ws.receive_json()
    assert response["success"]
    assert response["result"]["reply_to"] == 7001
    assert response["result"]["reply_preview"]["text"] == "question"

    await ws.send_json({"id": 2, **request, "reply_to": "nope"})
    response = await ws.receive_json()
    assert not response["success"]
    assert "not available" in response["error"]["message"]


async def test_gateway_position_accuracy_follows_its_primary_channel(chat_setup) -> None:
    _, chat, client = chat_setup
    # no precision on the channel: nothing to draw (an exact position)
    assert (await chat.async_node_details(GATEWAY))["position"]["accuracy"] is None
    assert (await chat.async_node_details(ANNA))["gateway_position"]["accuracy"] is None

    # what the mesh sees of the gateway is limited by the position precision of its primary channel
    client.channels[0]["settings"]["moduleSettings"] = {"positionPrecision": 13}
    assert (await chat.async_node_details(GATEWAY))["position"]["accuracy"] == 2918
    assert (await chat.async_node_details(ANNA))["gateway_position"]["accuracy"] == 2918
    nodes = {n["num"]: n for n in (await chat.async_map_nodes())["nodes"]}
    assert nodes[GATEWAY]["position"]["accuracy"] == 2918
    assert nodes[ANNA]["position"]["accuracy"] == 2918  # its own precision bits, not the gateway's

    # full precision is exact, and a channel that shares no position leaves nothing to draw either
    client.channels[0]["settings"]["moduleSettings"] = {"positionPrecision": 32}
    assert (await chat.async_node_details(GATEWAY))["position"]["accuracy"] == 0
    client.channels[0]["settings"]["moduleSettings"] = {"positionPrecision": 0}
    assert (await chat.async_node_details(GATEWAY))["position"]["accuracy"] is None


def test_routing_error_names_the_error_and_says_what_it_means() -> None:
    message = str(MeshRoutingError(5))
    assert message.startswith("Routing error: MAX_RETRANSMIT (")
    assert "out of range" in message
    # an error without an explanation (or one this version does not know) still names itself
    assert str(MeshRoutingError(0)) == "Routing error: NONE"
    assert str(MeshRoutingError(99)) == "Routing error: 99"


async def test_api_client_telemetry_failure_names_the_routing_error() -> None:
    client = MeshtasticApiClient.__new__(MeshtasticApiClient)
    client._interface = SimpleNamespace(request_telemetry=AsyncMock(side_effect=MeshRoutingError(5)))  # noqa: SLF001
    with pytest.raises(MeshtasticApiClientError, match="No response for device_metrics: Routing error: MAX_RETRANSMIT"):
        await client.request_telemetry(ANNA, TelemetryType.DEVICE_METRICS)
