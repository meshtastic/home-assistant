# SPDX-FileCopyrightText: 2026 Richard Powell
#
# SPDX-License-Identifier: MIT

import asyncio
from collections.abc import Coroutine
from typing import ClassVar, Self

import aiomqtt
import pytest
from aiomqtt import MqttError

from custom_components.meshtastic.aiomeshtastic import MeshInterface
from custom_components.meshtastic.aiomeshtastic import interface as interface_module
from custom_components.meshtastic.aiomeshtastic.protobuf import channel_pb2, config_pb2, mesh_pb2

Role = channel_pb2.Channel.Role
Preset = config_pb2.Config.LoRaConfig.ModemPreset
real_sleep = asyncio.sleep


async def fast_sleep(_delay: float) -> None:
    """Skip the reconnect back-off while still yielding to the event loop."""
    await real_sleep(0)


class RecordingConnection:
    def __init__(self) -> None:
        self.sent: list[mesh_pb2.ToRadio] = []
        self.has_sent = asyncio.Event()
        self.accepts = True

    async def send_packet(self, to_radio: mesh_pb2.ToRadio) -> bool:
        self.sent.append(to_radio)
        self.has_sent.set()
        return self.accepts


def channel(index: int, name: str = "", *, downlink: bool = True, role: int | None = None) -> channel_pb2.Channel:
    if role is None:
        role = Role.PRIMARY if index == 0 else Role.SECONDARY
    return channel_pb2.Channel(
        index=index, role=role, settings=channel_pb2.ChannelSettings(name=name, downlink_enabled=downlink)
    )


def make_interface(
    channels: list[channel_pb2.Channel],
    *,
    root: str = "msh/US",
    preset: int = Preset.LONG_FAST,
    use_preset: bool = True,
) -> tuple[MeshInterface, RecordingConnection]:
    connection = RecordingConnection()
    interface = MeshInterface(connection=connection, enable_mqtt_proxy=True)
    interface._connected_node_channels = channels
    interface._connected_node_module_config.mqtt.root = root
    interface._connected_node_local_config.lora.use_preset = use_preset
    interface._connected_node_local_config.lora.modem_preset = preset
    return interface, connection


def message(topic: str, payload: object, *, retain: bool = False) -> aiomqtt.Message:
    return aiomqtt.Message(topic, payload, qos=1, retain=retain, mid=1, properties=None)


def test_topics_mirror_firmware_subscriptions() -> None:
    interface, _ = make_interface(
        [
            channel(0),  # unnamed primary: named after the preset
            channel(1, "Private"),
            channel(2, "NoDownlink", downlink=False),
            channel(3, "Disabled", role=Role.DISABLED),
            channel(4, "Private"),  # same id as channel 1, subscribed once
        ]
    )

    assert interface._mqtt_downlink_topics() == [
        "msh/US/2/e/LongFast/+",
        "msh/US/2/e/Private/+",
        "msh/US/2/e/PKI/+",
    ]


def test_no_downlink_channel_means_no_subscription() -> None:
    interface, _ = make_interface([channel(0, downlink=False), channel(1, "Private", downlink=False)])

    assert interface._mqtt_downlink_topics() == []


@pytest.mark.parametrize(
    ("preset", "use_preset", "expected"),
    [
        (Preset.MEDIUM_FAST, True, "MediumFast"),
        (Preset.SHORT_TURBO, True, "ShortTurbo"),
        (Preset.LONG_MODERATE, True, "LongMod"),
        (Preset.LONG_FAST, False, "Custom"),
    ],
)
def test_unnamed_channel_uses_firmware_channel_id(preset: int, use_preset: bool, expected: str) -> None:  # noqa: FBT001
    interface, _ = make_interface([channel(0)], preset=preset, use_preset=use_preset)

    assert interface._mqtt_channel_id(interface._connected_node_channels[0]) == expected


def test_empty_root_falls_back_to_msh() -> None:
    interface, _ = make_interface([channel(0, "Private")], root="")

    assert interface._mqtt_downlink_topics() == ["msh/2/e/Private/+", "msh/2/e/PKI/+"]


async def test_message_is_handed_to_radio_unchanged() -> None:
    interface, connection = make_interface([])

    await interface._forward_mqtt_message_to_radio(message("msh/US/2/e/Private/!1234abcd", b"\x01\x02", retain=True))

    assert connection.sent == [
        mesh_pb2.ToRadio(
            mqttClientProxyMessage=mesh_pb2.MqttClientProxyMessage(
                topic="msh/US/2/e/Private/!1234abcd", data=b"\x01\x02", retained=True
            )
        )
    ]


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        ("msh/US/2/e/Private/!1234abcd", b""),
        ("msh/US/2/e/Private/!1234abcd", None),
        ("msh/US/2/e/Private/!1234abcd", b"x" * 436),
        ("msh/US/2/e/" + "x" * 60 + "/!1234abcd", b"\x01"),
        ("msh/US/2/e/" + "x" * 39 + "/!1234abcd", b"\x01"),  # 60 bytes leaves no room for the NUL
        ("msh/US/2/e/" + "\u00e9" * 20 + "/!1234abcd", b"\x01"),  # 41 characters, 61 UTF-8 bytes
    ],
)
async def test_messages_firmware_cannot_take_are_dropped(topic: str, payload: object) -> None:
    interface, connection = make_interface([])

    await interface._forward_mqtt_message_to_radio(message(topic, payload))

    assert connection.sent == []


async def test_largest_message_firmware_can_take_is_forwarded() -> None:
    interface, connection = make_interface([])
    topic = "msh/US/2/e/" + "x" * 38 + "/!1234abcd"
    assert len(topic.encode("utf-8")) == 59

    await interface._forward_mqtt_message_to_radio(message(topic, b"x" * 435))

    assert [sent.mqttClientProxyMessage.topic for sent in connection.sent] == [topic]


class FakeMqttClient:
    instances: ClassVar[list["FakeMqttClient"]] = []

    def __init__(self, **_kwargs: object) -> None:
        self.subscriptions: list[str] = []
        self.subscribed = asyncio.Event()
        self.queue: asyncio.Queue[aiomqtt.Message | Exception] = asyncio.Queue()
        FakeMqttClient.instances.append(self)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def subscribe(self, topic: str, qos: int) -> None:
        assert qos == 1
        if any(level not in {"+", "#"} and ("+" in level or "#" in level) for level in topic.split("/")):
            msg = "Invalid subscription filter."  # what paho raises
            raise ValueError(msg)
        self.subscriptions.append(topic)
        self.subscribed.set()

    @property
    def messages(self) -> Self:
        return self

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> aiomqtt.Message:
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item


async def test_connection_subscribes_forwards_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeMqttClient.instances = []
    monkeypatch.setattr(interface_module.aiomqtt, "Client", FakeMqttClient)
    interface, connection = make_interface([channel(0, "Private")])
    interface._mqtt_config = {}
    interface._is_running.set()

    task = asyncio.create_task(interface._maintain_mqtt_connection())
    await asyncio.sleep(0)
    client = FakeMqttClient.instances[0]
    await asyncio.wait_for(client.subscribed.wait(), timeout=1)
    client.queue.put_nowait(message("msh/US/2/e/Private/!1234abcd", b"\x01"))
    await asyncio.wait_for(connection.has_sent.wait(), timeout=1)
    interface._is_stopped.set()
    await asyncio.wait_for(task, timeout=1)

    assert client.subscriptions == ["msh/US/2/e/Private/+", "msh/US/2/e/PKI/+"]
    assert connection.sent[0].mqttClientProxyMessage.data == b"\x01"
    assert not interface._mqtt_connected
    assert len(FakeMqttClient.instances) == 1


async def test_dropped_stream_raises_so_the_connection_is_retried() -> None:
    interface, _ = make_interface([])
    client = FakeMqttClient()
    interface._mqtt_client = client
    client.queue.put_nowait(MqttError("Disconnected during message iteration"))

    with pytest.raises(MqttError, match="Disconnected"):
        await interface._forward_mqtt_downlink_until_stopped()


async def test_dropped_connection_reconnects_and_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeMqttClient.instances = []
    monkeypatch.setattr(interface_module.aiomqtt, "Client", FakeMqttClient)
    monkeypatch.setattr(interface_module.asyncio, "sleep", fast_sleep)
    interface, connection = make_interface([channel(0, "Private")])
    interface._mqtt_config = {}
    interface._is_running.set()

    task = asyncio.create_task(interface._maintain_mqtt_connection())
    async with asyncio.timeout(1):
        while not FakeMqttClient.instances:
            await real_sleep(0)
    first = FakeMqttClient.instances[0]
    await asyncio.wait_for(first.subscribed.wait(), timeout=1)
    first.queue.put_nowait(MqttError("Disconnected during message iteration"))
    async with asyncio.timeout(1):
        while len(FakeMqttClient.instances) < 2:
            await real_sleep(0)
    second = FakeMqttClient.instances[1]
    await asyncio.wait_for(second.subscribed.wait(), timeout=1)
    second.queue.put_nowait(message("msh/US/2/e/Private/!1234abcd", b"\x02"))
    await asyncio.wait_for(connection.has_sent.wait(), timeout=1)
    interface._is_stopped.set()
    await asyncio.wait_for(task, timeout=1)

    assert second.subscriptions == ["msh/US/2/e/Private/+", "msh/US/2/e/PKI/+"]
    assert [sent.mqttClientProxyMessage.data for sent in connection.sent] == [b"\x02"]


async def test_client_bounds_its_incoming_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    interface, _ = make_interface([])
    mqtt = interface._connected_node_module_config.mqtt
    mqtt.enabled = True
    mqtt.proxy_to_client_enabled = True
    mqtt.address = "broker.example"
    interface._connected_node_info = mesh_pb2.MyNodeInfo(my_node_num=0x1234ABCD)
    interface._connected_node_ready.set()

    def capture(coroutine: Coroutine[None, None, None], name: str) -> None:
        assert name == "mqtt-connection"
        coroutine.close()

    monkeypatch.setattr(interface, "_add_background_task", capture)
    await interface._init_mqtt_client()

    assert interface._mqtt_config["max_queued_incoming_messages"] == 100
    assert interface._mqtt_config["queue_type"] is interface_module._MqttDownlinkQueue


async def test_queue_refuses_what_the_radio_cannot_take() -> None:
    queue = interface_module._MqttDownlinkQueue(maxsize=1)
    for _ in range(3):
        queue.put_nowait(message("msh/US/2/e/Private/!1234abcd", b"x" * 436))
        queue.put_nowait(message("msh/US/2/e/Private/!1234abcd", b""))
    fits = message("msh/US/2/e/Private/!1234abcd", b"\x01")
    queue.put_nowait(fits)

    assert queue.get_nowait() is fits
    assert queue.empty()


async def test_invalid_topic_filter_is_skipped_and_the_rest_subscribed(caplog: pytest.LogCaptureFixture) -> None:
    interface, _ = make_interface([channel(0, "Room#1"), channel(1, "Private")])
    client = FakeMqttClient()
    interface._mqtt_client = client

    await interface._subscribe_to_downlink_topics()

    assert client.subscriptions == ["msh/US/2/e/Private/+", "msh/US/2/e/PKI/+"]
    assert "msh/US/2/e/Room#1/+" in caplog.text


async def test_message_the_radio_refuses_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    interface, connection = make_interface([])
    connection.accepts = False

    await interface._forward_mqtt_message_to_radio(message("msh/US/2/e/Private/!1234abcd", b"\x01"))

    assert "Radio did not accept MQTT message on msh/US/2/e/Private/!1234abcd" in caplog.text
